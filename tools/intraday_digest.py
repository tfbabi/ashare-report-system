#!/usr/bin/env python3
"""盘中盯盘确定性摘要：新浪全市场快照 + 腾讯指数 → 一页紧凑摘要。

用途：供盘中滚动盯盘任务降本——脚本先把全市场数据压成摘要文件，
盯盘 agent 只读摘要做判断，不再把数千只快照读进上下文。

数据源（本机只能走 HTTP 代理，urllib 自动读环境变量）：
- 新浪 Market_Center.getHQNodeData（node=hs_a，num=50 分页拉全，缺页补拉）
- 腾讯 qt.gtimg.cn 完整版指数（上证/深成指/创业板/科创50）
- 外围联动：Yahoo chart API（韩/日/台指数）+ 新浪 hf_ 期货（美股期货/A50），
  细节与陈旧值陷阱见 fetch_external 上方注释；外围失败不影响 A 股部分与退出码。

产出：~/workspace/youzi-ge/盯盘摘要-最新.md（同时打印到 stdout）。
整体失败（快照拉不到可用数据）时：摘要文件首行写明失败原因，退出码非 0。
无第三方依赖，仅标准库。
"""
import glob
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
OUT_PATH = os.path.join(HOME, "workspace/youzi-ge/盯盘摘要-最新.md")
WATCHLIST_PATH = os.path.join(HOME, "workspace/youzi-ge/盘中滚动名单-最新.md")
SCREENER_DIR = os.path.join(HOME, "workspace/stock-screener")

SINA_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData?page={page}&num=50&sort=symbol&asc=1"
    "&node=hs_a&symbol=&_s_r_a=page"
)
TENCENT_CODES = ["sh000001", "sz399001", "sz399006", "sh000688"]
# 注意：qt.gtimg 的 s_ 简版指数在本机代理下返回陈旧错值（2026-10-22 实测
# 上证报 3876.02/-0.27%，与新浪及腾讯完整版 3679.86/-0.89% 均不符），
# 故用完整版 q= 代码格式，涨跌幅由 现价/昨收 自行计算，不依赖字段位。
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn",
}
PAGE_NUM = 50
MAX_PAGES = 160
MIN_STOCKS = 3000  # 低于此数视为快照整体失败（≥5000 即使有缺页也可用）
# 2026-10-22 稳定性修复：当日上午 num=100＋8 并发运行正常，午后 14:00、
# 14:15 两趟连续失败——本机代理对大响应/高并发不稳定，num=100 分页被
# 截断、低并发回退仍缺页（覆盖仅 85–92%）。故改小页 num=50、并发 ≤3、
# 每页重试 4 次（递增间隔 0.5/1/2/4s），并在第一轮后对缺页做第二轮补拉；
# 补拉后仍缺页只在摘要注明，不再判失败（失败线仍是总数 <3000）。
RETRY_BACKOFF = (0.5, 1.0, 2.0, 4.0)  # 每页首次失败后的重试间隔（共 4 次重试）


def fetch_text(url, timeout=25):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("gbk", errors="replace")


def fetch_page(page):
    """单页快照，失败最多重试 4 次（间隔 0.5/1/2/4s 递增）；最终失败抛异常。"""
    last_err = None
    for attempt in range(len(RETRY_BACKOFF) + 1):
        try:
            raw = fetch_text(SINA_URL.format(page=page))
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError(f"返回非列表: {raw[:80]}")
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < len(RETRY_BACKOFF):
                time.sleep(RETRY_BACKOFF[attempt])
    raise RuntimeError(f"第 {page} 页拉取失败: {last_err}")


def _end_and_missing(pages_rows):
    """由已拉页推断 (末页, 缺页)：连续 2 个空页才确认列表结束（防单个空页
    误判丢掉尾页），第一个空页的前一页为末页；末页之前的失败页（None）、
    以及非末页的不足页都算缺页。尚未确认结束时末页返回 None。
    """
    run = 0
    end_empty = None
    for p in sorted(pages_rows):
        rows = pages_rows[p]
        if rows is not None and len(rows) == 0:
            run += 1
            if run >= 2:
                end_empty = p - 1  # 连续空页的第一个
                break
        else:
            run = 0
    if end_empty is None:
        return None, [p for p in sorted(pages_rows) if pages_rows[p] is None]
    last_page = end_empty - 1
    missing = [p for p in range(1, last_page + 1)
               if pages_rows.get(p) is None
               or (p != last_page and len(pages_rows[p]) < PAGE_NUM)]
    return last_page, missing


def _fetch_forward(ex, pages_rows, start):
    """从 start 起按批（12 页/批，池内 ≤3 并发）往后拉，直到确认结束或 MAX_PAGES。"""
    while start <= MAX_PAGES:
        batch_pages = [p for p in range(start, min(start + 12, MAX_PAGES + 1))
                       if p not in pages_rows]
        if batch_pages:
            futs = {p: ex.submit(_safe_page, p) for p in batch_pages}
            for p in batch_pages:
                pages_rows[p] = futs[p].result()
        start += 12
        last_page, _ = _end_and_missing(pages_rows)
        if last_page is not None:
            break


def fetch_snapshot():
    """分页拉全市场快照（num=50，≤3 并发），返回 {code: row} 与仍缺页列表。

    第一轮按批拉到「连续 2 空页」确认结束；第二轮只补缺页，并复核末页后
    的第一个空页（防空页误报丢尾页）——复核回来非空则继续往后拉。
    补拉后仍缺的页如实返回，由摘要注明；是否判整体失败看总数（见 main）。
    """
    from concurrent.futures import ThreadPoolExecutor

    first = fetch_page(1)  # 第 1 页失败（已含重试）直接抛 → 整体失败
    pages_rows = {1: first}
    with ThreadPoolExecutor(max_workers=3) as ex:
        if len(first) >= PAGE_NUM:
            _fetch_forward(ex, pages_rows, 2)
        for _ in range(3):  # 补拉轮（每轮内部每页再走完整重试）
            last_page, missing = _end_and_missing(pages_rows)
            targets = set(missing)
            if last_page is not None:
                nxt = pages_rows.get(last_page + 1)
                if nxt is not None and len(nxt) == 0:
                    targets.add(last_page + 1)  # 复核第一个空页
            if not targets:
                break
            futs = {p: ex.submit(_safe_page, p) for p in targets}
            changed = False
            for p, fut in futs.items():
                rows = fut.result()
                if rows is not None and rows != pages_rows.get(p):
                    pages_rows[p] = rows
                    changed = True
            new_last, _ = _end_and_missing(pages_rows)
            if new_last is None or (last_page is not None and new_last > last_page):
                _fetch_forward(ex, pages_rows, (last_page or 1) + 1)
            if not changed:
                break
    last_page, missing = _end_and_missing(pages_rows)
    if last_page is None:  # 始终未确认结束（极端情况）：以已拉最大页为界
        last_page = max(pages_rows) if pages_rows else 1
        missing = [p for p in range(1, last_page + 1)
                   if pages_rows.get(p) is None]
    stocks = {}
    for p in range(1, last_page + 1):
        rows = pages_rows.get(p)
        if not rows:
            continue
        for r in rows:
            code = str(r.get("code", "")).zfill(6)
            if code:
                stocks[code] = r
    return stocks, missing


def _safe_page(page):
    try:
        return fetch_page(page)
    except RuntimeError:
        return None


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _parse_tencent_indices(raw):
    out = []
    for line in raw.strip().splitlines():
        if '="' not in line:
            continue
        payload = line.partition('="')[2].rstrip('";').strip()
        f = payload.split("~")
        if len(f) < 5:
            continue
        point, prev = fnum(f[3]), fnum(f[4])
        if not point or not prev:
            continue
        out.append((f[1], point, (point - prev) / prev * 100))
    return out


def fetch_indices():
    """腾讯完整版指数（http；https 多代码会被重置，代理延迟高，超时给 45s）。

    批量请求最多试 2 次，能解析几只算几只；全失败返回 None
    （不影响宽度与名单核对）。
    """
    url = "http://qt.gtimg.cn/q=" + ",".join(TENCENT_CODES)
    for attempt in range(2):
        try:
            out = _parse_tencent_indices(fetch_text(url, timeout=45))
            if out:
                return out
        except Exception:  # noqa: BLE001
            pass
        if attempt == 0:
            time.sleep(3)
    return None


# ---------------- 外围联动 ----------------
# 最终数据源（2026-10-22 午间逐一实测，均验过时间戳新鲜度）：
# - 韩国KOSPI / 日经225 / 台湾加权：Yahoo Finance chart API
#   （query1.finance.yahoo.com/v8/finance/chart/%5EKS11|%5EN225|%5ETWII），
#   meta.regularMarketTime 是 UTC epoch，可换算北京时间验日期；
#   延迟数分钟（台湾实测可达约 20 分钟），摘要如实标数据时间。
# - 道指期货 / 纳指期货 / 富时中国A50期货：新浪 hq.sinajs.cn 的 hf_ 代码
#   （hf_YM / hf_NQ / hf_CHA50CFD），字段[6]=北京时间、[12]=日期，实时。
# 陈旧值陷阱（勿用）：
# - 新浪 int_ 全球指数（int_nikkei 等）无时间戳且数值严重陈旧：
#   2026-10-22 实测日经报 44,946.64，同期 Yahoo 为 63,306，差出一个年代。
# - 腾讯 qt.gtimg 的 usDJI/usIXIC 等全球代码经本机代理 http 返回空。
# - Stooq q/l 在本机代理下返回空响应或错误页，不可用。
YAHOO_INDICES = [
    ("韩国KOSPI", "%5EKS11"),
    ("日经225", "%5EN225"),
    ("台湾加权", "%5ETWII"),
]
SINA_HF_FUTURES = [
    ("道指期货", "hf_YM"),
    ("纳指期货", "hf_NQ"),
    ("富时中国A50期货", "hf_CHA50CFD"),
]
EXT_NAMES = [n for n, _ in YAHOO_INDICES] + [n for n, _ in SINA_HF_FUTURES]


def _fmt_ext_time(dt, today):
    """外围数据时间：今日只给 HH:MM，非今日带月-日，便于识别定格旧值。"""
    if dt.date() == today:
        return dt.strftime("%H:%M")
    return dt.strftime("%m-%d %H:%M")


def _fetch_yahoo_index(name, symbol):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           "?interval=5m&range=1d")
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    meta = data["chart"]["result"][0]["meta"]
    price = fnum(meta.get("regularMarketPrice"))
    ts = int(meta.get("regularMarketTime") or 0)
    if not price or not ts:
        raise ValueError(f"Yahoo {symbol} meta 缺字段")
    # 涨跌幅优先用官方 regularMarketChangePercent：^TWII 的 previousClose
    # 字段与官方涨幅对不上（2026-10-22 实测 prev=46292.9  implied -0.01%，
    # 官方为 -1.26%，真实昨收约 46887.8），用价/昨收自算会出错。
    if meta.get("regularMarketChangePercent") is not None:
        pct = fnum(meta["regularMarketChangePercent"])
    else:
        prev = fnum(meta.get("previousClose") or meta.get("chartPreviousClose"))
        if not prev:
            raise ValueError(f"Yahoo {symbol} 无昨收可算涨跌幅")
        pct = (price - prev) / prev * 100
    dt = datetime.fromtimestamp(ts, CST)
    return name, price, pct, dt


def _fetch_sina_hf():
    """新浪 hf_ 期货批量拉取，返回 {显示名: (价, 涨跌幅%, 时间datetime)}。"""
    codes = ",".join(c for _, c in SINA_HF_FUTURES)
    raw = fetch_text(f"https://hq.sinajs.cn/list={codes}", timeout=15)
    by_code = {}
    for line in raw.splitlines():
        m = re.search(r'hq_str_(\w+)="([^"]*)"', line)
        if not m:
            continue
        f = m.group(2).split(",")
        if len(f) < 13:
            continue
        price, prev = fnum(f[0]), fnum(f[7])
        if not price or not prev:
            continue
        try:
            dt = datetime.strptime(f"{f[12]} {f[6]}", "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=CST)
        except ValueError:
            continue
        by_code[m.group(1)] = (price, (price - prev) / prev * 100, dt)
    return {name: by_code[code] for name, code in SINA_HF_FUTURES
            if code in by_code}


def fetch_external():
    """外围六品种：返回 (行列表[(名称,价,幅,时间dt)], 未取到名单)。

    每个品种独立容错：单个失败只进「未取到」，整体异常返回全未取到，
    绝不向上抛——外围失败不能拖垮 A 股摘要。
    """
    from concurrent.futures import ThreadPoolExecutor

    got = {}
    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {name: ex.submit(_fetch_yahoo_index, name, sym)
                    for name, sym in YAHOO_INDICES}
            fut_hf = ex.submit(_fetch_sina_hf)
            for name, fut in futs.items():
                try:
                    _, price, pct, dt = fut.result()
                    got[name] = (price, pct, dt)
                except Exception:  # noqa: BLE001
                    pass
            try:
                got.update(fut_hf.result())
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    rows = [(n, *got[n]) for n in EXT_NAMES if n in got]
    missing = [n for n in EXT_NAMES if n not in got]
    return rows, missing


def find_pool():
    """在 stock-screener 目录找最新合格池缓存，返回 (代码列表, 来源文件名) 或 (None, None)。

    线索优先级：文件名含 池/pool 的 json → 其余 *.json 按修改时间倒序，
    结构认 {hits:[{code}...]} 或 [{code}...] 或 {codes:[...]}。
    """
    candidates = []
    for path in glob.glob(os.path.join(SCREENER_DIR, "*.json")):
        name = os.path.basename(path)
        hint = 0 if ("池" in name or "pool" in name.lower()) else 1
        candidates.append((hint, -os.path.getmtime(path), path))
    candidates.sort()
    for _, _, path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            continue
        codes = None
        if isinstance(data, dict) and isinstance(data.get("hits"), list):
            codes = [str(h.get("code", "")).zfill(6) for h in data["hits"]
                     if isinstance(h, dict) and h.get("code")]
        elif isinstance(data, dict) and isinstance(data.get("codes"), list):
            codes = [str(c).zfill(6) for c in data["codes"]]
        elif isinstance(data, list):
            codes = [str(h.get("code", "")).zfill(6) for h in data
                     if isinstance(h, dict) and h.get("code")]
        if codes and 30 <= len(codes) <= 5000:
            return codes, os.path.basename(path)
    return None, None


def _file_code_map(text):
    """从状态文件正文的「名称 代码」出现处建名称→代码映射。

    衔接段只有「名称 数值」没有代码，靠正文核对/名单段里现成的代码补齐。
    """
    cmap = {}
    for m in re.finditer(r"([一-龥][一-龥A-Za-z0-9]*?)\s+(\d{6})(?!\d)", text):
        cmap.setdefault(m.group(1), m.group(2))
    return cmap


def parse_watchlist(stocks=None):
    """解析盘中滚动名单-最新.md，返回 [(名称, 代码, 证伪线或None)]（保序去重）。

    证伪线优先取文件末尾衔接小节的「证伪线：」行：按 ｜ 切段，每段取第一组
    「名称 数值」（降级观察票取降级线；括号注记里的破开盘价、失效线排在
    后面，不会误取）；代码由正文「名称 代码」映射或快照名称匹配补齐。

    2026-10-22 解析修复：旧逻辑找的字面小节名「下一趟衔接证伪线」在现行
    状态文件里不存在（实际是「## 下一趟衔接」小节＋一行「- 证伪线：…」），
    且衔接段只有名称+数值、旧的「名称+代码」正则永远匹配不到；正文兜底又
    只认 | 表格行，现行文件无表格 → 名单核对恒解析 0 只。故按现行实际格式
    重写主路径，旧格式（名称+代码衔接段、正文表格行）保留为兜底。
    """
    try:
        with open(WATCHLIST_PATH, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []

    entries = {}  # code -> [name, line]
    order = []

    def _add(name, code, line):
        if code is None:
            return
        if code not in entries:
            entries[code] = [name, line]
            order.append(code)
        elif entries[code][1] is None and line is not None:
            entries[code][1] = line

    cmap = _file_code_map(text)
    snap_map = {}
    if stocks:
        for code, r in stocks.items():
            nm = re.sub(r"\s+", "", str(r.get("name", "")))
            if nm:
                snap_map.setdefault(nm, code)

    def _code_of(name):
        return cmap.get(name) or snap_map.get(name)

    # 衔接小节：优先「## …下一趟衔接」标题，旧字面串兜底
    sec = None
    hm = re.search(r"(?m)^#{1,6}[^\n]*下一趟衔接.*?(?=^#{1,6}\s|\Z)",
                   text, re.S)
    if hm:
        sec = hm.group(0)
    else:
        om = re.search(r"下一趟衔接证伪线.*?(?=\n## |\Z)", text, re.S)
        if om:
            sec = om.group(0)

    # 1) 衔接小节（兜底整文）里的「证伪线：」行：名称+数值，括号注记不影响
    for scope in [s for s in (sec, text) if s]:
        found = False
        for raw_line in scope.splitlines():
            s = raw_line.strip().lstrip("-*• ").strip()
            if not s.startswith("证伪线"):
                continue
            body = re.sub(r"^证伪线\s*[:：]\s*", "", s)
            for seg in body.split("｜"):
                pm = re.search(
                    r"([一-龥][一-龥A-Za-z0-9]*?)\s+(\d+(?:\.\d+)?)", seg)
                if not pm:
                    continue
                _add(pm.group(1), _code_of(pm.group(1)), float(pm.group(2)))
                found = True
        if found:
            break

    # 2) 衔接小节旧格式兜底：「名称 代码 …数值」（代码后第一个数字即证伪线）
    if sec:
        hits = list(re.finditer(r"([一-龥][一-龥A-Za-z\-]*?)\s*(\d{6})", sec))
        for i, hit in enumerate(hits):
            start = hit.end()
            end = hits[i + 1].start() if i + 1 < len(hits) else len(sec)
            num = re.search(r"\d+\.\d+|\d+", sec[start:end])
            _add(hit.group(1), hit.group(2),
                 float(num.group(0)) if num else None)

    # 3) 正文表格行兜底（证伪 X / 失效线）
    for row in text.splitlines():
        if not row.startswith("|"):
            continue
        cm = re.search(r"([一-龥][一-龥A-Za-z\-]*?)\s*(\d{6})", row)
        if not cm:
            continue
        lm = re.search(r"证伪[^\d]{0,6}(\d+\.?\d*)", row)
        _add(cm.group(1), cm.group(2), float(lm.group(1)) if lm else None)

    return [(entries[c][0], c, entries[c][1]) for c in order]


def is_limit_board(code, pct):
    """涨跌停近似口径：创业板/科创 20%，其余 10%（阈值 9.8/19.6）。"""
    if code.startswith(("300", "301", "688", "689")):
        return pct >= 19.6, pct <= -19.6
    return pct >= 9.8, pct <= -9.8


def build_digest(stocks, failed_pages, indices, pool, pool_src, watchlist,
                 ext=None):
    now = datetime.now(CST)
    ticks = Counter(str(r.get("ticktime", "")) for r in stocks.values())
    tick = ticks.most_common(1)[0][0] if ticks else "未知"

    ups = downs = flats = lu = ld = 0
    total_amount = 0.0
    for r in stocks.values():
        pct = fnum(r.get("changepercent"))
        total_amount += fnum(r.get("amount"))
        if pct > 0:
            ups += 1
        elif pct < 0:
            downs += 1
        else:
            flats += 1
        up_lim, dn_lim = is_limit_board(str(r.get("code", "")).zfill(6), pct)
        if up_lim:
            lu += 1
        if dn_lim:
            ld += 1

    lines = []
    lines.append(
        f"# 盯盘摘要 {now.strftime('%Y-%m-%d %H:%M:%S')}"
        f"（数据时点：新浪快照 ticktime {tick} 定格，共 {len(stocks)} 只"
        + (f"；缺页 {len(failed_pages)} 页（页号 {failed_pages}），覆盖以 {len(stocks)} 只为准"
           if failed_pages else "")
        + "）"
    )
    if indices:
        idx_txt = "｜".join(f"{n} {p:,.2f} {c:+.2f}%" for n, p, c in indices)
        lines.append(f"指数：{idx_txt}")
    else:
        lines.append("指数：腾讯指数拉取失败（宽度与名单核对不受影响）")
    lines.append(
        f"宽度：涨 {ups}／跌 {downs}／平 {flats}｜涨停 {lu}／跌停 {ld}"
        f"｜两市成交 {total_amount / 1e8:,.0f} 亿"
    )

    # 池内统计
    if pool:
        pool_rows = [stocks[c] for c in pool if c in stocks]
        reds = sum(1 for r in pool_rows if fnum(r.get("changepercent")) > 0)
        lines.append(
            f"池内（合格池 {len(pool)} 只，源 {pool_src}）："
            f"红盘 {reds}/{len(pool_rows)}（快照覆盖 {len(pool_rows)} 只）"
        )
        srt = sorted(pool_rows, key=lambda r: fnum(r.get("changepercent")),
                     reverse=True)
        lines.append("池内涨幅前10：")
        for r in srt[:10]:
            lines.append(
                f"- {r.get('name')} {str(r.get('code')).zfill(6)}｜"
                f"{fnum(r.get('trade')):,.2f}｜{fnum(r.get('changepercent')):+.2f}%"
            )
        lines.append("池内跌幅前10：")
        for r in srt[-10:][::-1]:
            lines.append(
                f"- {r.get('name')} {str(r.get('code')).zfill(6)}｜"
                f"{fnum(r.get('trade')):,.2f}｜{fnum(r.get('changepercent')):+.2f}%"
            )
    else:
        lines.append("池内：未找到合格池缓存（stock-screener 内无可解析的池文件），跳过池内统计")

    # 全市场涨跌前排（2026-10-22 应纠正补加：选股主业是扫描当下全市场
    # 形态好的个股，盯盘扫描面不能只有合格池——池外强势集群也要进视野；
    # 数据用已拉到的全市场快照现算，不新增请求）
    ranked = sorted(stocks.values(),
                    key=lambda r: fnum(r.get("changepercent")), reverse=True)

    def _mover_line(r):
        return (
            f"- {r.get('name')} {str(r.get('code')).zfill(6)}｜"
            f"{fnum(r.get('trade')):,.2f}｜{fnum(r.get('changepercent')):+.2f}%"
            f"｜额 {fnum(r.get('amount')) / 1e8:.2f} 亿"
            f"｜换手 {fnum(r.get('turnoverratio')):.2f}%"
        )

    lines.append("全市场涨幅前20：")
    for r in ranked[:20]:
        lines.append(_mover_line(r))
    lines.append("全市场跌幅前10：")
    for r in ranked[-10:][::-1]:
        lines.append(_mover_line(r))

    # 全市场宽扫描分桶（2026-11-03 纠正：选股要全市场筛、只剔 ST，
    # 不能只看涨幅前 20——没涨飞但资金已在动的票也要进扫描面。
    # 以下分桶全部由已拉到的全市场快照逐只现算，不新增请求。）
    def _not_limit_up(r):
        up_lim, _ = is_limit_board(str(r.get("code", "")).zfill(6),
                                   fnum(r.get("changepercent")))
        return not up_lim

    lu_rows = sorted((r for r in stocks.values() if not _not_limit_up(r)),
                     key=lambda r: fnum(r.get("amount")), reverse=True)
    lines.append(f"涨停梯队（共 {len(lu_rows)} 只，按成交额前25）：")
    for r in lu_rows[:25]:
        lines.append(_mover_line(r))

    waking = sorted(
        (r for r in stocks.values()
         if 1.0 <= fnum(r.get("changepercent")) <= 7.5 and _not_limit_up(r)),
        key=lambda r: fnum(r.get("amount")), reverse=True)
    lines.append("放量走强未涨停（涨幅 1%–7.5%，按成交额前20）：")
    for r in waking[:20]:
        lines.append(_mover_line(r))

    strong_small = sorted(
        (r for r in stocks.values()
         if 3.0 <= fnum(r.get("changepercent")) < 9.8 and _not_limit_up(r)),
        key=lambda r: fnum(r.get("changepercent")), reverse=True)
    lines.append("走强未封板（涨幅 3%–9.8%，按涨幅前20，不看成交额——小成交票从这里出）：")
    for r in strong_small[:20]:
        lines.append(_mover_line(r))

    by_turnover = sorted(stocks.values(),
                         key=lambda r: fnum(r.get("turnoverratio")),
                         reverse=True)
    lines.append("换手活跃前15：")
    for r in by_turnover[:15]:
        lines.append(_mover_line(r))

    by_amount = sorted(stocks.values(),
                       key=lambda r: fnum(r.get("amount")), reverse=True)
    lines.append("成交额前15（容量核心）：")
    for r in by_amount[:15]:
        lines.append(_mover_line(r))

    # 名单核对
    lines.append(f"名单核对（{len(watchlist)} 只）：")
    if not watchlist:
        lines.append("⚠️ 名单解析失败/为空，请人工核对状态文件（盘中滚动名单-最新.md）")
    for name, code, line in watchlist:
        r = stocks.get(code)
        line_txt = f"{line:,.2f}" if line is not None else "未解析"
        if r is None:
            lines.append(f"- {name} {code}｜快照无此票｜证伪线 {line_txt}｜状态 未知")
            continue
        price = fnum(r.get("trade"))
        pct = fnum(r.get("changepercent"))
        high = fnum(r.get("high"))
        low = fnum(r.get("low"))
        amt = fnum(r.get("amount")) / 1e8
        if line is None:
            status = "证伪线未解析"
        else:
            dist = (price - line) / line * 100 if line else 0.0
            if price < line:
                status = f"🔴已破线 {dist:+.2f}%"
            elif low < line:
                status = f"⚠️盘中破线已收回 {dist:+.2f}%"
            else:
                status = f"守线 {dist:+.2f}%"
        lines.append(
            f"- {name} {code}｜{price:,.2f} {pct:+.2f}%｜高 {high:,.2f}／低 {low:,.2f}"
            f"｜额 {amt:.2f} 亿｜证伪线 {line_txt}｜{status}"
        )

    # 外围联动（A 股与韩/日/台及美股期货、A50 的跟随参照）
    ext_rows, ext_missing = ext if ext else ([], EXT_NAMES)
    lines.append("外围联动：")
    for ename, eprice, epct, edt in ext_rows:
        lines.append(
            f"- {ename}｜{eprice:,.2f}｜{epct:+.2f}%｜{_fmt_ext_time(edt, now.date())}"
        )
    if ext_missing:
        lines.append(f"未取到：{'、'.join(ext_missing)}")
    return "\n".join(lines) + "\n"


def write_out(text):
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(text)
    sys.stdout.write(text)


def main():
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as ex:
        fut_idx = ex.submit(fetch_indices)  # 与快照并行，藏住腾讯的慢延迟
        fut_ext = ex.submit(fetch_external)  # 外围联动，内部已容错
        try:
            stocks, failed_pages = fetch_snapshot()
            if len(stocks) < MIN_STOCKS:
                raise RuntimeError(
                    f"快照仅取到 {len(stocks)} 只（<{MIN_STOCKS}），视为整体失败"
                )
        except Exception as e:  # noqa: BLE001
            now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
            write_out(f"# 盯盘摘要 拉取失败 {now}：{e}\n")
            print(f"intraday_digest 失败: {e}", file=sys.stderr)
            return 1
        indices = fut_idx.result()
        try:
            ext = fut_ext.result()
        except Exception:  # noqa: BLE001
            ext = ([], EXT_NAMES)
    pool, pool_src = find_pool()
    watchlist = parse_watchlist(stocks)
    write_out(build_digest(stocks, failed_pages, indices, pool, pool_src,
                           watchlist, ext))
    return 0


if __name__ == "__main__":
    sys.exit(main())
