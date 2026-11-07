#!/usr/bin/env python3
"""竞价放量筛选器（2026-11-03 定版口径）。

读竞价任务 09:25 已落盘的全市场快照（当日＋最近一个基线日），本地算竞价
较昨日放量比例，按两档口径筛选，全程不重新抓全市场：
  基础池：放量 >250%，只剔 ST
  小额档：竞价额 50万~200万、竞价涨幅 0%~+4.5%、放量 >500%
  大额档：竞价额 >200万、竞价涨幅 0%~+2%、放量 400%~1000%
  竞价额 <50万一律不看。
入选后只对名单（约几十只）发一批腾讯报价：核名剔 ST（新浪快照名会剥
ST 前缀，必须以腾讯名为准）、取现价算当前涨幅。题材取本地全市场表
（东财行业二级）兜底。
名单表（2026-11-04 改版）：题材｜名称｜代码｜竞价涨幅｜当前涨幅｜竞价较昨日｜
竞价额｜图形形态；名称、代码纯文本分列、无现价列，链接由共用转换器按定版
规格挂（名称→hammerspoon 跳经典版，代码→东财网页）；
「图形形态」列——用腾讯前复权日 K＋现价算 MA5/MA10（并发 ≤5）：
站上 5 日线且 MA5≥MA10 为「形态好」，站上但 MA5<MA10 为「形态一般」，
未站上为「形态差」，并注明距 5 日线幅度；取不到日 K 写「未核」。
输出：markdown 名单 + stdout 一行 JSON 摘要。
当日快照缺失时 exit 2（竞价量事后无法补回，不许用盘中数据冒充）。
"""
import argparse, csv, json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SNAP_DIR = Path.home() / "workspace/goals/goal/hidden_files"
THEME_CSV = Path.home() / "workspace/stock-screener/全市场快照-2026-10-22.csv"


def load_snapshot(date):
    p = SNAP_DIR / f"auction-snapshot-{date}.json"
    if not p.exists():
        return None
    return {r["code"]: r for r in json.loads(p.read_text())}


def latest_baseline(date):
    """最近一个「有效」基线日：竞价额过半非零才算数。2026-11-05 的快照被
    测试误覆盖、金额全置 0 作废（见该文件 SOURCE.txt），若拿它当基线放量
    比例全算不出、名单会空——故自动跳过作废快照，回退到上一个有效日
    （口径行会如实打印基线日）。"""
    files = sorted(SNAP_DIR.glob("auction-snapshot-*.json"))
    prev = [f for f in files if f.stem.replace("auction-snapshot-", "") < date]
    for f in reversed(prev):
        rows = json.loads(f.read_text())
        valid = sum(1 for r in rows if float(r.get("amount") or 0) > 0)
        if rows and valid > len(rows) / 2:
            return f.stem.replace("auction-snapshot-", ""), {r["code"]: r for r in rows}
    return None


def theme_map():
    m = {}
    if not THEME_CSV.exists():
        return m
    with open(THEME_CSV, encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        code_k = next((k for k in rd.fieldnames if "代码" in k), None)
        ind_k = next((k for k in rd.fieldnames if "行业二级" in k), None) or \
                next((k for k in rd.fieldnames if "行业" in k), None)
        if not code_k or not ind_k:
            return m
        for row in rd:
            m[str(row[code_k]).zfill(6)] = row[ind_k] or "—"
    return m


def tencent_quotes(symbols):
    """100 只一批，返回 {code: (name, price, prev_close, amount_yuan, vol_hands, mktcap_yi)}。
    成交额取报价第 37 字段（万元）换算为元——今日累计成交额，与取价同一批
    响应顺带拿到，不加请求（2026-11-05 加「当前成交额/当前较昨日放量」）。
    成交量取第 6 字段（手），与成交额同响应顺带，用于算分时均价（黄均线，
    2026-11-06 加列），同样不加请求。"""
    out = {}
    for i in range(0, len(symbols), 100):
        batch = symbols[i:i + 100]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            raw = subprocess.run(["curl", "-s", "-m", "30", url],
                                 capture_output=True).stdout
            txt = raw.decode("gbk", errors="replace")  # 腾讯报价是 GBK 编码
        except Exception:
            continue
        for line in txt.splitlines():
            if "=" not in line or '"' not in line:
                continue
            body = line.split('"')[1]
            f = body.split("~")
            if len(f) < 5:
                continue
            sym = line.split("=")[0].replace("v_", "").strip()
            code = sym[2:]
            try:
                amt = float(f[37]) * 1e4 if len(f) > 37 and f[37] else 0.0
                vol = float(f[6]) if len(f) > 6 and f[6] else 0.0
                mktcap = float(f[45]) if len(f) > 45 and f[45] else 0.0
                out[code] = (f[1], float(f[3]), float(f[4]), amt, vol, mktcap)
            except (ValueError, IndexError):
                pass
    return out


def yesterday_amounts(date):
    """昨日全天成交额（元）：读早间预备的全市场表 auction-universe-<date>.json
    （其 amount_yi 即昨日全天口径），本地文件、零请求。缺失返回 {}。"""
    p = SNAP_DIR / f"auction-universe-{date}.json"
    if not p.exists():
        return {}
    try:
        j = json.loads(p.read_text())
        return {r["code"]: float(r.get("amount_yi") or 0) * 1e8
                for r in j.get("rows", [])}
    except Exception:
        return {}


def daily_closes(symbol):
    """腾讯前复权日 K 收盘价序列（不含当日未完一根由调用方处理）。"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={symbol},day,,,60,qfq")
    try:
        raw = subprocess.run(["curl", "-s", "-m", "20", url],
                             capture_output=True).stdout
        data = json.loads(raw.decode("utf-8", errors="replace"))
        rows = data["data"][symbol].get("qfqday") or data["data"][symbol].get("day") or []
        return [(r[0], float(r[2])) for r in rows]
    except Exception:
        return []


def form_info(symbol, price, today_str):
    """图形形态＋连涨天数。形态列只写 5 日线位置与距离（2026-11-05 定：
    形态好/一般这类评级陈述没用、去掉）；分档（好0/一般1/差2/未核3）只在内部
    排序用、不印文字。连涨天数＝截至今日（含现价）连续收涨天数。
    日 K 缺失返回 ("未核", 3, None)。"""
    closes = daily_closes(symbol)
    if len(closes) < 10 or price <= 0:
        return "未核", 3, None
    seq = [c for d, c in closes if d != today_str]
    seq.append(price)
    if len(seq) < 10:
        return "未核", 3, None
    ma5 = sum(seq[-5:]) / 5
    ma10 = sum(seq[-10:]) / 10
    dist = (price / ma5 - 1) * 100 if ma5 > 0 else 0.0
    if price > ma5:
        tier = 0 if ma5 >= ma10 else 1
        disp = f"5日线上 {dist:+.2f}%"
    else:
        tier = 2
        disp = f"5日线下 {dist:+.2f}%"
    streak = 0
    for i in range(len(seq) - 1, 0, -1):
        if seq[i] > seq[i - 1]:
            streak += 1
        else:
            break
    return disp, tier, streak


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    date = args.date

    today = load_snapshot(date)
    if today is None:
        print(f"ERROR: 当日竞价快照缺失 auction-snapshot-{date}.json，竞价量无法补回", file=sys.stderr)
        sys.exit(2)
    base = latest_baseline(date)
    if base is None:
        print("ERROR: 无基线日快照，算不出竞价较昨日", file=sys.stderr)
        sys.exit(2)
    base_date, yday = base

    rows = []
    for code, r in today.items():
        y = yday.get(code)
        if not y:
            continue
        amt_t, amt_y = float(r.get("amount") or 0), float(y.get("amount") or 0)
        op, st = float(r.get("open") or 0), float(r.get("settlement") or 0)
        if amt_y <= 0 or op <= 0 or st <= 0:
            continue
        ratio = (amt_t / amt_y - 1) * 100
        if ratio <= 250:
            continue
        rows.append({"code": code, "symbol": r["symbol"], "amt_t": amt_t,
                     "ratio": ratio, "chg": (op / st - 1) * 100})

    small = [r for r in rows if 500_000 <= r["amt_t"] < 2_000_000
             and 0 <= r["chg"] <= 4.5 and r["ratio"] > 500]
    big = [r for r in rows if r["amt_t"] >= 2_000_000
           and 0 <= r["chg"] <= 2 and 400 < r["ratio"] <= 1000]
    sel = small + big

    quotes = tencent_quotes([r["symbol"] for r in sel])
    themes = theme_map()
    yday_amt = yesterday_amounts(date)
    kept = []
    for r in sel:
        q = quotes.get(r["code"])
        if q is None:
            continue
        name, price, prev, amt_now, vol_now, mktcap_yi = q
        if "ST" in name or "退" in name:
            continue
        # 黄均线＝现价较分时均价幅度；均价＝今日累计成交额÷(成交量手×100)
        if amt_now > 0 and vol_now > 0 and price > 0:
            vwap = amt_now / (vol_now * 100)
            vwap_s = f"{(price / vwap - 1) * 100:+.2f}%" if price * 0.5 <= vwap <= price * 2 else "未核"
        else:
            vwap_s = "未核"
        r = dict(r, name=name, price=price, amt_now=amt_now, vwap=vwap_s, vol=vol_now, mktcap_yi=mktcap_yi,
                 amt_yday=yday_amt.get(r["code"], 0.0),
                 now_chg=(price / prev - 1) * 100 if prev > 0 else 0.0,
                 theme=themes.get(r["code"], "—"))
        kept.append(r)
    with ThreadPoolExecutor(max_workers=5) as ex:
        forms = list(ex.map(lambda r: form_info(r["symbol"], r["price"], date), kept))
    # 未核多为腾讯 K 线限流（501 拦截页）：串行慢速重试一轮，还取不到才留未核
    for i, f in enumerate(forms):
        if f[1] == 3:
            time.sleep(0.4)
            forms[i] = form_info(kept[i]["symbol"], kept[i]["price"], date)
    for r, (f, t, s) in zip(kept, forms):
        r["form"] = f
        r["tier"] = t
        r["streak"] = s
    # 档内排序（2026-11-05 定）：形态好（绿）最前、形态一般次之、其余最后，组内按当前涨幅降序
    def tier_key(x):
        return (x.get("tier", 3), -x["now_chg"])
    small = sorted([r for r in kept if r["amt_t"] < 2_000_000], key=tier_key)
    big = sorted([r for r in kept if r["amt_t"] >= 2_000_000], key=tier_key)

    def table(title, data):
        _hdr = "| 题材 | 名称 | 代码 | 竞价涨幅 | 当前涨幅 | 竞价较昨日 | 竞价额 | 当前较昨日放量 | 图形形态 | 黄均线 | 当前成交额 | 市值 | 连涨天数 |"
        lines = [f"## {title}（{len(data)} 只）", "",
                 _hdr, "|" + "---|" * (len(_hdr.strip("|").split("|")))]
        for r in data:
            amt = f"{r['amt_t']/1e8:.2f}亿" if r['amt_t'] >= 1e8 else f"{r['amt_t']/1e4:.0f}万"
            amt_now = f"{r['amt_now']/1e8:.2f}亿" if r.get("amt_now", 0) > 0 else "未核"
            if r.get("amt_now", 0) > 0 and r.get("amt_yday", 0) > 0:
                vr = r["amt_now"] / r["amt_yday"] - 1
                # 一律带符号写「放量±X%」（2026-11-05 定：缩量写法没有负号、
                # 点击排序会把 -13% 误读成 +13%）
                vol_now = f"放量{vr*100:+.0f}%"
            else:
                vol_now = "未核"
            streak = f"{r['streak']}天" if r.get("streak") is not None else "未核"
            lines.append(f"| {r['theme']} | {r['name']} | {r['code']} | {r['chg']:+.2f}% "
                         f"| {r['now_chg']:+.2f}% "
                         f"| 放量{r['ratio']:+.0f}% | {amt} | {vol_now} | {r['form']} | {r.get('vwap', '未核')} | {amt_now} | {r['mktcap_yi']:.1f}亿 | {streak} |")
        return lines

    md = [f"# 竞价放量筛选器名单 {date}", "",
          f"口径：基础池放量>250%只剔ST（基线日 {base_date}，基础池 {len(rows)} 只）；"
          f"大额档 >200万/竞价0~+2/放量400%~1000%；小额档 50万~200万/竞价0~+4.5/放量>500%；"
          f"竞价额<50万不看。档内排序：形态好在前、组内按当前涨幅降序。"
          f"形态列只写5日线位置与距离；连涨天数为截至今日（含现价）连续收涨天数。"
          f"当前成交额为筛选时点今日累计成交额；当前较昨日放量＝今日累计÷昨日全天成交额"
          f"（昨日额取早间预备全市场表）。黄均线＝现价较分时均价（今日累计成交额÷累计成交量）幅度。"
          f"现价与当前涨幅为筛选时刻行情。", ""]
    md += table("大额档", big) + [""] + table("小额档", small) + [""]
    text = "\n".join(md)
    if args.out:
        Path(args.out).write_text(text)
    print(json.dumps({"date": date, "base": len(rows), "small": len(small),
                      "big": len(big), "total": len(small) + len(big)}))


if __name__ == "__main__":
    main()
