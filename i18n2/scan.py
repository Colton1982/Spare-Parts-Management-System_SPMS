# -*- coding: utf-8 -*-
# SPMS 备件辅料仓管系统 (Spare Parts Management System)
# 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
# 邮箱：510658927@qq.com
# QQ：510658927
# Gitee：https://gitee.com/rookie-uncle
# GitHub：https://github.com/Colton1982
# Copyright (c) 2026 rookie-uncle. All rights reserved.
# 开源协议：Apache-2.0
"""i18n2.scan —— 界面漏翻扫描（运行期诊断，不产生词条 ID）

为什么需要它
------------
i18n 的故障永远是**静默**的：查不到就回退中文，页面照常渲染，不报错、
不白屏。所以「界面还是中文」这件事，只能靠主动扫出来。

三类问题的成因和修法完全不同，混在一起看必然误判
------------------------------------------------
  A  unwrapped     模板里直接写的中文，压根没走取词（<div>库存分析</div>）
                   → 翻译表里加多少条都没用，必须改模板包上 tr()
  B  unregistered  写了 tr('…')，但词条表里没有这条
                   → 一般是改了界面没重建词条表；一键登记即可，之后能翻
  C  untranslated  词条在、这门语言的译文是空的
                   → 纯翻译工作量，直接交给翻译引擎

只报 A 不报 B，用户会以为「都包了 tr 就是好的」；只报 B 不报 A，用户会
以为「扫描器没发现问题」，而界面上一半文案还是中文。三个都必须分开列。

关于正则
--------
取词形态的识别规则与 tools/build_catalog.py 保持一致（tr / TR / TRP / TRN /
_i18n.t / _xl）。两边**故意各写一份**：build_catalog 是构建期的建表工具，
依赖 tools/ 目录，而运行期必须能在只有应用目录的机器上工作。
一致性的自检手段很直接 —— 刚跑完 build_catalog 时，B 类应当为 0。
"""
from __future__ import annotations

import io
import json
import os
import re

CJK = re.compile(r"[\u4e00-\u9fff]")
# 全角标点：`{name}（{role} / {dept}）` 这种**只有括号没有汉字**的格式串，
# 光看 CJK 判据会被整条跳过 —— 它却实实在在是中文写法，英文界面下
# 用户下拉会显示 `张三（管理员 / 线物料库）`。所以「含全角标点」也算候选。
FULLWIDTH = re.compile(r"[（）「」『』，。：；！？、—…“”‘’]")
# 纯标点（`…`、`（`）不算文案：它们在任何语言里都长一样，没必要进词条表。
# 只有「全角标点 + 占位符/字母」才算中文写法，例如 {name}（{role} / {dept}）。
HAS_WORD = re.compile(r"[A-Za-z0-9{]")


def _looks_chinese_authored(key: str) -> bool:
    if CJK.search(key):
        return True
    return bool(FULLWIDTH.search(key) and HAS_WORD.search(key))

# 与 build_catalog 一致：只认「函数名( 引号」这种确定形态。
# 前缀 `(?<![\w.])` 是防止把 `xxx.tr(` 这类同名方法误当取词；但 `window.TR(...)`
# 恰恰是**合法**写法（前端拿不到裸 TR 时的兜底），所以要把 window./self./globalThis.
# 这几种前缀显式放行 —— 否则 charts.js 里的 window.TR('暂无数据') 会被报成
# 「模板里裸写的中文没走取词」，修都没法修（它明明走了）。
RE_CALL = re.compile(
    r"(?<![\w.])(?:(?:window|self|globalThis)\.)?"
    r"(?:tr|TR|TRP|TRN|_i18n\.t|i18n2\.t|i18n\.t|_xl)\s*\(\s*(['\"])((?:\\.|(?!\1).)*)\1",
    re.S)

# 文本节点：> 与 < 之间
RE_NODE = re.compile(r">([^<>]{1,400})<")
# Jinja 表达式 / 语句 / 注释
RE_EXPR = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)
# HTML 注释
RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# 模板里被 Jinja 包住的取词：{{ tr('…') }}
RE_WRAPPED = re.compile(r"\{\{\s*(?:tr|TR)\s*\(", re.S)

SKIP_DIRS = {"runtime", "__pycache__", ".git", "data", "logs", "locale",
             "i18n2", "_backup", "_removed", "dist", "vendor", "lib", "plugins",
             "node_modules"}
SKIP_FILES = {"i18n.py", "build_catalog.py", "codemod.py"}

# 压缩过的第三方脚本里中文全是乱码注释，扫进来只有噪声
def _skip_name(fn: str) -> bool:
    low = fn.lower()
    return (fn in SKIP_FILES or low.endswith(".min.js")
            or low.endswith(".min.css") or low.startswith("jquery"))

# 模板里这些纯符号/单位不该出现在漏翻清单里，噪声太大
NOISE = {"×", "·", "→", "—", "…", "￥", "¥", "：", "，", "。", "、", "；"}


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _read(path: str) -> str:
    try:
        return io.open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return ""


def _line_of(src: str, pos: int) -> int:
    return src.count("\n", 0, pos) + 1


def _iter_files(root: str, exts=(".html", ".js")):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if _skip_name(fn) or not fn.endswith(exts):
                continue
            yield os.path.join(dirpath, fn), os.path.relpath(
                os.path.join(dirpath, fn), root).replace("\\", "/")


def _blank(src: str, spans) -> str:
    """把指定区间替换成等长空白。

    等长是关键：换行符保留，后面算出来的行号才不会整体错位。
    """
    if not spans:
        return src
    buf = list(src)
    for a, b in spans:
        for i in range(a, min(b, len(buf))):
            if buf[i] != "\n":
                buf[i] = " "
    return "".join(buf)


def _spans(rx, src: str):
    return [(m.start(), m.end()) for m in rx.finditer(src)]


def suggest_wrap(text: str) -> str:
    """给出「这条该怎么包」的建议写法，直接复制去改模板。"""
    t = (text or "").strip()
    if "'" in t and '"' not in t:
        return '{{ tr("%s") }}' % t
    return "{{ tr('%s') }}" % t.replace("'", "\\'")


# --------------------------------------------------------------------------
# A 类：模板里裸写的中文
# --------------------------------------------------------------------------
def scan_unwrapped(root: str) -> list:
    out = []
    for path, rel in _iter_files(root, (".html",)):
        src = _read(path)
        if not src or not CJK.search(src):
            continue
        # 先抹掉注释与 <script>/<style> 整块：脚本里的中文是另一类问题
        # （多数是 JS 里拼的提示语），混进模板文本里会给出错误的修改建议
        spans = _spans(RE_HTML_COMMENT, src) + _spans(RE_EXPR, src)
        spans += _spans(re.compile(r"<script[^>]*>.*?</script>", re.S), src)
        spans += _spans(re.compile(r"<style[^>]*>.*?</style>", re.S), src)
        clean = _blank(src, spans)

        for m in RE_NODE.finditer(clean):
            raw = m.group(1).strip()
            if not raw or not CJK.search(raw):
                continue
            if raw in NOISE:
                continue
            # <pre> 里的 JSON / 接口示例：含键值对结构，不是界面文案。
            # 这类在设置页的「对外接口」说明里成片出现，报出来是纯噪声。
            if raw.lstrip()[:1] in ("{", "[") or '":"' in raw:
                continue
            # 纯符号 + 中文（如「（共 3 条）」已被表达式抹成空白后剩下的括号）跳过
            if not re.search(r"[\u4e00-\u9fff]{1,}", raw):
                continue
            # 只剩标点/单位，没有实际语义
            if len(raw) <= 2 and not re.search(r"[\u4e00-\u9fff]{2,}", raw):
                continue
            pos = m.start(1) + (len(m.group(1)) - len(m.group(1).lstrip()))
            out.append({
                "file": rel,
                "line": _line_of(clean, pos),
                "text": raw,
                "kind": "template",
                "suggest": suggest_wrap(raw),
            })
    return _dedup(out)


# --------------------------------------------------------------------------
# A 类附加：<script> 里裸写的中文
# --------------------------------------------------------------------------
RE_JS_STR = re.compile(r"(['\"`])((?:\\.|(?!\1).)*)\1", re.S)


def scan_unwrapped_js(root: str) -> list:
    out = []
    for path, rel in _iter_files(root, (".html", ".js")):
        src = _read(path)
        if not src or not CJK.search(src):
            continue
        # 只看 <script> 块（.js 文件整体算一块）
        blocks = ([(m.start(1), m.end(1)) for m in
                   re.finditer(r"<script[^>]*>(.*?)</script>", src, re.S)]
                  if rel.endswith(".html") else [(0, len(src))])
        for a, b in blocks:
            body = src[a:b]
            # 全部用「等长空白」替换，不能用删除：一旦变短，后面算行号就会整体错位，
            # 报出来的行号和内容对不上（用户会以为扫描器坏了）。
            # 1) Jinja 表达式：{{ tr('离线') | tojson }} 里的 '离线' 不是 JS 字符串，
            #    不先抹掉会被当成裸中文报出来（这是误报的主要来源）。
            body = _blank(body, _spans(RE_EXPR, body))
            # 2) 注释：注释里的中文不是界面文案
            body = _blank(body, _spans(re.compile(r"//[^\n]*"), body))
            body = _blank(body, _spans(re.compile(r"/\*.*?\*/", re.S), body))
            # 3) 已经被取词函数包住的先抹掉，避免重复报。
            # 必须按「到行尾」抹，不能只抹到第一个右括号：anTitle() 的实参
            # 常写成三元表达式，而文案里本身带半角括号（'安全库存数(下限合计)'），
            # 用 [^()]* 匹配会在文案内部的括号处提前收尾，把已翻译的串报成裸中文。
            body = _blank(body, _spans(re.compile(
                r"(?<![\w.])(?:(?:window|self|globalThis)\.)?"
                r"(?:TRP|TRN|TR|anTitle)\s*\([^\n]*"), body))
            for m in RE_JS_STR.finditer(body):
                text = m.group(2)
                if len(text) < 2 or not CJK.search(text):
                    continue
                if text.strip() in NOISE:
                    continue
                if "{{" in text or "}}" in text:
                    continue
                out.append({
                    "file": rel,
                    "line": _line_of(src, a + m.start(2)),
                    "text": text.strip(),
                    "kind": "js",
                    "suggest": "TR('%s')" % text.strip().replace("'", "\\'"),
                })
    return _dedup(out)


# --------------------------------------------------------------------------
# B 类：取了词但词条表里没有
# --------------------------------------------------------------------------
def scan_unregistered(root: str, legacy_map: dict) -> list:
    """tr('中文') 出现了，legacy_map 里查不到 → 这条永远显示中文。

    通常意味着「改了界面但没重建词条表」。这类是最冤的一种：代码明明
    写了取词，看起来完全正确，就是没生效。

    比对口径必须和登记（manage.register_terms）**完全一致**，否则会出现
    一种特别误导的现象：提示「这条没登记」，点登记后回答「已经在词条表里」，
    重扫一遍它还在。历史上就是这么错的 ——
      · 词条表的 key 是**运行时**形态（`\\n` 已还原成真换行）
      · 扫描抓的是**源码**形态（字面 `\\n`）
      · 另有首尾空白差异：模板里 `TR(' ？…')` 前导空格是给名字拼接用的，
        词条表保留了这个空格，而登记时被 strip 掉了
    三种写法指向同一句文案，这里全部归一到（还原转义 + 去首尾空白）再比。
    """
    from .manage import _unescape_js_text

    def norm(text: str) -> str:
        return _unescape_js_text(text or "").strip()

    index = {}
    for k in (legacy_map or {}):
        index.setdefault(norm(k), k)

    hits = {}
    for path, rel in _iter_files(root, (".html", ".js")):
        src = _read(path)
        if not src:
            continue
        for m in RE_CALL.finditer(src):
            key = m.group(2)
            if not key or not _looks_chinese_authored(key):
                continue
            if "{{" in key or "}}" in key:
                continue
            # 展示与登记都用运行时形态：带真换行，登记接口拿到后不需要再猜
            real = _unescape_js_text(key)
            info = hits.setdefault(real, {"text": real, "file": rel, "line": 0, "count": 0})
            info["count"] += 1
            if not info["line"]:
                info["line"] = _line_of(src, m.start())
    out = []
    for real, info in hits.items():
        if norm(real) in index:
            continue
        out.append(info)
    out.sort(key=lambda x: (x["file"], x["line"]))
    return out


# --------------------------------------------------------------------------
# A 类附加：Python 里裸写的中文（提示信息 / 导出表头）
# --------------------------------------------------------------------------
RE_PY_STR = re.compile(r"(['\"])((?:\\.|(?!\1).){2,200})\1", re.S)


def scan_unwrapped_py(root: str, limit: int = 120) -> list:
    """app.py 等 Python 文件里直接写的中文。

    这一类数量极大（导出、flash 提示、校验报错），而且**多数是正当的中文**
    —— 它们会经过 _xl() 或 _i18n.t() 在运行期取词。所以这里只做统计和抽样，
    不作为「必须改」的清单，避免把真正要处理的问题淹掉。
    """
    out = []
    for fn in ("app.py", "config.py", "db.py"):
        path = os.path.join(root, fn)
        if not os.path.exists(path):
            continue
        src = _read(path)
        if not src:
            continue
        # 抹掉注释与三引号 docstring
        clean = re.sub(r'"""(.*?)"""', lambda m: " " * len(m.group(0)), src, flags=re.S)
        clean = re.sub(r"'''(.*?)'''", lambda m: " " * len(m.group(0)), clean, flags=re.S)
        clean = re.sub(r"#[^\n]*", lambda m: " " * len(m.group(0)), clean)
        for m in RE_PY_STR.finditer(clean):
            text = m.group(2)
            if not CJK.search(text) or len(text.strip()) < 2:
                continue
            if text in NOISE:
                continue
            out.append({"file": fn, "line": _line_of(clean, m.start()),
                        "text": text.strip(), "kind": "python"})
            if len(out) >= limit:
                return _dedup(out)
    return _dedup(out)


def _dedup(items):
    seen, out = set(), []
    for it in items:
        k = (it.get("file"), it.get("text"))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


# --------------------------------------------------------------------------
# D 类：词条在、译文也有，但**这一页的分片没下发**
# --------------------------------------------------------------------------
def _shard_names(page: str) -> list:
    """与 __init__.page_keys 的取名口径保持一致（endpoint ↔ 模板名两种写法）。"""
    names = [page]
    stem = page[:-5] if page.endswith("_page") else page
    names.append(stem + ".html")
    return names


def scan_shard_gaps(root: str, locale_dir: str, lang: str, legacy_map: dict) -> list:
    """找出「写了 TR()、词条表里有、这一页却拿不到」的取词点。

    为什么必须单独成一类
    --------------------
    这一类既不算 A（确实走了取词），也不算 B（legacy_map 里有 ID），
    也不算 C（这门语言有译文）。三类计数全是 0，界面上却明明白白露着中文 ——
    用户的困惑「为什么还有中文，并且扫不到」就是这么来的。

    典型成因：新加了 TR('今日')，但 pages.json 里这一页的分片从来没登记过它。
    前端 TR() 只拿得到自己那页的词表（整本词典 400KB，不可能每页全发），
    分片里没有就静默回退中文。
    """
    from .__init__ import page_keys  # 延迟导入，避免循环依赖

    idx = {}
    for k in (legacy_map or {}):
        idx[(k or "").strip()] = legacy_map[k]

    tpl_dir = os.path.join(root, "templates")
    try:
        with io.open(os.path.join(locale_dir, "pages.json"), encoding="utf-8") as f:
            pages = json.load(f)
    except (OSError, ValueError):
        return []

    # base.html 的取词每页都下发（_base 桶），单独算一份，别重复报
    base_ids = set(pages.get("_base") or [])

    # 同一页在 pages.json 里有两种桶：endpoint（dashboard）和模板名（dashboard.html）。
    # 运行期取的是两者并集，所以这里也必须并集 —— 只查一个桶会报出一堆假阳性
    # （「今日」明明在 dashboard 桶里，却因为查的是 dashboard.html 桶而报缺）。
    tpl_buckets = {}
    for key in pages:
        if key == "_base" or key.startswith("_"):
            continue
        for n in _shard_names(key):
            if n.endswith(".html"):
                tpl_buckets.setdefault(n, set()).add(key)

    out = []
    for tpl, keys in tpl_buckets.items():
        path = os.path.join(tpl_dir, tpl)
        if not os.path.isfile(path):
            continue
        shard_ids = set(base_ids)
        for k in keys:
            shard_ids |= set(page_keys(k))
        if not shard_ids:
            continue
        src = _read(path)
        if not src:
            continue
        # 判据必须和浏览器一致：TR() 是按**中文原文**查表的，不是按 ID。
        # 同一句中文可能挂着多个 ID，只要有一个 ID 在这页分片里，运行期就能取到。
        # 按 ID 判会把这句报成缺失 —— 假阳性比漏报更糟，管理员点了也没变化。
        shard_texts = {(t or "").strip() for t, i in (legacy_map or {}).items()
                       if i in shard_ids}
        # 服务端取词（{{ tr('…') }}）由 Jinja 渲染，不经过前端分片；
        # 注释里的 TR('…') 也不会执行。两者都先抹掉再扫。
        clean = RE_EXPR.sub(lambda m: " " * len(m.group(0)), src)
        clean = RE_HTML_COMMENT.sub(lambda m: " " * len(m.group(0)), clean)
        clean = re.sub(r"^\s*//.*$", lambda m: " " * len(m.group(0)), clean, flags=re.M)
        clean = re.sub(r"/\*[\s\S]*?\*/", lambda m: " " * len(m.group(0)), clean)
        for m in RE_CALL.finditer(clean):
            key = m.group(2)
            if not key or not _looks_chinese_authored(key):
                continue
            if "{{" in key or "}}" in key:
                continue
            real = key.replace("\\n", "\n")
            plain = real.strip()
            if plain in shard_texts:
                continue
            tid = idx.get(plain)
            if not tid:
                continue  # 没登记：那是 B 类，别在这里重复报
            out.append({"text": real, "id": tid, "page": _page_alias(sorted(keys)),
                        "file": tpl, "line": _line_of(src, m.start())})
    return _dedup(out)


def _page_alias(keys: list) -> str:
    """补齐分片时该写哪个桶：优先 endpoint（运行期读的是它），别写模板名。

    运行期 page_keys(endpoint) 会并上模板名桶，反过来不成立 ——
    写进 `settings.html` 桶后，`system_settings` 这页照样拿不到。
    """
    for k in keys:
        if not k.endswith(".html"):
            return k
    return keys[0] if keys else ""


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def scan(root: str, locale_dir: str, lang: str, legacy_map: dict,
         include_py: bool = False) -> dict:
    """一次扫描，返回三类的完整清单。

    untranslated 不在这里算（那是纯查表，manage.pending_items 更快），
    但要把条数带上，前端才能显示「还有 N 条没翻」。
    """
    from . import manage

    unwrapped = scan_unwrapped(root) + scan_unwrapped_js(root)
    unregistered = scan_unregistered(root, legacy_map or {})
    missing = manage.pending_items(locale_dir, lang) if lang != "zh" else []
    # 僵尸词条：中文真源是程序输出（历史上把日志行/已翻好的整句误登记进来了）。
    # 不参与待翻译计数 —— 它们永远翻不出来，只会让进度条卡住。
    from . import core as _core
    missing = [it for it in missing if not _core.is_program_output(it[1])]
    gaps = scan_shard_gaps(root, locale_dir, lang, legacy_map or {})

    out = {
        "lang": lang,
        "counts": {
            "unwrapped": len(unwrapped),
            "unregistered": len(unregistered),
            "untranslated": len(missing),
            "shard_gap": len(gaps),
        },
        "unwrapped": unwrapped,
        "unregistered": unregistered,
        "shard_gap": gaps,
        # 只带前 300 条给界面预览，完整清单走补翻译任务
        "untranslated": [{"id": i, "text": t} for i, t in missing[:300]],
    }
    if include_py:
        py = scan_unwrapped_py(root)
        out["unwrapped_py"] = py
        out["counts"]["unwrapped_py"] = len(py)
    return out
