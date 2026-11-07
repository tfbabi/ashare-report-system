#!/usr/bin/env python3
"""把 markdown 转成带内联样式的 HTML 片段（供 Gmail --html 正文使用）。
用法: md_to_html_email.py <输入.md> <输出.html>"""
import json
import re
import sys
from pathlib import Path

import markdown

STYLES = {
    "h1": "font-size:22px;border-bottom:3px solid #444;padding-bottom:8px;margin:0 0 14px 0;color:#111;",
    "h2": "font-size:17px;background:#f2f4f7;border-left:4px solid #666;padding:7px 11px;margin:20px 0 10px 0;color:#111;",
    "h3": "font-size:15px;margin:14px 0 6px 0;color:#111;",
    "table": "border-collapse:collapse;width:100%;margin:10px 0 14px 0;font-size:15px;line-height:1.65;",
    "th": "border:1px solid #b6c2d2;padding:8px 10px;background:#dbe4f0;color:#1f3a5f;font-weight:bold;text-align:left;white-space:nowrap;",
    "td": "border:1px solid #c9ced6;padding:8px 10px;vertical-align:top;",
    "blockquote": "border-left:3px solid #d4a017;background:#fffbe9;margin:8px 0;padding:6px 10px;color:#444;",
    "p": "margin:7px 0;",
    "li": "margin:4px 0;",
}

def inline_styles(html: str) -> str:
    for tag, style in STYLES.items():
        html = re.sub(rf"<{tag}(\s[^>]*)?>", lambda m, t=tag, s=style: f'<{t} style="{s}">', html)
    return html

def apply_highlight(md: str) -> str:
    """重点标注（ 2026-10-22 可读性规范）：!!!文字!!! → 大号红字（全篇最重要的一条）；
    ==文字== → 红色加粗。"""
    md = re.sub(r"!!!(.+?)!!!", r'<span style="color:#c0392b;font-weight:bold;font-size:19px;">\1</span>', md)
    md = re.sub(r"==(.+?)==", r'<span style="color:#c0392b;font-weight:bold;">\1</span>', md)
    return md


def conclusion_box(html: str) -> str:
    """以【结论】开头的段落 → 红色结论框（更大字号、浅红底、红左边框）。
    在 inline_styles 之后运行，整段替换 p 的开标签样式。"""
    return re.sub(
        r"<p(?:\s[^>]*)?>(【结论】)",
        '<p style="background:#fdf0ef;border-left:4px solid #c0392b;padding:8px 12px;font-size:15px;font-weight:bold;color:#7b1c12;margin:10px 0;">\\1',
        html,
    )


def _em_market(code: str) -> str:
    """东方财富个股页市场前缀：沪市 sh、深市 sz、北交所 bj。
    920 必须先判：首位 9 会被沪市分支抢先命中（原顺序下北交所 920 永远拼成 sh，
    2026-11-04 验证代码格链接时发现并修正）。"""
    if code.startswith("920") or code[0] in "48":
        return "bj"
    if code[0] in "69":
        return "sh"
    return "sz"


def _link_html(code: str, text: str) -> str:
    url = f"https://quote.eastmoney.com/{_em_market(code)}{code}.html"
    return (f'<a href="{url}" target="_blank" '
            f'style="color:#1a5fb4;text-decoration:none;">{text}</a>')


def _jump_link_html(code: str, text: str) -> str:
    """名称链接（ 2026-11-04 定版）：hammerspoon://emjump?code=裸 6 位代码，
    Mac 上点名称经 Hammerspoon 唤醒东方财富经典版并跳到个股页。
    （同日早先的「看盘」胶囊按钮＋末列方案已废，名称本身即唤醒链接。）"""
    url = f"hammerspoon://emjump?code={code}"
    return (f'<a href="{url}" target="_blank" '
            f'style="color:#1a5fb4;text-decoration:none;">{text}</a>')


def linkify_codes(html: str) -> str:
    """表格个股链接（ 2026-11-04 定版）：名称挂 Hammerspoon 唤醒链接
    （点名称跳东方财富经典版个股页），代码挂东方财富网页链接（点代码开
    网页版）。分列（名称、代码两列）为主：纯 6 位代码独占一格时，名称取
    相邻格（代码在首列取后一格、否则取前一格）加唤醒链接，代码格本身加
    网页链接。同格（名称 代码在一个格里）一般已由散文路径处理成对链接，
    这里只兜底未被散文命中的裸文本格。不再有「看盘」按钮与末列。"""

    def process_row(row):
        tds = list(re.finditer(r"<td(\s[^>]*)?>(.*?)</td>", row, flags=re.S))
        if not tds:
            return row
        # 分列：纯 6 位代码独占一格
        code_idx, code = None, None
        for i, td in enumerate(tds):
            text = re.sub(r"<[^>]+>", "", td.group(2)).strip()
            if re.fullmatch(r"\d{6}", text):
                code_idx, code = i, text
                break
        if code is not None:
            target = code_idx - 1 if code_idx > 0 else code_idx + 1
            if target >= len(tds):
                return row
            td = tds[target]
            inner = td.group(2)
            if "<a " in inner or not inner.strip():
                return row
            new_td = f"<td{td.group(1) or ''}>{_jump_link_html(code, inner.strip())}</td>"
            row = row[:td.start()] + new_td + row[td.end():]
            # 替换后格数不变，按原下标重新定位代码格再包网页链接
            tds2 = list(re.finditer(r"<td(\s[^>]*)?>(.*?)</td>", row, flags=re.S))
            ctd = tds2[code_idx]
            new_ctd = f"<td{ctd.group(1) or ''}>{_link_html(code, code)}</td>"
            return row[:ctd.start()] + new_ctd + row[ctd.end():]
        # 同格兜底：裸文本「名称 代码」、未被散文路径处理
        for td in tds:
            inner = td.group(2)
            if "<" not in inner:
                mm = re.fullmatch(r"(\S.{0,11}?)\s+(\d{6})", inner.strip())
                if mm:
                    code = mm.group(2)
                    new_td = (f"<td{td.group(1) or ''}>"
                              f"{_jump_link_html(code, mm.group(1))} {_link_html(code, code)}</td>")
                    return row[:td.start()] + new_td + row[td.end():]
        return row

    def table_repl(m):
        block = m.group(0)
        rows = list(re.finditer(r"<tr>.*?</tr>", block, flags=re.S))
        if not rows:
            return block
        out, pos = [], 0
        for rm in rows:
            out.append(block[pos:rm.start()])
            row = rm.group(0)
            out.append(row if "<th" in row else process_row(row))
            pos = rm.end()
        out.append(block[pos:])
        return "".join(out)

    return re.sub(r"<table.*?</table>", table_repl, html, flags=re.S)


def linkify_prose_codes(html: str) -> str:
    """正文/简介里「名称 代码」挨在一起的散文写法 → 成对链接（ 2026-11-04
    定版，与表格路径同规）：名称挂 Hammerspoon 唤醒链接（点名称跳东方财富
    经典版个股页），代码挂东方财富网页链接（点代码开网页版），中间空格
    原样。已在 <a> 内的片段跳过，不套娃。"""
    pat = re.compile(r"([一-龥A-Za-z][一-龥A-Za-z·]{1,7})\s(\d{6})(?!\d)")
    parts = re.split(r"(<a\s[^>]*>.*?</a>)", html, flags=re.S)
    for i, part in enumerate(parts):
        if part.startswith("<a "):
            continue
        parts[i] = pat.sub(lambda m: _jump_link_html(m.group(2), m.group(1))
                           + " " + _link_html(m.group(2), m.group(2)), part)
    return "".join(parts)


_PCT_TOKEN = re.compile(r"([+\-−])(\d+(?:\.\d+)?)%")


def _color_pct_text(text: str) -> str:
    """带符号百分比按红涨绿跌着色加粗（ 2026-11-06 定：所有百分比列都这样、
    涉及所有表格形式）。无符号的百分比不动。"""
    def repl(m):
        color = "#c0392b" if m.group(1) == "+" else "#1e8449"
        return f'<span style="color:{color};font-weight:bold;">{m.group(0)}</span>'
    return _PCT_TOKEN.sub(repl, text)


def _color_pct_html(inner: str) -> str:
    """只给标签外的文本段着色，不碰链接 href 等属性。"""
    parts = re.split(r"(<[^>]+>)", inner)
    return "".join(p if p.startswith("<") else _color_pct_text(p) for p in parts)


def emphasize_table_columns(html: str) -> str:
    """重点突出（ 2026-11-03 定，2026-11-06 修订）：
    证伪线/失效线列橙色加粗（警示位，与结论红区分）；
    所有表格单元格里的带符号百分比一律红涨绿跌着色加粗（不限于涨幅列——
    开盘涨幅、当前较昨日放量、黄均线、图形形态、现价括号内涨幅都算）；
    现价列改「涨幅在前、价格在后」：52.22（+12.57%）→ +12.57%（52.22），
    涨幅着色、价格深色加粗。正文核心结论的红色由 == == 标注负责。"""
    state = {"f": None, "p": None}

    def row_repl(m):
        row = m.group(0)
        if "<th" in row:
            headers = [re.sub(r"<[^>]+>", "", h).strip()
                       for h in re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", row, flags=re.S)]
            state["f"] = next((i for i, h in enumerate(headers)
                                if "证伪线" in h or "失效线" in h), None)
            state["p"] = next((i for i, h in enumerate(headers) if "现价" in h), None)
            return row
        f, p = state["f"], state["p"]
        cells = list(re.finditer(r"<td(\s[^>]*)?>(.*?)</td>", row, flags=re.S))
        for i in range(len(cells) - 1, -1, -1):
            td = cells[i]
            inner = td.group(2)
            if i == f:
                new_inner = f'<span style="color:#d35400;font-weight:bold;">{inner}</span>'
            elif i == p:
                plain = re.sub(r"<[^>]+>", "", inner).strip()
                cm = re.fullmatch(
                    r"([\d,]+(?:\.\d+)?)\s*[（(]\s*([+\-−]?\d+(?:\.\d+)?%)\s*[）)]",
                    plain)
                if cm and "<" not in inner:
                    price, pct = cm.group(1), cm.group(2)
                    new_inner = (_color_pct_text(pct) +
                                 f'<span style="color:#111;font-weight:bold;">（{price}）</span>')
                elif "%" in plain:
                    new_inner = (f'<span style="color:#111;font-weight:bold;">'
                                 f'{_color_pct_html(inner)}</span>')
                else:
                    new_inner = f'<span style="color:#111;font-weight:bold;">{inner}</span>'
            else:
                new_inner = _color_pct_html(inner)
            if new_inner != inner:
                new = f'<td{td.group(1) or ""}>{new_inner}</td>'
                row = row[:td.start()] + new + row[td.end():]
        return row

    return re.sub(r"<tr>.*?</tr>", row_repl, html, flags=re.S)


def nowrap_short_columns(html: str) -> str:
    """短列不换行（ 2026-11-03 定）：除句子列外，其余列单元格一律
    white-space:nowrap，名称/题材/证伪线等不许折成第二行。
    2026-11-04 晚补（尾盘十列表实测）：句子列＝「理由」「验证点」两类——
    都放开换行；且「理由」列加 min-width:160px 保底，否则十列宽表里
    不换行的长列（失效线/验证点）会把理由列挤成一字一行的竖条。"""
    state = {"wrap": set(), "reason": None}

    def add_style(td_open: str, style: str) -> str:
        if 'style="' in td_open:
            return td_open.replace('style="', f'style="{style}', 1)
        return td_open[:-1] + f' style="{style}">'

    def row_repl(m):
        row = m.group(0)
        if "<th" in row:
            headers = [re.sub(r"<[^>]+>", "", h).strip()
                       for h in re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", row, flags=re.S)]
            state["wrap"] = {i for i, h in enumerate(headers)
                             if "理由" in h or "验证点" in h}
            state["reason"] = next((i for i, h in enumerate(headers) if "理由" in h), None)
            cells = list(re.finditer(r"(<th(?:\s[^>]*)?>)(.*?)(</th>)", row, flags=re.S))
            for i in range(len(cells) - 1, -1, -1):
                th = cells[i]
                if i == state["reason"]:
                    row = row[:th.start()] + add_style(th.group(1), "min-width:160px;") + th.group(2) + th.group(3) + row[th.end():]
                elif i not in state["wrap"]:
                    row = row[:th.start()] + add_style(th.group(1), "white-space:nowrap;") + th.group(2) + th.group(3) + row[th.end():]
            return row
        cells = list(re.finditer(r"(<td(?:\s[^>]*)?>)(.*?)(</td>)", row, flags=re.S))
        for i in range(len(cells) - 1, -1, -1):
            td = cells[i]
            if i == state["reason"]:
                row = row[:td.start()] + add_style(td.group(1), "min-width:160px;") + td.group(2) + td.group(3) + row[td.end():]
            elif i not in state["wrap"]:
                row = row[:td.start()] + add_style(td.group(1), "white-space:nowrap;") + td.group(2) + td.group(3) + row[td.end():]
        return row

    return re.sub(r"<tr>.*?</tr>", row_repl, html, flags=re.S)


LINE_BANNERS = [
    # (关键词, 底色, 字色, 色块) —— 浅底深字（ 2026-11-03 定：深色太重，弄浅点）
    ("柚子", "#dbeafe", "#1e40af", "🟦"),
    ("专家", "#ffedd5", "#9a3412", "🟧"),
    ("趋势", "#dcfce7", "#166534", "🟩"),
    ("ruobing", "#ede9fe", "#5b21b6", "🟪"),
    ("核心", "#ede9fe", "#5b21b6", "🟪"),
]


def style_line_banners(html: str) -> str:
    """四线段标题 → 整行浅色横幅（ 2026-11-03 定：每条线看不清，要一眼知道是谁选的）。
    柚子浅蓝／专家浅橙／趋势浅绿／ruobing核心浅紫，浅底＋同色系深字，加粗大字号。"""
    def repl(m):
        tag, text = m.group(1), m.group(3)
        plain = re.sub(r"<[^>]+>", "", text).strip()
        for key, bg, fg, emoji in LINE_BANNERS:
            if key in plain:
                label = plain if emoji in plain else f"{emoji} {plain}"
                return (f'<{tag} style="background:{bg};color:{fg};font-size:17px;'
                        f'font-weight:bold;padding:9px 14px;margin:20px 0 10px 0;">{label}</{tag}>')
        return m.group(0)

    return re.sub(r"<(h2|h3)(\s[^>]*)?>(.*?)</\1>", repl, html, flags=re.S)


def color_line_cells(html: str) -> str:
    """表格首列若是线别名（柚子/专家/趋势/ruobing核心），整格染成线别色底白字，
    与段头横幅同色系——简介小表里也能一眼对上是谁选的（ 2026-11-03 定）。"""
    def row_repl(m):
        row = m.group(0)
        cells = list(re.finditer(r"(<td)(\s[^>]*)?>(.*?)(</td>)", row, flags=re.S))
        if not cells:
            return row
        td = cells[0]
        plain = re.sub(r"<[^>]+>", "", td.group(3)).strip()
        for key, bg, fg, emoji in LINE_BANNERS:
            if key in plain and len(plain) <= 10:
                attrs = td.group(2) or ""
                extra = f"background:{bg};color:{fg};font-weight:bold;"
                if 'style="' in attrs:
                    attrs = attrs.replace('style="', f'style="{extra}', 1)
                else:
                    attrs = attrs + f' style="{extra}"'
                new = f"<td{attrs}>{td.group(3)}</td>"
                return row[:td.start()] + new + row[td.end():]
        return row

    return re.sub(r"<tr>.*?</tr>", row_repl, html, flags=re.S)


def _limit_threshold(code: str, name: str) -> float:
    """涨停判定阈值（按板块涨幅限制，取略低于名义值容误差）：
    ST 5%、双创 20%、北交所 30%、主板 10%（口径同独钓：主板≥9.8/双创≥19.7）。"""
    if "ST" in name.upper():
        return 4.8
    if code.startswith(("688", "689", "300", "301")):
        return 19.7
    if code.startswith("920") or (code and code[0] in "48"):
        return 29.7
    return 9.8


def highlight_outrun_rows(html: str) -> str:
    """整行分级突出（ 2026-11-03 定）：表格同时有「竞价涨幅」列和当前侧涨幅列
    （当前涨幅／今日涨幅／收盘涨幅）时——
    ① 已涨停的行（当前涨幅达到该板块涨停阈值）：整行深红背景＋白字加粗；
    ② 当前涨幅 > 竞价涨幅的行（开盘后继续走强）：整行深蓝背景＋白字加粗
    （原浅黄， 22:25 改定深蓝、22:32 嫌 #1e40af 太深调浅到 #4472c4）。
    涨停优先于深蓝。背景同时写到 tr 与每个 td 上（部分邮件客户端不认 tr 背景）。"""
    LIGHT = "background:#4472c4;color:#ffffff;font-weight:bold;"
    DEEP = "background:#d64545;color:#ffffff;font-weight:bold;"
    state = {"a": None, "c": None, "code": None, "name": None}

    def pct(text: str):
        m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", text)
        return float(m.group(1)) if m else None

    def add_style(open_tag: str, style: str) -> str:
        if 'style="' in open_tag:
            return open_tag.replace('style="', f'style="{style}', 1)
        return open_tag[:-1] + f' style="{style}">'

    def row_repl(m):
        row = m.group(0)
        if "<th" in row:
            headers = [re.sub(r"<[^>]+>", "", h).strip()
                       for h in re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", row, flags=re.S)]
            state["a"] = next((i for i, h in enumerate(headers) if "竞价涨幅" in h), None)
            state["c"] = next((i for i, h in enumerate(headers)
                                if h in ("当前涨幅", "今日涨幅", "收盘涨幅")), None)
            state["code"] = next((i for i, h in enumerate(headers) if "代码" in h), None)
            state["name"] = next((i for i, h in enumerate(headers) if "名称" in h), None)
            return row
        a, c = state["a"], state["c"]
        if a is None or c is None:
            return row
        cells = list(re.finditer(r"<td(?:\s[^>]*)?>.*?</td>", row, flags=re.S))
        if max(a, c) >= len(cells):
            return row
        texts = [re.sub(r"<[^>]+>", "", cell.group(0)).strip() for cell in cells]
        va, vc = pct(texts[a]), pct(texts[c])
        if vc is None:
            return row
        code = texts[state["code"]] if state["code"] is not None and state["code"] < len(texts) else ""
        if not code:
            # 无代码列时从名称/代码的链接里提取代码（判涨停阈值要用）：
            # 代码的东财网页链接或名称的 Hammerspoon 唤醒链接都带裸代码
            mm = re.search(r"quote\.eastmoney\.com/(?:sh|sz|bj)(\d{6})", row)
            if not mm:
                mm = re.search(r"emjump\?code=(\d{6})", row)
            code = mm.group(1) if mm else ""
        name = texts[state["name"]] if state["name"] is not None and state["name"] < len(texts) else ""
        if vc >= _limit_threshold(code, name) or (va is not None and vc > va):
            style = DEEP if vc >= _limit_threshold(code, name) else LIGHT
            # 深底行：内部链接/彩色 span 的深色字改白，否则在深底上看不清；
            # md 直写的 <a> 没有内联颜色（客户端默认蓝/紫），也要强制改白
            for old in ("color:#1a5fb4", "color:#c0392b", "color:#1e8449",
                        "color:#d35400", "color:#111"):
                row = row.replace(old, "color:#ffffff")
            row = re.sub(r'<a (?![^>]*style=)', '<a style="color:#ffffff;" ', row)
            # 名称（Hammerspoon 唤醒链接）与代码（网页链接）都不是胶囊，
            # 一律白字化，否则深底上看不清（2026-11-04 定版后无按钮豁免）。
            row = re.sub(r'(<a[^>]*style="[^"]*?)color:#[0-9a-fA-F]{3,6}',
                         r"\1color:#ffffff", row)
            row = re.sub(r"(<td(?:\s[^>]*)?>)", lambda mm: add_style(mm.group(1), style), row)
            return re.sub(r"<tr>", f'<tr style="{style}">', row, count=1)
        return row

    return re.sub(r"<tr>.*?</tr>", row_repl, html, flags=re.S)


def style_ma5_cells(html: str) -> str:
    """在 5 日线上的形态格重点突出（ 2026-11-03 定）：「图形形态」列里
    写明「5日线上」的单元格加浅绿底＋加粗；整行已是深底（涨停深红/走强深蓝）
    的不再叠底色，只保留加粗（深底行本身已加粗）。"""
    state = {"form": None}

    def row_repl(m):
        row = m.group(0)
        if "<th" in row:
            headers = [re.sub(r"<[^>]+>", "", h).strip()
                       for h in re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", row, flags=re.S)]
            state["form"] = next((i for i, h in enumerate(headers) if "形态" in h), None)
            return row
        idx = state["form"]
        if idx is None:
            return row
        cells = list(re.finditer(r"(<td(?:\s[^>]*)?>)(.*?)(</td>)", row, flags=re.S))
        if idx >= len(cells):
            return row
        td = cells[idx]
        plain = re.sub(r"<[^>]+>", "", td.group(2))
        if "5日线上" not in plain:
            return row
        open_tag = td.group(1)
        if "background:#d64545" in open_tag or "background:#4472c4" in open_tag:
            return row  # 深底行已白字加粗，不叠色
        if 'style="' in open_tag:
            new_open = open_tag.replace('style="', 'style="background:#dcfce7;font-weight:bold;', 1)
        else:
            new_open = open_tag[:-1] + ' style="background:#dcfce7;font-weight:bold;">'
        return row[:td.start()] + new_open + td.group(2) + td.group(3) + row[td.end():]

    return re.sub(r"<tr>.*?</tr>", row_repl, html, flags=re.S)


def linkify_prose_names(html: str) -> str:
    """名称词典链接（ 2026-11-04 晚定：凡提到股票名称就复用链接规则）：
    正文句子、没有代码列的表格里只出现名称时，用同目录 stock_names.json
    （全市场名录：10-22 全市场表＋每日竞价快照补充）查代码，给名称挂
    经典版跳转链接。已成对挂上的链接、标签与样式块不动；只处理纯文本段。
    名录缺失就原样返回，不阻断转换。"""
    try:
        name2code = json.loads(
            (Path(__file__).parent / "stock_names.json").read_text(encoding="utf-8"))
    except Exception:
        return html
    # 通用词与公司名撞车的排除名单（散文里多为通用义，挂上会误导）：
    # 「机器人」多指行业（≠300024 机器人）、「新产业」多指新产业逻辑（≠300832 新产业）
    stoplist = {"机器人", "新产业"}
    plain = re.sub(r"<[^>]+>", "", html)
    hits = sorted({n for n in name2code
                   if len(n) >= 3 and n not in stoplist and n in plain},
                  key=len, reverse=True)
    if not hits:
        return html
    pat = re.compile("|".join(re.escape(n) for n in hits))

    def repl(m):
        name = m.group(0)
        return (f'<a href="hammerspoon://emjump?code={name2code[name]}" '
                f'target="_blank" style="color:#1a5fb4;text-decoration:none;">{name}</a>')

    parts = re.split(r"(<a\b[^>]*>.*?</a>|<style.*?</style>|<[^>]+>)",
                     html, flags=re.S)
    return "".join(p if p.startswith("<") else pat.sub(repl, p) for p in parts)


# 附件页表格点击排序（2026-11-05 问后加）：整页布局才注入；点表头按该列升/降序，
# 数字列按数值（金额自动换算万/亿），其余按文本。邮箱正文里脚本会被邮箱剥掉，无副作用。
SORT_SNIPPET = """
<style>th[data-sortable]{cursor:pointer}th[data-sortable]:hover{background:#dbe7f7}
tbody tr:hover{filter:brightness(1.15)}tbody tr:hover td{background-color:rgba(255,183,28,.38)!important}tbody tr:hover td:first-child{box-shadow:inset 5px 0 0 #f59e0b}tbody tr[data-jump]{cursor:pointer}</style>
<script>
function __cellNum(td){
  if(!td) return null;
  var t=td.textContent.replace(/,/g,'').trim();
  if(t===''||t==='未核'||t==='—'||t==='-') return null;
  var m=t.match(/([+-]?[0-9]+(?:\\.[0-9]+)?)\\s*亿/); if(m) return parseFloat(m[1])*1e8;
  m=t.match(/([+-]?[0-9]+(?:\\.[0-9]+)?)\\s*万/); if(m) return parseFloat(m[1])*1e4;
  if(t.indexOf('%')>=0){ var all=t.match(/[+-]?[0-9]+(?:\\.[0-9]+)?\\s*%/g);
    if(all) return parseFloat(all[all.length-1]); }
  m=t.match(/[+-]?[0-9]+(?:\\.[0-9]+)?/); if(m) return parseFloat(m[0]);
  return t;
}
function __formTier(td){
  if(!td) return 2;
  var t=td.textContent.trim();
  if(t.indexOf('形态好')===0) return 0;
  if(t.indexOf('形态一般')===0) return 1;
  return 2;
}
function __sortBy(tb, spec){
  var body=tb.querySelector('tbody'); if(!body) return;
  var rows=Array.prototype.slice.call(body.querySelectorAll('tr'));
  rows.sort(function(a,b){
    for(var k=0;k<spec.length;k++){
      var idx=spec[k][0], kind=spec[k][1], va, vb, c=0;
      if(kind==='tier'){ va=__formTier(a.children[idx]); vb=__formTier(b.children[idx]); c=va-vb; }
      else if(__cellNum(a.children[idx])===null||__cellNum(b.children[idx])===null){
        var na=__cellNum(a.children[idx])===null, nb=__cellNum(b.children[idx])===null;
        if(na&&nb) continue; return na?1:-1; }
      else { va=__cellNum(a.children[idx]); vb=__cellNum(b.children[idx]);
        if(typeof va==='number'&&typeof vb==='number') c=va-vb; else c=String(va).localeCompare(String(vb),'zh');
        if(kind==='desc') c=-c; }
      if(c!==0) return c;
    }
    return 0;
  });
  rows.forEach(function(r){body.appendChild(r);});
}
document.querySelectorAll('table').forEach(function(tb){
  var ths=tb.querySelectorAll('thead th');
  var heads=Array.prototype.map.call(ths,function(x){return x.textContent.trim();});
  ths.forEach(function(th,i){
    th.setAttribute('data-sortable','1'); th.title='点击按此列排序';
    th.addEventListener('click',function(){
      var dir=th.getAttribute('data-dir')==='asc'?'desc':'asc';
      ths.forEach(function(x){x.removeAttribute('data-dir'); x.textContent=x.textContent.replace(/ [▲▼]$/,'');});
      th.setAttribute('data-dir',dir); th.textContent=th.textContent+' '+(dir==='asc'?'\\u25B2':'\\u25BC');
      __sortBy(tb,[[i,dir]]);
    });
  });
  // 默认排序（ 2026-11-05 23:11 改定）：打开即按「竞价额」降序；
  // 没有竞价额列的表，依次用「当前涨幅」「涨幅」降序。
  var iAmt=heads.indexOf('竞价额'), iNow=heads.indexOf('当前涨幅'), iChg=heads.indexOf('涨幅');
  var iDef=iAmt>=0?iAmt:(iNow>=0?iNow:iChg);
  if(iDef>=0){
    __sortBy(tb,[[iDef,'desc']]);
    var th=ths[iDef];
    th.setAttribute('data-dir','desc'); th.textContent=th.textContent+' \\u25BC';
  }
  // 整行点击唤醒（ 2026-11-05 要）：行内有经典版跳转链接时，点本行任意
  // 空白处都跳经典版；点名称链接本身走默认、点代码的网页链接仍开网页。
  tb.querySelectorAll('tbody tr').forEach(function(tr){
    var link=tr.querySelector('a[href^="hammerspoon://"]');
    if(!link) return;
    tr.setAttribute('data-jump','1'); tr.title='点击本行任意处跳经典版个股';
    tr.addEventListener('click',function(e){
      if(e.target.closest('a')) return;
      window.location.href=link.getAttribute('href');
    });
  });
});
</script>
"""


_SEP_CELL = re.compile(r"^:?-{3,}:?$")


def normalize_table_separators(md):
    """分隔行格数与表头不一致时 python-markdown 会把整张表当普通段落输出
    （2026-11-06 盘中放量筛选样例实测踩坑：表头 12 格、分隔行 11 格，
    邮件正文整表变纯文本）。转换前把分隔行按上一行表头的格数重写。"""
    lines = md.split("\n")
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells or not all(_SEP_CELL.match(c) for c in cells):
            continue
        for j in range(i - 1, -1, -1):
            prev = lines[j].strip()
            if not prev:
                continue
            if prev.startswith("|") and prev.endswith("|"):
                n = len(prev.strip("|").split("|"))
                if n != len(cells):
                    lines[i] = "|" + "---|" * n
            break
    return "\n".join(lines)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8") as fh:
        md = fh.read()
    md = normalize_table_separators(md)
    md = apply_highlight(md)
    html = markdown.markdown(md, extensions=["tables", "sane_lists"])
    html = inline_styles(html)
    html = linkify_prose_codes(html)
    html = linkify_codes(html)
    # 名称词典兜底必须最后跑：先让成对路径（散文对/表格对）把名称＋代码挂全，
    # 词典只补剩下裸提的名称；若它先跑，表格名称格会被抢先挂上单链接，
    # linkify_codes 见格内已有 <a> 就整行跳过，代码格的网页链接会丢。
    html = linkify_prose_names(html)
    html = emphasize_table_columns(html)
    html = nowrap_short_columns(html)
    html = style_line_banners(html)
    html = color_line_cells(html)
    html = highlight_outrun_rows(html)
    html = style_ma5_cells(html)
    html = conclusion_box(html)
    # 布局两种（ 2026-11-03 定）：默认＝邮件正文（不居中、全宽左对齐），所有任务统一；
    # 附件/整页报告才用居中版心，转换时显式设 MD_HTML_LAYOUT=page
    import os
    if os.environ.get("MD_HTML_LAYOUT") == "page":
        wrap_style = ('font-family:\'Noto Sans CJK SC\',\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
                      'font-size:16px;line-height:1.75;color:#222;max-width:1600px;margin:0 auto;padding:18px 24px;')
    else:
        wrap_style = ('font-family:\'Noto Sans CJK SC\',\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
                      'font-size:16px;line-height:1.75;color:#222;padding:6px 4px;')
    # 悬停/整行点击/排序脚本两种布局都注入（2026-11-06 改）：正文版以前不带，
    # 在本地报告服务里打开正文时就没悬停、点行内空白不跳、只能点股票名；
    # 邮件客户端会自己剥掉脚本，对邮件本身无副作用。
    html += SORT_SNIPPET
    wrapped = f'<div style="{wrap_style}">' + html + "</div>"
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(wrapped)
    print("written", dst)

if __name__ == "__main__":
    main()
