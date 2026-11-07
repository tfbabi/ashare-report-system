#!/usr/bin/env python3
"""盘中放量筛选器（2026-11-06 一次性需求，样式对齐竞价放量筛选器）。

口径：时点（默认 11:00）今日累计成交额 ÷ 昨日全天成交额 = 当前较昨日放量。
过滤（全部定）：
  - 剔 ST／退市
  - 昨日全天成交额 < 1 亿的去除
  - 当前较昨日放量 < +40% 的去除
  - 分时不在黄线上的去除：黄线＝分时均价＝今日累计成交额÷今日累计成交量
    （与现价同一批腾讯报价顺带取量，零新增请求）；现价 ≤ 均价的去除
  - 不在 5 日线上的去除：现价 ≤ MA5（MA5 含当日现价一根，与竞价筛选器
    图形形态口径一致）的去除
昨日全天成交额取早间预备全市场表 auction-universe-<date>.json 的 amount_yi
（昨日全天口径，本地文件零请求）；该文件缺失时回退到最近一期并在口径行
注明其基线日。
输出：完整 md（--out）＋简要 md（--brief-out，前 N 只）＋stdout 一行 JSON。
腾讯 K 线限流时对未核票串行慢速重试一轮（与竞价筛选器同款保护）。
"""
import argparse, csv, json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SNAP_DIR = Path.home() / "workspace/goals/goal/hidden_files"
THEME_CSV = Path.home() / "workspace/stock-screener/全市场快照-2026-10-22.csv"


def load_universe(date):
    """返回 (rows, base_date, used_date)。当日文件缺失回退最近一期。"""
    p = SNAP_DIR / f"auction-universe-{date}.json"
    if not p.exists():
        files = sorted(SNAP_DIR.glob("auction-universe-*.json"))
        prev = [f for f in files if f.stem.replace("auction-universe-", "") < date]
        if not prev:
            return None, None, None
        p = prev[-1]
    j = json.loads(p.read_text())
    return j.get("rows", []), j.get("base_date"), p.stem.replace("auction-universe-", "")


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


def fetch_batch(symbols):
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    out = {}
    try:
        raw = subprocess.run(["curl", "-s", "-m", "30", url],
                             capture_output=True).stdout
        txt = raw.decode("gbk", errors="replace")
    except Exception:
        return out
    for line in txt.splitlines():
        if "=" not in line or '"' not in line:
            continue
        body = line.split('"')[1]
        f = body.split("~")
        if len(f) < 46:
            continue
        sym = line.split("=")[0].replace("v_", "").strip()
        code = sym[2:]
        try:
            out[code] = {
                "name": f[1], "price": float(f[3]), "prev": float(f[4]),
                "vol_hands": float(f[6]) if f[6] else 0.0,
                "open": float(f[5]) if f[5] else 0.0,
                "high": float(f[33]) if len(f) > 33 and f[33] else 0.0,
                "low": float(f[34]) if len(f) > 34 and f[34] else 0.0,
                "amt": float(f[37]) * 1e4 if f[37] else 0.0,
                "mktcap_yi": float(f[45]) if f[45] else 0.0,
            }
        except (ValueError, IndexError):
            pass
    return out


def tencent_quotes_all(symbols):
    batches = [symbols[i:i + 100] for i in range(0, len(symbols), 100)]
    out = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        for part in ex.map(fetch_batch, batches):
            out.update(part)
    return out


def daily_closes(symbol):
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
    """返回 (形态显示, tier, 连涨天数, 站上MA5?)；K 线缺失 tier=3、站上=None。"""
    closes = daily_closes(symbol)
    if len(closes) < 10 or price <= 0:
        return "未核", 3, None, None
    seq = [c for d, c in closes if d != today_str]
    seq.append(price)
    if len(seq) < 10:
        return "未核", 3, None, None
    ma5 = sum(seq[-5:]) / 5
    ma10 = sum(seq[-10:]) / 10
    dist = (price / ma5 - 1) * 100 if ma5 > 0 else 0.0
    above = price > ma5
    if above:
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
    return disp, tier, streak, above


def fmt_amt(yuan):
    return f"{yuan/1e8:.2f}亿" if yuan >= 1e8 else f"{yuan/1e4:.0f}万"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--label", default="1100")
    ap.add_argument("--out", default=None)
    ap.add_argument("--brief-out", default=None)
    ap.add_argument("--topn", type=int, default=10)
    ap.add_argument("--min-vol-pct", type=float, default=40.0)
    ap.add_argument("--min-yday-yi", type=float, default=1.0)
    args = ap.parse_args()
    date = args.date

    rows, base_date, used_date = load_universe(date)
    if rows is None:
        print("ERROR: 无 auction-universe 文件，昨日成交额无来源", file=sys.stderr)
        sys.exit(2)

    symbols = [r["symbol"] for r in rows if r.get("symbol")]
    quotes = tencent_quotes_all(symbols)
    themes = theme_map()

    cand, n_yday_ok, n_vol_ok, n_vwap_ok = [], 0, 0, 0
    for r in rows:
        code = r["code"]
        if r.get("is_st"):
            continue
        q = quotes.get(code)
        if q is None:
            continue
        name = q["name"]
        if "ST" in name or "退" in name:
            continue
        yday_yi = float(r.get("amount_yi") or 0)
        if yday_yi < args.min_yday_yi:
            continue
        n_yday_ok += 1
        price, amt_now = q["price"], q["amt"]
        if price <= 0 or amt_now <= 0 or q["prev"] <= 0:
            continue
        ratio = (amt_now / (yday_yi * 1e8) - 1) * 100
        if ratio < args.min_vol_pct:
            continue
        n_vol_ok += 1
        # 黄线（分时均价）＝今日累计成交额 ÷ 今日累计成交量（手×100 股）
        if q["vol_hands"] <= 0:
            continue
        vwap = amt_now / (q["vol_hands"] * 100)
        # 单位自检：均价应落在当日高低之间（留 2% 容差），对不上不拿它卡人
        if q["low"] > 0 and q["high"] > 0 and not (q["low"] * 0.98 <= vwap <= q["high"] * 1.02):
            continue
        if price <= vwap:
            continue
        n_vwap_ok += 1
        cand.append({"code": code, "symbol": r["symbol"], "name": name,
                     "price": price, "prev": q["prev"], "vol": q["vol_hands"],
                     "open_chg": (q["open"] / q["prev"] - 1) * 100 if q.get("open") else None,
                     "chg": (price / q["prev"] - 1) * 100,
                     "mktcap_yi": q.get("mktcap_yi", 0.0),
                     "amt_now": amt_now, "yday_yi": yday_yi, "ratio": ratio,
                     "vwap_dist": (price / vwap - 1) * 100,
                     "theme": themes.get(code, "—")})

    with ThreadPoolExecutor(max_workers=5) as ex:
        forms = list(ex.map(lambda c: form_info(c["symbol"], c["price"], date), cand))
    for i, f in enumerate(forms):
        if f[1] == 3:
            time.sleep(0.4)
            forms[i] = form_info(cand[i]["symbol"], cand[i]["price"], date)
    kept, unverified = [], 0
    for c, (disp, tier, streak, above) in zip(cand, forms):
        if above is None:
            unverified += 1
            continue
        if not above:
            continue
        c = dict(c, form=disp, tier=tier, streak=streak)
        kept.append(c)
    kept.sort(key=lambda x: -x["ratio"])

    header = [f"# 成交放量筛选器名单 {date} {args.label}", "",
              f"口径：{args.label} 时点今日累计成交额÷昨日全天成交额，放量≥+{args.min_vol_pct:.0f}%；"
              f"昨日成交额≥{args.min_yday_yi:.0f}亿（基线日 {base_date}，全市场表 {used_date} 期）；"
              f"只剔ST；现价须在分时黄线（均价）上、且站上5日线。"
              f"漏斗：昨日额达标 {n_yday_ok} 只 → 放量达标 {n_vol_ok} 只 → 黄线上 {n_vwap_ok} 只 "
              f"→ 站上5日线 {len(kept)} 只（K线未核剔除 {unverified} 只）。"
              f"表内默认按当前较昨日放量降序；黄均线＝现价较分时均价幅度。", ""]

    def table(data):
        _hdr = "| 题材 | 名称 | 代码 | 开盘涨幅 | 当前涨幅 | 当前较昨日放量 | 图形形态 | 黄均线 | 当前成交额 | 市值 | 连涨天数 |"
        lines = [_hdr, "|" + "---|" * (len(_hdr.strip("|").split("|")))]
        for c in data:
            streak = f"{c['streak']}天" if c.get("streak") is not None else "未核"
            open_s = f"{c['open_chg']:+.2f}%" if c.get('open_chg') is not None else "未核"
            lines.append(f"| {c['theme']} | {c['name']} | {c['code']} | {open_s} | {c['chg']:+.2f}% "
                         f"| 放量{c['ratio']:+.0f}% "
                         f"| {c['form']} | {c['vwap_dist']:+.2f}% "
                         f"| {fmt_amt(c['amt_now'])} | {c['mktcap_yi']:.1f}亿 | {streak} |")
        return lines

    full = header + [f"## 全部（{len(kept)} 只）", ""] + table(kept) + [""]
    if args.out:
        Path(args.out).write_text("\n".join(full))
    if args.brief_out:
        brief = header + [f"## 前 {min(args.topn, len(kept))} 只（按放量降序）", ""] \
            + table(kept[:args.topn]) + [""]
        Path(args.brief_out).write_text("\n".join(brief))
    print(json.dumps({"date": date, "label": args.label, "base_date": base_date,
                      "yday_ok": n_yday_ok, "vol_ok": n_vol_ok,
                      "vwap_ok": n_vwap_ok, "kept": len(kept),
                      "unverified": unverified}))


if __name__ == "__main__":
    main()
