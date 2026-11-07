#!/usr/bin/env python3
"""表格常用列批量计算器（2026-11-06 定：常用列全任务统一）。

给一串股票代码，一次算齐五列展示字段，供各报告名单定稿后并入表格：
  成交量（今日累计，万手）｜当前较昨日放量｜黄均线｜图形形态｜连涨天数
口径与竞价/盘中放量筛选器完全一致：
  - 当前较昨日放量＝今日累计成交额÷昨日全天成交额−1（昨日额取当日
    auction-universe-<date>.json 的 amount_yi，缺失回退最近一期）
  - 黄均线＝现价较分时均价（今日累计成交额÷累计成交量）幅度，均价须落在
    当日高低之间（2% 容差），对不上写未核
  - 图形形态＝5 日线位置与距离（MA5 含当日现价一根），只写数据不写评级
  - 连涨天数＝截至现价的连续收涨天数
数据：腾讯完整版报价（100 只一批）＋前复权日 K（并发 ≤5，未核串行慢速
重试一轮）。盘前/盘后运行时报价为最近收盘口径，字段自然按收盘算。
输出：markdown 表格（按代码查并）＋stdout 一行 JSON 摘要。
"""
import argparse, json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SNAP_DIR = Path.home() / "workspace/goals/goal/hidden_files"


def symbol_of(code):
    if code.startswith("92"):
        return "bj" + code
    if code.startswith(("60", "68")):
        return "sh" + code
    return "sz" + code


def load_yday_amounts(date):
    p = SNAP_DIR / f"auction-universe-{date}.json"
    if not p.exists():
        files = sorted(SNAP_DIR.glob("auction-universe-*.json"))
        prev = [f for f in files if f.stem.replace("auction-universe-", "") < date]
        if not prev:
            return {}, None
        p = prev[-1]
    j = json.loads(p.read_text())
    return {r["code"]: float(r.get("amount_yi") or 0) for r in j.get("rows", [])}, j.get("base_date")


def fetch_batch(symbols):
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    out = {}
    try:
        raw = subprocess.run(["curl", "-s", "-m", "30", url], capture_output=True).stdout
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
        code = line.split("=")[0].replace("v_", "").strip()[2:]
        try:
            out[code] = {"name": f[1], "price": float(f[3]), "prev": float(f[4]),
                         "vol_hands": float(f[6]) if f[6] else 0.0,
                         "open": float(f[5]) if f[5] else 0.0,
                         "high": float(f[33]) if f[33] else 0.0,
                         "low": float(f[34]) if f[34] else 0.0,
                         "amt": float(f[37]) * 1e4 if f[37] else 0.0,
                         "mktcap_yi": float(f[45]) if f[45] else 0.0}
        except (ValueError, IndexError):
            pass
    return out


def daily_closes(symbol):
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={symbol},day,,,60,qfq")
    try:
        raw = subprocess.run(["curl", "-s", "-m", "20", url], capture_output=True).stdout
        data = json.loads(raw.decode("utf-8", errors="replace"))
        rows = data["data"][symbol].get("qfqday") or data["data"][symbol].get("day") or []
        return [(r[0], float(r[2])) for r in rows]
    except Exception:
        return []


def form_info(symbol, price, today_str):
    closes = daily_closes(symbol)
    if len(closes) < 10 or price <= 0:
        return "未核", None
    seq = [c for d, c in closes if d != today_str]
    seq.append(price)
    if len(seq) < 10:
        return "未核", None
    ma5 = sum(seq[-5:]) / 5
    dist = (price / ma5 - 1) * 100 if ma5 > 0 else 0.0
    disp = f"5日线上 {dist:+.2f}%" if price > ma5 else f"5日线下 {dist:+.2f}%"
    streak = 0
    for i in range(len(seq) - 1, 0, -1):
        if seq[i] > seq[i - 1]:
            streak += 1
        else:
            break
    return disp, streak


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--codes", required=True, help="逗号分隔的六位代码")
    args = ap.parse_args()
    codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]

    yday_amt, base_date = load_yday_amounts(args.date)
    symbols = [symbol_of(c) for c in codes]
    quotes = {}
    batches = [symbols[i:i + 100] for i in range(0, len(symbols), 100)]
    with ThreadPoolExecutor(max_workers=5) as ex:
        for part in ex.map(fetch_batch, batches):
            quotes.update(part)

    rows = []
    for code, sym in zip(codes, symbols):
        q = quotes.get(code)
        if q is None or q["price"] <= 0:
            rows.append({"code": code, "name": "未核", "openchg": "未核", "volchg": "未核",
                         "vwap": "未核", "form": "未核", "streak": "未核"})
            continue
        open_s = f"{(q['open'] / q['prev'] - 1) * 100:+.2f}%" if q.get("open") and q["prev"] > 0 else "未核"
        y = yday_amt.get(code, 0)
        volchg = f"放量{(q['amt'] / (y * 1e8) - 1) * 100:+.0f}%" if y > 0 and q["amt"] > 0 else "未核"
        if q["vol_hands"] > 0 and q["amt"] > 0:
            vwap = q["amt"] / (q["vol_hands"] * 100)
            ok = not (q["low"] > 0 and q["high"] > 0) or (q["low"] * 0.98 <= vwap <= q["high"] * 1.02)
            vwap_s = f"{(q['price'] / vwap - 1) * 100:+.2f}%" if ok else "未核"
        else:
            vwap_s = "未核"
        rows.append({"code": code, "name": q["name"], "openchg": open_s, "volchg": volchg,
                     "vwap": vwap_s, "price": q["price"], "symbol": sym,
                     "mktcap": f"{q['mktcap_yi']:.1f}亿" if q.get("mktcap_yi") else "未核",
                     "form": None, "streak": None})
    with ThreadPoolExecutor(max_workers=5) as ex:
        forms = list(ex.map(lambda r: form_info(r["symbol"], r["price"], args.date)
                            if r.get("symbol") else ("未核", None), rows))
    for i, (disp, streak) in enumerate(forms):
        if disp == "未核" and rows[i].get("symbol"):
            time.sleep(0.4)
            disp, streak = form_info(rows[i]["symbol"], rows[i]["price"], args.date)
        rows[i]["form"] = disp
        rows[i]["streak"] = f"{streak}天" if streak is not None else "未核"

    print("| 代码 | 名称 | 开盘涨幅 | 当前较昨日放量 | 黄均线 | 图形形态 | 连涨天数 | 市值 |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['code']} | {r['name']} | {r['openchg']} | {r['volchg']} | {r['vwap']} | {r['form']} | {r['streak']} | {r.get('mktcap', '未核')} |")
    print(json.dumps({"date": args.date, "base_date": base_date, "n": len(rows)}), file=sys.stderr)


if __name__ == "__main__":
    main()
