# -*- coding: utf-8 -*-
# SPMS 备件辅料仓管系统 (Spare Parts Management System)
# 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
# 邮箱：510658927@qq.com
# QQ：510658927
# Gitee：https://gitee.com/rookie-uncle
# GitHub：https://github.com/Colton1982
# Copyright (c) 2026 rookie-uncle. All rights reserved.
# 开源协议：Apache-2.0
"""i18n2.manage —— 语言增删与覆盖率（运行期可用，不依赖 tools/）

为什么要单独一个模块
--------------------
以前「加一门语言」只有一条路：在命令行跑 tools/newlang.py。这个操作对现场
管理员不可达 —— 客户机器上未必有 Python 环境、未必知道 tools 目录在哪。
语言本质上是一项**配置**，配置就该在设置页里改。

四条设计约束
------------
L1 锁定：zh 是中文真源（所有译文的对照基准），en 是通用回退语言，
   两者不可删除、不可覆盖。删了 zh 整个系统没有原文，删了 en 其它语言的
   对照基线就没了。

L2 软删：删除 = 注册表摘掉 + catalog 移到 locale/_removed/<code>.<时间戳>.json。
   语言包背后是几百条人工/机器翻译，误删要能捞回来。

L3 立即生效：只写文件，靠 Store 的 mtime 失效感知，不重启、不 reload。
   这是这套 i18n 既有约定（catalog / registry / legacy_map 都带 mtime 失效）。

L4 空串 = 未翻译：新增语言的骨架全是空串，运行期逐条回退中文，
   不会白屏。所以「加一门语言」和「翻完这门语言」是两件事，可以先加后翻。
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import threading
import time

from . import core

# 基准语言：中文是原文真源，英文是所有其它语言的对照基线
LOCKED = ("zh", "en")

# 语言码：BCP-47 的常见子集（zh / en / pt-BR / zh-Hant）
RE_CODE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z]{2,4})?$")

# 用「万/亿」进位体系的语言。换语言不换进位，5万 会被写成 510K
_SCALE_WAN = ("zh", "ja", "ko")

# 写 catalog 的互斥锁：后台翻译任务和用户在页面上手动保存可能同时发生，
# 两边都是「读整个文件 → 改 → 写回」，不加锁后写的会吃掉先写的。
_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# 路径与读写
# --------------------------------------------------------------------------
def _p(locale_dir: str, *parts) -> str:
    return os.path.join(locale_dir, *parts)


def _atomic_write_json(path: str, data) -> None:
    """先写临时文件再原子替换。

    直接原地覆盖有个隐患：写到一半断电/被杀，留下一份半截 JSON，
    下一次启动整个语言的词条表就读不出来了（会静默回退成全是中文）。
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def read_registry(locale_dir: str) -> dict:
    """locale/languages.json 原样读（含 _default / _comment 等下划线键）。"""
    try:
        with io.open(_p(locale_dir, "languages.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def write_registry(locale_dir: str, data: dict) -> None:
    # sort_keys=True 会把 _default 排到最前（下划线 ASCII 小于字母），
    # 恰好是希望的位置：文件打开先看到默认语言
    _atomic_write_json(_p(locale_dir, "languages.json"), data)


def read_catalog(locale_dir: str, code: str) -> dict:
    try:
        with io.open(_p(locale_dir, "catalog", code + ".json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def write_catalog(locale_dir: str, code: str, data: dict) -> None:
    _atomic_write_json(_p(locale_dir, "catalog", code + ".json"), data)


# --------------------------------------------------------------------------
# 覆盖率
# --------------------------------------------------------------------------
def is_filled(value) -> bool:
    """这一条算不算「已翻译」。复数 dict 要至少有一个非空形式。"""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(isinstance(v, str) and v.strip() for v in value.values())
    return bool(value)


def coverage(locale_dir: str, code: str):
    """(已译, 总条数)。

    总条数以**中文真源**为准 —— 这才是「要翻多少」。拿本语言自己的条目数
    当分母，骨架语言会显示 100%（因为条目都在，只是值为空），毫无意义。
    """
    zh = read_catalog(locale_dir, "zh")
    keys = [k for k in zh if not k.startswith("_")]
    total = len(keys)
    if code == "zh":
        return total, total
    cur = read_catalog(locale_dir, code)
    filled = sum(1 for k in keys if is_filled(cur.get(k)))
    return filled, total


def pending_items(locale_dir: str, code: str):
    """该语言还差哪些：[(id, 中文原文)]，按 id 排序保证可分批续跑。

    中文源是复数词条时值是 {"other": "{n} 行"} 这类 dict —— 这种**必须**也进队列，
    否则新语言里那十几条永远不进翻译任务，覆盖率一辈子到不了 100%。
    取 other（没有就取任一非空形式）当待翻文本，写回时 save_translations 会
    只填 other 位、保留 one/few/many 的空位。
    """
    zh = read_catalog(locale_dir, "zh")
    cur = read_catalog(locale_dir, code)
    out = []
    for k in sorted(zh):
        if k.startswith("_"):
            continue
        if is_filled(cur.get(k)):
            continue
        src = zh.get(k)
        if isinstance(src, dict):
            src = src.get("other") or next(
                (v for v in src.values() if isinstance(v, str) and v.strip()), "")
        if isinstance(src, str) and src.strip():
            out.append((k, src))
    return out


# --------------------------------------------------------------------------
# 语言清单
# --------------------------------------------------------------------------
def list_languages(locale_dir: str) -> list:
    reg = read_registry(locale_dir)
    default = reg.get("_default") or "zh"
    out = []
    for code, prof in reg.items():
        if code.startswith("_") or not isinstance(prof, dict):
            continue
        filled, total = coverage(locale_dir, code)
        pct = round(filled * 100.0 / total, 1) if total else 0.0
        out.append({
            "code": code,
            "native": prof.get("native") or prof.get("name") or code,
            "en": prof.get("en") or code,
            "status": prof.get("status") or "ready",
            "dir": core.text_dir(code, prof.get("dir")),
            "locale": prof.get("locale") or code,
            "total": total,
            "filled": filled,
            "missing": total - filled,
            "pct": pct,
            "locked": code in LOCKED,
            "default": code == default,
            "exists": os.path.exists(_p(locale_dir, "catalog", code + ".json")),
        })
    # zh / en 排最前，其余保持注册表里的原始顺序（Python sort 稳定）
    rank = {"zh": 0, "en": 1}
    out.sort(key=lambda x: rank.get(x["code"], 2))
    return out


def build_profile(code: str, native: str, en_name: str = "", status: str = "draft") -> dict:
    """给新语言生成一份完整档案。

    补齐这些字段的原因：复数形式决定要填几个空位、date/num/money 决定数字
    和日期怎么显示、scale 决定「万」还是「K」。少一项，运行期就退回通用默认，
    德语就会显示成 1,234.56 而不是 1.234,56。
    """
    base = core.base_lang(code)
    forms = list(core.plural_forms(code))
    return {
        "native": native or code,
        "en": en_name or native or code,
        "plural": len(forms),
        "locale": code,
        "forms": forms,
        "date": core._DATE_ORDER_DEFAULT.get(base, "{Y}-{m}-{d}"),
        "num": list(core._NUM_STYLE_DEFAULT.get(base, (",", "."))),
        "money": core._MONEY_FMT_DEFAULT.get(base, "{sym}{num}"),
        "currency": "¥",
        "status": status,
        "dir": core.text_dir(code),
        "scale": "wan" if base in _SCALE_WAN else "short",
    }


def make_skeleton(locale_dir: str, code: str) -> dict:
    """以中文真源为骨架，生成全空的本语言词条表。

    复数条目要保留 {"one": "", "other": ""} 的结构：塞一个空字符串进去，
    译员（或机器翻译）就不知道这里有复数形式要分开填，运行时也只能按
    「other」取，俄语的 2~4 与 5 以上会共用同一个词形。
    判断依据是英文词条 —— 中文没有复数变化，值永远是字符串。
    """
    zh = read_catalog(locale_dir, "zh")
    en = read_catalog(locale_dir, "en")
    forms = list(core.plural_forms(code)) or ["other"]
    out = {}
    for k, v in zh.items():
        if k.startswith("_"):
            continue
        if isinstance(en.get(k), dict):
            d = {f: "" for f in forms}
            d.setdefault("other", "")
            out[k] = d
        else:
            out[k] = ""
    return out


# --------------------------------------------------------------------------
# 增
# --------------------------------------------------------------------------
def add_language(locale_dir: str, code: str, native: str,
                 en_name: str = "", status: str = "draft") -> dict:
    """新增一门语言。幂等：已存在则只补缺失的档案字段与 catalog 文件。"""
    code = (code or "").strip()
    if not RE_CODE.match(code):
        raise ValueError("语言代码不合法（应形如 es / pt-BR / zh-Hant）")
    if not (native or "").strip():
        raise ValueError("请填写这门语言的自称（如 Español）")

    reg = read_registry(locale_dir)
    existed = code in reg
    if existed:
        prof = reg[code] if isinstance(reg[code], dict) else {}
        # 已存在就补齐缺的字段，不覆盖已有译文设置
        merged = build_profile(code, prof.get("native") or native,
                               prof.get("en") or en_name,
                               prof.get("status") or status)
        merged.update({k: v for k, v in prof.items() if v not in (None, "", [], {})})
        reg[code] = merged
    else:
        reg[code] = build_profile(code, native, en_name, status)
    write_registry(locale_dir, reg)

    cat_path = _p(locale_dir, "catalog", code + ".json")
    created = False
    if not os.path.exists(cat_path):
        write_catalog(locale_dir, code, make_skeleton(locale_dir, code))
        created = True

    filled, total = coverage(locale_dir, code)
    return {"code": code, "existed": existed, "catalog_created": created,
            "filled": filled, "total": total}


# --------------------------------------------------------------------------
# 删
# --------------------------------------------------------------------------
def remove_language(locale_dir: str, code: str) -> dict:
    """删除一门语言（软删）。返回备份路径。"""
    code = (code or "").strip()
    if code in LOCKED:
        raise ValueError("中文与英文是基准语言，不能删除")
    reg = read_registry(locale_dir)
    if code not in reg:
        raise ValueError("语言不存在：" + code)
    if (reg.get("_default") or "zh") == code:
        raise ValueError("这门语言是系统默认语言，请先把默认语言改成别的再删")

    del reg[code]
    write_registry(locale_dir, reg)

    src = _p(locale_dir, "catalog", code + ".json")
    backup = ""
    if os.path.exists(src):
        dst_dir = _p(locale_dir, "_removed")
        os.makedirs(dst_dir, exist_ok=True)
        backup = os.path.join(dst_dir, "%s.%s.json" % (code, time.strftime("%Y%m%d-%H%M%S")))
        shutil.move(src, backup)
    return {"code": code, "backup": backup}


def restore_language(locale_dir: str, backup_path: str) -> dict:
    """从 _removed 里捞回一门被删的语言。"""
    name = os.path.basename(backup_path or "")
    code = name.split(".")[0]
    if not RE_CODE.match(code):
        raise ValueError("备份文件名里读不出语言代码")
    if not os.path.exists(backup_path):
        raise ValueError("备份文件不存在")
    shutil.copy2(backup_path, _p(locale_dir, "catalog", code + ".json"))
    reg = read_registry(locale_dir)
    if code not in reg:
        reg[code] = build_profile(code, code.upper(), code.upper(), "draft")
        write_registry(locale_dir, reg)
    return {"code": code}


# --------------------------------------------------------------------------
# 改 / 查
# --------------------------------------------------------------------------
def set_status(locale_dir: str, code: str, status: str) -> dict:
    """ready / draft / skeleton。

    skeleton 不出现在顶栏语言选择器里（整页回退中文，用户会以为是故障），
    但仍可用 ?lang=xx 预览 —— 译员用的就是这个入口。
    """
    if status not in ("ready", "draft", "skeleton"):
        raise ValueError("状态只能是 ready / draft / skeleton")
    reg = read_registry(locale_dir)
    if code not in reg:
        raise ValueError("语言不存在")
    reg[code]["status"] = status
    write_registry(locale_dir, reg)
    return {"code": code, "status": status}


def save_translations(locale_dir: str, code: str, items: dict, overwrite: bool = False) -> int:
    """把译文写回 catalog。默认只填空位，不覆盖已有译文。

    overwrite=False 是刻意的：机器翻译请求可能重放（网络超时后重试），
    第二次的结果通常更差，不该把第一次人工校对过的内容冲掉。
    """
    if not items:
        return 0
    with _LOCK:
        cur = read_catalog(locale_dir, code)
        n = 0
        for k, v in items.items():
            if not isinstance(v, str) or not v.strip():
                continue
            old = cur.get(k)
            if isinstance(old, dict):
                # 目标语言这条是复数结构（俄/捷/阿拉伯），只能填 other 位，
                # 整个替换成字符串会把 one/few/many 的空位一起抹掉
                if not overwrite and is_filled(old):
                    continue
                d = dict(old)
                d["other"] = v
                cur[k] = d
                n += 1
                continue
            if not overwrite and is_filled(old):
                continue
            cur[k] = v
            n += 1
        if n:
            write_catalog(locale_dir, code, cur)
        return n


# --------------------------------------------------------------------------
# legacy_map：中文原文 → 词条 ID
#
# 这是整套 i18n 的「零改代码」基石：模板里写 tr('库存分析')，运行期靠这张表
# 换成语义 ID 再去查译文。表里没有这条 → 查不到 → 原样显示中文。
# 所以「界面上新加了一句中文」这件事，真正的第一道门槛是**登记**，
# 而不是翻译。扫描器报出来的 B 类（unregistered）指的就是这里缺条。
# --------------------------------------------------------------------------
def read_legacy(locale_dir: str) -> dict:
    try:
        with io.open(_p(locale_dir, "legacy_map.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def write_legacy(locale_dir: str, data: dict) -> None:
    _atomic_write_json(_p(locale_dir, "legacy_map.json"), data)


def _slug(text: str, limit: int = 16) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:limit].rstrip("_") or ""


def _hash(text: str, n: int = 5) -> str:
    """FNV-1a。与 tools/build_catalog.py 的 short_hash 同算法 —— 不是必须，
    但两边算出同一个值，比对差异时不会互相干扰。"""
    h = 2166136261
    for ch in text.encode("utf-8"):
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return ("%08x" % h)[:n]


def new_id(text: str, module: str, used) -> str:
    """给一条新文案分配词条 ID。

    规则和 build_catalog 不同（那边能用英文译文做语义 slug，这里还没有译文），
    但**不影响一致性**：build_catalog 重建时会优先复用 legacy_map 里已有的 ID
    （`old = existing.get(key)`），所以这里分配的 ID 一旦落地就不会再变。
    """
    base = "%s.%s" % (_slug(module, 14) or "app", _hash(text, 5))
    cand, n = base, 2
    while cand in used:
        cand = "%s_%d" % (base, n)
        n += 1
    return cand


def _unescape_js_text(text: str) -> str:
    """把 JS 字符串字面量里的转义还原成真实字符。

    扫描器抓到的是**源码里的写法**：`TR('？\\n（仅库存为 0 的可删除）')`，
    而浏览器运行时那个 key 里的 `\\n` 已经是真换行。若照原样登记，
    词典的 key 带反斜杠、页面查的是换行 —— 永远命中不了，界面还是中文，
    而且翻译引擎会把 `\\n` 当正文翻成「?\\ n (...)`，英文界面就出现半个斜杠。

    只处理换行/制表这两类：界面文案里出现合法反斜杠的概率极低，
    误伤的代价（翻译对不上）比漏处理的代价（永久漏翻）大得多。
    """
    t = text or ""
    if "\\n" in t or "\\t" in t:
        t = t.replace("\\r", "").replace("\\n", "\n").replace("\\t", "\t")
    return t


def register_terms(locale_dir: str, items, module: str = "app") -> dict:
    """把扫描出来的新文案登记进词条表。

    items 可以是 [文本...] 或 [{text, file}...]（带来源文件时顺便补页面分片）。

    只写三样东西：legacy_map（原文→ID）、catalog/zh（ID→中文原文）、
    pages.json（该页的词条分片）。**不写译文** —— 登记和翻译是两件事，
    登记完这条就能被翻译引擎看到了。
    """
    norm = []
    for it in (items or []):
        if isinstance(it, str):
            norm.append((_unescape_js_text(it).strip(), ""))
        elif isinstance(it, dict):
            t = _unescape_js_text(it.get("text") or "").strip()
            if t:
                norm.append((t, it.get("file") or ""))
    if not norm:
        return {"added": 0, "items": {}}

    with _LOCK:
        legacy = read_legacy(locale_dir)
        zh = read_catalog(locale_dir, "zh")
        used = set(legacy.values()) | set(zh)
        # 查重不能只看字面相等：模板里 `TR(' ？…')` 的前导空格是给名字拼接用的，
        # 词条表保留了这个空格，而这里做 strip 是为了清理误输入 —— 两边一对不上，
        # 每点一次「登记为词条」就多一条一模一样的重复词条（历史上已经发生过 4 条）。
        # 所以按「去首尾空白」建索引，命中就复用原 ID，只补分片不新建。
        plain = {}
        for k, v in legacy.items():
            plain.setdefault(k.strip(), v)
        added = {}
        reused = {}
        for text, _f in norm:
            if text in legacy or text in zh:
                continue
            hit = plain.get(text)
            if hit and hit in zh:
                reused[text] = hit
                continue
            tid = new_id(text, module, used)
            used.add(tid)
            legacy[text] = tid
            zh[tid] = text
            plain[text] = tid
            added[text] = tid
        if added:
            write_legacy(locale_dir, legacy)
            write_catalog(locale_dir, "zh", zh)

        # 页面分片：前端 TR() 只能看到自己页那份词表，不进分片等于没登记。
        # 已登记的词条**也必须补**：扫描器报的往往是「词条在、翻译也有，
        # 就是这一页没下发」，只补新词条等于点了没反应。以前这里有个
        # `if not added and not reused: return`，把这条路径直接掐断了 ——
        # 表现就是「点登记没效果、界面照样中文」（dashboard 的「今日」即此）。
        pg_path = _p(locale_dir, "pages.json")
        try:
            with io.open(pg_path, encoding="utf-8") as f:
                pages = json.load(f)
        except (OSError, ValueError):
            pages = {}
        if isinstance(pages, dict):
            changed = False
            for text, file in norm:
                tid = (added.get(text) or reused.get(text)
                       or legacy.get(text) or plain.get(text))
                if not tid or not file:
                    continue
                page = os.path.basename(file)
                lst = pages.setdefault(page, [])
                if tid not in lst:
                    lst.append(tid)
                    changed = True
            if changed:
                _atomic_write_json(pg_path, pages)

    return {"added": len(added), "reused": len(reused), "items": added}

