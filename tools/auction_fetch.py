#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竞价双通道竞速取数（2026-11-05 为竞价提速新建，当晚实测新浪全量约 17 秒）。

09:25 定格后由竞价任务调用一次：
- 腾讯完整版批量报价（按预备池宇宙分批 ≤60、并发 5）与新浪全市场分页快照同时跑，
  逐段先到先用、互相补缺，不再串行等一条通道超时才切另一条。
- 合并结果按历史快照的规范格式（新浪原始行构成的 JSON 数组）落盘
  ~/workspace/goals/goal/hidden_files/auction-snapshot-<日期>.json，
  腾讯命中的行用腾讯的今开/竞价额覆盖 open/trade/amount 并标 src=tencent，
  保证竞价放量筛选器（读快照本地算）与竞价较昨日对比口径不变。
- 覆盖报告写 auction-coverage-<日期>.json（总数、双通道行数、缺段、双通道对账抽样）。
- 若当前时间在 09:25:05 之前（09:24 提前启动做准备），先等待到 09:25:05 再取数。
- 单请求超时：腾讯 8s / 新浪 10s；整体软上限 ~150s，到点用已合并数据落盘并报告缺口。

口径：竞价价＝今开；竞价额＝09:25–09:30 窗口内累计成交额（该窗口无连续交易，即竞价额）。
腾讯成交额字段单位万元，写入时换算为元（与新浪 amount 同口径）。
"""
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

BASE = os.path.expanduser("~/workspace/goals/goal/hidden_files")
UA = {"User-Agent": "Mozilla/5.0"}
TODAY = datetime.now().strftime("%Y-%m-%d")


def http_get(url, timeout):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_tencent_batch(symbols):
    url = "https://qt.gtimg.cn/q=" + ",".join(symbols)
    raw = http_get(url, 8).decode("gbk", "ignore")
    out = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part.startswith("v_") or '="' not in part:
            continue
        sym = part[2:part.index("=")]
        f = part.split('="', 1)[1].rstrip('"').split("~")
        if len(f) < 38:
            continue
        code = sym[2:]
        try:
            open_px = float(f[5]); prev = float(f[4])
            amt_yuan = float(f[37]) * 10000.0
        except (ValueError, IndexError):
            continue
        if prev <= 0:
            continue
        out[code] = {"open": open_px, "prev": prev, "amount": amt_yuan}
    return out


EM_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def fetch_em_page(page):
    """东方财富 82.push2 全市场列表（第三通道）：f17 开盘、f18 昨收、f6 成交额（元）、f14 名称。"""
    url = ("https://82.push2.eastmoney.com/api/qt/clist/get?"
           f"pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12&fs={EM_FS}"
           "&fields=f2,f6,f12,f14,f17,f18")
    for attempt in range(2):  # 高并发下偶发断连，重试一次
        try:
            d = json.loads(http_get(url, 10))
            out = {}
            for x in d["data"]["diff"]:
                try:
                    open_px = float(x["f17"]); prev = float(x["f18"])
                    amt = float(x["f6"])
                except (TypeError, ValueError, KeyError):
                    continue
                if prev <= 0:
                    continue
                out[x["f12"]] = {"open": open_px, "prev": prev, "amount": amt}
            return out
        except Exception:
            if attempt == 1:
                raise
            time.sleep(0.5)


def fetch_sina_page(page):
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node=hs_a")
    raw = http_get(url, 10).decode("gbk", "ignore")
    out = {}
    for x in json.loads(raw):
        out[x.get("code", "")] = x  # 新浪原始行，原样保留
    return out


def main():
    now = datetime.now()
    target = now.replace(hour=9, minute=25, second=5, microsecond=0)
    if now < target and now.hour == 9:
        time.sleep((target - now).total_seconds())

    symbols, name_map = [], {}
    upath = os.path.join(BASE, f"auction-universe-{TODAY}.json")
    if os.path.exists(upath):
        try:
            u = json.load(open(upath, encoding="utf-8"))
            for r in u.get("rows", []):
                name_map[r["code"]] = (r.get("symbol", ""), r.get("name", ""))
                if r.get("in_universe"):
                    symbols.append(r["symbol"])
        except Exception:
            symbols = []
    batches = [symbols[i:i + 60] for i in range(0, len(symbols), 60)]

    # 三通道各用独立线程池并行跑：死通道（如腾讯超时）只耗自己的池，
    # 不许饿死新浪/东财。腾讯先探活：前 6 批全无返回就整条放弃，不陪它超时到底。
    def run_pool(fn, jobs, workers, sink, deadline, probe=0):
        ex = cf.ThreadPoolExecutor(max_workers=workers)
        futs = [ex.submit(fn, j) for j in jobs]
        try:
            done_n = 0
            for fut in cf.as_completed(futs):
                done_n += 1
                try:
                    sink.update(fut.result())
                except Exception:
                    pass
                if probe and done_n >= probe and not sink:
                    break  # 探活失败，放弃该通道
                if time.time() > deadline:
                    break
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    sina_rows, tencent, em = {}, {}, {}
    t0 = time.time()
    deadline = t0 + 130
    with cf.ThreadPoolExecutor(max_workers=3) as outer:
        f_s = outer.submit(run_pool, fetch_sina_page, list(range(1, 61)), 8, sina_rows, deadline)
        f_e = outer.submit(run_pool, fetch_em_page, list(range(1, 61)), 5, em, deadline)
        f_t = outer.submit(run_pool, fetch_tencent_batch, batches, 5, tencent, deadline, 6)
        for fut in (f_s, f_e):
            try:
                fut.result(timeout=max(0, deadline - time.time()))
            except Exception:
                pass
        # 尽快切换（2026-11-05 定）：新浪＋东财已覆盖齐宇宙就不等腾讯；
        # 只有还有缺口时才给腾讯最多 30 秒补缺，超时照样往下走。
        uni_codes = {s[2:] for s in symbols}
        if uni_codes - (set(sina_rows) | set(em)):
            try:
                f_t.result(timeout=min(30, max(0, deadline - time.time())))
            except Exception:
                pass

    rows, both, err_sum, err_max = [], 0, 0.0, 0.0
    for code in sorted(set(sina_rows) | set(tencent) | set(em)):
        t = tencent.get(code)
        s = sina_rows.get(code)
        e = em.get(code)
        if s is not None:
            row = dict(s)
            if t is not None:
                try:
                    sa = float(row.get("amount") or 0)
                    if sa > 0 and t["amount"] > 0:
                        er = abs(t["amount"] - sa) / sa
                        both += 1; err_sum += er; err_max = max(err_max, er)
                except (TypeError, ValueError):
                    pass
                row["open"] = f'{t["open"]:.2f}'
                row["trade"] = f'{t["open"]:.2f}'
                row["amount"] = int(t["amount"])
                row["src"] = "tencent"
            rows.append(row)
        elif t is not None:
            sym, nm = name_map.get(code, ("", ""))
            rows.append({"symbol": sym, "code": code, "name": nm,
                         "trade": f'{t["open"]:.2f}', "settlement": f'{t["prev"]:.2f}',
                         "open": f'{t["open"]:.2f}', "amount": int(t["amount"]),
                         "src": "tencent"})
        elif e is not None:
            sym, nm = name_map.get(code, ("", ""))
            rows.append({"symbol": sym, "code": code, "name": nm,
                         "trade": f'{e["open"]:.2f}', "settlement": f'{e["prev"]:.2f}',
                         "open": f'{e["open"]:.2f}', "amount": int(e["amount"]),
                         "src": "em"})

    universe_codes = {s[2:] for s in symbols}
    missing = sorted(c for c in universe_codes
                     if c not in sina_rows and c not in tencent and c not in em)
    # 防误覆盖：只在竞价窗口（09:20–10:00）写正式快照，其余时间写 .test 后缀
    in_window = (now.hour == 9 and now.minute >= 20) or (now.hour == 10 and now.minute == 0)
    suffix = "" if in_window else ".test"
    with open(os.path.join(BASE, f"auction-snapshot-{TODAY}.json{suffix}"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    cov = {"date": TODAY, "total_rows": len(rows), "tencent_rows": len(tencent),
           "sina_rows": len(sina_rows), "em_rows": len(em), "universe": len(universe_codes),
           "missing_codes": missing, "reconcile_sample": both,
           "reconcile_avg_err": round(err_sum / both, 4) if both else None,
           "reconcile_max_err": round(err_max, 4) if both else None,
           "elapsed_sec": round(time.time() - t0, 1), "test_mode": bool(suffix)}
    with open(os.path.join(BASE, f"auction-coverage-{TODAY}.json{suffix}"), "w", encoding="utf-8") as f:
        json.dump(cov, f, ensure_ascii=False, indent=1)
    print(f"竞价快照 {TODAY}：共 {len(rows)} 只（腾讯命中 {len(tencent)}、新浪 {len(sina_rows)}、"
          f"东财 {len(em)}），宇宙 {len(universe_codes)}、缺 {len(missing)} 只，"
          f"对账抽样 {both} 只平均误差 {cov['reconcile_avg_err']}，耗时 {cov['elapsed_sec']}s"
          f"{'（测试模式，未覆盖正式快照）' if suffix else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
