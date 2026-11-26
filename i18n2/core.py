# -*- coding: utf-8 -*-
# SPMS 备件辅料仓管系统 (Spare Parts Management System)
# 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
# 邮箱：510658927@qq.com
# QQ：510658927
# Gitee：https://gitee.com/rookie-uncle
# GitHub：https://github.com/Colton1982
# Copyright (c) 2026 rookie-uncle. All rights reserved.
# 开源协议：Apache-2.0
"""
i18n2.core —— 本地化引擎内核（零第三方依赖）

设计契约
--------
C1  单一真源：locale/catalog/<lang>.json，key 是稳定语义 ID，值是整句带 {占位符}
C2  三端同构：Python / Jinja / 浏览器共用同一份 catalog 与同一套占位符语法
C3  接口只回码：服务端只产出 (code, params)，渲染发生在「消费端」
C4  数据不翻译：库内存稳定码（pending / pcs / NULL），显示层用 label() 映射

查找顺序（任一环命中即止）
--------------------------
  id 直查 → legacy_map(中文原文→id) → 中文原文兜底 → 返回 key 并记一次 missing

复数
----
  值可以是 {"one": "...", "few": "...", "many": "...", "other": "..."}，
  按 CLDR 规则选形式；单形式值就是普通字符串。
"""
from __future__ import annotations

import json
import os
import re
import threading

# --------------------------------------------------------------------------
# 语言档案（注册表的兜底来源）
#
# 运行期真正的取值顺序是：locale/languages.json 里的显式字段 → 下面这张表 → 通用默认。
# 也就是说「新增语言」优先改注册表，本表只负责给 newlang.py 生成合理初值，
# 以及兼容没写这些字段的老注册表。加一门表里没有的语言不会崩，只会走默认。
# --------------------------------------------------------------------------
_PLURAL_FORMS = {
    # 无复数变化
    "zh": ("other",), "ja": ("other",), "ko": ("other",), "th": ("other",),
    "vi": ("other",), "id": ("other",), "ms": ("other",), "my": ("other",),
    "lo": ("other",), "tr": ("other",), "fa": ("other",), "ka": ("other",),
    # 单/复两分
    "en": ("one", "other"), "de": ("one", "other"), "nl": ("one", "other"),
    "sv": ("one", "other"), "da": ("one", "other"), "nb": ("one", "other"),
    "es": ("one", "other"), "it": ("one", "other"), "pt": ("one", "other"),
    "el": ("one", "other"), "he": ("one", "other"), "hi": ("one", "other"),
    "hu": ("one", "other"), "fi": ("one", "other"), "et": ("one", "other"),
    # 三形式
    "fr": ("one", "other"),   # CLDR 的 many 只用于 10^6 级，计数场景用不到
    "cs": ("one", "few", "many", "other"), "sk": ("one", "few", "many", "other"),
    "ru": ("one", "few", "many", "other"), "uk": ("one", "few", "many", "other"),
    "pl": ("one", "few", "many", "other"), "lt": ("one", "few", "many", "other"),
    "ro": ("one", "few", "other"),
    # 六形式
    "ar": ("zero", "one", "two", "few", "many", "other"),
}

# 日期顺序：占位符 {Y} 年 {m} 月 {d} 日 {M} 月份名
_DATE_ORDER_DEFAULT = {
    "zh": "{Y}-{m}-{d}", "ja": "{Y}/{m}/{d}", "ko": "{Y}.{m}.{d}",
    "en": "{d} {M} {Y}", "de": "{d}.{m}.{Y}", "fr": "{d}/{m}/{Y}",
    "ru": "{d}.{m}.{Y}", "th": "{d}/{m}/{Y}", "vi": "{d}/{m}/{Y}",
    "id": "{d}/{m}/{Y}", "es": "{d}/{m}/{Y}", "it": "{d}/{m}/{Y}",
    "pt": "{d}/{m}/{Y}", "nl": "{d}-{m}-{Y}", "pl": "{d}.{m}.{Y}",
    "tr": "{d}.{m}.{Y}", "uk": "{d}.{m}.{Y}", "cs": "{d}. {m}. {Y}",
}

# 数字分隔符：thousand / decimal
_NUM_STYLE_DEFAULT = {
    "zh": (",", "."), "en": (",", "."), "ja": (",", "."), "ko": (",", "."),
    "th": (",", "."), "vi": (".", ","), "id": (".", ","),
    "ru": ("\u00a0", ","), "fr": ("\u202f", ","), "de": (".", ","),
    "es": (".", ","), "it": (".", ","), "pt": (".", ","),
    "nl": (".", ","), "pl": ("\u00a0", ","), "tr": (".", ","),
    "uk": ("\u00a0", ","), "cs": ("\u00a0", ","),
}

# 金额写法：{sym} 货币符号 {num} 数字
_MONEY_FMT_DEFAULT = {
    "zh": "{sym}{num}", "ja": "{sym}{num}", "ko": "{sym}{num}",
    "en": "{sym}{num}", "de": "{num} {sym}", "fr": "{num} {sym}",
    "ru": "{num} {sym}", "es": "{num} {sym}", "it": "{num} {sym}",
    "pt": "{num} {sym}", "nl": "{sym} {num}", "pl": "{num} {sym}",
    "tr": "{sym}{num}", "uk": "{num} {sym}", "cs": "{num} {sym}",
}

# --------------------------------------------------------------------------
# 「这句话是不是界面文案」判据
#
# 漏词上报（浏览器取词落空）和静态扫描共用一条口径，否则会出现两类脏数据：
#   · 已经翻好的整句被模板二次取词 → 上报成漏词，登记后变成僵尸词条
#     （例：flash 里带路径的 "Initialization complete, database location: C:\…"）
#   · 日志、异常栈、SQL 片段被当成待译文案
#
# 判据：含汉字 → 是文案；不含汉字但含全角标点且至少有一个字母数字
# → 是「占位符 + 中文标点」的格式串（如 {name}（{role} / {dept}））。
# 纯标点（…）和纯英文长句都不算 —— 前者没内容，后者是程序输出。
# --------------------------------------------------------------------------
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_FW_RE = re.compile(r"[（）「」『』，。：；！？、—…“”‘’]")
_WORD_RE = re.compile(r"[0-9A-Za-z]")
# 路径/文件名特征：带这些的句子是「程序输出」（带参数的 flash、日志），不是待译文案
_PATH_RE = re.compile(r"[A-Za-z]:[\\/]|\\\\|\.(?:db|exe|bat|py|json|log|sqlite3?)\b")


def looks_chinese_authored(text) -> bool:
    s = str(text or "").strip()
    if not s or len(s) > 200:
        return False
    # 有汉字就是文案 —— 哪怕句子里带 URL 或安装路径（「部署设置已保存：http://…」）
    if _CJK_RE.search(s):
        return True
    if _PATH_RE.search(s):
        return False
    return bool(_FW_RE.search(s) and _WORD_RE.search(s))


def is_program_output(text) -> bool:
    """是不是程序输出（日志行、异常、带路径的整句），不该进待翻译队列。

    只对「不像文案」的串再判一次：短英文词（Name / Unit）是正常词条，
    不算程序输出；带路径或长句（>40 字符）才算。
    """
    s = str(text or "").strip()
    if not s or looks_chinese_authored(s):
        return False
    return bool(_PATH_RE.search(s)) or len(s) > 40


# --------------------------------------------------------------------------
# 币种元数据
#
# 币种是「这套系统在哪个国家记账」，跟界面语言无关：中国工厂切到英文界面，
# 单价依然是人民币。所以这里只是一张字典，当前用哪个币种由
# sys_settings.currency 决定（见 __init__.set_currency）。
#
#   symbol   符号       zh   中文单位名（文案里「单价(元)」的那个「元」）
#   en       英文名（非中文界面用，ISO 4217 标准写法）
#   decimals 默认小数位（日元/韩元/越南盾没有分）
# --------------------------------------------------------------------------
CURRENCIES = {
    "CNY": {"symbol": "¥", "zh": "元", "en": "Chinese Yuan", "decimals": 2},
    "USD": {"symbol": "$", "zh": "美元", "en": "US Dollar", "decimals": 2},
    "EUR": {"symbol": "€", "zh": "欧元", "en": "Euro", "decimals": 2},
    "JPY": {"symbol": "¥", "zh": "日元", "en": "Japanese Yen", "decimals": 0},
    "KRW": {"symbol": "₩", "zh": "韩元", "en": "Korean Won", "decimals": 0},
    "HKD": {"symbol": "HK$", "zh": "港元", "en": "Hong Kong Dollar", "decimals": 2},
    "TWD": {"symbol": "NT$", "zh": "新台币", "en": "New Taiwan Dollar", "decimals": 2},
    "GBP": {"symbol": "£", "zh": "英镑", "en": "Pound Sterling", "decimals": 2},
    "SGD": {"symbol": "S$", "zh": "新加坡元", "en": "Singapore Dollar", "decimals": 2},
    "AUD": {"symbol": "A$", "zh": "澳元", "en": "Australian Dollar", "decimals": 2},
    "CAD": {"symbol": "C$", "zh": "加元", "en": "Canadian Dollar", "decimals": 2},
    "THB": {"symbol": "฿", "zh": "泰铢", "en": "Thai Baht", "decimals": 2},
    "VND": {"symbol": "₫", "zh": "越南盾", "en": "Vietnamese Dong", "decimals": 0},
    "IDR": {"symbol": "Rp", "zh": "印尼盾", "en": "Indonesian Rupiah", "decimals": 0},
    "MYR": {"symbol": "RM", "zh": "林吉特", "en": "Malaysian Ringgit", "decimals": 2},
    "PHP": {"symbol": "₱", "zh": "比索", "en": "Philippine Peso", "decimals": 2},
    "INR": {"symbol": "₹", "zh": "印度卢比", "en": "Indian Rupee", "decimals": 2},
    "RUB": {"symbol": "₽", "zh": "卢布", "en": "Russian Ruble", "decimals": 2},
    "BRL": {"symbol": "R$", "zh": "雷亚尔", "en": "Brazilian Real", "decimals": 2},
    "MXN": {"symbol": "MX$", "zh": "比索", "en": "Mexican Peso", "decimals": 2},
    "CHF": {"symbol": "CHF", "zh": "法郎", "en": "Swiss Franc", "decimals": 2},
    "AED": {"symbol": "AED", "zh": "迪拉姆", "en": "UAE Dirham", "decimals": 2},
    "SAR": {"symbol": "SAR", "zh": "里亚尔", "en": "Saudi Riyal", "decimals": 2},
}

# 同一符号对应多个币种 —— 只看符号会闹出「人民币当日元」的量级错误。
# 实际只有 ¥ 是真问题（CNY 与 JPY 差 20 倍）；$ / ₩ 等符号的多义性不影响判断。
_AMBIGUOUS_SYM = {"¥": ("CNY", "JPY")}

# --------------------------------------------------------------------------
# 自定义币种：内置表之外的「一条」补充
#
# 内置 23 种覆盖不了所有市场（新西兰、波兰、巴基斯坦、海湾各国…）。穷举全球
# 180 多种没有意义 —— 符号、小数位、中文名都要人维护，而一次部署只用得上一种。
# 所以留一个口子：管理员在「金额单位」页填代码/符号/名称/小数位，存进
# sys_settings.currency.custom，这里查不到内置表时用它兜底。
# 只存一条：币种是「这套系统在哪个国家记账」，同时存在两种没有语义。
# --------------------------------------------------------------------------
_CUSTOM = {}


def normalize_custom(meta) -> dict:
    """校验并规整自定义币种。不合规直接抛 ValueError（带中文原因）。"""
    meta = meta or {}
    code = str(meta.get("code") or "").strip().upper()
    if not re.match(r"^[A-Z]{3}$", code):
        raise ValueError("币种代码要是 3 个字母，例如 NZD / PLN / PKR")
    if code in CURRENCIES:
        raise ValueError("%s 是内置币种，直接在下拉里选就行" % code)
    sym = str(meta.get("symbol") or "").strip()[:6] or code
    zh = str(meta.get("zh") or "").strip()[:20]
    en = str(meta.get("en") or "").strip()[:40]
    try:
        dec = int(meta.get("decimals", 2))
    except (TypeError, ValueError):
        dec = 2
    dec = max(0, min(4, dec))
    return {"code": code, "symbol": sym, "zh": zh or code,
            "en": en or zh or code, "decimals": dec}


def set_custom_currency(meta):
    """装一条自定义币种；传 None / 空则清掉。返回规整后的记录或空 dict。"""
    global _CUSTOM
    if not meta:
        _CUSTOM = {}
        return {}
    _CUSTOM = normalize_custom(meta)
    return dict(_CUSTOM)


def custom_currency() -> dict:
    return dict(_CUSTOM)


def currency(code: str | None) -> dict:
    """币种元数据。内置表优先，其次当前这一条自定义。"""
    c = str(code or "").strip().upper()
    if c in CURRENCIES:
        return CURRENCIES[c]
    if _CUSTOM and _CUSTOM.get("code") == c:
        return _CUSTOM
    return {}

# 语言的本国币种：只有语言和币种对得上时，才用有歧义的符号
_LANG_CURRENCY = {
    "zh": "CNY", "ja": "JPY", "ko": "KRW", "en": "USD", "de": "EUR",
    "fr": "EUR", "es": "EUR", "it": "EUR", "pt": "EUR", "ru": "RUB",
    "th": "THB", "vi": "VND", "id": "IDR", "pl": "PLN", "tr": "TRY",
    "uk": "UAH", "cs": "CZK", "nl": "EUR",
}


def base_lang(lang: str) -> str:
    """zh-Hant / pt-BR → zh / pt。"""
    return (lang or "zh").replace("_", "-").split("-")[0].lower()


# 从右往左书写的语言。加这门语言时 <html dir> 要跟着变，否则整页排版是反的。
_RTL_LANGS = {"ar", "he", "fa", "ur", "yi", "ps", "sd", "ug"}


def text_dir(lang: str, declared=None) -> str:
    """ltr / rtl。注册表写了 dir 就用它，没写按语言码推断。"""
    if declared in ("ltr", "rtl"):
        return declared
    return "rtl" if base_lang(lang) in _RTL_LANGS else "ltr"


def plural_forms(lang: str, declared=None) -> tuple:
    """该语言需要几种复数形式。

    declared 来自 languages.json 的 forms 字段（译员可显式覆盖）。
    返回值决定 newlang 生成几个空位、fill_missing 校验几个形式。
    """
    if declared:
        if isinstance(declared, str):
            declared = [x.strip() for x in declared.split(",") if x.strip()]
        if isinstance(declared, (list, tuple)) and declared:
            return tuple(declared)
    return _PLURAL_FORMS.get(base_lang(lang), ("one", "other"))


def plural_index(lang: str, n) -> str:
    """返回 CLDR 复数类别名：zero / one / two / few / many / other。"""
    lang = base_lang(lang)
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return "other"

    forms = _PLURAL_FORMS.get(lang, ("one", "other"))
    if forms == ("other",):
        return "other"
    if lang in ("en", "de", "nl", "sv", "da", "nb", "es", "it", "pt",
                "el", "he", "hu", "fi", "et"):
        return "one" if n == 1 else "other"
    if lang == "hi":
        return "one" if n in (0, 1) else "other"
    if lang in ("fr", "ro"):
        return "one" if n in (0, 1) else "other"
    if lang in ("cs", "sk"):
        if n == 1:
            return "one"
        if 2 <= n <= 4:
            return "few"
        return "many"
    if lang in ("ru", "uk", "pl", "lt"):
        if n % 10 == 1 and n % 100 != 11:
            return "one"
        if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
            return "few"
        return "many"
    if lang == "ar":
        if n == 0:
            return "zero"
        if n == 1:
            return "one"
        if n == 2:
            return "two"
        if 3 <= n % 100 <= 10:
            return "few"
        if 11 <= n % 100 <= 99:
            return "many"
        return "other"
    return "other"


# --------------------------------------------------------------------------
# 占位符
# --------------------------------------------------------------------------
_RE_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def placeholders(text: str) -> set:
    """提取 {name} 形式的占位符名集合。"""
    return set(_RE_PLACEHOLDER.findall(text or ""))


def placeholder_counts(text: str) -> dict:
    """{占位符: 出现次数}。

    只看集合会放过「{n} 条 / {n} 行」被翻成「{n} 条 / 行」这种：名字还在，
    数量少了，界面上就少显示一个数字。校验必须按次数比。
    """
    out = {}
    for name in _RE_PLACEHOLDER.findall(text or ""):
        out[name] = out.get(name, 0) + 1
    return out


def is_empty(value) -> bool:
    """词条是否等于「没翻译」。

    这是新增语言的关键判定：骨架语言（newlang 生成）每条都是空串，
    如果空串被当成命中，界面会**变空白**而不是回退中文。
    复数 dict 只有全空才算空。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, dict):
        return not any(isinstance(v, str) and v.strip() for v in value.values())
    return not value


def safe_format(text: str, params: dict) -> str:
    """只替换已知占位符，缺参数时保留原样，绝不让 .format 抛异常打断渲染。

    也处理 Jinja 风格 {{ name }}（历史词条里混用过）。
    """
    if not text:
        return text
    if "{" not in text:
        return text
    out = text
    for k, v in (params or {}).items():
        out = out.replace("{{ %s }}" % k, str(v)).replace("{{%s}}" % k, str(v))
        out = out.replace("{" + k + "}", str(v))
    return out


# --------------------------------------------------------------------------
# 数字 / 日期 / 货币本地化（避免为了千分位引入 babel）
#
# 三个函数都接受可选的 profile 覆盖（来自 languages.json），
# 这样新增语言只要在注册表里写 date/num/money，就不用改 Python。
# --------------------------------------------------------------------------
def _num_style(lang: str, style=None):
    if isinstance(style, (list, tuple)) and len(style) == 2:
        return tuple(style)
    return _NUM_STYLE_DEFAULT.get(base_lang(lang), (",", "."))


def fmt_num(value, lang: str = "zh", decimals: int = 0, style=None) -> str:
    """数字本地化：1234567.5 + en → 1,234,567.5；de → 1.234.567,5。"""
    if value is None or value == "":
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    thousands, decimal = _num_style(lang, style)
    s = ("{:,." + str(decimals) + "f}").format(num) if decimals else "{:,.0f}".format(num)
    return s.replace(",", "\x00").replace(".", decimal).replace("\x00", thousands)


_DATE_ORDER = _DATE_ORDER_DEFAULT
_MONTHS = {
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "de": ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."],
    "fr": ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."],
    "es": ["ene.", "feb.", "mar.", "abr.", "may.", "jun.", "jul.", "ago.", "sept.", "oct.", "nov.", "dic."],
    "it": ["gen.", "feb.", "mar.", "apr.", "mag.", "giu.", "lug.", "ago.", "set.", "ott.", "nov.", "dic."],
    "pt": ["jan.", "fev.", "mar.", "abr.", "mai.", "jun.", "jul.", "ago.", "set.", "out.", "nov.", "dez."],
    "ru": ["янв.", "февр.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сент.", "окт.", "нояб.", "дек."],
}
_CURRENCY_SYMBOL = {"zh": "¥", "ja": "¥", "ko": "₩", "en": "¥", "de": "¥", "fr": "¥", "ru": "¥"}


def money_mark(code: str, lang: str, form=None, override=None) -> str:
    """这个币种在这门语言下该写成什么。

    form: auto（默认）| symbol | code | both
      auto —— 符号没歧义就用符号；有歧义（¥ 可能是人民币也可能是日元）
              只有「语言本国币种 == 当前币种」时才用符号，否则用 ISO 代码。
              中文看 ¥ 不会读成日元，英文看 ¥ 就不一定了，这是踩过的坑。
    """
    code = (code or "CNY").upper()
    meta = currency(code)
    if isinstance(override, str) and override:
        return override
    form = form if form in ("symbol", "code", "both") else "auto"
    sym = meta.get("symbol") or code
    if form == "code":
        return code
    if form == "both":
        return code if sym == code else "%s %s" % (sym, code)
    if form == "symbol":
        return sym
    owners = _AMBIGUOUS_SYM.get(sym)
    if owners and len(owners) > 1:
        return sym if _LANG_CURRENCY.get(base_lang(lang)) == code else code
    return sym


def money_decimals(code: str, default: int = 2) -> int:
    """币种默认小数位（日元 0 位，人民币 2 位）。"""
    return currency(code or "CNY").get("decimals", default)


def fmt_money(value, lang: str = "zh", decimals=None,
              symbol=None, pattern=None, style=None,
              code=None, form=None) -> str:
    """金额本地化：币种决定写什么，语言决定怎么排。

    code 币种 ISO 码（CNY/USD/...）；不给就按语言猜一个（只用于兼容旧调用）
    form 显示形态，见 money_mark
    """
    if value is None or value == "":
        return ""
    lang2 = base_lang(lang)
    code = (code or _LANG_CURRENCY.get(lang2) or "CNY").upper()
    if decimals is None:
        decimals = money_decimals(code)
    sym = money_mark(code, lang2, form, symbol)
    num = fmt_num(value, lang, decimals, style)
    pattern = pattern or _MONEY_FMT_DEFAULT.get(lang2, "{sym}{num}")
    # 用 ISO 代码当符号时不能紧贴数字：CNY1,234 读不出来，要 CNY 1,234
    tail = sym[-1:]
    if tail.isascii() and tail.isalpha() and "{sym}{num}" in pattern:
        pattern = pattern.replace("{sym}{num}", "{sym} {num}")
    return pattern.replace("{sym}", sym).replace("{num}", num)


def fmt_date(value, lang: str = "zh", order=None, months=None) -> str:
    """日期本地化，接受 date/datetime/'YYYY-MM-DD'。

    order 形如 "{d}/{m}/{Y}"；缺省按语言码取内置表，再不认识就 {Y}-{m}-{d}。
    """
    if not value:
        return ""
    y = m = d = None
    if hasattr(value, "year"):
        y, m, d = value.year, value.month, value.day
    else:
        s = str(value)[:10]
        parts = re.split(r"[-/.]", s)
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    if not y:
        return str(value)
    lang2 = base_lang(lang)
    order = order or _DATE_ORDER.get(lang2, "{Y}-{m}-{d}")
    months = months if isinstance(months, (list, tuple)) and len(months) == 12 else _MONTHS.get(lang2)
    M = months[m - 1] if months and 1 <= m <= 12 else "%02d" % m
    try:
        return order.format(Y=y, m="%02d" % m, d="%02d" % d, M=M)
    except (KeyError, IndexError, ValueError):
        return "%04d-%02d-%02d" % (y, m, d)


# --------------------------------------------------------------------------
# Catalog 容器
# --------------------------------------------------------------------------
class Catalog:
    """一个语言目录的运行期容器。

    strings: {id: str | {"one":..,"few":..,"many":..,"other":..}}
    legacy:  {中文原文: id}   兼容层，让旧的 tr('中文') 不改代码就能吃新表
    """

    __slots__ = ("lang", "strings", "legacy", "meta")

    def __init__(self, lang: str, strings: dict | None = None,
                 legacy: dict | None = None, meta: dict | None = None):
        self.lang = lang
        self.strings = strings or {}
        self.legacy = legacy or {}
        self.meta = meta or {}

    def resolve_id(self, key: str) -> str | None:
        """把外部 key（可能是 id，也可能是中文原文）解析成 catalog 里的 id。"""
        if not key:
            return None
        if key in self.strings:
            return key
        return self.legacy.get(key)

    def raw(self, key: str):
        rid = self.resolve_id(key)
        if rid is None:
            return None
        return self.strings.get(rid)

    def has(self, key: str) -> bool:
        return self.resolve_id(key) is not None


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class Store:
    """多语言目录装载器：线程安全、惰性加载、按需分片。"""

    def __init__(self, locale_dir: str):
        self.dir = locale_dir
        self._lock = threading.RLock()
        self._cache: dict[str, Catalog] = {}
        self._cat_mt: dict[str, float | None] = {}
        self._legacy: dict | None = None
        self._legacy_mt = None
        self._registry: dict | None = None
        self._mt = None
        self.missing: dict[tuple, int] = {}
        self.hits = 0
        self.misses = 0
        self.fallbacks = 0   # 回退中文的次数（新增语言体检用）
        self.blanks = 0      # 渲染成空白的次数（必须恒为 0）

    # ---------------- 语言注册表 ----------------
    def _registry_path(self) -> str:
        return os.path.join(self.dir, "languages.json")

    def _mtime(self):
        try:
            return os.path.getmtime(self._registry_path())
        except OSError:
            return None

    def registry(self) -> dict:
        """locale/languages.json：新增语言的唯一登记处。

        带 mtime 失效：把 languages.json 改完（或 newlang.py 跑完）不用重启，
        下一个请求就认识新语言。少了这一步，「加语言只改注册表」在
        运行中的进程上其实不成立。
        """
        with self._lock:
            mt = self._mtime()
            if self._registry is None or mt != self._mt:
                data = _read_json(self._registry_path()) or {}
                self._registry = {k: v for k, v in data.items() if not k.startswith("_")}
                self._mt = mt
            return self._registry

    def profile(self, lang: str) -> dict:
        """某语言的本地化档案：native / locale / forms / date / num / money。"""
        item = self.registry().get(lang)
        return item if isinstance(item, dict) else {}

    def languages(self, published_only: bool = True) -> dict:
        """{code: 本地语言名}，保持注册顺序。

        published_only 会滤掉 status=skeleton 的语言（刚建骨架、还没翻译）。
        这类语言不该出现在用户的语言选择器里 —— 点进去整页回退中文，
        用户会以为是系统故障。译员仍可用 ?lang=xx 直接预览。
        """
        out = {}
        for k, v in self.registry().items():
            if isinstance(v, dict) and published_only and v.get("status") == "skeleton":
                continue
            out[k] = (v.get("native") or v.get("name") or k) if isinstance(v, dict) else str(v)
        return out

    def all_languages(self) -> dict:
        """含骨架语言，供译员预览与体检使用。"""
        return self.languages(published_only=False)

    def default_lang(self) -> str:
        data = _read_json(self._registry_path()) or {}
        return data.get("_default") or "zh"

    def is_supported(self, lang: str) -> bool:
        return bool(lang) and lang in self.registry()

    def match_tag(self, tag: str) -> str:
        """把 Accept-Language 的一个标签映射成已注册语言。

        先精确匹配完整标签（zh-hant → zh-Hant），再退主语言（es-MX → es）。
        只做「主语言」匹配的话，zh-TW / zh-Hant 会永远落到简体，
        等于永远无法支持繁体或任何地区变体。
        """
        tag = (tag or "").strip().replace("_", "-").lower()
        if not tag:
            return ""
        reg = {k.lower(): k for k in self.registry()}
        if tag in reg:
            return reg[tag]
        return reg.get(tag.split("-")[0], "")

    # ---------------- catalog ----------------
    def legacy_map(self) -> dict:
        """中文原文 → id。全球共享一张表，不随语言变化。

        带 mtime 失效，和 catalog / registry 一样：build_catalog 重建后
        **新登记的串必须立刻能查到**。少了这一步会出一种很难查的现象 ——
        词条表里有、译文也有，界面却还是中文：因为进程内存里那张旧映射
        还算不出这条原文的 ID，取词直接回退原文。
        """
        with self._lock:
            path = os.path.join(self.dir, "legacy_map.json")
            try:
                mt = os.path.getmtime(path)
            except OSError:
                mt = None
            if self._legacy is None or mt != self._legacy_mt:
                self._legacy = _read_json(path) or {}
                self._legacy_mt = mt
            return self._legacy


    def get(self, lang: str) -> Catalog:
        lang = lang or self.default_lang()
        if not self.is_supported(lang):
            lang = self.default_lang()
        with self._lock:
            path = os.path.join(self.dir, "catalog", lang + ".json")
            try:
                mt = os.path.getmtime(path)
            except OSError:
                mt = None
            cat = self._cache.get(lang)
            # 译文文件改了就重新读：译员补一条、页面刷新即生效，不用重启
            if cat is not None and self._cat_mt.get(lang) == mt:
                return cat
            data = _read_json(path) or {}
            strings = {k: v for k, v in data.items() if not k.startswith("_")}
            cat = Catalog(lang, strings, self.legacy_map(), data.get("_meta") or {})
            self._cache[lang] = cat
            self._cat_mt[lang] = mt
            return cat

    def clear(self):
        """语言包热更新：清缓存，下次请求重新读盘。"""
        with self._lock:
            self._cache.clear()
            self._cat_mt.clear()
            self._legacy = None
            self._legacy_mt = None
            self._registry = None
            self._mt = None

    # ---------------- 统计 ----------------
    def note_missing(self, lang: str, key: str):
        with self._lock:
            self.misses += 1
            k = (lang, key)
            self.missing[k] = self.missing.get(k, 0) + 1

    def note_hit(self):
        with self._lock:
            self.hits += 1

    def note_fallback(self):
        """该语言没有译文，回退到了中文。新增语言体检看这个数。"""
        with self._lock:
            self.fallbacks += 1

    def note_blank(self, lang: str, key: str):
        """该渲染成空白的次数。恒为 0 才算健康：空白比漏译更像故障。"""
        with self._lock:
            self.blanks += 1
