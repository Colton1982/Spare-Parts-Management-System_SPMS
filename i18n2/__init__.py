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
i18n2 —— SPMS 多语言门面

对外只有这一层被业务代码看见。旧代码的两种写法都继续可用：

    Python :  _i18n.t("物料查询", lang)          # 中文原文当 key（过渡期）
              i18n2.t("parts.query")             # 语义 ID（目标态）
    Jinja  :  {{ tr('物料查询') }}                # 不动一行模板即可切到新内核
              {{ t('parts.query') }}
              {{ tn('parts.count', n) }}         # 复数

新增一门语言的全部动作
----------------------
  1. locale/languages.json 里加一行
  2. locale/catalog/<lang>.json 放译文（可先空文件，运行期自动回退）
  3. 重启（或点「重载语言包」）—— 不需要改任何模板、任何 Python
"""
from __future__ import annotations

import logging
import os
import threading

_log = logging.getLogger("i18n2")

from . import core as _core
from . import iso_langs as _iso
from .core import (Catalog, Store, base_lang, is_empty, placeholders,
                   plural_forms, plural_index, safe_format)
from .core import fmt_date as _fmt_date_raw
from .core import fmt_money as _fmt_money_raw
from .core import fmt_num as _fmt_num_raw

__all__ = [
    "init", "t", "tn", "tr", "label", "fmt_num", "fmt_date", "fmt_money",
    "set_currency", "get_currency", "cur_label", "currency_meta", "currency_list",
    "set_custom_currency", "custom_currency", "normalize_custom",
    "iso_lang_list", "looks_chinese_authored", "term_known",
    "msg", "render_msg", "current_lang", "set_lang", "languages",
    "tr_json", "page_keys", "install_jinja", "install_flask", "coverage",
    "missing_report", "clear_missing", "stats", "reload", "store",
    "ENUM_KINDS", "normalize_enum", "plural_index", "plural_forms", "is_empty",
    "text_dir",
    "locale_tag",
    "num_scale",
]

# --------------------------------------------------------------------------
# 全局状态
# --------------------------------------------------------------------------
_BASE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_LOCALE_DIR = os.path.join(os.path.dirname(_BASE), "locale")
store: Store = Store(_DEFAULT_LOCALE_DIR)

_ACTIVE = threading.local()
_LOG_LOCK = threading.Lock()
_MISSING_KEYS: dict[tuple, int] = {}

# 数据枚举：库里存的是码，界面显示靠翻译。只有登记在此的 kind 才走 label()
ENUM_KINDS = (
    "status", "result", "tx", "role", "unit", "category",
    "approval", "stocktake", "notify", "priority",
)

# 历史数据里可能已经存了中文（旧版本写入），读时归一成码
_ENUM_ALIAS: dict[tuple, str] = {}


def _build_alias():
    """从 zh 目录反推：enum.<kind>.<code> 的中文值 → code。"""
    if _ENUM_ALIAS:
        return
    zh = store.get(store.default_lang())
    for key, val in zh.strings.items():
        if not key.startswith("enum."):
            continue
        parts = key.split(".")
        if len(parts) == 3 and isinstance(val, str):
            _ENUM_ALIAS[(parts[1], val.strip())] = parts[2]


def normalize_enum(kind: str, value):
    """把可能存成中文的旧值归一为稳定码。读路径用，不写库。"""
    if value is None or value == "":
        return value
    _build_alias()
    return _ENUM_ALIAS.get((kind, str(value).strip()), value)


def label(kind: str, code, lang: str | None = None) -> str:
    """枚举码 → 本地化显示名。绝不返回 None，找不到就显示码本身。

    双候选查找让存量符号键（status_pending / type_in …）零迁移即可复用：
        label('status', 'pending')  → enum.status.pending → status_pending
    """
    if code is None or code == "":
        return ""
    code = normalize_enum(kind, code)
    for cand in ("enum.%s.%s" % (kind, code), "%s_%s" % (kind, code)):
        text = t(cand, lang)
        if text != cand:
            return text
    return str(code)


# --------------------------------------------------------------------------
# 初始化
# --------------------------------------------------------------------------
def init(locale_dir: str | None = None, default_lang: str | None = None):
    global store, _ENUM_ALIAS
    if locale_dir:
        store = Store(os.path.abspath(locale_dir))
    _ENUM_ALIAS = {}
    if default_lang:
        _FALLBACK_DEFAULT = default_lang
    return store


_FALLBACK_DEFAULT = "zh"

# 会话里「这是用户自己选的语言」的凭证。
# 旧版本没有这个标记，会把它自动猜出来的语言写进会话并永久生效；
# 换个值就等于作废所有来路不明的历史选择（只影响一次）。
LANG_SRC = "lang_src"
LANG_SRC_VER = "v2"


# --------------------------------------------------------------------------
# 语言解析
# --------------------------------------------------------------------------
def languages(published_only: bool = True) -> dict:
    """语言选择器用的列表。默认不含还没翻译的骨架语言。"""
    return store.languages(published_only)


def text_dir(lang: str | None = None) -> str:
    """当前语言的书写方向（ltr/rtl）。模板里写 <html dir="{{ cur_dir }}">。"""
    lang = lang or current_lang()
    return _core.text_dir(lang, store.profile(lang).get("dir"))


def locale_tag(lang: str | None = None) -> str:
    """BCP-47 标签，给前端 toLocaleString / Intl 用。

    前端以前把 'zh-CN' 写死在每个页面的 fmt() 里，结果是：切到德语，数字
    还是 1,234.56（应为 1.234,56），日期还是中文序。语言档案里的 locale
    字段就是为此存在的，这里统一取出来。
    """
    lang = lang or current_lang()
    tag = (store.profile(lang) or {}).get("locale")
    return tag or (lang or "zh")


def num_scale(lang: str | None = None) -> str:
    """数字进位体系：'wan'（万/亿）还是 'short'（K/M/B）。

    这不是翻译问题，是**单位体系**问题。中文「5万」如果只把「万」这个词
    翻成 "10K"，界面上会变成 "510K" —— 数值直接错了一个数量级。
    所以换语言必须换进位方式，而不是换后缀文字。
    """
    lang = lang or current_lang()
    return (store.profile(lang) or {}).get("scale") or "short"


def _from_request() -> str:
    """URL ?lang= → 会话（用户显式选择）→ 账号档案 → Cookie → 系统默认 → 浏览器猜测。

    顺序即优先级；任一层命中即返回，因此：
      - 分享出去的链接可以带 lang，收件人看到自己的语言
      - 用户显式切换过的语言存在会话/Cookie，换页面不丢
      - 管理员在设置页定的系统语言优先于浏览器 Accept-Language 的猜测
    """
    try:
        from flask import g, request, session
        cached = getattr(g, "_i18n2_lang", None)
    except Exception:
        # 没有请求上下文（后台线程、定时任务、CLI 脚本）：交给调用方走默认语言
        return ""
    if cached:
        return cached

    lang = ""
    q = (request.args.get("lang") or "").strip()
    if q:
        # 完整标签优先（zh-Hant / pt-BR 这类变体必须能选中），再退主语言
        lang = store.match_tag(q)
    # 会话 > 账号档案：会话只会在「用户显式切换」时写入，
    # 它必须是最高优先级的持久层，否则在设置页/登录页选完语言，
    # 下一次请求会被 users.lang 的老值盖回去（表现为「怎么点都切不动」）。
    if not lang:
        s = (session.get("lang") or "").strip()
        # 只有带 LANG_SRC 标记的才算「用户选过」。旧版本会把浏览器猜出来的语言
        # 自动写进会话，那不是用户选择，却能把界面永久锁在一门语言上——
        # 遇到这种无标记的旧值直接作废，让它重新走一遍正常解析。
        if s and session.get(LANG_SRC) == LANG_SRC_VER and store.is_supported(s):
            lang = s
        elif s and session.get(LANG_SRC) != LANG_SRC_VER:
            session.pop("lang", None)
    if not lang:
        try:
            uid = session.get("uid")
            if uid:
                # 注意：database 模块里的连接函数叫 get_conn，没有 get_db
                #（app.py 的 get_db 是它自己的包装）。写错名字会被下面的
                # except 吞掉，表现为「账号语言偏好永远不生效」。
                from database import get_conn
                conn = get_conn()
                try:
                    row = conn.execute("SELECT lang FROM users WHERE id=?", (uid,)).fetchone()
                    if row and row["lang"] and store.is_supported(row["lang"]):
                        lang = row["lang"]
                finally:
                    conn.close()
        except Exception as e:
            _log.debug("读取账号语言偏好失败：%s", e)
    if not lang:
        c = (request.cookies.get("spms_lang") or "").strip()
        if c and store.is_supported(c):
            lang = c
    # 管理员在设置页定的系统语言，优先于浏览器 Accept-Language 的猜测：
    # 否则中文 Windows 上 Accept-Language=zh-CN 会把系统设置顶掉，
    # 表现为「设置页保存英文，谁打开都还是中文」。
    if not lang:
        try:
            import settings as _settings
            sys_lang = (_settings.load() or {}).get("language") or ""
        except Exception:
            sys_lang = ""
        if store.is_supported(sys_lang):
            lang = sys_lang
    if not lang:
        best, best_q = "", 0.0
        for part in (request.headers.get("Accept-Language") or "").split(","):
            try:
                tag, _, qv = part.strip().partition(";")
                qv = float(qv.split("=")[1]) if "=" in qv else 1.0
            except Exception:
                continue
            code = store.match_tag(tag)
            if code and qv > best_q:
                best, best_q = code, qv
        lang = best
    if not lang:
        lang = _FALLBACK_DEFAULT

    try:
        g._i18n2_lang = lang
    except Exception:
        pass
    return lang


def current_lang() -> str:
    """当前语言。请求外（后台线程/脚本）返回线程上下文或系统默认。"""
    forced = getattr(_ACTIVE, "lang", None)
    if forced:
        return forced
    lang = _from_request()
    if lang:
        return lang
    try:
        import settings as _settings
        sys_lang = (_settings.load() or {}).get("language") or ""
        if store.is_supported(sys_lang):
            return sys_lang
    except Exception:
        pass
    return _FALLBACK_DEFAULT


def _persist_user_lang(lang: str):
    """把语言偏好写到 users.lang。

    这是「通知发对人」的前提：邮件渲染语言取自收件人档案，
    而不是渲染那一刻的请求上下文（后台线程根本没有请求）。
    """
    try:
        from flask import session
        uid = session.get("uid")
        if not uid:
            return
        from database import get_conn
        conn = get_conn()
        try:
            conn.execute("UPDATE users SET lang=? WHERE id=?", (lang, uid))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _log.debug("写入账号语言偏好失败：%s", e)


def set_lang(lang: str, persist: bool = True):
    """切换语言：写会话 + Cookie + 用户档案，三处都记住。"""
    if not store.is_supported(lang):
        return False
    try:
        from flask import g, session
        session["lang"] = lang
        session[LANG_SRC] = LANG_SRC_VER  # 打上「这是用户自己选的」标记
        g._i18n2_lang = lang
        g._i18n2_persist = lang
    except Exception:
        pass
    if persist:
        _persist_user_lang(lang)
    return True


class lang_scope:
    """with lang_scope('ru'): ... —— 在后台线程/邮件渲染里显式指定语言。

    这是修「俄语同事永远收到中文邮件」的关键：渲染时按收件人语言，而不是
    按「当前请求语言」（后台线程根本没有请求）。
    """

    def __init__(self, lang: str):
        self.lang = lang if store.is_supported(lang) else _FALLBACK_DEFAULT
        self.prev = None

    def __enter__(self):
        self.prev = getattr(_ACTIVE, "lang", None)
        _ACTIVE.lang = self.lang
        return self

    def __exit__(self, *exc):
        _ACTIVE.lang = self.prev
        return False


# --------------------------------------------------------------------------
# 本地化格式：日期 / 数字 / 金额
#
# 优先读注册表档案（languages.json 的 date / num / money / months / currency），
# 缺省时走 core 内置表。这样新增语言只要在注册表写一行，不必改 Python。
# --------------------------------------------------------------------------
def fmt_date(value, lang: str | None = None) -> str:
    lang = lang or current_lang()
    p = store.profile(lang)
    return _fmt_date_raw(value, lang, p.get("date"), p.get("months"))


def fmt_num(value, lang: str | None = None, decimals: int = 0) -> str:
    lang = lang or current_lang()
    p = store.profile(lang)
    return _fmt_num_raw(value, lang, decimals, p.get("num"))


# --------------------------------------------------------------------------
# 币种：跟语言正交
#
# 币种是「这套系统在哪个国家记账」，由 sys_settings.currency 决定，一次部署定一个；
# 语言是「这个人想看哪种文字」，随时可切。以前把 currency 写在 languages.json 的
# 语言档案里，等于假设「说英语就是美元区」，所以十个语言一律填了 ¥，字段成了摆设。
#
# form 控制文案里怎么标注这个币种：
#   auto   中文用「元 / 美元」，其他语言用 ISO 码（避开 ¥ 既是人民币又是日元）
#   symbol 一律用符号  ¥1,234
#   code   一律用代码  CNY 1,234
#   both   符号 + 代码  ¥ CNY 1,234
# --------------------------------------------------------------------------
_CUR: dict = {"code": "CNY", "form": "auto"}


def set_currency(code: str | None = None, form: str | None = None) -> dict:
    """设置当前币种与显示形态。app 启动时从 sys_settings 注入一次。"""
    if code:
        c = str(code).strip().upper()
        if c:
            _CUR["code"] = c
    if form:
        _CUR["form"] = form if form in ("auto", "symbol", "code", "both") else "auto"
    return dict(_CUR)


def get_currency() -> dict:
    """当前币种配置（只读副本）。"""
    return dict(_CUR)


def currency_meta(code: str | None = None) -> dict:
    """币种元数据：symbol / zh（中文单位名）/ decimals。查不到内置表时会用自定义那条。"""
    return dict(_core.currency(code or _CUR["code"]))


def set_custom_currency(meta) -> dict:
    """装一条内置表之外的自定义币种（设置页填写）。"""
    return _core.set_custom_currency(meta)


def term_known(text) -> bool:
    """这段字面量在词条表里吗（ID 或中文原文都算）。

    模板用它决定 flash 要不要走取词：带参数拼出来的整句本来就不在表里，
    走一遍只会被记成「运行期漏词」，管理员点登记就多出一条僵尸词条。
    """
    s = str(text or "").strip()
    if not s:
        return False
    try:
        return store.get(store.default_lang()).has(s)
    except Exception:
        return False


def looks_chinese_authored(text) -> bool:
    """这句话像不像界面上要翻译的文案（不含日志/已翻好的整句）。"""
    return _core.looks_chinese_authored(text)


def iso_lang_list() -> list:
    """ISO 639-1 全量语言表，给「添加语言」下拉：[code, native, en, zh]。"""
    return _iso.iso_lang_list()


def normalize_custom(meta) -> dict:
    """校验自定义币种（不写入）。不合规抛 ValueError，原因给中文。"""
    return _core.normalize_custom(meta)


def custom_currency() -> dict:
    return _core.custom_currency()


def currency_list() -> list:
    """全部可选币种，给设置界面下拉用。

    name 随界面语言走：中文给「加元」，其余给英文标准名「Canadian Dollar」。
    只给 zh 名的话，英文界面的下拉里会冒出「加元 / 卢布 / 印尼盾」，
    等于把没翻译的内容摆在最显眼的设置页第一屏。
    """
    lang = base_lang(current_lang())
    out = []
    for k, v in sorted(_core.CURRENCIES.items()):
        code_zh = v.get("zh") or k
        code_en = v.get("en") or k
        out.append({"code": k, "symbol": v.get("symbol") or k,
                    "zh": code_zh, "en": code_en,
                    "name": code_zh if lang == "zh" else code_en,
                    "decimals": v.get("decimals", 2), "custom": False})
    # 自定义那条排在最后：它只属于当前这套部署，不该混进「世界币种表」中间
    cm = _core.custom_currency()
    if cm:
        out.append({"code": cm["code"], "symbol": cm.get("symbol") or cm["code"],
                    "zh": cm.get("zh") or cm["code"], "en": cm.get("en") or cm["code"],
                    "name": (cm.get("zh") if lang == "zh" else cm.get("en")) or cm["code"],
                    "decimals": cm.get("decimals", 2), "custom": True})
    return out


def cur_label(lang: str | None = None, form: str | None = None) -> str:
    """文案里的单位标注 —— 「单价({cur})」里的那一截。

    中文给「元 / 美元」，其余语言给 ISO 码：英文界面写 Unit Price (CNY) 比
    写 Unit Price (¥) 明确，因为 ¥ 在英文语境里读者会先想到日元。
    """
    lang = base_lang(lang or current_lang())
    code = _CUR["code"]
    form = form or _CUR["form"]
    meta = _core.currency(code)
    if form in ("symbol", "code", "both"):
        return _core.money_mark(code, lang, form)
    if lang == "zh":
        return meta.get("zh") or code
    return code


def fmt_money(value, lang: str | None = None, decimals: int | None = None) -> str:
    lang = lang or current_lang()
    p = store.profile(lang)
    return _fmt_money_raw(value, lang, decimals,
                          None, p.get("money"), p.get("num"),
                          code=_CUR["code"], form=_CUR["form"])


# --------------------------------------------------------------------------
# 取词
# --------------------------------------------------------------------------
def _lookup(lang: str, key: str):
    """返回 (文本或复数dict, 命中与否)。

    命中 = 该语言**真有这条译文**。空串、全空复数字典都算「还没翻译」，
    继续往中文真源回退。否则骨架语言（newlang 生成的全空表）会让
    整个界面变成空白 —— 比漏译严重得多，因为它看起来像页面坏了。
    """
    cat = store.get(lang)
    val = cat.raw(key)
    if not is_empty(val):
        return val, True

    # 标点变体重试：全角括号 ↔ 半角括号。词条只登记一种写法，而源码里两种
    # 都可能出现（有人手打半角、有人从中文字典复制全角）。不试这一步，
    # 「盘点差异二级审批（终审）」就会因为一个括号形状整句退回中文。
    for conv in (_PUNCT_WIDE, _PUNCT_NARROW):
        alt = key.translate(conv)
        if alt != key:
            val = cat.raw(alt)
            if not is_empty(val):
                return val, True

    # 当前语言没这条（或还是空位）→ 回退中文真源（保证界面不出现空白/ID）
    default = store.get(store.default_lang())
    val = default.raw(key)
    if not is_empty(val):
        return val, False
    return None, False


_CJK_RE = __import__("re").compile(r"[\u4e00-\u9fff]")

# 全角/半角标点变体：同一句文案在不同文件里可能写成「（终审）」也可能写成
# "(终审)"，词条表只会登记其中一种。查不到时按变体再试一次，避免因为一个
# 括号形状让整句退回中文。只做标点映射，不动任何汉字。
_PUNCT_WIDE = str.maketrans({
    "（": "(", "）": ")", "，": ",", "：": ":", "；": ";",
    "！": "!", "？": "?", "／": "/", "－": "-",
})
_PUNCT_NARROW = str.maketrans({
    "(": "（", ")": "）", ",": "，", ":": "：", ";": "；",
    "!": "！", "?": "？",
})

# 中文标点 → 西文标点。标点不是词条，是排版规则：不该让译员去翻「（」，
# 也不该让英文界面出现全角逗号。只有宽字符标点才在非中日语言下被替换。
_PUNCT_WIDE2ASCII = {
    "，": ",", "。": ".", "：": ":", "；": ";", "！": "!", "？": "?",
    "（": "(", "）": ")", "、": ",", "「": '"', "」": '"',
    "【": "[", "】": "]", "《": '"', "》": '"', "～": "~",
    "％": "%", "＋": "+", "＝": "=", "／": "/", "＼": "\\",
    "＆": "&", "＃": "#", "＠": "@", "　": " ",
}


def _punct_fallback(key: str, lang: str):
    """纯标点串在非中日语境下换成西文标点；否则返回 None 表示不适用。"""
    if not key or lang in ("zh", "ja"):
        return None
    if not all(ch in _PUNCT_WIDE2ASCII for ch in key):
        return None
    return "".join(_PUNCT_WIDE2ASCII[ch] for ch in key)


def _parse_args(args, params):
    """从位置参数里识别 (语言码, 中文兜底)。

    支持全部历史与目标写法：
        t('parts.x')
        t('parts.x', 'ru')
        t('parts.x', '中文原文')
        t('parts.x', '中文原文', 'ru')
        t('parts.x', {'n': 3})
    """
    lang, fallback = None, None
    for a in args:
        if isinstance(a, dict):
            params = {**a, **params}
        elif isinstance(a, str):
            if lang is None and store.is_supported(a):
                lang = a
            elif fallback is None and _CJK_RE.search(a):
                fallback = a
    return lang, fallback, params


def _zh_text(key: str):
    """中文真源里该 ID 的文本（复数取 other）。给 tn 的兜底用。"""
    v = store.get(store.default_lang()).raw(key)
    if isinstance(v, dict):
        return v.get("other") or next((x for x in v.values() if x), "")
    return v if isinstance(v, str) else ""


def t(key: str, *args, **params) -> str:
    """取词。缺译时依次回退：当前语言 → 中文真源 → 调用点给的 fallback → key。"""
    # 空 key 不是「漏翻」，是数据本身为空（审计详情、可选字段常把 '' 传进来）。
    # 不在这里短路的话，note_blank 会把这类空值算成「渲染成空白」，把体检指标弄脏。
    if key is None or key == "":
        return ""
    lang, fallback, params = _parse_args(args, params)
    if lang is None:
        lang = current_lang()

    val, hit = _lookup(lang, key)
    if val is None:
        punct = _punct_fallback(key, lang)
        if punct is not None:
            store.note_hit()
            return safe_format(punct, params)
        store.note_missing(lang, key)
        # 只登记「像界面文案」的键：flash 里已经翻好的整句、日志行、异常栈
        # 换个语言必然取不到词，但它们不该出现在待翻译队列里。
        if _core.looks_chinese_authored(key):
            with _LOG_LOCK:
                _MISSING_KEYS[(lang, key)] = _MISSING_KEYS.get((lang, key), 0) + 1
        # 回退也要替换占位符：否则界面会出现 "第 {n} 列" 这种带花括号的残句
        raw = fallback or str(key)
        out = safe_format(raw, _with_cur(raw, params, lang))
        if is_empty(out):
            # 连中文真源都没有：宁可显示 key，也不要渲染成空白
            store.note_blank(lang, key)
            return str(key)
        return out
    store.note_hit()
    if not hit:
        store.note_fallback()

    if isinstance(val, dict):
        # 复数形式但走的是单数接口：取 other，避免把字典渲染到界面
        val = val.get("other") or next(iter(val.values()), "")
    if is_empty(val):
        raw = fallback or key
        out = safe_format(raw, _with_cur(raw, params, lang))
        if is_empty(out):
            store.note_blank(lang, key)
            return str(key)
        return out
    return safe_format(val, _with_cur(val, params, lang))


def _with_cur(text, params, lang):
    """词条里有 {cur} 就补上当前币种；没有就原样返回（绝大多数词条走这条路，零开销）。"""
    if not isinstance(text, str) or "{cur}" not in text or "cur" in params:
        return params
    p = dict(params)
    p["cur"] = cur_label(lang)
    return p


def tn(key: str, n, *args, **params) -> str:
    """复数取词。tn('parts.count', 5) / tn('parts.count', 5, 'ru')"""
    lang, fallback, params = _parse_args(args, params)
    if lang is None:
        lang = current_lang()

    val, hit = _lookup(lang, key)
    if val is None:
        store.note_missing(lang, key)
        # 只登记「像界面文案」的键：flash 里已经翻好的整句、日志行、异常栈
        # 换个语言必然取不到词，但它们不该出现在待翻译队列里。
        if _core.looks_chinese_authored(key):
            with _LOG_LOCK:
                _MISSING_KEYS[(lang, key)] = _MISSING_KEYS.get((lang, key), 0) + 1
        tpl = fallback or _zh_text(key) or key
        return safe_format(tpl, {**params, "n": n, "count": n})

    if isinstance(val, dict):
        cat = plural_index(lang, n)
        # 该形式缺失（新语言常只填了 other）→ 退 other → 退中文同形式 → 退任一非空
        text = val.get(cat)
        if is_empty(text):
            text = val.get("other")
        if is_empty(text):
            zh = store.get(store.default_lang()).raw(key)
            if isinstance(zh, dict):
                text = zh.get(cat) or zh.get("other")
            elif isinstance(zh, str):
                text = zh
            store.note_fallback()
        if is_empty(text):
            text = next((v for v in val.values() if isinstance(v, str) and v.strip()), "")
    else:
        text = val
    if not hit:
        store.note_fallback()
    if is_empty(text):
        text = fallback or key
        store.note_blank(lang, key)
    params = {**params, "n": n, "count": n}
    return safe_format(text, params)


# 旧名兼容
tr = t


def fmt_dt(value, fmt: str = "date", lang: str | None = None) -> str:
    lang = lang or current_lang()
    return fmt_date(value, lang) if fmt == "date" else str(value)


# --------------------------------------------------------------------------
# 延迟消息：服务端只产码，渲染发生在消费端
# --------------------------------------------------------------------------
def msg(code: str, **params) -> dict:
    """构造消息对象（不是字符串）。可放进 jsonify、日志、队列、邮件任务。

    return jsonify({"error": msg("error.parts_not_found", code="X-999")})
    前端拿到 {"code": "...", "params": {...}} 后用自己的语言渲染。
    """
    return {"code": code, "params": params}


def render_msg(m, lang: str | None = None) -> str:
    """把消息对象渲染成指定语言的字符串。"""
    if m is None:
        return ""
    if isinstance(m, str):
        return t(m, lang) if lang else t(m)
    code = m.get("code") or ""
    params = m.get("params") or {}
    lang = lang or current_lang()
    if "n" in params or "count" in params:
        return tn(code, params.get("n", params.get("count")), lang, **params)
    return t(code, lang, **params)


# --------------------------------------------------------------------------
# 前端分片
# --------------------------------------------------------------------------
def page_keys(page: str | None = None) -> set:
    """读取构建期生成的「页面 → 用到的词条 ID」映射。没有就返回空集。

    pages.json 里有两种口径的键：endpoint（parts_page）和模板名（parts.html）。
    运行期只会用 endpoint（_page_key() 取 request.endpoint），而**登记**时扫描器
    给的是模板名 —— 只认一种，登记进 parts.html 的那条前端就永远拿不到
    （历史上已经漏过：parts_manage.html 里的 app.0620d）。
    所以这里把同一页的两种口径**取并集**，老数据不用迁移也能生效。
    """
    if not page:
        return set()
    data = _read(os.path.join(store.dir, "pages.json")) or {}
    names = [page]
    base = os.path.basename(page)
    if base != page:
        names.append(base)
    stem = page[:-5] if page.endswith("_page") else page
    names.append(stem + ".html")
    if base.endswith(".html") and base != page:
        names.append(base)
    keys = set()
    for n in names:
        keys |= set(data.get(n) or [])
    # 公共分片：base.html（顶栏、用户下拉、通知）的取词每一页都会用到。
    # 以前没有这一桶，这些词条只能逐页登记，漏一页就那一页露中文 ——
    # `{name}（{role} / {dept}）` 同时漏在 25 个页面上就是这么来的。
    keys |= set(data.get("_base") or [])
    return keys


def _read(path):
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def tr_json(lang: str | None = None, page: str | None = None) -> dict:
    """给浏览器的词条字典。

    两个关键点：
      1. **按页分片**：传 page 只返回该页真正用到的词条。整本词典俄语 400KB 级别，
         每页都内联等于让用户为用不到的 95% 付费。
      2. **key 用中文原文**：JS 里现存 276 处 TR('中文') 一行不改就能命中，
         不必为了分片去改前端调用。
    拿不到分片信息时回退全量，绝不会因为缺 pages.json 而白屏。
    """
    lang = lang or current_lang()
    is_default = lang == store.default_lang()
    cat = store.get(lang)
    default = store.get(store.default_lang())
    keys = page_keys(page) if page else set()
    if keys:
        ids = [k for k in keys if k in cat.strings or k in default.strings]
    else:
        ids = sorted(set(cat.strings) | set(default.strings))

    # ID → 中文原文，让前端沿用中文 key。
    # 同一个 ID 可能有**多种原文形态**（例如模板里写 TR(' ？…') 带前导空格用于
    # 拼接名字，而词条表里同时存了不带空格的一条）。以前这里用字典推导，
    # 后出现的把先出现的覆盖掉，只有一种写法能命中，另一种永远回退中文。
    # 现在把该 ID 的所有形态都下发，前端怎么写都能取到。
    rev = {}
    for zh_text, tid in default.legacy.items():
        rev.setdefault(tid, []).append(zh_text)
    out = {}
    for k in ids:
        v = cat.strings.get(k)
        if v is None:
            v = default.strings.get(k)
        srcs = rev.get(k) or [k]
        if isinstance(v, dict):
            # 复数词条整包下发（保留 one/few/many），前端 TRN() 才能按数字选形式。
            # 以前这里只取 other，导致英文界面永远显示 "records"，连 "1 records" 都出来了。
            if is_default:
                continue
            forms = {f: s for f, s in v.items() if isinstance(s, str) and s}
            if not forms:
                continue
            for src in srcs:
                out[src] = forms
            continue
        if not isinstance(v, str) or not v:
            continue
        if "{cur}" in v:
            v = v.replace("{cur}", cur_label(lang))
        elif is_default:
            continue
        for src in srcs:
            out[src] = v
    # 中文界面以前整份返回空，结果 JS 里的 TR('库存金额(元)') 拿不到词条、
    # 直接吐原文，换币种后首页表头还写着「(元)」。现在只补发含 {cur} 的那几条。
    return out


# --------------------------------------------------------------------------
# 框架装配
# --------------------------------------------------------------------------
# 中文标点 → 拉丁标点。标点不该进词条表：它没有语义，只有排版规则。
_PUNCT_LATIN = {
    # 每个值都带自己需要的空格：中文里「，：；」后面不空，英文必须空一格；
    # 句号以前漏了空格，导致英文界面出现 "approval.Stock in:" 这种粘在一起的句子。
    "（": " (", "）": ")", "：": ": ", "，": ", ", "。": ". ",
    "；": "; ", "、": ", ", "「": '"', "」": '"', "｜": " | ", "——": "—",
}
_CJK_PUNCT_LOCALES = ("zh", "ja")


def mk(ch: str, lang: str | None = None) -> str:
    """按语言返回标点：中日用全角，其余用半角。

    模板里写 {{ mk('（') }} 而不是 {{ tr('（') }}，标点就不会再产生
    "（ ）" 这类全角字符漏进英文界面的问题，也不会占用 666 个词条位。
    """
    lang = (lang or current_lang() or "").lower()
    if lang.startswith(_CJK_PUNCT_LOCALES):
        return ch
    return _PUNCT_LATIN.get(ch, ch)


def install_jinja(app, page_var: str = "page_name"):
    """注册 Jinja 全局函数 + 本地化过滤器。

    模板里旧写法 {{ tr('中文') }} 与新写法 {{ t('parts.query') }} 并存，
    因为 tr 与 t 是同一个函数，内核会在 legacy_map 里把中文换成 ID。
    """
    env = app.jinja_env
    env.globals.update(
        t=t, tr=t, tn=tn, label=label, mk=mk,
        fmt_num=lambda v, d=0: fmt_num(v, current_lang(), d),
        fmt_date=lambda v: fmt_date(v, current_lang()),
        fmt_money=lambda v, d=None: fmt_money(v, current_lang(), d),
        cur=lambda: cur_label(),
        langs=languages,
        cur_lang_fn=current_lang,
    )


def install_flask(app):
    """语言解析中间件：把 ?lang= 变成可持久化的会话+Cookie+账号档案。

    注意：这个函数必须在 app 创建后立刻调用一次（app.py 里已挂）。
    漏挂时 ?lang= 只在当次请求有效，点一下导航就退回原语言。
    """

    @app.before_request
    def _apply_lang():
        try:
            from flask import request, session, g
        except Exception:
            return
        q = (request.args.get("lang") or "").strip()
        if not q:
            return
        code = store.match_tag(q)  # 容忍 zh-CN / en-US / zh-Hant 这类写法
        if not code:
            return
        session["lang"] = code
        session[LANG_SRC] = LANG_SRC_VER
        g._i18n2_lang = code
        g._i18n2_persist = code
        # 已登录用户：顺手写进 users.lang，换台电脑登录也是这个语言
        _persist_user_lang(code)

    @app.after_request
    def _persist_lang(resp):
        try:
            from flask import g
        except Exception:
            return resp
        lang = getattr(g, "_i18n2_persist", None)
        if lang:
            resp.set_cookie("spms_lang", lang, max_age=365 * 24 * 3600,
                            samesite="Lax", httponly=False)
        return resp


# --------------------------------------------------------------------------
# 运维接口
# --------------------------------------------------------------------------
def missing_report() -> dict:
    """本次运行期未命中的词条（CI 门禁与「补译清单」都用它）。"""
    with _LOG_LOCK:
        return dict(_MISSING_KEYS)


def clear_missing():
    with _LOG_LOCK:
        _MISSING_KEYS.clear()


def stats() -> dict:
    return {
        "languages": len(store.registry()),
        "hits": store.hits,
        "misses": store.misses,
        "fallbacks": store.fallbacks,
        "blanks": store.blanks,
        "miss_rate": (store.misses / float(store.hits + store.misses))
        if (store.hits + store.misses) else 0.0,
    }


def coverage(lang: str) -> dict:
    """某语言的真实完成度（按中文真源的条目数算，空串不算已翻译）。

    这是「新增语言能不能上线」的判据：finish 只统计真正有值的条目，
    骨架语言会被如实报成 0%，而不是因为界面变空白而显示「无残留中文」。
    """
    default = store.get(store.default_lang())
    cat = store.get(lang)
    ids = [k for k in default.strings if not is_empty(default.strings[k])]
    filled = [k for k in ids if not is_empty(cat.strings.get(k))]
    forms = plural_forms(lang, store.profile(lang).get("forms"))
    return {
        "lang": lang,
        "total": len(ids),
        "filled": len(filled),
        "missing": len(ids) - len(filled),
        "percent": round(len(filled) * 100.0 / max(1, len(ids)), 1),
        "plural_forms": list(forms),
        "empty_slots": sum(1 for k in ids if k in cat.strings
                           and is_empty(cat.strings[k])),
    }


def reload():
    store.clear()
    _ENUM_ALIAS.clear()
    return True
