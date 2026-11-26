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
库存管理系统 - 主程序（桌面直连版）
========================================
启动方式：双击 exe（或执行 python app.py）
访问方式：软件自动打开浏览器进入本机页面 http://127.0.0.1:端口
数据库：  首次启动进入初始化向导，填写内网公共盘数据库路径后所有用户共享
"""
import json
import logging
import math
import os
import re
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, has_app_context, jsonify, redirect, render_template,
                   request, Response, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import config
import database as db
import settings as _settings
import i18n as _i18n

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
# 模板修改立即生效（内网部署/迭代阶段无需重启服务即可刷新页面样式与布局）
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.filters["fromjson"] = json.loads

# 语言中间件：URL 上的 ?lang=xx 落成 会话 + Cookie + 账号档案。
# 这一行以前漏了，导致「选了英文，点一下导航就变回中文」。
_i18n.install_flask(app)

# ------------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------------
def get_db():
    return db.get_conn()


def current_user():
    uid = session.get("uid")
    if not uid or not config.DB_PATH:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE id=? AND active=1", (uid,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


# ------------------------------------------------------------------
# 日期时间筛选
#
# 列表页的起止条件改成原生选择器（日期框 + 时间框），传上来的值可能是
#   "2026-11-02" + time=          （只选日期 = 整天）
#   "2026-11-02" + time=14:30     （日期框 + 时间框）
#   "2026-11-02T14:30"            （老链接 / 收藏夹里的 datetime-local 值）
#   "2026/11/02"                  （更早的手录格式）
# 统一归一成 created_at 里存的 "YYYY-MM-DD HH:MM:SS" 再比较，字符串序即时间序。
# ------------------------------------------------------------------
_DT_ARG_RE = re.compile(
    r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})"
    r"(?:[ T]+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?$"
)


def dt_parts(raw):
    """拆成 (日期, 时间) 两个可用于回填的字符串；非法或空返回 ("", "")。

    时间部分原样补零到 HH:MM，秒被丢掉（选择器只到分钟）。
    """
    s = (raw or "").strip()
    if not s:
        return ("", "")
    m = _DT_ARG_RE.match(s)
    if not m:
        return ("", "")
    y, mo, d, hh, mi, ss = m.groups()
    y, mo, d = int(y), int(mo), int(d)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ("", "")
    day = "%04d-%02d-%02d" % (y, mo, d)
    if hh is None:
        return (day, "")
    hh, mi = int(hh), int(mi)
    if hh > 23 or mi > 59:
        return ("", "")
    return (day, "%02d:%02d" % (hh, mi))


def dt_bound(raw, end=False):
    """把日期/时间参数归一成可比较的时间串；空值或格式非法返回 ""（= 不加此条件）。

    end=True 表示「截止」：只给日期取当天 23:59:59，给到分钟取该分钟的 59 秒，
    这样「截止 14:30」不会把 14:30:20 的记录漏掉。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    m = _DT_ARG_RE.match(s)
    if not m:
        return ""
    y, mo, d, hh, mi, ss = m.groups()
    y, mo, d = int(y), int(mo), int(d)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    if hh is None:
        return "%04d-%02d-%02d %s" % (y, mo, d, "23:59:59" if end else "00:00:00")
    hh, mi = int(hh), int(mi)
    ss = int(ss) if ss is not None else (59 if end else 0)
    if hh > 23 or mi > 59 or ss > 59:
        return ""
    return "%04d-%02d-%02d %02d:%02d:%02d" % (y, mo, d, hh, mi, ss)


def dt_date_in(raw):
    """日期框回填值（YYYY-MM-DD）。"""
    return dt_parts(raw)[0]


def dt_time_in(raw, explicit=""):
    """时间框回填值（HH:MM）；显式传来的 time 参数优先，非法时回落到 URL 里内嵌的时间。"""
    exp = (explicit or "").strip()
    if exp:
        hits = dt_parts("2000-01-01 " + exp)[1]
        if hits:
            return hits
    return dt_parts(raw)[1]


def dt_where(where, args, col, date_from, date_to, time_from="", time_to=""):
    """把起止时间条件追加进 where/args（列名由调用方给，防串表）。

    时间留空 = 该日整天（起始 00:00:00 / 截止 23:59:59）。
    """
    d1, t1 = dt_parts(date_from)
    d2, t2 = dt_parts(date_to)
    lo = dt_bound("%s %s" % (d1, time_from or t1)) if d1 else ""
    hi = dt_bound("%s %s" % (d2, time_to or t2), end=True) if d2 else ""
    if lo:
        where.append(col + ">=?")
        args.append(lo)
    if hi:
        where.append(col + "<=?")
        args.append(hi)


app.jinja_env.globals["dtv_d"] = dt_date_in
app.jinja_env.globals["dtv_t"] = dt_time_in


# ------------------------------------------------------------------
# 币种同步：与语言正交，一次部署定一个
#
# 存在 sys_settings.currency（JSON: {"code":"CNY","form":"auto"}）。每个请求带 3 秒
# 缓存读一次，推进 i18n2 引擎，fmt_money 和词条里的 {cur} 就全跟着变。
# 以前 currency 写在 languages.json 的语言档案里，等于假设「说英语就是美元区」，
# 所以十个语言一律填了 ¥，那个字段从没起过作用。
# ------------------------------------------------------------------
_CUR_CACHE = {"t": 0.0, "code": "CNY", "form": "auto", "custom": {}}


def currency_cfg(force=False):
    """当前币种配置 {code, form, custom}。3 秒缓存，避免每请求都查库。"""
    import time as _time
    now = _time.time()
    if not force and now - _CUR_CACHE["t"] < 3:
        return dict(_CUR_CACHE)
    code, form, custom = "CNY", "auto", {}
    if config.DB_PATH:
        try:
            conn = get_db()
            try:
                raw = db.get_setting(conn, "currency")
                if raw:
                    if raw.strip().startswith("{"):
                        d = json.loads(raw)
                        code = (d.get("code") or "CNY").upper()
                        form = d.get("form") or "auto"
                        # 内置表之外的那一条：客户市场是新西兰/波兰/巴基斯坦时，
                        # 管理员自己填的代码、符号、中文名、小数位就存在这里。
                        c = d.get("custom")
                        if isinstance(c, dict) and c.get("code"):
                            custom = c
                    else:
                        code = raw.strip().upper()
            finally:
                conn.close()
        except Exception:
            pass
    _CUR_CACHE.update({"t": now, "code": code, "form": form, "custom": custom})
    _i18n.set_currency(code, form)
    try:
        _i18n.set_custom_currency(custom)
    except ValueError:
        pass   # 库里存了不合规的旧记录：忽略，绝不让它把页面拖垮
    return dict(_CUR_CACHE)


def set_currency_cfg(code, form="auto", conn=None, custom=None):
    """写币种配置并立即生效。只由换算接口调用，不要在页面里随手改。"""
    code = (code or "CNY").strip().upper()
    form = form if form in ("auto", "symbol", "code", "both") else "auto"
    # 自定义币种要跟着一起写回去：这里的 JSON 是整条覆盖，
    # 只带 code/form 会把管理员填的那条悄无声息地抹掉。
    custom = custom if isinstance(custom, dict) else dict(_CUR_CACHE.get("custom") or {})
    own = conn is None
    if own:
        conn = get_db()
    try:
        payload = {"code": code, "form": form}
        if custom:
            payload["custom"] = custom
        db.set_setting(conn, "currency", json.dumps(payload))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()
    _CUR_CACHE["t"] = 0.0
    _CUR_CACHE["custom"] = custom
    _i18n.set_currency(code, form)
    try:
        _i18n.set_custom_currency(custom)
    except ValueError:
        pass
    return dict(_CUR_CACHE)


def set_custom_currency_cfg(meta, conn=None):
    """只改「自定义币种」这一条，不动当前币种。

    为什么要分开：加一条币种和「把全库金额换算成它」是两件事。
    前者随时可做、可反复改；后者要填汇率、要确认词、要整表乘率，
    走的是 /parts/currency/apply。混在一起等于让「填个符号」变成财务操作。
    """
    custom = _i18n.normalize_custom(meta) if meta else {}
    cfg = currency_cfg(force=True)
    own = conn is None
    if own:
        conn = get_db()
    try:
        payload = {"code": cfg["code"], "form": cfg["form"]}
        if custom:
            payload["custom"] = custom
        db.set_setting(conn, "currency", json.dumps(payload))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()
    _CUR_CACHE.update({"t": 0.0, "custom": custom})
    _i18n.set_custom_currency(custom)
    return custom


@app.before_request
def _push_currency():
    if not config.DB_PATH:
        return
    try:
        currency_cfg()
    except Exception:
        pass


def can(role, module, action=None):
    """权限判断：数据库权限矩阵优先，未配置时按默认矩阵。
    action=None 表示模块级判断；action 指定时：
      模块级 (role,module,'') allow=1 → 拥有该模块全部操作（优先于操作级）；
      否则需操作级 (role,module,action) allow=1"""
    conn = get_db()
    try:
        row0 = conn.execute(
            "SELECT allow FROM permissions WHERE role=? AND module=? AND action=''",
            (role, module),
        ).fetchone()
        if action is not None:
            # 操作级优先：已配置则按操作级；未配置跟随模块级；模块级未配置回退默认矩阵
            row = conn.execute(
                "SELECT allow FROM permissions WHERE role=? AND module=? AND action=?",
                (role, module, action),
            ).fetchone()
            if row:
                return bool(row["allow"])
            # 老库没有这条操作级记录（例如后来新增的「借用管理」）：
            # 先看角色默认值再退回模块级。否则新增操作会因为「模块级已勾选」
            # 被无差别放开给所有人 —— 权限项一上线就形同虚设。
            _dflt = (config.DEFAULT_PERM_ACTIONS.get(role) or {}).get(module) or {}
            if action in _dflt:
                return bool(_dflt[action])
            if row0:
                return bool(row0["allow"])
            return module in config.DEFAULT_PERMS.get(role, [])
        if row0:
            return bool(row0["allow"])
        # 模块级未配置任何行：回退默认矩阵
        return module in config.DEFAULT_PERMS.get(role, [])
    finally:
        conn.close()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("uid"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def perm_required(module, action=None):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if not can(user["role"], module, action):
                abort(403)
            return f(*args, **kwargs)
        return wrapper
    return deco


def role_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if user["role"] not in roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapper
    return deco


def _brand_localized(brand):
    """品牌名是管理员填的配置内容，默认按原样显示，不随界面语言翻译。

    以前的做法是「只要标题等于默认值就翻译」，想法是让默认品牌名跟着语言走。
    但管理员在设置页点一次保存，数据库里存下的就是和默认值一模一样的字符串，
    从此无法区分「他没改过」和「他就想用这个名字」——结果用户在英文界面下，
    自己填的中文标题被悄悄换成了英文。

    现在改成显式开关（设置页「品牌名随界面语言翻译」），默认关闭；
    打开后才把标题/副标题当界面文案去查词条，查不到仍显示原文。
    """
    b = dict(brand)
    if not b.get("i18n"):
        return b
    for key in ("title", "subtitle"):
        raw = (b.get(key) or "").strip()
        if raw:
            b[key] = _i18n.t(raw, _i18n.current_lang()) or raw
    return b


def _page_key():
    """当前请求对应的页面名，用于前端词条分片。

    构建期把 endpoint / 模板名两种口径都写进了 locale/pages.json，
    所以这里用 request.endpoint 命不中时还能退到模板名。
    """
    try:
        from flask import request
        ep = (request.endpoint or "")
        return ep.split(".")[-1]
    except Exception:
        return ""


@app.context_processor
def inject_globals():
    user = current_user() if session.get("uid") else None
    _lang = _i18n.current_lang()
    return {
        "cur_user": user,
        "cur_lang": _lang,
        "cur_dir": _i18n.text_dir(_lang),
        # 语言选择器（顶栏/登录页都从这里取；只含已发布语言，骨架语言不出现）
        "LANGUAGES": _i18n.LANGUAGES,
        # 前端 toLocaleString 用：以前写死 'zh-CN'，导致英文界面的千分位/小数
        # 仍然按中文习惯排版（1,234,567.89 在德语应为 1.234.567,89）。
        "cur_locale": _i18n.locale_tag(_lang),
        # 数字进位体系：wan=万/亿，short=K/M/B。图表 Y 轴用它，不能只翻后缀。
        "cur_scale": _i18n.num_scale(_lang),
        "ROLE_NAMES": {k: _i18n.t(v, _lang) for k, v in config.ROLE_NAMES.items()},
        "PERM_MODULES": {k: _i18n.t(v, _lang) for k, v in config.PERM_MODULES.items()},
        "PERM_ACTIONS": {m: {a: _i18n.t(al, _lang) for a, al in acts.items()} for m, acts in config.PERM_ACTIONS.items()},
        "SCREEN_MODULES": {k: _i18n.t(v, _lang) for k, v in config.SCREEN_MODULES.items()},
        "APP_PORT": config.PORT,
        # 作者信息（页脚版权与「关于作者」弹窗共用，单一真源在 config.AUTHOR）
        "author": config.AUTHOR,
        "brand": _brand_localized(get_brand()),
        "theme": get_theme(),
        "nav_style": get_nav_style(),
        # 界面说明提示开关：关闭后所有 .ui-hint 元素不显示（系统设置里可切换）
        "ui_hints": get_ui_hints(),
        # 浏览器桌面通知总开关（v1.0.77：系统设置里关掉后不再弹通知，也不再申请授权）
        "notify_browser": int(_settings.load().get("notify_browser", 1) or 0),
        "extra_fields": get_extra_fields(),
        "repair_categories": get_repair_categories(),
        "can": (lambda m, a=None: can(user["role"], m, a)) if user else (lambda m, a=None: False),
        "today": datetime.now().strftime("%Y-%m-%d"),
        "tr": (lambda key, **kw: _i18n.t(key, _i18n.current_lang(), **kw)),
        # 是否真词条：flash 里的整句（参数已代入、已翻译）不该再走一次取词。
        # 取词落空虽然显示效果一样（回退原文），但会被记成「运行期漏词」，
        # 管理员点登记就生成一条僵尸词条 —— 所以先在模板里把它们排除掉。
        "term_known": (lambda key: _i18n.term_known(key)),
        # 标点：{{ mk('（') }} —— 中日全角，其余半角；标点不进词条表
        "mk": (lambda ch: _i18n.mk(ch, _lang)),
        # 复数：{{ tn('ui.count_items', rows|length) }}
        "tn": (lambda key, n, **kw: _i18n.tn(key, n, _i18n.current_lang(), **kw)),
        # 数据枚举 → 本地化显示：{{ label('status', a.status) }}
        "label": (lambda kind, code: _i18n.label(kind, code, _lang)),
        # 本地化数字/日期/金额
        "fmt_num": (lambda v, d=0: _i18n.fmt_num(v, _lang, d)),
        "fmt_date": (lambda v: _i18n.fmt_date(v, _lang)),
        # 金额：币种由 sys_settings.currency 决定，语言只决定分隔符和符号位置
        "fmt_money": (lambda v, d=None: _i18n.fmt_money(v, _lang, d)),
        # 当前币种：{{ CUR.code }} / {{ cur() }} → 「元」或「CNY」
        "CUR": _i18n.get_currency(),
        "cur": (lambda form=None: _i18n.cur_label(_lang, form)),
        # 只给本页用到的词条（分片），而不是把整本词典塞进每个页面
        "tr_json": _i18n.tr_json(_lang, _page_key()),
    }


DEFAULT_BRAND = {
    "logo_file": "",
    "title": "库存管理系统",
    "subtitle": "Inventory Management System",
    "logo_height": 40,        # LOGO 图片高度 px（宽度自适应）
    "title_size": 16,         # 标题字号 px
    "subtitle_size": 11,      # 副标题字号 px
    "title_color": "",        # 空=跟随侧栏文字色
    "subtitle_color": "",     # 空=默认半透明白
    "font_family": "",        # 品牌区字体（空=跟随系统）
    "i18n": False,            # 品牌名是否随界面语言翻译（默认关：这是配置内容）
}


def get_brand(conn=None):
    """读取品牌配置（数据库 sys_settings，管理员在系统设置页配置）"""
    brand = dict(DEFAULT_BRAND)
    if not db_configured():
        return brand
    own = conn is None
    if own:
        conn = get_db()
    try:
        raw = db.get_setting(conn, "brand")
        if raw:
            try:
                d = json.loads(raw)
                if isinstance(d, dict):
                    for k in brand:
                        if d.get(k) is not None and str(d.get(k)) != "":
                            brand[k] = d[k]
            except Exception:
                pass
    finally:
        if own:
            conn.close()
    # 数字/颜色边界校验
    try:
        brand["logo_height"] = max(12, min(int(brand["logo_height"]), 160))
    except (TypeError, ValueError):
        brand["logo_height"] = DEFAULT_BRAND["logo_height"]
    try:
        brand["title_size"] = max(10, min(int(brand["title_size"]), 36))
    except (TypeError, ValueError):
        brand["title_size"] = DEFAULT_BRAND["title_size"]
    try:
        brand["subtitle_size"] = max(8, min(int(brand["subtitle_size"]), 22))
    except (TypeError, ValueError):
        brand["subtitle_size"] = DEFAULT_BRAND["subtitle_size"]
    for k in ("title_color", "subtitle_color"):
        if brand[k] and not _is_hex_color(str(brand[k])):
            brand[k] = ""
    if not isinstance(brand.get("font_family"), str) or len(brand["font_family"]) > 80:
        brand["font_family"] = ""
    return brand


def get_theme(conn=None):
    """读取界面主题配置（管理员可自由配置色系）"""
    theme = dict(config.DEFAULT_THEME)
    if not db_configured():
        return theme
    own = conn is None
    if own:
        conn = get_db()
    try:
        raw = db.get_setting(conn, "theme")
        if raw:
            try:
                d = json.loads(raw)
                if isinstance(d, dict):
                    for k in theme:
                        if d.get(k):
                            theme[k] = d[k]
            except Exception:
                pass
    finally:
        if own:
            conn.close()
    return theme


def get_extra_fields(conn=None):
    """读取自定义表头配置（物料列表额外列），返回 [{key,label},...]"""
    if not db_configured():
        return []
    own = conn is None
    if own:
        conn = get_db()
    try:
        raw = db.get_setting(conn, "extra_fields")
        if raw:
            try:
                d = json.loads(raw)
                if isinstance(d, list):
                    out = []
                    for item in d:
                        if isinstance(item, dict) and item.get("key") and item.get("label"):
                            t = str(item.get("type") or config.DEFAULT_EXTRA_TYPE).strip()
                            if t not in config.EXTRA_FIELD_TYPES:
                                t = config.DEFAULT_EXTRA_TYPE
                            out.append({"key": str(item["key"])[:20],
                                        "label": str(item["label"])[:30],
                                        "type": t})
                    return out[: config.MAX_EXTRA_FIELDS]
            except Exception:
                pass
    finally:
        if own:
            conn.close()
    return []


def _extra_norm(raw, efs=None):
    """把 parts.extra 归一成 {列key: 值}。

    历史坑：早期 Excel 导入按「列名」当 key 写库（`{"每组数量": "20"}`），
    而列表、详情、编辑弹窗一律按列 key（f1/f2…）取值，于是
    「模板里明明填了，导入后哪儿都看不到」。这里按 key 优先、列名兜底补齐。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except (TypeError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        return {}
    if efs is None:
        efs = get_extra_fields()
    out = {}
    for ef in efs:
        k, lab = ef["key"], ef["label"]
        if k in raw:
            out[k] = raw[k]
        elif lab in raw:
            out[k] = raw[lab]
    # 没有对应列的旧键原样留着：保存时不能把不认识的历史字段抹掉
    for k, v in raw.items():
        out.setdefault(k, v)
    return out


def get_part_categories(conn=None):
    """读取物料分类字典（系统设置页可维护）。未配置 → 用出厂值。"""
    if not db_configured():
        return list(config.DEFAULT_PART_CATEGORIES)
    own = conn is None
    if own:
        conn = get_db()
    try:
        raw = db.get_setting(conn, "part_categories")
        if raw:
            try:
                d = json.loads(raw)
                if isinstance(d, list):
                    out = []
                    limit = category_max_len(conn)
                    for x in d:
                        s = str(x).strip()
                        if s and s not in out:
                            out.append(s[:limit])
                    if out:
                        return out[: config.MAX_CATEGORIES]
            except Exception:
                pass
    finally:
        if own:
            conn.close()
    return list(config.DEFAULT_PART_CATEGORIES)


def save_part_categories(conn, cats, rejects=None):
    """写回分类字典：去空、去重、限长、限量。返回实际落库的列表。

    rejects 传入一个 list 时，超长分类会被**丢掉**并记进 rejects（设置页用：
    用户手输的分类名超长要明确告诉他被拒绝，不能悄悄截成半截存进去）；
    不传则退化成截断 —— _ensure_category 那种「顺着用户操作补字典」的场景
    不该因为一个超长值就丢掉整条建档。
    """
    out = []
    limit = category_max_len(conn)
    for x in cats:
        s = str(x or "").strip()
        if not s:
            continue
        if len(s) > limit:
            if rejects is not None:
                rejects.append((s, limit))
                continue
            s = s[:limit]
        if s in out:
            continue
        out.append(s)
        if len(out) >= config.MAX_CATEGORIES:
            break
    db.set_setting(conn, "part_categories", json.dumps(out, ensure_ascii=False))
    return out


def _ensure_category(conn, cat):
    """建档/编辑/导入遇到字典外的分类：自动补进字典，不阻断用户操作。
    返回值原样返回，方便直接写库。"""
    cat = (cat or "").strip()
    if not cat:
        return ""
    cat = cat[: category_max_len(conn)]
    if cat not in get_part_categories(conn):
        save_part_categories(conn, get_part_categories(conn) + [cat])
    return cat


def get_head_clip(conn=None):
    """表头设置：每列最多显示几个字符。

    返回 {"std": {code,name,category,spec,locs,apps}, "extra": {f1..f8}}，
    单位 = 字符（1 个中文算 1 个）。没配置的列用 config.DEFAULT_HEAD_CLIP。
    """
    std = {k: config.DEFAULT_HEAD_CLIP for k, _ in config.STD_TEXT_COLS}
    extra = {f"f{i}": config.DEFAULT_HEAD_CLIP for i in range(1, config.MAX_EXTRA_FIELDS + 1)}
    out = {"std": std, "extra": extra}
    if not db_configured():
        return out
    own = conn is None
    if own:
        conn = get_db()
    try:
        raw = db.get_setting(conn, "head_clip")
        if raw:
            try:
                d = json.loads(raw)
                if isinstance(d, dict):
                    for grp in ("std", "extra"):
                        vals = d.get(grp)
                        if not isinstance(vals, dict):
                            continue
                        for k, v in vals.items():
                            if k in out[grp]:
                                try:
                                    out[grp][k] = max(config.HEAD_CLIP_MIN,
                                                      min(config.HEAD_CLIP_MAX, int(v)))
                                except (TypeError, ValueError):
                                    continue
            except Exception:
                pass
    finally:
        if own:
            conn.close()
    return out


def part_text_limits(conn=None):
    """标准表头各文本列的录入上限（字符数），与系统设置「表头设置」同一份配置。"""
    return get_head_clip(conn)["std"]


# 标准表头里「用户真的手输」的列。存放库位、适用产线/设备是系统按选择生成的，
# 用户没有输入框，自然也谈不上超长。
PART_TEXT_INPUTS = (("part_code", "料号"), ("name", "名称"),
                    ("category", "分类"), ("spec", "规格型号"))


def _too_long_errs(f, conn=None, old=None):
    """建档/编辑时按表头设置校验标准表头的文本列，超长返回 [(label, 现长, 上限)]。

    以前这里只截断显示：填 300 字也能存进去，列表只露 60 字，看着像数据被吃掉。
    现在录入端直接拒绝。old 传旧值 —— 编辑时「值没变」的列跳过，免得历史脏数据
    因为改了别的字段就整条存不回去。
    """
    lim = part_text_limits(conn)
    out = []
    for field, label in PART_TEXT_INPUTS:
        v = (f.get(field) or "").strip()
        key = "code" if field == "part_code" else field
        n = lim.get(key, config.DEFAULT_HEAD_CLIP)
        if not v or len(v) <= n:
            continue
        if old is not None and str(old.get(key) or "") == v:
            continue
        out.append((label, len(v), n))
    return out


def _too_long_msg(errs, translate=True):
    """把第一处超长拼成一句可翻译的提示"""
    label, cur, lim = errs[0]
    lab = _i18n.t(label, _i18n.current_lang()) if translate else label
    return _xl("「{label}」最多 {n} 个字符，当前 {m} 个，请删短后再保存",
               label=lab, n=lim, m=cur)


def category_max_len(conn=None):
    """单个分类名的字符上限 —— 跟随「表头设置」里标准表头「分类」那一列。

    以前写死 20 字，而同一张设置页的分类列又写着 60 字符，两处口径互相打架。
    现在统一：标准表头「分类」填多少，分类名就最多多少字（列表显示、筛选、
    字典保存、Excel 导入截断全用这一个数）。读不到配置时退回
    config.MAX_CATEGORY_LEN（也是 60）。
    """
    try:
        n = int(get_head_clip(conn)["std"].get("category") or config.MAX_CATEGORY_LEN)
    except Exception:
        n = config.MAX_CATEGORY_LEN
    return max(config.HEAD_CLIP_MIN, min(config.HEAD_CLIP_MAX, n))


def clip_disp(text, n=None):
    """按字符数截断，超出加省略号。1 个中文、1 个英文都算 1 个字符。"""
    if text is None:
        return ""
    s = str(text)
    try:
        limit = int(n)
    except (TypeError, ValueError):
        limit = config.DEFAULT_HEAD_CLIP
    if limit <= 0:
        return s
    return s if len(s) <= limit else s[:limit] + "…"


@app.template_filter("clip")
def _tpl_clip(text, n=None):
    return clip_disp(text, n)


def get_nav_style(conn=None):
    """读取左侧菜单外观配置（系统设置页可配置字号/图标大小/侧栏宽度/字体等）"""
    nav = dict(config.DEFAULT_NAV_STYLE)
    if not db_configured():
        return nav
    own = conn is None
    if own:
        conn = get_db()
    try:
        raw = db.get_setting(conn, "nav_style")
        if raw:
            try:
                d = json.loads(raw)
                if isinstance(d, dict):
                    for k in nav:
                        if d.get(k) is not None and str(d.get(k)) != "":
                            nav[k] = d[k]
            except Exception:
                pass
    finally:
        if own:
            conn.close()
    return nav


def get_ui_hints(conn=None):
    """界面说明提示是否显示（系统设置里可一键隐藏；默认显示）

    说明类元素在模板里统一带 class="ui-hint"，body 带 no-hints 时由 CSS 隐藏。
    老库没有这个设置项，按显示处理，升级后行为不变。
    """
    if not db_configured():
        return True
    own = conn is None
    if own:
        conn = get_db()
    try:
        raw = db.get_setting(conn, "ui_hints")
    finally:
        if own:
            conn.close()
    return str(raw or "").strip() not in ("0", "false", "off", "no")


# v1.0.19：维修品类型出厂字典（二级：一级类目 → 二级词条）。
# 老版本该设置项默认为空，导致维修下拉没有内容；这里直接给一套可改的出厂值。
DEFAULT_REPAIR_CATEGORIES = [
    {"group": "电气类", "items": ["传感器", "PLC", "电源/开关", "电机/马达/泵",
                                  "控制板卡", "接触器/继电器", "其它电器类"]},
    {"group": "机械类", "items": ["气缸/油缸", "电磁阀/调压阀/液压阀", "切刀",
                                  "齿轮/轴承/丝杆", "其它机械类"]},
    {"group": "工装工具类", "items": ["工具", "工装治具", "测试仪器", "其它"]},
]


def _normalize_repair_categories(raw):
    """把设置项归一化成 [{group, items}]；兼容 v1.0.16 的扁平字符串数组。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    if not isinstance(raw, list):
        raw = []
    flat, groups = [], []
    for x in raw:
        if isinstance(x, dict):
            name = str(x.get("group") or x.get("name") or "").strip()
            items = x.get("items") or []
            if not isinstance(items, list):
                items = []
            clean = []
            for it in items:
                s = str(it).strip()
                if s and s not in clean:
                    clean.append(s)
            if name:
                groups.append({"group": name, "items": clean})
        else:
            s = str(x).strip()
            if s and s not in flat:
                flat.append(s)
    if flat:
        groups.insert(0, {"group": _i18n.t("未分组", _i18n.current_lang()), "items": flat})
    # v1.0.20 护栏：不设上限的话，误粘贴几万条会把 settings.json 撑到几 MB，
    # 而每个页面都 `| tojson` 内联整份字典 → 打开任何页面都被拖死。
    # 30 组 × 200 条 = 最多 6000，实际按总条数 1000 收口（下拉一次也只显示命中前 50）。
    LIM_G, LIM_I, LIM_T, LIM_L = 30, 200, 1000, 40
    out, seen, total = [], set(), 0
    for g in groups[:LIM_G]:
        name = str(g.get("group") or "").strip()[:LIM_L]
        clean = []
        for it in (g.get("items") or []):
            s = str(it).strip()[:LIM_L]
            if not s or s in seen:
                continue
            seen.add(s)
            clean.append(s)
            total += 1
            if len(clean) >= LIM_I or total >= LIM_T:
                break
        if name and clean:
            out.append({"group": name, "items": clean})
        if total >= LIM_T:
            break
    return out


def get_repair_categories(conn=None):
    """维修品类型字典（二级分组）：系统设置「表头设置」里可增删改，维修申请按 optgroup 下拉选。

    老库没有这个设置项时返回出厂字典，避免维修品类型下拉空白。
    """
    if not db_configured():
        return json.loads(json.dumps(DEFAULT_REPAIR_CATEGORIES, ensure_ascii=False))
    own = conn is None
    if own:
        conn = get_db()
    try:
        raw = db.get_setting(conn, "repair_categories")
    finally:
        if own:
            conn.close()
    if raw is None or str(raw).strip() in ("", "[]"):
        return json.loads(json.dumps(DEFAULT_REPAIR_CATEGORIES, ensure_ascii=False))
    return _normalize_repair_categories(raw)


def flat_repair_categories(cats=None):
    """把二级字典摊平成词条列表（老回归的统计/校验口径用，不改变存储结构）。

    v1.0.77 曾作为零引用死代码删除，但 check_v1019 用它核对字典摊平口径；
    作为测试兼容入口保留，业务路径不调用。
    """
    out = []
    for g in (cats if cats is not None else get_repair_categories()):
        for it in (g.get("items") or []):
            s = str(it).strip()
            if s and s not in out:
                out.append(s)
    return out




@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403,
                           msg=_xl("您没有权限执行此操作，请联系高级管理人员配置权限。")), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, msg=_xl("页面不存在。")), 404


# v1.0.26：数据库 / 共享盘异常统一兜底。
# 以前只有 403/404 handler，SQLite 的 OperationalError 会冒泡成 Flask 默认 500
# 通用页；断网时用户只看到白板，运维也只能翻各机本地日志。
@app.errorhandler(500)
@app.errorhandler(Exception)
def _unexpected(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    import share_guard
    _log = logging.getLogger("main")
    _ajax = (request.path.startswith("/api/") or request.is_json or
             request.headers.get("X-Requested-With") == "XMLHttpRequest")
    if isinstance(e, share_guard.ShareUnavailable):
        _log.warning("写入被拒绝（%s）：%s", e.kind, e.message)
        if _ajax:
            return jsonify(ok=False, readonly=True, kind=e.kind, msg=e.message), 409
        flash(e.message, "warning")
        return redirect(request.referrer or url_for("dashboard"))
    if isinstance(e, (share_guard.LockBusy, share_guard.LockLost)):
        _log.warning("写入锁冲突：%s", e)
        if _ajax:
            return jsonify(ok=False, busy=True, msg=str(e)), 409
        flash(str(e), "warning")
        return redirect(request.referrer or url_for("dashboard"))
    _log.exception("未处理异常：%s", e)
    _msg = _xl("系统暂时无法完成该操作，请稍后重试；若持续出现请联系管理员查看日志。")
    if _ajax:
        return jsonify(ok=False, msg=_msg), 500
    return render_template("error.html", code=500, msg=_msg), 500


# ------------------------------------------------------------------
# 认证
# ------------------------------------------------------------------
def _clear_session_keep_lang():
    """清会话，但保留「用户自己选的语言」。

    session.clear() 会连语言一起清掉，登录/登出后就弹回系统默认。
    只保留带 LANG_SRC 标记的值：旧版本自动写入、并非用户选择的语言
    不保留，否则那种值会一辈子锁着界面。
    """
    lang = session.get("lang")
    src = session.get(_i18n.LANG_SRC)
    session.clear()
    if lang and src == _i18n.LANG_SRC_VER:
        session["lang"] = lang
        session[_i18n.LANG_SRC] = src


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        # 剔除控制字符：SQLite 比较时会把 "admin\x00" 当 "admin" 匹配，
        # 等于允许在用户名后面挂垃圾串绕过字面校验
        username = "".join(ch for ch in username if ch >= " " and ch != "\x7f")
        password = request.form.get("password", "")
        cs = _login_case_sensitive()
        collate = _username_collate(cs)
        conn = get_db()
        try:
            row = conn.execute(
                f"SELECT * FROM users WHERE username=?{collate} AND active=1", (username,)
            ).fetchone()
        finally:
            conn.close()
        if row and check_password_hash(row["password_hash"], password):
            _clear_session_keep_lang()
            session["uid"] = row["id"]
            session["role"] = row["role"]
            session["name"] = row["display_name"]
            nxt = request.args.get("next", "")
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("dashboard"))
        flash(_i18n.t("用户名或密码错误，或账号已被停用", _i18n.current_lang()), "danger")
    # 未登录时也允许切换界面语言（?lang= 已由 i18n 中间件处理，这里兜底）
    # 注意：以前这里有一句「session 里没语言就把当前语言写进去」，
    # 那等于把浏览器猜出来的语言固化成用户选择，界面从此再切不动，已删除。
    lang = request.args.get("lang", "").strip()
    if lang in _i18n.LANGUAGES:
        _i18n.set_lang(lang, persist=False)
    return render_template("login.html", ad_enabled=_ad_cfg().get("enabled") or False,
                           cur_lang=_i18n.current_lang(), LANGUAGES=_i18n.LANGUAGES)


# ------------------------------------------------------------------
# 登录用户名匹配策略：是否区分大小写（数据库 sys_settings 共享，所有电脑一致）
# 0=不区分（AD 用户输入 ZHANGSAN 也能匹配 zhangsan，默认）；1=区分（SQLite 默认行为）
# 同时作用于：普通账号登录（/login）与 AD 域登录白名单匹配（/login_ad）
# ------------------------------------------------------------------
def _login_case_sensitive(conn=None):
    """读取「用户名匹配是否区分大小写」设置；未配置时默认 0=不区分大小写"""
    if not db_configured():
        return 0
    own = conn is None
    if own:
        conn = get_db()
    try:
        raw = db.get_setting(conn, "login_case_sensitive")
        try:
            return 1 if int(raw) else 0
        except (TypeError, ValueError):
            return 0
    finally:
        if own:
            conn.close()


def _username_collate(cs):
    """根据大小写策略返回 SQL 匹配后缀：区分大小写=精确等值；不区分=COLLATE NOCASE"""
    return "" if cs else " COLLATE NOCASE"


# ------------------------------------------------------------------
# AD 域登录（公司总登录系统验证；验证通过后按用户名匹配白名单，不在则进入访客模式）
# ------------------------------------------------------------------
def _ad_cfg():
    return _settings.load().get("ad") or {}


def _ad_ldap_connect(srv, user, password, timeout, enc):
    """建立并绑定 LDAP 连接：enc=none 明文 / ldaps TLS / starttls 389 升级加密"""
    from ldap3 import Connection
    conn = Connection(srv, user=user, password=password,
                      raise_exceptions=True, receive_timeout=timeout)
    if enc == "starttls":
        conn.open()
        conn.start_tls()
        conn.bind()
    else:
        conn.bind()
    return conn


def _ad_authenticate(username, password):
    """通过 LDAP 连接公司 AD 验证账号密码。返回 (ok, errmsg)
    支持两种绑定方式（软件内配置）：
      direct  = 用户账号直接绑定（user@域名 / 域名\\user）
      search  = 服务账号搜索 + 用户 DN 再绑定（适合禁止用户直绑的域控）
    支持加密方式：明文 389 / LDAPS 636 / StartTLS（389 端口升级加密）"""
    cfg = _ad_cfg()
    if not cfg.get("enabled"):
        return False, "AD 登录未启用"
    server = (cfg.get("server") or "").strip()
    domain = (cfg.get("domain") or "").strip()
    if not server or not domain:
        return False, "AD 配置不完整，请联系管理员在「系统设置 → AD 域登录配置」填写服务器地址与域名"
    if not username or not password:
        return False, "请输入域用户名与密码"
    try:
        port = int(cfg.get("port") or 389)
        timeout = max(3, min(int(cfg.get("timeout") or 10), 60))
    except (TypeError, ValueError):
        port, timeout = 389, 10
    enc = (cfg.get("enc") or "").strip()
    if enc not in ("none", "ldaps", "starttls"):
        enc = "ldaps" if cfg.get("ssl") else "none"   # 兼容旧配置 ssl 布尔
    bind_mode = (cfg.get("bind_mode") or "direct").strip()
    nf = cfg.get("name_format") or "upn"  # upn=user@domain / ntdomain=domain\user
    # 登录账号自动识别：已含 @ 或 \ 则视为完整格式，不再重复拼接
    if nf == "upn":
        user_dn = username if "@" in username else f"{username}@{domain}"
    else:
        user_dn = username if ("\\" in username or "/" in username) else f"{domain}\\{username}"
    try:
        from ldap3 import Server, Connection
        from ldap3.core.exceptions import LDAPInvalidCredentialsResult, LDAPException
        srv = Server(server, port=port, use_ssl=(enc == "ldaps"), get_info="NO_INFO")
        if bind_mode == "search":
            # 方式二：服务账号搜索
            svc_dn = (cfg.get("svc_dn") or "").strip()
            svc_pwd = cfg.get("svc_pwd") or ""
            base_dn = (cfg.get("base_dn") or "").strip()
            attr = (cfg.get("search_attr") or "sAMAccountName").strip()
            if not svc_dn or not base_dn:
                return False, "AD 配置不完整：服务账号搜索模式需要「服务账号 DN、服务账号密码、Base DN、搜索属性」"
            try:
                conn = _ad_ldap_connect(srv, svc_dn, svc_pwd, timeout, enc)
            except LDAPInvalidCredentialsResult:
                return False, "服务账号连接 AD 失败（服务账号或密码错误，请检查系统设置）"
            try:
                import ldap3
                esc = username.replace("\\", r"\\").replace("*", r"\*") \
                              .replace("(", r"\(").replace(")", r"\)")
                conn.search(search_base=base_dn, search_filter=f"({attr}={esc})",
                            attributes=["distinguishedName"], size_limit=2)
                entries = conn.entries
                if not entries:
                    return False, _xl("在 AD 中未找到该账号（请检查 Base DN 与搜索属性 {attr} 是否正确）", attr=attr)
                user_dn = entries[0].entry_dn
                try:
                    conn.unbind()
                except Exception:
                    pass
                conn = _ad_ldap_connect(srv, user_dn, password, timeout, enc)
                try:
                    conn.unbind()
                except Exception:
                    pass
                return True, ""
            except LDAPInvalidCredentialsResult:
                return False, "域账号或密码错误"
            except LDAPException as e:
                return False, _xl("AD 搜索/验证失败，请检查系统设置：{err}", err=str(e)[:120])
        # 方式一：直接绑定
        try:
            conn = _ad_ldap_connect(srv, user_dn, password, timeout, enc)
            try:
                conn.unbind()
            except Exception:
                pass
            return True, ""
        except LDAPInvalidCredentialsResult:
            return False, "域账号或密码错误"
        except LDAPException as e:
            return False, _xl("无法连接 AD 服务器，请检查系统设置：{err}", err=str(e)[:120])
    except LDAPInvalidCredentialsResult:
        return False, "域账号或密码错误"
    except LDAPException as e:
        return False, _xl("无法连接 AD 服务器，请检查系统设置：{err}", err=str(e)[:120])
    except Exception as e:
        return False, _xl("AD 验证异常：{err}", err=str(e)[:120])


@app.route("/login_ad", methods=["POST"])
def login_ad():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    ok, err = _ad_authenticate(username, password)
    if not ok:
        flash(err, "danger")
        return redirect(url_for("login", tab="ad"))
    # AD 验证通过：按用户名查白名单（本系统 users 表）；不在则进入访客模式
    conn = get_db()
    try:
        cs = _login_case_sensitive(conn)
        collate = _username_collate(cs)
        local_name = username
        # 可配置：AD 登录账号匹配本地白名单时剥离 @域名后缀
        # （用户输 zhangsan@company.com 时，本地白名单通常存 zhangsan）
        if _ad_cfg().get("strip_upn_suffix", 1) and "@" in local_name:
            local_name = local_name.split("@", 1)[0]
        row = conn.execute(
            f"SELECT * FROM users WHERE username=?{collate} AND active=1", (local_name,)
        ).fetchone()
        guest_mode = False
        if not row:
            row = conn.execute(
                "SELECT * FROM users WHERE username='guest' AND active=1"
            ).fetchone()
            guest_mode = True
    finally:
        conn.close()
    if not row:
        flash(_i18n.t("访客账号不可用，请联系系统管理员", _i18n.current_lang()), "danger")
        return redirect(url_for("login", tab="ad"))
    _clear_session_keep_lang()
    session["uid"] = row["id"]
    session["role"] = row["role"]
    session["name"] = row["display_name"]
    if guest_mode:
        session["guest_ad_user"] = username
        flash(_i18n.t("AD 账号 {0} 不在白名单中，已以访客身份进入，仅可查阅物料与呆滞物料", _i18n.current_lang()).format(username), "warning")
        return redirect(url_for("parts_page"))
    flash(_xl("AD 登录成功，欢迎 {p1}", p1=row['display_name']), "success")
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    _clear_session_keep_lang()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        old = request.form.get("old_pwd", "")
        new1 = request.form.get("new_pwd", "")
        new2 = request.form.get("new_pwd2", "")
        user = current_user()
        if not check_password_hash(user["password_hash"], old):
            flash(_i18n.t("原密码不正确", _i18n.current_lang()), "danger")
        elif len(new1) < 6:
            flash(_i18n.t("新密码长度至少 6 位", _i18n.current_lang()), "danger")
        elif new1 != new2:
            flash(_i18n.t("两次输入的新密码不一致", _i18n.current_lang()), "danger")
        else:
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE users SET password_hash=? WHERE id=?",
                    (generate_password_hash(new1), user["id"]),
                )
                conn.commit()
            finally:
                conn.close()
            flash(_i18n.t("密码修改成功", _i18n.current_lang()), "success")
            return redirect(url_for("dashboard"))
    return render_template("password.html")


# ------------------------------------------------------------------
# 数据图表主页
# ------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
@perm_required("dashboard")
def dashboard():
    # v1.0.41：首页要展示「当期延期原因」。口径直接复用盘点看板的周期序列，
    # 避免首页另算一套 late/missed 规则导致「图上没延期、首页说延期」。
    stk_delay = {"late": False, "rows": [], "cycle": "", "state": ""}
    try:
        conn = get_db()
        try:
            _days, _tol, _anchor = _stk_cycle_cfg(conn)
            # v1.0.45：看板窗口右移后最右列固定是「下一期」，首页延期面板要当期——
            # 取 2 个节点（当期+下期），再按 current 标记挑当期，不依赖节点顺序。
            _series = _stk_cycle_series(conn, _days, nodes=2)
            _tl = _series.get("timeline") or []
            _cur = next((t for t in _tl if t.get("current")), (_tl[0] if _tl else {}))
            _key = _cur.get("cycle_key") or ""
            _rows = []
            if _key:
                try:
                    _rows = [dict(r) for r in conn.execute(
                        """SELECT id, no, delay_reason, done_at FROM stocktakes
                           WHERE cycle_key=? AND COALESCE(delay_reason,'')<>'' ORDER BY id DESC""",
                        (_key,)).fetchall()]
                except Exception:
                    _rows = []
            stk_delay = {"late": _cur.get("state") in ("late", "missed"),
                         "rows": _rows,
                         "cycle": _cur.get("plan_base") or _key,
                         "state": _cur.get("state") or ""}
        finally:
            conn.close()
    except Exception:
        pass  # 初始化/老库缺列时首页照常打开，只是不显示延期块
    return render_template("dashboard.html", stk_delay=stk_delay)


# ------------------------------------------------------------------
# v1.0.15 审批单：出库 / 盘点待办的**唯一可见范围**
# 角标与页面必须调用同一组函数，否则又会出现「菜单有红点、点进去空列表」。
# ------------------------------------------------------------------
_OUT_APPROVAL_KIND = (
    "a.app_type='out' AND (p.is_valuable=1 OR "
    "IFNULL(a.out_type,'') IN ('return','exchange','internal','giveback','maintenance','verify','scrap'))")


def _approval_out_can_act(row, user):
    """这一行出库申请当前用户能不能批（管理员可代批全站）。"""
    if user["role"] == "admin":
        return True
    if row["status"] == "pending":
        return row["assigned_approver_id"] == user["id"]
    if row["status"] == "pending2":
        return row["approver2_id"] == user["id"]
    return False


def _approval_out_rows(conn, user):
    """待审出库申请行。admin 看全站；其他人只看指派给自己对应阶段的。
    v1.0.51：纸档单须申请人点「提交审批」后才进待审（doc_submitted=1）。"""
    where = (_OUT_APPROVAL_KIND + " AND a.status IN ('pending','pending2')"
             + " AND (IFNULL(a.attachment_required,0)=0 OR IFNULL(a.doc_submitted,1)=1)")
    args = []
    if user["role"] != "admin":
        where += (" AND ((a.status='pending' AND a.assigned_approver_id=?)"
                  " OR (a.status='pending2' AND a.approver2_id=?))")
        args = [user["id"], user["id"]]
    rows = conn.execute(
        """SELECT a.*, p.part_code, p.name, p.spec, p.unit_price, p.is_valuable,
                  u.display_name app_name, u.department app_dept,
                  ua.display_name assignee_name, ub.display_name approver2_name,
                  CASE WHEN l.bin='' THEN l.warehouse||'-'||l.rack||'-'||l.shelf
                       ELSE l.warehouse||'-'||l.rack||'-'||l.shelf||'-'||l.bin END AS location_text
           FROM applications a
           JOIN parts p ON a.part_id=p.id
           LEFT JOIN locations l ON p.location_id=l.id
           LEFT JOIN users u ON a.applicant_id=u.id
           LEFT JOIN users ua ON a.assigned_approver_id=ua.id
           LEFT JOIN users ub ON a.approver2_id=ub.id
           WHERE %s ORDER BY a.id DESC LIMIT 500""" % where, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["assignee_name"] = (d["approver2_name"] if d["status"] == "pending2"
                              else d["assignee_name"]) or ""
        d["can_act"] = _approval_out_can_act(d, user)
        out.append(d)
    return out


def _approval_stk_can_act(conn, d, user):
    """盘点单当前环节能不能批：口径与 stocktake_approve() 完全一致。"""
    if user["role"] == "admin":
        return True
    assigned_id = d["assigned_id"]
    valid = False
    if assigned_id:
        u = conn.execute("SELECT active FROM users WHERE id=?", (assigned_id,)).fetchone()
        valid = bool(u and u["active"])
    if valid:
        return user["id"] == assigned_id
    perm = "approve1" if d["level"] == 1 else "approve2"
    if not can(user["role"], "stocktake", perm):
        return False
    return (user.get("department") or "") == (d["created_dep"] or "")


def _approval_stk_rows(conn, user):
    """待审盘点单。admin 看全站；其他人只列自己能批的（指派即授权）。"""
    rows = conn.execute(
        """SELECT s.*, u.display_name created_name, u.department created_dep,
                  u1.display_name ap1_name, u2.display_name ap2_name
           FROM stocktakes s
           LEFT JOIN users u ON u.id=s.created_by_id
           LEFT JOIN users u1 ON u1.id=s.approver1_id
           LEFT JOIN users u2 ON u2.id=s.approver2_id
           WHERE s.status IN ('pending1','pending2')
           ORDER BY s.id DESC LIMIT 500""").fetchall()
    diffs = {}
    for r in conn.execute(
            """SELECT it.stocktake_id sid, COUNT(*) n,
                      COALESCE(SUM(ABS(it.diff)*COALESCE(p.unit_price,0)),0) amt
               FROM stocktake_items it LEFT JOIN parts p ON p.id=it.part_id
               WHERE COALESCE(it.diff,0)<>0 GROUP BY it.stocktake_id"""):
        diffs[r["sid"]] = (r["n"], r["amt"])
    out = []
    for r in rows:
        d = dict(r)
        d["level"] = 1 if d["status"] == "pending1" else 2
        d["assigned_id"] = d["approver1_id"] if d["level"] == 1 else d["approver2_id"]
        d["assigned_name"] = (d["ap1_name"] if d["level"] == 1 else d["ap2_name"]) or ""
        d["can_act"] = _approval_stk_can_act(conn, d, user)
        if user["role"] != "admin" and not d["can_act"]:
            continue
        d["diff_rows"], d["diff_amt"] = diffs.get(d["id"], (0, 0))
        d["scope_disp"] = _stk_scope_display(d)
        d["cause"] = _rowv(d, "cause")
        d["action"] = _rowv(d, "action")
        out.append(d)
    return out


@app.route("/api/pending_counts")
@login_required
def api_pending_counts():
    """导航待办动态提示：按当前用户的权限/指派返回各模块待办数"""
    user = current_user()
    conn = get_db()
    try:
        cnt = {"approval": 0, "apply": 0, "stocktake": 0, "inout": 0, "borrow": 0}
        # 1) 审批单角标 = 出库待办 + 盘点待办，与 /approval 页面同源
        #    v1.0.48：出库侧走 _approval_out_docs（纸档未提单不计入），列表与角标一致
        cnt["approval"] = (len(_approval_out_docs(conn, user))
                           + len(_approval_stk_rows(conn, user)))
        # 2) 我的申请单（只统计本人发起、尚未办结的单据）。
        #    以前 admin 统计全站 pending，但「出入库调库申请」页面只列本人记录，
        #    于是出现「菜单有红点、点进去空列表」。待执行的申请应归到「出入库管理」。
        if can(user["role"], "apply"):
            cnt["apply"] = conn.execute(
                """SELECT COUNT(*) c FROM applications
                   WHERE applicant_id=? AND status IN ('pending','pending2','approved')""",
                (user["id"],),
            ).fetchone()["c"]
        # 2b) 出入库管理：待执行的申请（仓管/管理员实际要处理的），含普通出入库、调库与借用借出。
        #     口径必须和「出入库作业台」的待处理列表完全一致（_INOUT_READY_WHERE）：
        #     只算 pending 的话，「撤销待审批」「已批准待出库」这两种单子在页面上排着队，
        #     菜单却没有红点 —— 仓管看到的就是「有单子但没提示」。
        #     反过来，还在审批流程里（已指派审批人）的单据不该提示仓管：还没轮到他们。
        if can(user["role"], "stock_inout") and can(user["role"], "stock_inout", "execute"):
            cnt["inout"] = conn.execute(
                "SELECT COUNT(*) c FROM applications a WHERE " + _INOUT_READY_WHERE
            ).fetchone()["c"]
        # 2c) 借·换·检·修角标 = 预警中 + 已超期（不是全部未还回）。
        # v1.0.42：以前红点是「未还回总数」，几十条正常借出不该一直亮红；
        # 现场要的是「有几条要催还」，口径与台账页统计卡 stWarning / stOverdue 完全同源
        # （_borrow_color：黄色=达到 80% 期限、红色=已超预计归还）。
        if can(user["role"], "borrow"):
            if can(user["role"], "borrow", "manage"):
                brs = conn.execute(
                    "SELECT status,borrowed_at,expected_return,confirmed_at FROM borrows "
                    "WHERE status='borrowed'").fetchall()
            else:
                brs = conn.execute(
                    "SELECT status,borrowed_at,expected_return,confirmed_at FROM borrows "
                    "WHERE status='borrowed' AND borrower_user_id=?", (user["id"],)).fetchall()
            _now = datetime.now()
            cnt["borrow"] = sum(
                1 for b in brs
                if _borrow_color(b["status"], b["borrowed_at"], b["expected_return"],
                                 _now, b["confirmed_at"]) in ("yellow", "red"))
        # 3) v1.0.17：盘点菜单角标 = 未完成的改善行动任务数。
        #    口径与 /stocktakes/reports 的任务监控完全同源（已归档单 + 有改善处理
        #    + 未完成），点进去必然看得到，不会「有红点、空列表」。
        #    盘点审批仍在「审批单」页，不在这里重复计数。
        if can(user["role"], "stocktake"):
            cnt["stocktake"] = conn.execute(
                """SELECT COUNT(*) c FROM stocktake_items i
                   JOIN stocktakes s ON s.id=i.stocktake_id
                   WHERE s.status='done' AND COALESCE(i.action,'')<>''
                     AND COALESCE(i.action_status,'open')<>'done'"""
            ).fetchone()["c"]
        return jsonify(ok=True, counts=cnt)
    finally:
        conn.close()


@app.route("/api/dashboard")
@login_required
def api_dashboard():
    """按 日/周/月 返回出入库数量与金额 + 首页统计卡片 + 库存预警
    金额为敏感内容：无「物料金额查看」权限的角色，分析视图金额强制隐藏（普通用户默认无权限）"""
    user = current_user()
    period = request.args.get("period", "day")
    conn = get_db()
    try:
        # ---- 汇总卡片 ----
        cards = _period_summary(conn, period)
        # ---- 图表序列 ----
        series = _chart_series(conn, period)
        # ---- 领用趋势（线别/设备/设备类型，与上面同横轴）----
        show_amt = can(user["role"], "price")
        usage = _usage_trend(conn, period, show_amt)
        # ---- 维修趋势（维修子类型的 数量/金额 随时间，与上面同横轴）----
        repair = _repair_trend(conn, period, show_amt)
        # ---- 库存预警 ----
        alerts = _stock_alerts(conn)
        # ---- 分类占比 ----
        _lang = _i18n.current_lang()
        cats = conn.execute(
            """SELECT COALESCE(NULLIF(p.category,''),?) c, COALESCE(SUM(i.quantity*p.unit_price),0) v
               FROM parts p LEFT JOIN inventory i ON p.id=i.part_id
               WHERE p.deleted=0
               GROUP BY c ORDER BY v DESC""",
            (_i18n.t("未分类", _lang),),
        ).fetchall()
        # ---- 呆滞物料（阈值来自系统设置，默认90天）----
        stale = _stale_summary(conn, _stale_days(conn), 10)
        return jsonify({
            "cards": cards,
            "series": series,
            "usage": usage,
            "repair": repair,
            "alerts": alerts,
            "category": [{"name": r["c"], "value": round(r["v"], 2)} for r in cats],
            "stale": stale,
        })
    finally:
        conn.close()


# 图表风格（数据图表主页 5 种预设配色）
DASH_STYLES = {
    "tech":   {"name": "科技蓝",   "bars": ["#2563eb", "#3b82f6", "#60a5fa", "#93c5fd", "#1d4ed8", "#0ea5e9", "#38bdf8"],
               "pie": ["#2563eb", "#22c55e", "#f97316", "#8b5cf6", "#06b6d4", "#ef4444", "#eab308", "#14b8a6", "#f472b6", "#64748b"]},
    "green":  {"name": "墨绿商务", "bars": ["#047857", "#059669", "#10b981", "#34d399", "#065f46", "#14b8a6", "#0d9488"],
               "pie": ["#047857", "#059669", "#10b981", "#34d399", "#065f46", "#14b8a6", "#0d9488", "#84cc16", "#65a30d", "#4d7c0f"]},
    "gold":   {"name": "暗夜金",   "bars": ["#b45309", "#d97706", "#f59e0b", "#fbbf24", "#92400e", "#ea580c", "#fb923c"],
               "pie": ["#b45309", "#d97706", "#f59e0b", "#fbbf24", "#92400e", "#ea580c", "#fb923c", "#eab308", "#ca8a04", "#a16207"]},
    "clean":  {"name": "清新白",   "bars": ["#0284c7", "#0ea5e9", "#38bdf8", "#7dd3fc", "#0369a1", "#22d3ee", "#67e8f9"],
               "pie": ["#0284c7", "#0ea5e9", "#38bdf8", "#7dd3fc", "#0369a1", "#22d3ee", "#67e8f9", "#0891b2", "#06b6d4", "#0e7490"]},
    "orange": {"name": "活力橙",   "bars": ["#ea580c", "#f97316", "#fb923c", "#fdba74", "#c2410c", "#f59e0b", "#fbbf24"],
               "pie": ["#ea580c", "#f97316", "#fb923c", "#fdba74", "#c2410c", "#f59e0b", "#fbbf24", "#ef4444", "#f87171", "#b91c1c"]},
}
DASH_STYLE_ORDER = ["tech", "green", "gold", "clean", "orange"]


def _dash_cfg(conn=None):
    """数据图表主页配置（风格/TopN/图表类型/期间/显示金额），随数据库保存"""
    d = {"style": "tech", "top_n": 10, "chart_type": "bar", "range": "30"}
    try:
        c = conn or get_db()
        raw = db.get_setting(c, "dash_cfg")
        if raw:
            x = json.loads(raw)
            if x.get("style") in DASH_STYLES:
                d["style"] = x["style"]
            try:
                d["top_n"] = max(5, min(int(x.get("top_n") or 10), 200))
            except (TypeError, ValueError):
                pass
            if x.get("chart_type") in ("bar", "line"):
                d["chart_type"] = x["chart_type"]
            if x.get("range") in ("all", "7", "30", "90", "year", "custom"):
                d["range"] = x["range"]
    except Exception:
        pass
    return d


def _wh_prefix(wh):
    """仓库号过滤前缀：WH-01 -> (WH-01, 'WH-01-%') 待定 -> 仅等值"""
    wh = (wh or "").strip()
    if not wh:
        return None
    if wh == "待定":
        return ("待定", None)
    return (wh, wh + "-%")


def _loc_disp(location):
    """库位显示名：未分配库位在库里存的是字面「待定」，输出前按界面语言还原"""
    return _xl("待定") if (location or "").strip() == "待定" else (location or "")


def _loc_wh(location):
    """库位文本 -> 仓库号（库位格式：仓库号-货架号-货位号-格位号，每段可能含连字符）
    如 'WH-01-R-01-S-01-B-01' -> 'WH-01'；旧格式 'WH-01' -> 'WH-01'"""
    loc = (location or "").strip()
    if not loc or loc == "待定":
        return "待定"
    parts = loc.split("-")
    if len(parts) >= 2:
        return parts[0] + "-" + parts[1]
    return parts[0] if parts else loc


def _loc_rack(location):
    """库位文本 -> 货架维度（仓库号-货架号）
    如 'WH-01-R-01-S-01-B-01' -> 'WH-01-R-01'；旧格式 'WH-01' -> 'WH-01'"""
    loc = (location or "").strip()
    if not loc or loc == "待定":
        return "待定"
    parts = loc.split("-")
    if len(parts) >= 4:
        return "-".join(parts[:4])
    if len(parts) >= 2:
        return "-".join(parts[:2])
    return parts[0] if parts else loc


def _loc_dim(location, dim):
    """按维度取库位归属：dim=wh 仓库号 / dim=rack 货架号(仓库-货架)"""
    return _loc_rack(location) if dim == "rack" else _loc_wh(location)


@app.route("/api/analysis/meta")
@login_required
def api_analysis_meta():
    """分析页筛选项：仓库列表 / 物料名称 / 物料编码"""
    conn = get_db()
    try:
        whs = {}
        for r in conn.execute("SELECT DISTINCT location FROM inventory WHERE location IS NOT NULL AND location<>''").fetchall():
            w = _loc_wh(r["location"])
            whs[w] = 1
        for r in conn.execute("SELECT DISTINCT location FROM transactions WHERE location IS NOT NULL AND location<>'' AND location<>'待定'").fetchall():
            w = _loc_wh(r["location"])
            whs[w] = 1
        names = [r["name"] for r in conn.execute(
            "SELECT DISTINCT name FROM parts WHERE name IS NOT NULL AND name<>'' AND deleted=0 ORDER BY name").fetchall()]
        codes = [r["part_code"] for r in conn.execute(
            "SELECT part_code FROM parts WHERE deleted=0 ORDER BY part_code").fetchall()]
        return jsonify({"ok": True,
                        "whs": sorted(whs.keys()),
                        "names": names,
                        "codes": codes})
    finally:
        conn.close()


@app.route("/api/analysis")
@login_required
def api_analysis():
    """库存/入库/出库 分析数据（筛选 + 统计 + 柱/线图 + 饼图 + 明细）"""
    user = current_user()
    tab = request.args.get("tab", "stock")
    wh = (request.args.get("wh") or "").strip()
    name = (request.args.get("name") or "").strip()
    code = (request.args.get("code") or "").strip()
    rng = request.args.get("range", "") or ""
    dim = (request.args.get("dim") or "wh").strip()
    if dim not in ("wh", "rack", "line", "device", "model"):
        dim = "wh"
    gran = (request.args.get("gran") or "none").strip()
    # 30/90：滚动窗口（近 30/90 日）。自然周期（今日/本周/本月）在月初几天常常一笔领用都没有，
    # 图表整片「暂无数据」看着像坏了 —— 分析默认窗口因此用近 30 日。
    if gran not in ("none", "day", "week", "month", "30", "90"):
        gran = "none"
    cfg = _dash_cfg()
    if rng not in ("all", "7", "30", "90", "year", "custom"):
        rng = cfg["range"]
    start = (request.args.get("start") or "").strip()
    end = (request.args.get("end") or "").strip()
    top = cfg["top_n"]
    try:
        top = max(5, min(_int(request.args.get("top") or top), 200))
    except (TypeError, ValueError):
        pass
    chart_type = cfg["chart_type"]
    if request.args.get("chart") in ("bar", "line"):
        chart_type = request.args.get("chart")
    # 金额敏感：金额显示统一由「物料金额查看」权限（系统设置→权限配置）控制，
    # 无权限（普通用户默认无）→ 分析视图金额一律隐藏，不随请求参数变化
    show_amt = can(user["role"], "price")

    conn = get_db()
    try:
        if tab == "stock":
            res = _an_stock(conn, wh, name, code, top, dim, show_amt)
        elif tab in ("uline", "udevice"):
            # 设备分析页也会带上库存页的 dim=wh/rack（同一个下拉框），
            # 直接把 dim 透传下去会让「设备分析」按线体分组 ——
            # 标题也跟着变成「各线体领用数量」。这里只认 model，其余一律 device。
            udim = "line" if tab == "uline" else ("model" if dim == "model" else "device")
            res = _an_usage(conn, udim, rng, top, show_amt, start, end, gran)
        else:
            res = _an_flow(conn, "in" if tab == "in" else "out", wh, name, code, rng, top, dim, show_amt, start, end)
        res["cfg"] = {"style": cfg["style"], "chart_type": chart_type,
                      "show_amount": show_amt, "top_n": top, "range": rng,
                      "dim": dim, "gran": gran}
        return jsonify({"ok": True, **res})
    finally:
        conn.close()


def _an_combo(labels, qname, qdata, aname, adata, show_amt):
    """分析页统一给「数量柱 + 金额线」的组合图数据源。

    以前数量与金额是两张分离的柱状图（上下并排），读的时候要横着来回对同一根柱子，
    而且金额图还会因为量级差被单独缩放，看不出「这一项数量不大但金额很高」
    （夹具/备件尤其明显）。合成一张、数量走左轴金额走右轴才读得出来。
    无金额权限时 amt 直接给 None，前端不画折线也不画右侧刻度。
    """
    return {
        "labels": list(labels),
        "qty": {"name": qname, "data": [round(float(x or 0), 3) for x in qdata]},
        "amt": ({"name": aname, "data": [round(float(x or 0), 2) for x in adata]}
                if show_amt else None),
    }


def _an_stock(conn, wh, name, code, top, dim="wh", show_amt=True):
    sql = """SELECT p.id, p.part_code, p.name, p.spec, p.unit, p.qty_decimal,
                    COALESCE(p.min_stock,0) min_stock, COALESCE(p.unit_price,0) unit_price,
                    COALESCE(SUM(i.quantity),0) stock,
                    COALESCE((SELECT SUM(t.quantity) FROM transactions t WHERE t.part_id=p.id AND t.tx_type='in'),0) in_qty,
                    COALESCE((SELECT SUM(t.quantity) FROM transactions t WHERE t.part_id=p.id AND t.tx_type='out'
                              AND IFNULL(t.out_type,'')<>'stocktake'),0) out_qty
             FROM parts p LEFT JOIN inventory i ON p.id=i.part_id
             WHERE p.deleted=0"""
    params = []
    wp = _wh_prefix(wh)
    if wp:
        if wp[1]:
            sql += " AND EXISTS(SELECT 1 FROM inventory i2 WHERE i2.part_id=p.id AND (i2.location=? OR i2.location LIKE ?))"
            params += [wp[0], wp[1]]
        else:
            sql += " AND EXISTS(SELECT 1 FROM inventory i2 WHERE i2.part_id=p.id AND i2.location=?)"
            params.append(wp[0])
    if name:
        sql += " AND p.name=?"; params.append(name)
    if code:
        sql += " AND p.part_code=?"; params.append(code)
    sql += " GROUP BY p.id ORDER BY stock DESC LIMIT ?"
    params.append(top * 3)
    rows = conn.execute(sql, params).fetchall()

    # 库位/仓库归属（按料号）
    loc_map = {}
    for r in conn.execute("SELECT part_id, location, quantity FROM inventory").fetchall():
        loc_map.setdefault(r["part_id"], []).append((r["location"], r["quantity"]))

    stats = {"cur_stock": 0.0, "safe_stock": 0.0, "part_count": 0, "stock_value": 0.0}
    bar = []
    pie = {}
    pie_amt = {}
    detail = []
    for r in rows:
        p = dict(r)
        stock = p["stock"] or 0
        stats["cur_stock"] += stock
        stats["safe_stock"] += p["min_stock"]
        stats["part_count"] += 1
        stats["stock_value"] += stock * p["unit_price"]
        bar.append({"name": p["part_code"], "label": f"{p['part_code']} {p['name']}",
                    "value": round(stock, 3), "amt": round(stock * p["unit_price"], 2)})
        for loc, q in loc_map.get(p["id"], []):
            w = _loc_dim(loc, dim)
            pie[w] = round(pie.get(w, 0) + (q or 0), 3)
            # 同一张饼分不出「数量多但都是便宜料」还是「数量少但全是贵重件」，
            # 金额占比单独一张饼，两张并排才看得出价值分布
            pie_amt[w] = round(pie_amt.get(w, 0) + (q or 0) * p["unit_price"], 2)
        _tbd = _xl("待定")
        locs = ", ".join("%s(%s)" % (_loc_disp(l), q) for l, q in loc_map.get(p["id"], [])) or _tbd
        detail.append({
            "wh": ", ".join(sorted({_loc_disp(_loc_wh(l)) for l, _ in loc_map.get(p["id"], [])})) or _tbd,
            "locations": locs,
            "part_code": p["part_code"], "name": p["name"], "spec": p["spec"] or "",
            "unit": p["unit"] or "", "in_qty": round(p["in_qty"] or 0, 3),
            "out_qty": round(p["out_qty"] or 0, 3), "safe": p["min_stock"],
            "stock": round(stock, 3), "diff": round(stock - p["min_stock"], 3),
        })
    bar = bar[:top]
    if not show_amt:
        stats["stock_value"] = None
    _lang = _i18n.current_lang()
    combo = _an_combo([b["label"] for b in bar],
                      _i18n.t("当前库存", _lang), [b["value"] for b in bar],
                      _i18n.t("库存金额", _lang), [b["amt"] for b in bar], show_amt)
    return {
        "stats": {k: (round(v, 2) if v is not None else None) for k, v in stats.items()},
        "bar": {"labels": [b["label"] for b in bar],
                "series": [{"name": _i18n.t("当前库存", _lang), "data": [b["value"] for b in bar]}]},
        "combo": combo,
        "pie": [{"name": k, "value": v} for k, v in sorted(pie.items(), key=lambda x: -x[1])],
        "pie_amt": ([{"name": k, "value": v} for k, v in sorted(pie_amt.items(), key=lambda x: -x[1])]
                    if show_amt else None),
        "rows": detail,
        "title": _i18n.t("各产品当前库存数", _lang),
    }


def _an_since(rng, start="", end=""):
    """期间解析：all/7(近一周)/30/90/year/custom(起止 YYYY-MM-DD)。
    返回 (since_s, until_s)，None 表示不限。"""
    now = datetime.now()
    if rng == "7":
        return (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"), None
    if rng == "30":
        return (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"), None
    if rng == "90":
        return (now - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S"), None
    if rng == "year":
        return (now - timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S"), None
    if rng == "custom":
        since = until = None
        s = (start or "").strip()
        e = (end or "").strip()
        try:
            since = datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d 00:00:00")
        except (ValueError, TypeError):
            pass
        try:
            until = datetime.strptime(e, "%Y-%m-%d").strftime("%Y-%m-%d 23:59:59")
        except (ValueError, TypeError):
            pass
        return since, until
    return None, None


def _analysis_detail_sheet(conn, rng, start, end, wh, name, code, show_amt):
    """图表导出用的「明细流水」Sheet：把图上统计值背后的每一笔出入库记录下来。
    图上只有汇总（某天入库 35 件 / 某型号领用 29 件），用户拿表去核账时需要能追到单号、
    物料、库位、申请人、执行人与时间。窗口/仓库/物料筛选与图表完全同一口径。"""
    since_s, until_s = _an_since(rng, start, end)
    sql = ("SELECT t.created_at, t.tx_no, t.tx_type, p.part_code, p.name, p.spec, p.unit, "
           "t.location, t.quantity, t.unit_price, t.amount, t.line_name, t.device_name, "
           "COALESCE(a.display_name,'') applicant, COALESCE(o.display_name,'') operator, t.remark "
           "FROM transactions t JOIN parts p ON t.part_id=p.id "
           "LEFT JOIN users a ON a.id=t.applicant_id "
           "LEFT JOIN users o ON o.id=t.operator_id WHERE 1=1")
    params = []
    if since_s:
        sql += " AND t.created_at>=?"; params.append(since_s)
    if until_s:
        sql += " AND t.created_at<=?"; params.append(until_s)
    wp = _wh_prefix(wh)
    if wp:
        if wp[1]:
            sql += " AND (t.location=? OR t.location LIKE ?)"; params += [wp[0], wp[1]]
        else:
            sql += " AND t.location=?"; params.append(wp[0])
    if name:
        sql += " AND p.name=?"; params.append(name)
    if code:
        sql += " AND p.part_code=?"; params.append(code)
    sql += " ORDER BY t.created_at DESC, t.id DESC LIMIT 50000"
    head = [_xl(h) for h in (["时间", "单号", "类型", "物料编码", "物料名称", "规格型号",
                              "单位", "库位", "数量"] +
                             (["单价(元)", "金额(元)"] if show_amt else []) +
                             ["线别", "设备", "申请人", "执行人", "备注"])]
    type_map = {"in": _xl("入库"), "out": _xl("出库"),
                "transfer_in": _xl("调拨入"), "transfer_out": _xl("调拨出")}
    rows = []
    for r in conn.execute(sql, params).fetchall():
        row = [r["created_at"], r["tx_no"], type_map.get(r["tx_type"], r["tx_type"]),
               r["part_code"], r["name"], r["spec"] or "", r["unit"] or "",
               r["location"] or "", r["quantity"]]
        if show_amt:
            row += [r["unit_price"] or 0, r["amount"] or 0]
        row += [r["line_name"] or "", r["device_name"] or "", r["applicant"] or "",
                r["operator"] or "", r["remark"] or ""]
        rows.append(row)
    if not rows:
        rows.append([_xl("当前筛选条件下没有出入库流水")] + [""] * (len(head) - 1))
    return head, rows




def _an_flow(conn, tx, wh, name, code, rng, top, dim="wh", show_amt=True, start="", end=""):
    since_s, until_s = _an_since(rng, start, end)

    base = " FROM transactions t JOIN parts p ON t.part_id=p.id WHERE t.tx_type=?"
    params = [tx]
    if since_s:
        base += " AND t.created_at>=?"; params.append(since_s)
    if until_s:
        base += " AND t.created_at<=?"; params.append(until_s)
    wp = _wh_prefix(wh)
    if wp:
        if wp[1]:
            base += " AND (t.location=? OR t.location LIKE ?)"; params += [wp[0], wp[1]]
        else:
            base += " AND t.location=?"; params.append(wp[0])
    if name:
        base += " AND p.name=?"; params.append(name)
    if code:
        base += " AND p.part_code=?"; params.append(code)

    stats_row = conn.execute("SELECT COALESCE(SUM(t.quantity),0) q, COALESCE(SUM(t.amount),0) a, "
                             "COUNT(DISTINCT t.tx_no) n" + base, params).fetchone()
    stats = {"qty": round(stats_row["q"] or 0, 3), "amount": round(stats_row["a"] or 0, 2),
             "apps": stats_row["n"] or 0}

    bar_rows = conn.execute("SELECT p.part_code, p.name, SUM(t.quantity) q, SUM(t.amount) a" + base +
                            " GROUP BY p.id ORDER BY q DESC LIMIT ?", params + [top]).fetchall()
    bar = [{"name": r["part_code"], "label": f"{r['part_code']} {r['name']}",
            "value": round(r["q"], 3), "amt": round(r["a"] or 0, 2)} for r in bar_rows]

    pie = {}
    pie_amt = {}
    for r in conn.execute("SELECT t.location, t.quantity, t.amount" + base, params).fetchall():
        w = _loc_dim(r["location"], dim)
        pie[w] = round(pie.get(w, 0) + (r["quantity"] or 0), 3)
        pie_amt[w] = round(pie_amt.get(w, 0) + (r["amount"] or 0), 2)

    rows = conn.execute(
        "SELECT t.tx_no, t.part_id, p.part_code, p.name, t.location, t.quantity, t.amount, "
        "t.created_at, COALESCE(u1.display_name,'') applicant, COALESCE(u2.display_name,'') operator"
        + base.replace("JOIN parts p ON t.part_id=p.id",
                       "JOIN parts p ON t.part_id=p.id "
                       "LEFT JOIN users u1 ON t.applicant_id=u1.id "
                       "LEFT JOIN users u2 ON t.operator_id=u2.id") +
        " ORDER BY t.id DESC LIMIT 500", params).fetchall()
    detail = [{"tx_no": r["tx_no"], "part_code": r["part_code"], "name": r["name"],
               "location": r["location"] or _i18n.t("待定", _i18n.current_lang()), "quantity": round(r["quantity"], 3),
               "amount": (round(r["amount"] or 0, 2) if show_amt else None),
               "created_at": r["created_at"],
               "applicant": r["applicant"], "operator": r["operator"]} for r in rows]
    if not show_amt:
        stats["amount"] = None

    _lang = _i18n.current_lang()
    combo = _an_combo([b["label"] for b in bar],
                      _i18n.t("出库数量" if tx == "out" else "入库数量", _lang),
                      [b["value"] for b in bar],
                      _i18n.t("出库金额" if tx == "out" else "入库金额", _lang),
                      [b["amt"] for b in bar], show_amt)
    return {
        "stats": stats,
        "bar": {"labels": [b["label"] for b in bar],
                "series": [{"name": _i18n.t("出库数量" if tx == "out" else "入库数量", _lang),
                            "data": [b["value"] for b in bar]}]},
        "combo": combo,
        "pie": [{"name": k, "value": v} for k, v in sorted(pie.items(), key=lambda x: -x[1])],
        "pie_amt": ([{"name": k, "value": v} for k, v in sorted(pie_amt.items(), key=lambda x: -x[1])]
                    if show_amt else None),
        "rows": detail,
        "title": _i18n.t("各产品出库数量" if tx == "out" else "各产品入库数量", _lang),
    }


def _an_usage(conn, dim, rng, top, show_amt, start="", end="", gran="none"):
    """按线体/设备/设备型号统计领用（出库）数量与金额
    dim: 'line' 线体 / 'device' 设备名 / 'model' 设备型号
    gran: 'none' 全部期间 / 'day' 今日 / 'week' 本周 / 'month' 本月
          —— 统计窗口走自然周期（与大屏 KPI、总览导出完全同口径），
             柱子始终是各线体/设备，不是时间序列上的桶"""
    _lang = _i18n.current_lang()
    # "未分配" 会作为分组值写进 SQL，必须先本地化再转义（这里都是常量，无注入面）
    _un = _i18n.t("未分配", _lang).replace("'", "''")
    since_s, until_s = _an_since(rng, start, end)
    # gran 作为统计窗口覆盖期间：自然日=今天 0 点 / 自然周=本周一 0 点 / 自然月=本月 1 日 0 点
    # 30/90 是滚动窗口（近 N 日），月初、周一早上一进页面就有数据可看
    if gran in ("day", "week", "month"):
        _win_s, _win_e = _screen_period_window(gran)
        since_s = _win_s.strftime("%Y-%m-%d %H:%M:%S")
        until_s = _win_e.strftime("%Y-%m-%d %H:%M:%S")
    elif gran in ("30", "90"):
        since_s = (datetime.now() - timedelta(days=int(gran))).strftime("%Y-%m-%d %H:%M:%S")
        until_s = None

    if dim == "model":
        # 设备型号：设备配置表 equipment.model；型号为空时回退设备名，仍空显示"未分配"
        col = "COALESCE(NULLIF(e.model,''), NULLIF(t.device_name,''), '%s')" % _un
        join = " LEFT JOIN equipment e ON e.name=t.device_name"
        label = _i18n.t("设备型号", _lang)
    elif dim == "device":
        col = "COALESCE(NULLIF(t.device_name,''),'%s')" % _un
        join = ""
        label = _i18n.t("设备", _lang)
    else:
        col = "COALESCE(NULLIF(t.line_name,''),'%s')" % _un
        join = ""
        label = _i18n.t("线体", _lang)

    where = "t.tx_type='out' AND IFNULL(t.out_type,'')<>'stocktake'"
    params = []
    if since_s:
        where += " AND t.created_at>=?"; params.append(since_s)
    if until_s:
        where += " AND t.created_at<=?"; params.append(until_s)

    stats_row = conn.execute(
        "SELECT COALESCE(SUM(t.quantity),0) q, COALESCE(SUM(t.amount),0) a, COUNT(DISTINCT t.tx_no) n"
        " FROM transactions t" + join + " WHERE " + where, params).fetchone()
    stats = {"qty": round(stats_row["q"] or 0, 3), "amount": round(stats_row["a"] or 0, 2),
             "apps": stats_row["n"] or 0}

    # 分类 TopN（不分组维度时使用）
    rows = conn.execute(
        "SELECT %s d, SUM(t.quantity) q, SUM(t.amount) a FROM transactions t%s WHERE %s"
        " GROUP BY d ORDER BY q DESC LIMIT ?" % (col, join, where),
        params + [top]).fetchall()
    items = [{"name": r["d"], "qty": round(r["q"], 3), "amount": round(r["a"] or 0, 2)} for r in rows]

    # 时间趋势：不再生成时间趋势图（柱子始终按各线体/设备分组，gran 仅作统计窗口）
    trend = {"labels": [], "qty": [], "amt": [], "qty_series": [], "amt_series": []}

    title = _i18n.t("各{name}领用数量", _lang).format(name=label)
    # 金额敏感：无查看金额权限时，金额图与明细金额一律不返回
    amt = None
    if show_amt:
        amt = {"labels": [i["name"] for i in items],
               "series": [{"name": _i18n.t("领用金额", _lang), "data": [i["amount"] for i in items]}]}
    return {
        "stats": {"qty": stats["qty"], "apps": stats["apps"],
                  "amount": stats["amount"] if show_amt else None},
        "bar": {"labels": [i["name"] for i in items],
                "series": [{"name": _i18n.t("领用数量", _lang), "data": [i["qty"] for i in items]}]},
        "combo": _an_combo([i["name"] for i in items],
                           _i18n.t("领用数量", _lang), [i["qty"] for i in items],
                           _i18n.t("领用金额", _lang), [i["amount"] for i in items], show_amt),
        "amt": amt,
        "trend": trend,
        "rows": [{"name": i["name"], "qty": i["qty"],
                  "amount": i["amount"] if show_amt else None} for i in items],
        "title": title,
        "dim_label": label,
    }


def _pareto_window(period, idx):
    """把趋势图上的第 idx 个时间桶换算成 [start, end) 的绝对时间窗。

    柏拉图必须和柱子同一个窗口：图上「第17周」那根柱是 100，弹窗里
    各分类加起来也必须是 100。所以这里直接用 _series_buckets 的 keys
    （和画柱子时同一份），而不是另算一遍日期。
    """
    labels, keys, _s, _e, _b, _n = _series_buckets(period)
    if idx < 0 or idx >= len(keys):
        return None
    k = keys[idx]
    try:
        if period == "month":
            y, m = int(k[:4]), int(k[5:7])
            s = datetime(y, m, 1)
            e = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1)
        elif period == "week":
            s = datetime.strptime(k, "%Y-%m-%d")
            e = s + timedelta(days=7)
        else:
            s = datetime.strptime(k, "%Y-%m-%d")
            e = s + timedelta(days=1)
    except (TypeError, ValueError):
        return None
    return s, e, labels[idx]


_PARETO_BY = {"part": "物料", "line": "线体", "device": "设备", "model": "设备型号"}


def _pareto_items(conn, s, e, dim, pick, by, lang):
    """柏拉图明细：某时间窗内按 by 维度汇总领用数量与金额，按数量降序。

    dim + pick：先在「当前趋势图的维度」上限定范围（下拉选了某条线体就只看这条线体），
    by：再在限定范围内换一个维度拆（默认按物料 —— 「这条线这一周到底领走了什么」）。
    """
    _un = _i18n.t("未分配", lang).replace("'", "''")
    where = "t.tx_type='out' AND IFNULL(t.out_type,'')<>'stocktake' AND t.created_at>=? AND t.created_at<?"
    params = [s.strftime("%Y-%m-%d %H:%M:%S"), e.strftime("%Y-%m-%d %H:%M:%S")]
    joins = []

    def _addj(j):
        """同一个 JOIN 只能加一次：dim=model 且 by=model 时 equipment 会被加两遍，
        SQL 直接报 duplicate alias（界面上表现为柏拉图转圈不返回）。"""
        if j and j not in joins:
            joins.append(j)

    if pick and dim in ("line", "device", "model"):
        for key, col, jn in _USAGE_TREND_DIMS:
            if key == dim:
                _addj(jn)
                where += " AND " + (col % _un) + " = ?"
                params.append(pick)
                break
    if by == "part":
        # 物料编码 + 名称；两者都空（物料被删）时退回「未分配」，不显示空白行
        label_expr = ("COALESCE(NULLIF(TRIM(COALESCE(p.part_code,'') || ' ' ||"
                      " COALESCE(p.name,'')), ''), '%s')" % _un)
        _addj(" LEFT JOIN parts p ON p.id=t.part_id")
        group_by = "t.part_id"
    else:
        label_expr, group_by = "'%s'" % _un, "1"
        for key, col, jn in _USAGE_TREND_DIMS:
            if key == by:
                label_expr = col % _un
                _addj(jn)
                break
    join = "".join(joins)
    rows = conn.execute(
        "SELECT " + label_expr + " nm, COALESCE(SUM(t.quantity),0) q,"
        " COALESCE(SUM(t.amount),0) a"
        " FROM transactions t" + join + " WHERE " + where +
        " GROUP BY " + group_by + " ORDER BY q DESC LIMIT 300", params).fetchall()
    return [{"name": r["nm"] or _un, "qty": round(r["q"] or 0, 3),
             "amount": round(r["a"] or 0, 2)} for r in rows]


def _pareto_detail_sheet(conn, s, e, dim, pick, by, by_label, show_amt, lang):
    """柏拉图导出的「明细流水」Sheet：图上每根柱子背后的每一笔领用。

    图表只有「设备型号 VCM-3 领用 29 件」，用户拿表去核账时必须能追到单号、物料、
    库位、线别设备、申请人与时间。时间窗与 dim/pick 筛选和柏拉图完全同一口径，
    所以「明细的合计」永远等于「柱子上的数字」。
    """
    _un = _i18n.t("未分配", lang).replace("'", "''")
    where = "t.tx_type='out' AND IFNULL(t.out_type,'')<>'stocktake' AND t.created_at>=? AND t.created_at<?"
    params = [s.strftime("%Y-%m-%d %H:%M:%S"), e.strftime("%Y-%m-%d %H:%M:%S")]
    joins = []

    def _addj(j):
        if j and j not in joins:
            joins.append(j)

    if pick and dim in ("line", "device", "model"):
        for key, col, jn in _USAGE_TREND_DIMS:
            if key == dim:
                _addj(jn)
                where += " AND " + (col % _un) + " = ?"
                params.append(pick)
                break
    if by == "part":
        grp = ("COALESCE(NULLIF(TRIM(COALESCE(p.part_code,'') || ' ' ||"
               " COALESCE(p.name,'')), ''), '%s')" % _un)
        # parts 已在下面的基础 SQL 里 JOIN 过（别名 p），这里不能再加一次，
        # 否则 p.part_code 会 ambiguous（by=part 的导出直接 500）
    else:
        grp = "'%s'" % _un
        for key, col, jn in _USAGE_TREND_DIMS:
            if key == by:
                grp = col % _un
                _addj(jn)
                break
    sql = ("SELECT t.created_at, t.tx_no, " + grp + " AS grp, p.part_code, p.name, p.spec,"
           " p.unit, t.location, t.quantity, t.unit_price, t.amount, t.line_name,"
           " t.device_name, COALESCE(a.display_name,'') applicant,"
           " COALESCE(o.display_name,'') operator, t.remark "
           "FROM transactions t LEFT JOIN parts p ON p.id=t.part_id "
           "LEFT JOIN users a ON a.id=t.applicant_id "
           "LEFT JOIN users o ON o.id=t.operator_id" + "".join(joins) +
           " WHERE " + where + " ORDER BY t.created_at DESC, t.id DESC LIMIT 50000")
    head = [_xl(h) for h in (["时间", "单号", by_label, "物料编码", "物料名称", "规格型号",
                              "单位", "库位", "数量"] +
                             (["单价(元)", "金额(元)"] if show_amt else []) +
                             ["线别", "设备", "申请人", "执行人", "备注"])]
    rows = []
    for r in conn.execute(sql, params).fetchall():
        row = [r["created_at"], r["tx_no"], r["grp"] or _un, r["part_code"] or "",
               r["name"] or "", r["spec"] or "", r["unit"] or "", r["location"] or "",
               r["quantity"]]
        if show_amt:
            row += [r["unit_price"] or 0, r["amount"] or 0]
        row += [r["line_name"] or "", r["device_name"] or "", r["applicant"] or "",
                r["operator"] or "", r["remark"] or ""]
        rows.append(row)
    if not rows:
        rows.append([_xl("当前筛选条件下没有出入库流水")] + [""] * (len(head) - 1))
    return head, rows


def _pareto_args():
    """柏拉图两个接口共用的参数解析（校验不过就回落到安全默认值）"""
    period = (request.args.get("period") or "week").strip()
    if period not in ("day", "week", "month"):
        period = "week"
    try:
        idx = _int(request.args.get("idx"))
    except (TypeError, ValueError):
        idx = -1
    dim = (request.args.get("dim") or "line").strip()
    if dim not in ("line", "device", "model"):
        dim = "line"
    by = (request.args.get("by") or "part").strip()
    if by not in ("part", "line", "device", "model"):
        by = "part"
    pick = (request.args.get("pick") or "").strip()
    return period, idx, dim, by, pick


@app.route("/api/analysis/pareto")
@login_required
def api_analysis_pareto():
    """领用趋势点柱子 → 柏拉图数据（数量、金额各一份）

    金额是否返回由「物料金额查看」权限决定，和页面其它金额口径完全一致。
    """
    user = current_user()
    period, idx, dim, by, pick = _pareto_args()
    win = _pareto_window(period, idx)
    if not win:
        return jsonify({"ok": False, "error": "bad bucket"})
    s, e, label = win
    show_amt = can(user["role"], "price")
    lang = _i18n.current_lang()
    conn = get_db()
    try:
        items = _pareto_items(conn, s, e, dim, pick, by, lang)
    finally:
        conn.close()
    if not show_amt:
        for it in items:
            it["amount"] = None
    return jsonify({
        "ok": True, "bucket": label, "scope": pick, "by": by,
        "by_label": _i18n.t(_PARETO_BY.get(by, "物料"), lang),
        "dim_label": _i18n.t({"line": "线体", "device": "设备", "model": "设备型号"}.get(dim, "线体"), lang),
        "items": items, "show_amount": show_amt,
        "start": s.strftime("%Y-%m-%d"), "end": e.strftime("%Y-%m-%d"),
        "period": period,
    })


@app.route("/api/analysis/pareto/export")
@login_required
def api_analysis_pareto_export():
    """导出柏拉图数据：数量、金额各一张 Sheet，含占比与累计占比。

    导出的是**全量分类**（图上只画前 15 项 + 其他），表里要能查到
    「第 37 名的那个物料到底多少」，所以这里不做 TopN 归并。
    """
    user = current_user()
    period, idx, dim, by, pick = _pareto_args()
    win = _pareto_window(period, idx)
    if not win:
        return jsonify({"ok": False, "error": "bad bucket"})
    s, e, label = win
    show_amt = can(user["role"], "price")
    lang = _i18n.current_lang()
    conn = get_db()
    try:
        items = _pareto_items(conn, s, e, dim, pick, by, lang)
        by_label = _i18n.t(_PARETO_BY.get(by, "物料"), lang)

        def build(metric, unit):
            rows = [r for r in items if (r[metric] or 0) > 0]
            rows.sort(key=lambda r: -(r[metric] or 0))
            total = sum(r[metric] or 0 for r in rows) or 1
            out, acc = [], 0.0
            for i, r in enumerate(rows, 1):
                acc += r[metric] or 0
                out.append([i, r["name"], r[metric], round((r[metric] or 0) / total * 100, 2),
                            round(acc / total * 100, 2)])
            return [_xl("序号"), _xl(by_label), _xl(unit), _xl("占比(%)"), _xl("累计占比(%)")], out

        sheets = []
        head_q, rows_q = build("qty", "领用数量(件)")
        sheets.append((_xl("数量柏拉图"), head_q, rows_q))
        if show_amt:
            head_a, rows_a = build("amount", "领用金额(元)")
            sheets.append((_xl("金额柏拉图"), head_a, rows_a))
        # 明细流水放最后：先看结论，再翻原始单据（和「数据图表」主页导出的排法一致）
        d_head, d_rows = _pareto_detail_sheet(conn, s, e, dim, pick, by, by_label, show_amt, lang)
        sheets.append((_xl("明细流水"), d_head, d_rows))
    finally:
        conn.close()
    buf = _export_xlsx_multi(sheets)
    from io import BytesIO
    bio = BytesIO(buf.getvalue())
    bio.seek(0)
    safe = "".join(ch for ch in str(label) if ch not in '\\/:*?"<>|').strip() or "pareto"
    fname = _i18n.t("领用柏拉图_{bucket}_{ts}.xlsx", lang, bucket=_xl(safe),
                    ts=datetime.now().strftime("%Y%m%d_%H%M"))
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/api/analysis/repair/plat")
@login_required
def api_analysis_repair_plat():
    """维修趋势点柱子 → 柏拉图数据：按 维修品类型 与 物料料号 各拆一份（数量、金额各一份）。

    金额是否返回由「物料金额查看」权限决定，和页面其它金额口径完全一致。
    """
    user = current_user()
    period = (request.args.get("period") or "day").strip()
    if period not in ("day", "week", "month"):
        period = "day"
    try:
        idx = _int(request.args.get("idx"))
    except (TypeError, ValueError):
        idx = -1
    win = _pareto_window(period, idx)
    if not win:
        return jsonify({"ok": False, "error": "bad bucket"})
    s, e, label = win
    show_amt = can(user["role"], "price")
    lang = _i18n.current_lang()
    _un = _i18n.t("未分配", lang).replace("'", "''")
    s_s = s.strftime("%Y-%m-%d %H:%M:%S")
    e_s = e.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        # v1.0.45：未修好的还回在柏拉图上单独成柱（类型名后缀），NULL（未还回/老数据）不加后缀
        _unfixed = _i18n.t("（未修好）", lang).replace("'", "''")
        type_rows = conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(bw.repair_type),''), ?) ||"
            " CASE WHEN bw.repair_ok=0 THEN '" + _unfixed + "' ELSE '' END nm,"
            " COALESCE(SUM(bw.qty),0) q,"
            " COALESCE(SUM(bw.qty * COALESCE(p.unit_price,0)),0) a"
            " FROM borrows bw LEFT JOIN parts p ON p.id=bw.part_id"
            " WHERE bw.kind='maintenance' AND bw.created_at>=? AND bw.created_at<?"
            " GROUP BY nm ORDER BY q DESC LIMIT 60", (_un, s_s, e_s)).fetchall()
        code_rows = conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(COALESCE(bw.part_code,'') || ' ' ||"
            " COALESCE(bw.part_name,'')),''), ?) nm,"
            " COALESCE(SUM(bw.qty),0) q,"
            " COALESCE(SUM(bw.qty * COALESCE(p.unit_price,0)),0) a"
            " FROM borrows bw LEFT JOIN parts p ON p.id=bw.part_id"
            " WHERE bw.kind='maintenance' AND bw.created_at>=? AND bw.created_at<?"
            " GROUP BY nm ORDER BY q DESC LIMIT 60", (_un, s_s, e_s)).fetchall()

        def _pac(rows):
            return [{"name": r["nm"] or _un, "qty": round(r["q"] or 0, 3),
                    "amount": round(r["a"] or 0, 2)} for r in rows]
        by_type = _pac(type_rows)
        by_code = _pac(code_rows)
    finally:
        conn.close()
    if not show_amt:
        for it in by_type + by_code:
            it["amount"] = None
    return jsonify({
        "ok": True, "bucket": label,
        "by_type": by_type, "by_code": by_code,
        "show_amount": show_amt,
        "start": s.strftime("%Y-%m-%d"), "end": e.strftime("%Y-%m-%d"),
        "period": period,
    })


@app.route("/api/analysis/repair/export")
@login_required
def api_analysis_repair_export():
    """导出维修柏拉图数据：维修品类型 / 料号 各一张柏拉图 Sheet + 明细流水 Sheet。

    导出全量（不做 TopN 归并），明细流水能追到单号、料号、名称、数量、金额、出库人、去向。
    """
    user = current_user()
    period = (request.args.get("period") or "day").strip()
    if period not in ("day", "week", "month"):
        period = "day"
    try:
        idx = _int(request.args.get("idx"))
    except (TypeError, ValueError):
        idx = -1
    win = _pareto_window(period, idx)
    if not win:
        return jsonify({"ok": False, "error": "bad bucket"})
    s, e, label = win
    show_amt = can(user["role"], "price")
    lang = _i18n.current_lang()
    _un = _i18n.t("未分配", lang).replace("'", "''")
    s_s = s.strftime("%Y-%m-%d %H:%M:%S")
    e_s = e.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        type_rows = conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(bw.repair_type),''), ?) nm,"
            " COALESCE(SUM(bw.qty),0) q,"
            " COALESCE(SUM(bw.qty * COALESCE(p.unit_price,0)),0) a"
            " FROM borrows bw LEFT JOIN parts p ON p.id=bw.part_id"
            " WHERE bw.kind='maintenance' AND bw.created_at>=? AND bw.created_at<?"
            " GROUP BY nm ORDER BY q DESC", (_un, s_s, e_s)).fetchall()
        code_rows = conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(COALESCE(bw.part_code,'') || ' ' ||"
            " COALESCE(bw.part_name,'')),''), ?) nm,"
            " COALESCE(SUM(bw.qty),0) q,"
            " COALESCE(SUM(bw.qty * COALESCE(p.unit_price,0)),0) a"
            " FROM borrows bw LEFT JOIN parts p ON p.id=bw.part_id"
            " WHERE bw.kind='maintenance' AND bw.created_at>=? AND bw.created_at<?"
            " GROUP BY nm ORDER BY q DESC", (_un, s_s, e_s)).fetchall()
        detail = conn.execute(
            "SELECT bw.created_at, bw.borrow_no, COALESCE(NULLIF(TRIM(bw.repair_type),''), ?) rtype,"
            " bw.part_code, bw.part_name, bw.unit, bw.qty,"
            " COALESCE(p.unit_price,0) unit_price,"
            " COALESCE(bw.qty * COALESCE(p.unit_price,0),0) amount,"
            " bw.destination, bw.receiver, bw.out_person, bw.status"
            " FROM borrows bw LEFT JOIN parts p ON p.id=bw.part_id"
            " WHERE bw.kind='maintenance' AND bw.created_at>=? AND bw.created_at<?"
            " ORDER BY bw.created_at DESC, bw.id DESC LIMIT 50000",
            (_un, s_s, e_s)).fetchall()
    finally:
        conn.close()

    def _build(src, metric):
        items = [{"name": r["nm"] or _un, "qty": round(r["q"] or 0, 3),
                 "amount": round(r["a"] or 0, 2)} for r in src]
        if metric == "amt":
            rows = [it for it in items if (it["amount"] or 0) > 0]
            rows.sort(key=lambda it: -(it["amount"] or 0))
        else:
            rows = [it for it in items if (it["qty"] or 0) > 0]
            rows.sort(key=lambda it: -(it["qty"] or 0))
        total = sum(it["amount"] if metric == "amt" else it["qty"] for it in rows) or 1
        out, acc = [], 0.0
        for i, it in enumerate(rows, 1):
            v = it["amount"] if metric == "amt" else it["qty"]
            acc += v
            out.append([i, it["name"], v, round(v / total * 100, 2), round(acc / total * 100, 2)])
        return out

    sheets = []
    sheets.append((_xl("维修品类型数量柏拉图"),
                   [_xl("序号"), _xl("维修品类型"), _xl("维修数量(件)"), _xl("占比(%)"), _xl("累计占比(%)")],
                   _build(type_rows, "qty")))
    sheets.append((_xl("料号数量柏拉图"),
                   [_xl("序号"), _xl("物料(料号 名称)"), _xl("维修数量(件)"), _xl("占比(%)"), _xl("累计占比(%)")],
                   _build(code_rows, "qty")))
    if show_amt:
        sheets.append((_xl("维修品类型金额柏拉图"),
                       [_xl("序号"), _xl("维修品类型"), _xl("维修金额(元)"), _xl("占比(%)"), _xl("累计占比(%)")],
                       _build(type_rows, "amt")))
        sheets.append((_xl("料号金额柏拉图"),
                       [_xl("序号"), _xl("物料(料号 名称)"), _xl("维修金额(元)"), _xl("占比(%)"), _xl("累计占比(%)")],
                       _build(code_rows, "amt")))
    d_head = [_xl(h) for h in (["时间", "维修单号", "维修品类型", "料号", "物料名称", "单位", "数量"] +
                               (["单价(元)", "金额(元)"] if show_amt else []) +
                               ["目的公司/地址", "接收人", "出库人", "状态"])]
    d_rows = []
    for r in detail:
        row = [r["created_at"], r["borrow_no"], r["rtype"] or _un, r["part_code"] or "",
               r["part_name"] or "", r["unit"] or "", r["qty"]]
        if show_amt:
            row += [round(r["unit_price"] or 0, 2), round(r["amount"] or 0, 2)]
        row += [r["destination"] or "", r["receiver"] or "", r["out_person"] or "", r["status"] or ""]
        d_rows.append(row)
    sheets.append((_xl("明细流水"), d_head, d_rows))
    buf = _export_xlsx_multi(sheets)
    from io import BytesIO
    bio = BytesIO(buf.getvalue())
    bio.seek(0)
    safe = "".join(ch for ch in str(label) if ch not in '\\/:*?"<>|').strip() or "repair"
    fname = _i18n.t("维修柏拉图_{bucket}_{ts}.xlsx", lang, bucket=_xl(safe),
                    ts=datetime.now().strftime("%Y%m%d_%H%M"))
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/api/analysis/cfg", methods=["POST"])
@login_required
def api_analysis_cfg():
    """保存数据图表主页配置（风格/TopN/图表类型/期间/显示金额）"""
    d = _dash_cfg()
    f = request.form
    if f.get("style") in DASH_STYLES:
        d["style"] = f["style"]
    try:
        d["top_n"] = max(5, min(_int(f.get("top_n") or d["top_n"]), 200))
    except (TypeError, ValueError):
        pass
    if f.get("chart_type") in ("bar", "line"):
        d["chart_type"] = f["chart_type"]
    if f.get("range") in ("all", "7", "30", "90", "year", "custom"):
        d["range"] = f["range"]
    conn = get_db()
    try:
        db.set_setting(conn, "dash_cfg", json.dumps(d, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "cfg": d})


@app.route("/api/analysis/export")
@login_required
def api_analysis_export():
    """数据图表主页：按当前筛选导出 Excel（统计 + 明细，纯标准库生成）"""
    user = current_user()
    tab = request.args.get("tab", "stock")
    wh = (request.args.get("wh") or "").strip()
    name = (request.args.get("name") or "").strip()
    code = (request.args.get("code") or "").strip()
    rng = request.args.get("range", "") or ""
    dim = (request.args.get("dim") or "wh").strip()
    if dim not in ("wh", "rack", "line", "device", "model"):
        dim = "wh"
    gran = (request.args.get("gran") or "none").strip()
    # 30/90：滚动窗口（近 30/90 日）。自然周期（今日/本周/本月）在月初几天常常一笔领用都没有，
    # 图表整片「暂无数据」看着像坏了 —— 分析默认窗口因此用近 30 日。
    if gran not in ("none", "day", "week", "month", "30", "90"):
        gran = "none"
    cfg = _dash_cfg()
    if rng not in ("all", "7", "30", "90", "year", "custom"):
        rng = cfg["range"]
    start = (request.args.get("start") or "").strip()
    end = (request.args.get("end") or "").strip()
    top = cfg["top_n"]
    try:
        top = max(5, min(_int(request.args.get("top") or top), 200))
    except (TypeError, ValueError):
        pass
    show_amt = can(user["role"], "price")
    _lang = _i18n.current_lang()   # 表头/枚举/文件名都按当前界面语言

    conn = get_db()
    try:
        if tab == "overview":
            return _export_overview(conn, user)
        if tab == "stock":
            res = _an_stock(conn, wh, name, code, top, dim, show_amt)
            no_amt = _xl("（无查看金额权限）")
            stat_rows = [
                [_xl("当前库存数(件)"), res["stats"]["cur_stock"]],
                [_xl("安全库存数下限合计(件)"), res["stats"]["safe_stock"]],
                [_xl("库存料号数(种)"), res["stats"]["part_count"]],
                [_xl("库存金额(元)"), res["stats"]["stock_value"] if show_amt else no_amt],
            ]
            headers = ["仓库", "库位明细", "物料编码", "物料名称", "规格型号", "单位",
                       "入库数量", "领用数量", "安全库存", "当前库存", "与安全库存差值"]
            # 这组表头是当「数据行」塞进 out_rows 的，不会经过 _export_xlsx 的表头翻译
            headers = [_xl(h) for h in headers]
            detail = [[r["wh"], r["locations"], r["part_code"], r["name"], r["spec"],
                       r["unit"], r["in_qty"], r["out_qty"], r["safe"], r["stock"], r["diff"]]
                      for r in res["rows"]]
            # 以前这里漏了生成 buf，直接掉到最后统一 send_file → UnboundLocalError
            out_rows = stat_rows + [[]] + [headers] + detail
            # 任何页签导出都带一份「领用趋势」：用户从哪个入口查数都能看到
            # 线别/设备/设备类型的周期趋势（日/周/月 × 全量分类）
            u_head, u_rows = _usage_trend_sheet_rows(conn, show_amt)
            d_head, d_rows = _analysis_detail_sheet(conn, rng, start, end, wh, name, code, show_amt)
            buf = _export_xlsx_multi([
                ("库存分析", ["指标/列", "数值"], out_rows),
                ("领用趋势", u_head, u_rows),
                ("明细流水", d_head, d_rows),
            ])
        elif tab in ("uline", "udevice"):
            # 设备分析的 dim 参数在库存页可能是 wh/rack，那两个值不在设备维度表里 → KeyError
            udim = "line" if tab == "uline" else ("model" if dim == "model" else "device")
            res = _an_usage(conn, udim, rng, top, show_amt, start, end, gran)
            label = {"line": "线体", "device": "设备", "model": "设备型号"}[udim]
            no_amt = _xl("（无查看金额权限）")
            stat_rows = [
                [_xl("领用数量(件)"), res["stats"]["qty"]],
                [_xl("领用金额(元)"), res["stats"]["amount"] if show_amt else no_amt],
                [_xl("领用单据数(笔)"), res["stats"]["apps"]],
            ]
            headers = [label, "领用数量", "领用金额(元)"] if show_amt else [label, "领用数量"]
            headers = [_xl(h) for h in headers]
            detail = [[r["name"], r["qty"]] + ([r["amount"]] if show_amt else []) for r in res["rows"]]
            out_rows = stat_rows + [[]] + [headers] + detail
            # 周期名只用于文件名：口径已改为自然日/自然周/自然月
            gname = {"day": "日度", "week": "周度", "month": "月度",
                     "30": "近30日", "90": "近90日"}.get(gran)
            # 领用趋势：与总览导出同一口径（日/周/月 × 该维度全量分类）。
            # 以前这里只有「统计与分类」，用户从线别/设备页签导出时拿不到趋势数据；
            # 现在无论从哪个页签导，文件里都带一份「领用趋势」Sheet
            u_head, u_rows = _usage_trend_sheet_rows(conn, show_amt, ((udim, label),))
            d_head, d_rows = _analysis_detail_sheet(conn, rng, start, end, wh, name, code, show_amt)
            buf = _export_xlsx_multi([
                ("统计与分类", ["指标/列", "数值"], out_rows),
                ("领用趋势", u_head, u_rows),
                ("明细流水", d_head, d_rows),
            ])
            if gname:
                from io import BytesIO
                bio = BytesIO(buf.getvalue())
                bio.seek(0)
                fname = _i18n.t("物料{name}分析_{gran}_{ts}.xlsx", _lang,
                                name=_xl(label), gran=_xl(gname),
                                ts=datetime.now().strftime("%Y%m%d_%H%M"))
                return send_file(bio, as_attachment=True, download_name=fname,
                                 mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        else:
            res = _an_flow(conn, "in" if tab == "in" else "out", wh, name, code, rng, top, dim, show_amt, start, end)
            t2 = "入库" if tab == "in" else "出库"
            no_amt = _xl("（无查看金额权限）")
            stat_rows = [
                [_xl(t2 + "数量(件)"), res["stats"]["qty"]],
                [_xl(t2 + "金额(元)"), res["stats"]["amount"] if show_amt else no_amt],
                [_xl(t2 + "单据数(笔)"), res["stats"]["apps"]],
            ]
            headers = ["时间", "单号", "物料编码", "物料名称", "库位", "数量",
                       "金额(元)", "申请人", "执行人"] if show_amt else ["时间", "单号", "物料编码",
                                                                       "物料名称", "库位", "数量", "申请人", "执行人"]
            headers = [_xl(h) for h in headers]
            detail = [[r["created_at"], r["tx_no"], r["part_code"], r["name"], r["location"],
                       r["quantity"]] + ([r["amount"]] if show_amt else []) +
                      [r["applicant"] or "-", r["operator"] or "-"] for r in res["rows"]]
            out_rows = stat_rows + [[]] + [headers] + detail
            # 同库存页签：领用趋势随导出一起给到，避免「换个页签导出就查不到趋势」
            u_head, u_rows = _usage_trend_sheet_rows(conn, show_amt)
            d_head, d_rows = _analysis_detail_sheet(conn, rng, start, end, wh, name, code, show_amt)
            buf = _export_xlsx_multi([
                ("图表数据导出", ["指标/列", "数值"], out_rows),
                ("领用趋势", u_head, u_rows),
                ("明细流水", d_head, d_rows),
            ])
        from io import BytesIO
        bio = BytesIO(buf.getvalue())
        bio.seek(0)
        fname = _i18n.t("物料图表数据_{tab}_{ts}.xlsx", _lang, tab=tab,
                        ts=datetime.now().strftime("%Y%m%d_%H%M"))
        return send_file(bio, as_attachment=True, download_name=fname,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    finally:
        conn.close()


def _usage_trend_sheet_rows(conn, show_amt, dims=(("line", "线别"), ("device", "设备"), ("model", "设备型号"))):
    """「领用趋势」Sheet 的表头与数据行（总览导出与线别/设备页签导出共用同一口径）。
    图上是 Top5 + 其他，导出的却是全量分类 —— 图上只保留需要一眼看到的，
    表里要能给到「某个型号这一周到底领了多少」，所以这里不做归并。
    注意 model 维度取的是设备档案里的 equipment.model（型号），
    以前表头写成「设备类型」，和界面上「设备型号」对不上 —— 同一个数据两个叫法。"""
    u_head = ["维度", "周期", "日期", "分类", "领用数量(件)"] + (
        ["领用金额(元)"] if show_amt else [])
    u_rows = []
    for dkey, dlab in dims:
        for period, plab in (("day", "按日"), ("week", "按周"), ("month", "按月")):
            u = _usage_trend(conn, period, show_amt)
            d = (u.get("dims") or {}).get(dkey) or {}
            qty = d.get("all_qty") or []
            amt = {s["name"]: s["data"] for s in (d.get("all_amt") or [])}
            for s in qty:
                ad = amt.get(s["name"]) or []
                for i, lab in enumerate(u["labels"]):
                    row = [_xl(dlab), _xl(plab), lab, s["name"], s["data"][i]]
                    if show_amt:
                        row.append(ad[i] if i < len(ad) else 0)
                    u_rows.append(row)
    return u_head, u_rows


def _export_overview(conn, user):
    """总览导出：汇总统计 + 出入库趋势 + 库存预警 + 呆滞物料（多 Sheet，纯标准库）"""
    from io import BytesIO
    _lang = _i18n.current_lang()
    show_amt = can(user["role"], "price")

    # Sheet1 汇总统计（日/周/月 当期 vs 上期）
    sheets = []
    head1 = ["指标", "今日", "本周", "本月"]
    rows1 = []
    for period, lab in (("day", "今日"), ("week", "本周"), ("month", "本月")):
        cards = _period_summary(conn, period)
        rows1.append([
            lab + "入库数量(件)", cards["in_qty"], None, None])
    # 转置式：列=今日/本周/本月
    def _pick(field):
        return [_period_summary(conn, p)[field] for p in ("day", "week", "month")]
    rows1 = [
        [_xl("入库数量(件)")] + _pick("in_qty"),
        [_xl("入库金额(元)")] + (_pick("in_amt") if show_amt else [_xl("（无查看金额权限）")] * 3),
        [_xl("出库数量(件)")] + _pick("out_qty"),
        [_xl("出库金额(元)")] + (_pick("out_amt") if show_amt else [_xl("（无查看金额权限）")] * 3),
    ]
    stale = _stale_summary(conn, _stale_days(conn), 10)
    rows1 += [
        [_xl("呆滞料号数(种)"), stale["count"], "", ""],
        [_xl("呆滞库存金额(元)"), stale["amount"] if show_amt else _xl("（无查看金额权限）"), "", ""],
    ]
    sheets.append(("汇总统计", head1, rows1))

    # Sheet2 出入库趋势（日/周/月三个周期并排）
    t_rows = []
    head2 = ["周期", "日期", "入库数量", "出库数量"] + (["入库金额(元)", "出库金额(元)"] if show_amt else [])
    for period, lab in (("day", "按日"), ("week", "按周"), ("month", "按月")):
        s = _chart_series(conn, period)
        for i, d in enumerate(s["labels"]):
            row = [_xl(lab), d, s["in_qty"][i], s["out_qty"][i]] + (
                [s["in_amt"][i], s["out_amt"][i]] if show_amt else [])
            t_rows.append(row)
    sheets.append(("出入库趋势", head2, t_rows))

    # Sheet3 领用趋势（线别/设备/设备类型 × 日/周/月 × 各分类，和总览的图同一口径）
    u_head, u_rows = _usage_trend_sheet_rows(conn, show_amt)
    sheets.append(("领用趋势", u_head, u_rows))

    # Sheet3 库存预警
    alerts = _stock_alerts(conn)
    a_rows = []
    for r in alerts["low"]:
        a_rows.append([_xl("低于下限"), r["code"], r["name"], r["category"], r["stock"], r["limit"]])
    for r in alerts["high"]:
        a_rows.append([_xl("超过上限"), r["code"], r["name"], r["category"], r["stock"], r["limit"]])
    sheets.append(("库存预警", ["类型", "物料编码", "物料名称", "分类", "当前库存", "上下限"], a_rows))

    # Sheet4 呆滞物料 Top10
    s_head = ["物料编码", "物料名称", "单位", "最后领用", "未领用(天)", "库存",
              "库位明细"] + (["库存金额(元)"] if show_amt else [])
    s_rows = []
    for r in stale["top"]:
        row = [r["part_code"], r["name"], r["unit"], r["last_out"] or "-", r["days"],
               r["total_stock"], r["locs"]] + ([r["amount"]] if show_amt else [])
        s_rows.append(row)
    sheets.append(("呆滞物料Top10", s_head, s_rows))

    buf = _export_xlsx_multi(sheets)
    bio = BytesIO(buf.getvalue())
    bio.seek(0)
    fname = _i18n.t("物料总览数据_{ts}.xlsx", _i18n.current_lang(),
                    ts=datetime.now().strftime("%Y%m%d_%H%M"))
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ------------------------------------------------------------------
# 备份恢复（申请 + 审批流）
#   发起：仓库管理人员 / 最高用户(admin)
#   审批：**只有最高用户**（id 最小的启用管理员），且审批时必须手打 approve 确认
#   执行恢复：仓库管理人员 / 最高用户
# ------------------------------------------------------------------


def _top_admin(conn):
    """恢复申请的唯一审批人：最高用户（id 最小的启用管理员）。

    v1.0.7 起不再「自动指派部门经理」。恢复数据库是最高危操作：
    经理既看不出备份里有什么，也承担不了回滚失败的后果；按部门分权在这里
    只会造成「谁都能批、谁都不负责」。改为只由最高用户审批，并在提交审批时
    要求手打 approve（见 restore_approve）—— 一次误点不该能覆盖生产库。
    """
    return conn.execute(
        "SELECT id, display_name FROM users WHERE role='admin' AND active=1 ORDER BY id LIMIT 1"
    ).fetchone()



@app.route("/restore")
@login_required
def restore_page():
    user = current_user()
    if user["role"] not in ("admin", "warehouse", "manager"):
        abort(403)
    conn = get_db()
    try:
        backups = _list_backups()
        my_rows = conn.execute(
            """SELECT r.*, COALESCE(u1.display_name,'') applicant, COALESCE(u2.display_name,'') approver
               FROM restore_apps r
               LEFT JOIN users u1 ON r.applicant_id=u1.id
               LEFT JOIN users u2 ON r.approver_id=u2.id
               WHERE r.applicant_id=? ORDER BY r.id DESC""",
            (user["id"],)).fetchall()
        my_apps = [dict(r) for r in my_rows]
        if user["role"] == "admin":
            todo_rows = conn.execute(
                """SELECT r.*, COALESCE(u1.display_name,'') applicant
                   FROM restore_apps r LEFT JOIN users u1 ON r.applicant_id=u1.id
                   WHERE r.status='pending' ORDER BY r.id DESC""").fetchall()
        else:
            todo_rows = conn.execute(
                """SELECT r.*, COALESCE(u1.display_name,'') applicant
                   FROM restore_apps r LEFT JOIN users u1 ON r.applicant_id=u1.id
                   WHERE r.status='pending' AND r.assigned_approver_id=?
                   ORDER BY r.id DESC""", (user["id"],)).fetchall()
        todo = [dict(r) for r in todo_rows]
        # 恢复备份是最高危操作，审批人只能是最高管理员，其他经理无权审批
        approvers = []
        if user["role"] == "admin":
            approvers = conn.execute(
                """SELECT id, display_name, role, department FROM users
                   WHERE active=1 AND role = 'admin' ORDER BY id""").fetchall()
            approvers = [dict(x) for x in approvers]
        all_rows = conn.execute(
            """SELECT r.*, COALESCE(u1.display_name,'') applicant, COALESCE(u2.display_name,'') approver
               FROM restore_apps r
               LEFT JOIN users u1 ON r.applicant_id=u1.id
               LEFT JOIN users u2 ON r.approver_id=u2.id
               ORDER BY r.id DESC LIMIT 100""").fetchall()
        all_apps = [dict(r) for r in all_rows]
        return render_template("restore.html", backups=backups, my_apps=my_apps,
                               todo=todo, approvers=approvers, all_apps=all_apps,
                               pre_backup=request.args.get("backup", ""),
                               backup=_backup_cfg(conn))
    finally:
        conn.close()


@app.route("/restore/backup", methods=["POST"])
@login_required
def restore_backup():
    """恢复页上的备份操作：保存设置 / 立即备份 / 删除某个备份。

    单独一条路由的意义不只是好看：只要出岔子（浏览器没跑到 JS、请求被拦、
    服务端校验不过），回跳的目标都是 /restore。以前这几个表单一律 POST 到
    /settings，回跳也跟着去 /settings —— 用户在恢复页点「立即备份」，
    结果被甩到系统设置页，还以为备份没做成。
    """
    user = current_user()
    if user["role"] not in ("admin", "warehouse"):
        abort(403)
    if request.form.get("backup_delete"):
        return _backup_delete(endpoint="restore_page")
    if request.form.get("backup_now"):
        return _backup_now(endpoint="restore_page", save_form=True)
    return _save_backup(endpoint="restore_page")


def _backup_file_valid(name, bdir):
    """验证备份文件真实存在（防伪造路径）"""
    if not name or ".." in name or "/" in name or "\\" in name:
        return False
    if bdir and not os.path.isdir(bdir):
        return False
    fp = os.path.join(bdir or "", name)
    return os.path.isfile(fp) and os.path.basename(fp) == name


@app.route("/restore/apply", methods=["POST"])
@login_required
def restore_apply():
    user = current_user()
    if user["role"] not in ("admin", "warehouse"):
        abort(403)
    name = (request.form.get("backup_name") or "").strip()
    bdir = (request.form.get("backup_dir") or "").strip()
    if not _backup_file_valid(name, bdir):
        flash(_i18n.t("备份文件无效或不存在，请从列表中选择", _i18n.current_lang()), "danger")
        return redirect(url_for("restore_page"))
    fp = os.path.join(bdir, name)
    conn = get_db()
    try:
        # 审批人只有一个：最高用户。不论申请人是谁都不再选人/不改派经理
        top = _top_admin(conn)
        if not top:
            flash(_i18n.t("restore.no_top_admin", _i18n.current_lang()), "danger")
            return redirect(url_for("restore_page"))
        assigned = top["id"]
        app_no = db.gen_no("RST", conn, "restore_apps", "app_no")
        size = os.path.getsize(fp) if os.path.isfile(fp) else 0
        conn.execute(
            """INSERT INTO restore_apps(app_no, backup_name, backup_dir, backup_size, applicant_id,
               status, assigned_approver_id, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (app_no, name, bdir, size, user["id"], "pending", assigned, db.now_str()))
        conn.commit()
        flash(_i18n.t("恢复申请 {0} 已提交，等待审批", _i18n.current_lang()).format(app_no), "success")
    finally:
        conn.close()
    return redirect(url_for("restore_page"))


@app.route("/restore/<int:rid>/approve", methods=["POST"])
@login_required
def restore_approve(rid):
    user = current_user()
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM restore_apps WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] != "pending":
            flash(_i18n.t("申请不存在或已处理", _i18n.current_lang()), "danger"); return redirect(url_for("restore_page"))
        # 只有最高用户能批（不认「被指派人」，老库里指派给经理的待办也只有最高用户能处理）
        top = _top_admin(conn)
        if not top or user["id"] != top["id"]:
            abort(403)
        # 手打 approve 才放行：恢复会整库覆盖，误点一次不能算数
        if (request.form.get("confirm_word") or "").strip().lower() != "approve":
            flash(_i18n.t("restore.approve_word_bad", _i18n.current_lang()), "danger")
            return redirect(url_for("restore_page"))
        conn.execute(
            """UPDATE restore_apps SET status='approved', approver_id=?, approval_note=?,
               processed_at=? WHERE id=?""",
            (user["id"], (request.form.get("note") or "").strip()[:200], db.now_str(), rid))
        conn.commit()
        flash(_xl("已通过恢复申请 {p1}，发起人可执行恢复", p1=r['app_no']), "success")
    finally:
        conn.close()
    return redirect(url_for("restore_page"))


@app.route("/restore/<int:rid>/reject", methods=["POST"])
@login_required
def restore_reject(rid):
    user = current_user()
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM restore_apps WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] != "pending":
            flash(_i18n.t("申请不存在或已处理", _i18n.current_lang()), "danger"); return redirect(url_for("restore_page"))
        # 驳回同样只留给最高用户：避免经理把单子「顺手驳掉」，让人以为流程走过了
        top = _top_admin(conn)
        if not top or user["id"] != top["id"]:
            abort(403)
        conn.execute(
            """UPDATE restore_apps SET status='rejected', approver_id=?, approval_note=?,
               processed_at=? WHERE id=?""",
            (user["id"], (request.form.get("note") or "驳回").strip()[:200], db.now_str(), rid))
        conn.commit()
        flash(_xl("已驳回恢复申请 {p1}", p1=r['app_no']), "warning")
    finally:
        conn.close()
    return redirect(url_for("restore_page"))


@app.route("/restore/<int:rid>/cancel", methods=["POST"])
@login_required
def restore_cancel(rid):
    user = current_user()
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM restore_apps WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] != "pending":
            flash(_i18n.t("申请不存在或已处理", _i18n.current_lang()), "danger"); return redirect(url_for("restore_page"))
        if r["applicant_id"] != user["id"]:
            abort(403)
        conn.execute("UPDATE restore_apps SET status='canceled', processed_at=? WHERE id=?", (db.now_str(), rid))
        conn.commit()
        flash(_xl("已撤销恢复申请 {p1}", p1=r['app_no']), "success")
    finally:
        conn.close()
    return redirect(url_for("restore_page"))


_RESTART_CODE = 75   # 与 Start.bat 的约定：这个退出码表示「需要重开，请再拉我一次」


def _restart_open_path():
    return os.path.join(_settings.APP_DIR, ".restart_open")


def _set_open_on_restart(url):
    """记下「重启后要打开的地址」。

    改端口这种重启，旧页签指向的是已经不存在的端口，必须重开一个。
    但重启是启动器拉起来的，没法直接告诉新进程该开哪儿 —— 用个标记文件传话。
    """
    try:
        with open(_restart_open_path(), "w", encoding="utf-8") as f:
            f.write(url or "")
    except OSError as e:
        log.warning("记录重启后要打开的地址失败：%s", e)


def _take_open_on_restart():
    """取走重启标记；没有就返回 None。用清空代替删除，避开安全删除拦截。"""
    p = _restart_open_path()
    try:
        if not os.path.isfile(p):
            return None
        with open(p, encoding="utf-8") as f:
            url = f.read().strip()
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write("")
        except OSError:
            pass
        return url or None
    except OSError:
        return None


def _restart_self():
    """重启的唯一出口：设置页部署、库恢复等都在这里收口。"""
    import threading as _th
    _th.Timer(1.2, _restart_now).start()


def _restart_now():
    """请求启动器重启本程序（恢复库之后生效）。

    以前这里是「Popen 拉一个子进程 + os._exit(0)」，在 Start.bat 下会变成两个进程
    抢同一个控制台：批处理执行完落到 pause，用户随手按个键就 exit，控制台一关，
    子进程跟着被连带杀掉，页面从此失联，看起来就像「恢复完系统坏了」。
    改成「约定退出码 + 启动器循环」：全程只有一个进程，控制台永远是那个控制台，
    关窗口 = 停服务，语义不变。
    """
    try:
        import sys as _s
        _s.stdout.flush()
        _s.stderr.flush()
    except Exception:
        pass
    if os.environ.get("SMT_NO_RESTART"):
        # 回归脚本在同一个进程里跑请求，真退出会把测试进程一起带走
        logging.getLogger("restore").info("SMT_NO_RESTART 已设置，跳过实际退出")
        return
    logging.getLogger("restore").info("数据库已切换，退出并请求启动器重启（code=%s）", _RESTART_CODE)
    os._exit(_RESTART_CODE)


@app.route("/restore/<int:rid>/execute", methods=["POST"])
@login_required
def restore_execute(rid):
    user = current_user()
    if user["role"] not in ("admin", "warehouse"):
        abort(403)
    restored = False        # 真正的覆盖动作是否已完成（决定要不要重启）
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM restore_apps WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] != "approved":
            flash(_i18n.t("申请不存在或未获审批，不能执行恢复", _i18n.current_lang()), "danger"); return redirect(url_for("restore_page"))
        if not _backup_file_valid(r["backup_name"], r["backup_dir"]):
            flash(_i18n.t("备份文件已不存在（可能已被清理），无法恢复", _i18n.current_lang()), "danger"); return redirect(url_for("restore_page"))
        src = config.DB_PATH
        if not src or not os.path.isfile(src):
            flash(_i18n.t("当前数据库文件不存在，无法执行恢复", _i18n.current_lang()), "danger"); return redirect(url_for("restore_page"))
        # v1.0.26：整套恢复是 SQLite 文件级操作。服务器库必须走
        # mysqldump/pg_dump，绝不能拿 DB_PATH 去 copy2（后期换库的安全闸门）。
        if config.DB_TYPE != "sqlite":
            flash(str("恢复仅支持 SQLite 数据库；服务器库请用数据库自带备份工具"), "danger")
            return redirect(url_for("restore_page"))
        import share_guard
        # v1.0.26：覆盖前的备份文件校验。以前只查「文件存在 + 路径合法」，
        # 一个损坏 / 非 SQLite 的 .db 也能直接盖到生产库上，重启后才暴露。
        _bfile_chk = os.path.join(r["backup_dir"], r["backup_name"])
        _ok_b, _why = share_guard.verify_backup_file(_bfile_chk)
        if not _ok_b:
            flash(str(_why), "danger"); return redirect(url_for("restore_page"))
        # 1) 恢复前自动备份当前库（安全兜底）
        cfg = _backup_cfg(conn)
        bdir = _backup_target_dir(cfg)
        try:
            os.makedirs(bdir, exist_ok=True)
        except OSError:
            bdir = os.path.dirname(src)
        pre = os.path.join(bdir, os.path.splitext(os.path.basename(src))[0] +
                          "_prerestore_" + datetime.now().strftime("%Y%m%d%H%M%S") + ".db")
        try:
            # v1.0.26：改用 SQLite 在线备份 API。裸 copy2 在 WAL 模式下
            # 拿不到未 checkpoint 的帧，兜底备份可能是不一致快照。
            db.backup_database(pre)
        except Exception as e:
            flash(_i18n.t("恢复前自动备份失败：{0}（已中止恢复）", _i18n.current_lang()).format(e), "danger"); return redirect(url_for("restore_page"))
        # 2) 关闭当前连接后用备份覆盖数据库
        try:
            conn.close()
        except Exception:
            pass
        bfile = os.path.join(r["backup_dir"], r["backup_name"])
        # v1.0.26：同目录临时文件 + os.replace 原子替换，取代「打开 live
        # 文件、截断、写入备份字节」的 copy2。中途失败时原库完好；若其他
        # 客户端仍持有句柄导致 replace 失败，恢复明确报错而不是把库写坏。
        _swap = src + ".restore-tmp"
        try:
            import shutil as _sh
            _sh.copy2(bfile, _swap)
            os.replace(_swap, src)   # 保留原备份文件，可重复使用
        except OSError as e:
            try:
                os.remove(_swap)
            except OSError:
                pass
            flash(_i18n.t("覆盖数据库失败：{0}（恢复未完成）", _i18n.current_lang()).format(e), "danger"); return redirect(url_for("restore_page"))
        restored = True
        # 库文件已被替换：递增 epoch，客户端轮询到变化后丢弃旧连接重连，
        # 替代「只重启本机、其余 49~99 台毫不知情」的老行为。
        try:
            share_guard.bump_epoch()
        except Exception as _e:  # noqa: BLE001
            logging.getLogger("restore").warning("db_epoch 更新失败（老备份可能无此表）：%s", _e)
        # 2.5) 丢掉旧库的 WAL 边角料。
        # 备份文件是「单文件快照」，而被覆盖掉的旧库还留着 *.db-wal / *.db-shm。
        # 这两个文件属于旧库，留着会在下次打开时被回放到新库上 ——
        # 轻则数据混着旧内容，重则 database disk image is malformed。
        #
        # 这里**改名而不是删除**：实测有些机器（装了安全删除/终端防护的）会让
        # os.remove 直接失败，连日志都只留一行 warning，于是旧 WAL 原样留着 ——
        # 恢复「报成功」但下次打开就出问题，最难查。改名不受这类拦截影响，
        # 而 SQLite 只认精确文件名 spms.db-wal，改掉就等于失效。
        _stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        _wlog = logging.getLogger("restore")
        for _suffix in ("-wal", "-shm", "-journal"):
            _p = src + _suffix
            if not os.path.exists(_p):
                continue
            _dead = "%s.stale-%s" % (_p, _stamp)
            try:
                os.replace(_p, _dead)
            except OSError as e:
                _wlog.warning("旧库残留 %s 改名失败：%s", _p, e)
                continue
            try:
                os.remove(_dead)      # 能删就删干净；删不掉也无害，名字已失效
            except OSError:
                _wlog.info("已失效的 %s 保留为 %s（可随时手工清理）", _p, os.path.basename(_dead))
        # 3) 把本次恢复申请写回恢复后的库（保留审批轨迹），标记完成。
        #    这里必须是 upsert：备份往往是在「申请单已存在」之后才做的，
        #    恢复回来那一行本来就带着同一个 id，直接 INSERT 会撞主键报 500。
        #    另外老备份里可能没有 restore_apps 表 / 字段对不上，
        #    那种情况只记日志 —— 恢复本身已经完成，不能因为补记流水失败就告诉用户「失败」。
        _restore_log = logging.getLogger("restore")
        conn2 = get_db()
        try:
            conn2.execute(
                """INSERT INTO restore_apps(id, app_no, backup_name, backup_dir, backup_size,
                   applicant_id, status, assigned_approver_id, approver_id, approval_note,
                   created_at, processed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     status='completed',
                     processed_at=excluded.processed_at,
                     approver_id=excluded.approver_id,
                     approval_note=excluded.approval_note""",
                (r["id"], r["app_no"], r["backup_name"], r["backup_dir"], r["backup_size"],
                 r["applicant_id"], "completed", r["assigned_approver_id"], r["approver_id"],
                 r["approval_note"] or "", r["created_at"], db.now_str()))
            conn2.commit()
        except Exception as e:
            _restore_log.warning("恢复已完成，但补记恢复申请失败（不影响恢复结果）：%s", e)
            try:
                conn2.rollback()
            except Exception:
                pass
        finally:
            try:
                conn2.close()
            except Exception:
                pass
        flash(_xl("数据库已从备份 {p1} 恢复（恢复前库已另存，系统正在自动重启…）", p1=r['backup_name']), "success")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not restored:
        return redirect(url_for("restore_page"))
    # 恢复库后同样走启动器的循环重启（同端口、同页签，浏览器不用重开）
    _restart_self()
    return redirect(url_for("restore_page"))


# 大屏/图表数据轻量缓存：默认 20 秒内重复请求直接复用，显著加快大屏打开与刷新。
# 注意：TTL 不能写死 20 秒 —— 管理员把「刷新间隔」调到 5 秒时，页面每 5 秒来一次请求，
# 却要到 20 秒才换数据，看着像刷新间隔没生效。api_screen 会按当前配置把它压到 refresh 以内。
_SCREEN_DATA_CACHE = {}
_SCREEN_CACHE_TTL = 20


def _screen_cache_get(key):
    hit = _SCREEN_DATA_CACHE.get(key)
    if hit and time.time() - hit[0] < _SCREEN_CACHE_TTL:
        return hit[1]
    return None


def _screen_cache_set(key, data):
    _SCREEN_DATA_CACHE[key] = (time.time(), data)


def _screen_period_window(period):
    """大屏「统计周期」的时间窗：按日=今日 00:00 起，按周=本周一 00:00 起，按月=本月 1 日 00:00 起。
    返回 (start, end)，end 取当前时刻，与 _period_summary 的统计口径完全一致，
    这样柱状图各柱之和 == KPI 卡上的数字，不会出现"卡片今日 0、图里却有几百"的矛盾。"""
    now = datetime.now()
    if period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


# KPI 卡标题按周期换词：按日沿用原词条（10 语言译文本就齐），按周/按月用新词条
_STAT_PERIOD_LABEL = {
    ("week", "stat_in_qty"): "周度入库数量",
    ("week", "stat_out_qty"): "周度出库数量",
    ("week", "stat_in_amt"): "周度入库金额",
    ("week", "stat_out_amt"): "周度出库金额",
    ("month", "stat_in_qty"): "月度入库数量",
    ("month", "stat_out_qty"): "月度出库数量",
    ("month", "stat_in_amt"): "月度入库金额",
    ("month", "stat_out_amt"): "月度出库金额",
}


def _stat_card_label(key, period):
    """取 KPI 卡标题的源文（未翻译），按日时回落到 config.SCREEN_CARDS 的默认标题"""
    return _STAT_PERIOD_LABEL.get((period, key)) or config.SCREEN_CARDS.get(key, {}).get("label", key)


def _period_summary(conn, period):
    """按日/周/月统计当期与上一期 出入库数量、金额（20 秒缓存）"""
    ck = ("period", period)
    hit = _screen_cache_get(ck)
    if hit is not None:
        return hit
    now = datetime.now()
    if period == "day":
        cur_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        prev_start = cur_start - timedelta(days=1)
        prev_end = cur_start
    elif period == "week":
        cur_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        prev_start = cur_start - timedelta(days=7)
        prev_end = cur_start
    else:  # month
        cur_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_start = (cur_start - timedelta(days=1)).replace(day=1)
        prev_end = cur_start
    cur_end = now

    def _stat(s, e):
        rows = conn.execute(
            """SELECT tx_type, COALESCE(SUM(quantity),0) q, COALESCE(SUM(amount),0) a
               FROM transactions
               WHERE created_at>=? AND created_at<? AND tx_type IN ('in','out')
               GROUP BY tx_type""",
            (s.strftime("%Y-%m-%d %H:%M:%S"), e.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
        d = {"in": {"qty": 0, "amt": 0}, "out": {"qty": 0, "amt": 0}}
        for r in rows:
            if r["tx_type"] in d:
                d[r["tx_type"]] = {"qty": r["q"], "amt": round(r["a"], 2)}
        return d

    cur, prev = _stat(cur_start, cur_end), _stat(prev_start, prev_end)
    cards = {
        "period": period,
        "label": {"day": "日度", "week": "周度", "month": "月度"}[period],
        "in_qty": cur["in"]["qty"], "in_amt": cur["in"]["amt"],
        "out_qty": cur["out"]["qty"], "out_amt": cur["out"]["amt"],
        "prev": {
            "in_qty": prev["in"]["qty"], "in_amt": prev["in"]["amt"],
            "out_qty": prev["out"]["qty"], "out_amt": prev["out"]["amt"],
        },
    }
    _screen_cache_set(ck, cards)
    return cards


def _mshift(base, k):
    """当月 1 日往前推 k 个月（k 可为 0）"""
    y = base.year + (base.month - 1 - k) // 12
    m = (base.month - 1 - k) % 12 + 1
    return base.replace(year=y, month=m, day=1)


def _series_buckets(period, trend=None):
    """趋势图的时间窗与分桶（出入库趋势与领用趋势共用，保证横轴完全一致）
    返回 (labels, keys, start, end, bucket_expr, norm)
      bucket_expr：直接拼进 SQL 的分桶表达式（列别名必须写成 b）
      norm(raw)  ：把 SQL 查出来的桶值归一成 keys 里的字符串（按周要折到周一）

    默认回溯长度：日 31 / 周 30 / 月 13 —— 总览与分析视图要「日度看满一个月、
    周度看近大半年、月度看满一年再带一个月」，短窗口看不出月度节律。
    大屏另有大屏配置里的 trend（日 14/周 8/月 6），走参数覆盖，不跟着这里变。
    """
    now = datetime.now()
    trend = trend or {}
    if period == "week":
        n = max(2, min(int(trend.get("week", 30)), 52))
        cur_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        start = cur_start - timedelta(weeks=n - 1)
        end = cur_start + timedelta(weeks=1)
        keys = [(cur_start - timedelta(weeks=i)).strftime("%Y-%m-%d")
                for i in range(n - 1, -1, -1)]
        # 周标签以前写死「第N周」，英文界面的图表和大屏上会突然冒出中文
        lang = _i18n.current_lang()
        labels = [_i18n.t("第{d}周", lang, d=(cur_start - timedelta(weeks=i)).isocalendar()[1])
                  for i in range(n - 1, -1, -1)]

        def _norm(raw):
            dd = datetime.strptime(str(raw)[:10], "%Y-%m-%d")
            return (dd - timedelta(days=dd.weekday())).strftime("%Y-%m-%d")
        return labels, keys, start, end, "date(t.created_at)", _norm
    if period == "month":
        n = max(2, min(int(trend.get("month", 13)), 36))
        cur_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = _mshift(cur_start, n - 1)
        # 以前这里从「上月 1 日」往回推 i*31 天，结果最后一根柱子永远停在上个月，
        # 本月数据根本不画 —— 周期选「按月」却看不到当月，就是这里错位。
        end = _mshift(cur_start, -1)
        keys = [_mshift(cur_start, i).strftime("%Y-%m") for i in range(n - 1, -1, -1)]
        return list(keys), keys, start, end, "substr(t.created_at,1,7)", lambda raw: raw
    n = max(2, min(int(trend.get("day", 31)), 90))
    start = (now - timedelta(days=n - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    keys = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]
    return [k[5:] for k in keys], keys, start, end, "substr(t.created_at,1,10)", lambda raw: raw


def _chart_series(conn, period, trend=None):
    """生成折线/柱状图数据：日→近N天(默认31)，周→近N周(默认30)，月→近N月(默认13)
    trend: dict {day, week, month} 各周期的回溯长度（大屏配置会传自己的 14/8/6）；20 秒缓存"""
    if not trend:
        trend = {}
    tkey = "|".join(str(trend.get(k, "")) for k in ("day", "week", "month"))
    # 缓存键带上语言：周标签「第N周」是本地化文案，不加语言会让切语言的用户读上一种语言
    ck = ("series", period, tkey, _i18n.current_lang())
    hit = _screen_cache_get(ck)
    if hit is not None:
        return hit
    labels, keys, start, end, bexpr, norm = _series_buckets(period, trend)
    idx = {k: i for i, k in enumerate(keys)}
    n = len(keys)
    in_qty = [0] * n
    out_qty = [0] * n
    in_amt = [0.0] * n
    out_amt = [0.0] * n
    rows = conn.execute(
        "SELECT " + bexpr + " b, t.tx_type tx_type, COALESCE(SUM(t.quantity),0) q,"
        " COALESCE(SUM(t.amount),0) a"
        " FROM transactions t"
        " WHERE t.created_at>=? AND t.created_at<? AND t.tx_type IN ('in','out')"
        " GROUP BY b, t.tx_type",
        (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    for r in rows:
        i = idx.get(norm(r["b"]))
        if i is None:
            continue
        if r["tx_type"] == "in":
            in_qty[i] += r["q"]
            in_amt[i] += r["a"]
        else:
            out_qty[i] += r["q"]
            out_amt[i] += r["a"]
    out = {
        "labels": labels,
        "in_qty": in_qty, "out_qty": out_qty,
        "in_amt": [round(x, 2) for x in in_amt], "out_amt": [round(x, 2) for x in out_amt],
    }
    _screen_cache_set(ck, out)
    return out


# 领用趋势的三个维度：线别 / 设备 / 设备类型（设备类型取设备档案里的型号字段）
_USAGE_ALL_CAP = 60      # 下拉里最多列多少个分类（总览只画 Top5，单看从这份全量取）

_USAGE_TREND_DIMS = (
    ("line", "COALESCE(NULLIF(t.line_name,''),'%s')", ""),
    ("device", "COALESCE(NULLIF(t.device_name,''),'%s')", ""),
    ("model", "COALESCE(NULLIF(e.model,''),NULLIF(t.device_name,''),'%s')",
     " LEFT JOIN equipment e ON e.name=t.device_name"),
)


def _usage_trend(conn, period, show_amt=True, top=5):
    """总览「领用趋势」：按线别/设备/设备类型，在趋势图的同一批时间桶里堆叠出分布
    —— 出入库趋势回答「这周比上周多还是少」，这张图回答「多出来的是哪条线、哪台设备」。
    top：保留领用量最高的前 N 个分类，其余归入「其他」，避免十几条线挤成一片。
    """
    lang = _i18n.current_lang()
    ck = ("usage_trend", period, 1 if show_amt else 0, top, lang)
    hit = _screen_cache_get(ck)
    if hit is not None:
        return hit
    labels, keys, start, end, bexpr, norm = _series_buckets(period)
    idx = {k: i for i, k in enumerate(keys)}
    n = len(keys)
    # "未分配" 会作为分组值写进 SQL，必须先本地化再转义（常量，无注入面）
    _un = _i18n.t("未分配", lang).replace("'", "''")
    _other = _i18n.t("其他", lang)
    dims = {}
    for key, col, join in _USAGE_TREND_DIMS:
        rows = conn.execute(
            "SELECT " + bexpr + " b, " + (col % _un) + " d,"
            " COALESCE(SUM(t.quantity),0) q, COALESCE(SUM(t.amount),0) a"
            " FROM transactions t" + join +
            " WHERE t.tx_type='out' AND IFNULL(t.out_type,'')<>'stocktake'"
            " AND t.created_at>=? AND t.created_at<?"
            " GROUP BY b, d",
            (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
        agg = {}          # 分类 -> {q:[], a:[]}
        for r in rows:
            i = idx.get(norm(r["b"]))
            if i is None:
                continue
            slot = agg.setdefault(r["d"] or _un,
                                  {"q": [0] * n, "a": [0.0] * n, "tq": 0.0, "ta": 0.0})
            slot["q"][i] += r["q"]
            slot["a"][i] += r["a"]
            slot["tq"] += r["q"]
            slot["ta"] += r["a"]
        # 按领用数量排序取前 N，其余合并为「其他」（颜色顺序两张图一致，数量/金额能对着看）
        order = sorted(agg.items(), key=lambda kv: -kv[1]["tq"])
        kept, rest = order[:top], order[top:]
        qty_series = [{"name": k, "data": [round(x, 3) for x in v["q"]]} for k, v in kept]
        amt_series = [{"name": k, "data": [round(x, 2) for x in v["a"]]} for k, v in kept]
        if rest:
            rq = [0.0] * n
            ra = [0.0] * n
            for _k, v in rest:
                for i in range(n):
                    rq[i] += v["q"][i]
                    ra[i] += v["a"][i]
            qty_series.append({"name": _other, "data": [round(x, 3) for x in rq]})
            amt_series.append({"name": _other, "data": [round(x, 2) for x in ra]})
        # 全量分类：图表头部的下拉要能单看任意一条线/一台设备/一个型号。
        # 总览态才做 TopN 归并，单看态直接从这份全量里取，免得切换下拉又打一次接口。
        # 上限 60：真实台账里线别/型号都远小于这个数，超出的下拉本身也没人翻。
        full = order[:_USAGE_ALL_CAP]
        dims[key] = {
            "qty": qty_series, "amt": amt_series if show_amt else None,
            "all_qty": [{"name": k, "data": [round(x, 3) for x in v["q"]]} for k, v in full],
            "all_amt": ([{"name": k, "data": [round(x, 2) for x in v["a"]]} for k, v in full]
                        if show_amt else None),
            "opts": [k for k, _v in full],
        }
    out = {"labels": labels, "dims": dims}
    _screen_cache_set(ck, out)
    return out

def _repair_trend(conn, period, show_amt=True):
    """总览「维修趋势」：按时间桶统计维修（kind='maintenance'）的数量与金额。

    维修走借·换·检·修台账；v1.0.19 起办理时实扣库存并写 transactions，
    但「维修趋势」按台账口径统计，所以数据来自 borrows 表而非 transactions；
    金额由 维修数量 × 物料单价 现算（borrows 不存金额列）。
    与出入库趋势、领用趋势共用同一批时间桶，横轴完全一致。
    """
    lang = _i18n.current_lang()
    ck = ("repair_trend", period, 1 if show_amt else 0, lang)
    hit = _screen_cache_get(ck)
    if hit is not None:
        return hit
    labels, keys, start, end, bexpr, norm = _series_buckets(period)
    idx = {k: i for i, k in enumerate(keys)}
    n = len(keys)
    qty = [0.0] * n
    amt = [0.0] * n
    rows = conn.execute(
        "SELECT " + bexpr + " b, COALESCE(SUM(t.qty),0) q,"
        " COALESCE(SUM(t.qty * COALESCE(p.unit_price,0)),0) a"
        " FROM borrows t LEFT JOIN parts p ON p.id=t.part_id"
        " WHERE t.kind='maintenance' AND t.created_at>=? AND t.created_at<?"
        " GROUP BY b",
        (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    for r in rows:
        i = idx.get(norm(r["b"]))
        if i is None:
            continue
        qty[i] += r["q"]
        amt[i] += r["a"]
    out = {
        "labels": labels,
        "qty": [round(x, 3) for x in qty],
        "amt": [round(x, 2) for x in amt] if show_amt else None,
    }
    _screen_cache_set(ck, out)
    return out



def _auto_stock_cfg(conn):
    """读取自动上下限全局配置"""
    raw = db.get_setting(conn, "auto_stock")
    cfg = {"enabled": 0, "lead_days": 45, "safety_factor": 1.5,
           "consume_days": 120}
    if raw:
        try:
            cfg.update(json.loads(raw))
        except Exception:
            pass
    return cfg




def _suggest_map(conn, cfg):
    """一次算出所有物料的建议上下限 {part_id: (lo, hi)}。

    列表页过去对每个物料各查一次 transactions，2 万个物料 = 2 万条 SQL，
    单这一项就能把页面拖到分钟级。这里改成一条 GROUP BY part_id, ym 的查询。
    """
    days = int(cfg.get("consume_days") or 120)
    rows = conn.execute(
        """SELECT part_id, strftime('%Y-%m', created_at) ym, SUM(quantity) q, MIN(created_at) first_tx
           FROM transactions
           WHERE tx_type='out' AND IFNULL(out_type,'')<>'stocktake'
             AND created_at >= datetime('now', ?)
           GROUP BY part_id, ym""",
        (f"-{days} days",)
    ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["part_id"], []).append(r)
    return {pid: _suggest_from_rows(rs, cfg) for pid, rs in grouped.items()}


def _suggest_from_rows(rows, cfg):
    """根据历史出库量算建议上下限（纯计算，不碰数据库）。
    - 按月统计出库量，只对"有出库的月份"求月均（零月不计入）；
    - 基于需求波动动态调整安全库存：稳定料少备，波动料多备（变异系数法）；
    - 新料（不足配置窗口）按实际天数折算；
    - 无任何历史出库返回 (None, None)。"""
    import math
    lead = int(cfg.get("lead_days") or 45)
    k_base = float(cfg.get("safety_factor") or 1.5)
    if not rows:
        return None, None
    # 只算有出库的月份
    active_months = [r for r in rows if r["q"] and r["q"] > 0]
    if not active_months:
        return None, None
    monthly_list = sorted([(r["ym"], r["q"]) for r in active_months], key=lambda x: x[0])
    recent = monthly_list[-3:]
    recent_avg = sum(q for _, q in recent) / len(recent) if recent else 0
    all_avg = sum(q for _, q in monthly_list) / len(monthly_list) if monthly_list else 0
    base_monthly = recent_avg if recent_avg >= all_avg * 0.5 else max(recent_avg, all_avg)
    # 需求波动：变异系数 CV = 标准差/均值
    if len(monthly_list) >= 2:
        mean = all_avg
        variance = sum((q - mean) ** 2 for _, q in monthly_list) / len(monthly_list)
        std = math.sqrt(variance)
        cv = std / mean if mean > 0 else 0
    else:
        cv = 0.5  # 只有1个月数据，保守给中等波动
    # 动态安全系数：稳定料 CV=0 → 系数=k_base；波动料 CV=1 → 系数=k_base×2
    k_dynamic = k_base * (1 + cv)
    daily = base_monthly / 30.0
    lo = daily * lead * k_dynamic
    hi = lo * 1.5
    return round(lo, 2), round(hi, 2)


def _stock_alerts(conn):
    cfg = _auto_stock_cfg(conn)
    # v1.0.40：上下限只跟「可用」比（良品区里的良品 + 维修 OK 的维修品）。
    # 待维修/待报废件不算——以前把待报废区的一堆废件算进 stock，
    # 明明好件见底了还显示「正常」，补货提醒永远不响。
    rows = conn.execute(
        """SELECT p.id, p.part_code, p.name, p.category, p.min_stock, p.max_stock, p.auto_min,
                  COALESCE(SUM(i.quantity),0) stock,
                  COALESCE(SUM(CASE WHEN COALESCE(l.attribute,'good')='good'
                                     AND COALESCE(i.condition,'good')<>'defective'
                                    THEN i.quantity ELSE 0 END),0) avail
           FROM parts p LEFT JOIN inventory i ON p.id=i.part_id
           LEFT JOIN locations l ON l.id=i.location_id
           WHERE p.deleted=0 AND COALESCE(p.deprecated,0)=0
           GROUP BY p.id"""
    ).fetchall()
    low, high = [], []
    _sugg = _suggest_map(conn, cfg) if cfg.get("enabled") else {}
    for r in rows:
        lo, hi = r["min_stock"], r["max_stock"]
        auto_on = cfg.get("enabled") and r["auto_min"]
        if auto_on:
            slo, shi = _sugg.get(r["id"], (None, None))
            if slo is not None:
                lo, hi = slo, shi
        if lo and r["avail"] < lo:
            low.append({"id": r["id"], "code": r["part_code"], "name": r["name"],
                        "category": r["category"], "stock": r["avail"],
                        "limit": lo, "auto": 1 if auto_on else 0})
        elif hi and r["avail"] > hi:
            high.append({"id": r["id"], "code": r["part_code"], "name": r["name"],
                         "category": r["category"], "stock": r["avail"],
                         "limit": hi, "auto": 1 if auto_on else 0})
    return {"low": low, "high": high}


# 大屏配色预设。设计约束（改色前先跑一遍 tools/check_v335.py 里的配色断言）：
#   1. num:card 对比度 ≥ 6（大屏远距离看，数字必须跳出来）
#   2. label:card ≥ 4.5（说明文字不能糊）
#   3. 六套之间的 num 两两 ΔE76 ≥ 20，否则「换色不换脸」，用户分辨不出来
#   4. accent 与 num 必须拉开 ΔE ≥ 26，否则强调色失效（以前黑金两色几乎同色）
# 顺序即配置页展示顺序：蓝系 → 紫 → 金 → 绿 → 浅色。
# v2.9.70 起砍掉「青霓虹 / 暗夜金 / 星云紫」三套——与科技蓝、曜黑鎏金、深空极光
# 的主色 ΔE 只有 2.9 / 12.1 / 33，背景色差更小（2.3~6.2），本质是同一套配色换了个名字。
# 老库里存了这三个 key 的，SCREEN_STYLE_ALIASES 会把它们并到现存的那一套，不会报错。
# ---------------------------------------------------------------- 大屏三件套：样式 × 色系 × 排版
# 以前「风格」= 配色 + 皮肤绑死，9 套里其实只有 4 种形状在反复换色：客户想换颜色就得
# 连形状、间距一起换掉。现在拆成三个互不干扰的维度，任意组合 5 × 5 × 5 = 125 种：
#   style   样式 —— 只管形状语言（圆角/切角/描边/标题标记/底纹），颜色一律走 CSS 变量
#   palette 色系 —— 只管颜色（背景/卡片/数字/强调/图表色板），深浅各一套口径
#   layout  排版 —— 只管卡片排布（config.SCREEN_LAYOUTS 五套）
# 形状与颜色解耦的前提：皮肤 CSS 里不允许出现硬编码色值，否则白底一套就全糊。
SCREEN_SKINS = {
    "aurora": {"name": "深空极光", "tag": "玻璃流光 · 柔边圆角", "pv": "glass"},
    "command": {"name": "指挥舱", "tag": "仪表 HUD · 四角括号", "pv": "hud"},
    "ember": {"name": "锻炉钢板", "tag": "直角切角 · 工业钢线", "pv": "ember"},
    "rose": {"name": "绢面鎏金", "tag": "大圆角双环 · 轻奢", "pv": "rose"},
    "aqua": {"name": "声呐深潜", "tag": "线框竖边 · 冷静耐看", "pv": "aqua"},
    "bridge": {"name": "星舰舰桥", "tag": "梯形舰桥 · 斜角标签 · 星点", "pv": "bridge"},
}

# 色系：五套覆盖深底四档（深蓝/冰蓝/星云靛紫/松绿）+ 浅底一档（珍珠白）。
# 每套都自带 light 标记：浅底那套要连带切图表色板与投影强度。
# v1.0.11 起深底四档按「赛博投屏」重配：底色带色相、主数字高饱和、强调色同族但拉开 ΔE、
# cardb 提亮成可当发光描边用的亮色（body.dark .scr-card 已改为取 var(--cardb)，不再写死灰边）。
SCREEN_PALETTES = {
    "deepblue": {"name": "深蓝", "light": False, "tag": "墨蓝夜景 · 钢蓝数字", "vars": {
        "sbg": "#050b1c", "sbg2": "#0a1730", "head": "#071021", "head2": "#122a4d", "card": "#0a1930",
        "cardb": "#1c4a7a", "label": "#8fa9c9", "num": "#4da8ff", "title": "#eaf4ff", "text": "#cfdff2",
        "sub": "#8ea6c2", "accent": "#00e5ff", "alert_low": "#ff6b6b", "alert_high": "#ffa94d",
        }},
    "iceblue": {"name": "冰蓝", "light": False, "tag": "墨底冰白 · 冷光仪表", "vars": {
        "sbg": "#030a12", "sbg2": "#071a28", "head": "#040e18", "head2": "#0d2b40", "card": "#081d2c",
        "cardb": "#1d5673", "label": "#8fb6c9", "num": "#a8ecff", "title": "#e6f7ff", "text": "#cfe8f2",
        "sub": "#8eafc1", "accent": "#2f7bff", "alert_low": "#ff6b81", "alert_high": "#ffb04d",
        }},
    # v1.0.12：key 保留 gold 是让老库 palette='gold' 无缝升级（不丢配置），
    # 实际色系已换成「星云靛紫」：参考图那种深靛紫底 + 紫色数字 + 青色强调。
    "gold": {"name": "星云靛紫", "light": False, "tag": "深靛紫投影 · 星云流光", "vars": {
        "sbg": "#0a0e4c", "sbg2": "#14165e", "head": "#080b3d", "head2": "#232a7a", "card": "#141a63",
        "cardb": "#3b3f9e", "label": "#a89ec9", "num": "#b794ff", "title": "#efeaff", "text": "#d5cff2",
        "sub": "#9d95c9", "accent": "#00e5ff", "alert_low": "#ff6b81", "alert_high": "#ffb04d",
        }},
    "pine": {"name": "松绿", "light": False, "tag": "翡翠墨绿 · 护眼耐看", "vars": {
        "sbg": "#03110f", "sbg2": "#06211b", "head": "#041713", "head2": "#0a352b", "card": "#061d18",
        "cardb": "#176b52", "label": "#86b7a6", "num": "#00e5b0", "title": "#e4fff4", "text": "#cceade",
        "sub": "#83ab9e", "accent": "#7cf9ff", "alert_low": "#ff6b6b", "alert_high": "#ffb04d",
        }},
    "pearl": {"name": "珍珠白", "light": True, "tag": "白底商务 · 汇报演示", "vars": {
        "sbg": "#f2f6fb", "sbg2": "#e5edf7", "head": "#101d33", "head2": "#1d3253", "card": "#ffffff",
        "cardb": "#cbd9ea", "label": "#5b6f89", "num": "#0a52b8", "title": "#12233f", "text": "#22334d",
        "sub": "#7b8ca3", "accent": "#d97706", "alert_low": "#dc2626", "alert_high": "#ea580c",
        }},
}

# 图表色板：**跟着色系走，不跟样式走**。
# v1.0.11：前 7 色仍是同族明度阶梯（保证饼图一眼是「这套色系」），
# 第 8~10 色换成参考图里那类高饱和撞色（品红/紫/橙），多系列图表不再一片同色。
SCREEN_CHART_PALETTES = {
    "deepblue": {
        "in": "#2b9bff", "out": "#ffb03a",
        "pie": ["#0d3d6b", "#2b9bff", "#1a6fc0", "#59d0ff", "#2078d8", "#9ae8ff", "#1660a8", "#ff5c9d", "#a855f7", "#ffb03a"],
    },
    "iceblue": {
        "in": "#5fd4f5", "out": "#ffc44d",
        "pie": ["#0c3348", "#5fd4f5", "#1d7799", "#a5edff", "#2b9cc4", "#7fc9e0", "#175a78", "#7c6bff", "#ff6fae", "#ffc44d"],
    },
    "gold": {  # 星云靛紫：前 7 色紫族明度阶梯，后 3 色品红/青/橙撞色
        "in": "#a78bfa", "out": "#ffb03a",
        "pie": ["#2a1d6b", "#a78bfa", "#4a32a8", "#b49ae8", "#6d4fd0", "#8e79e0", "#38257f", "#ff5c9d", "#00e5ff", "#ffb03a"],
    },
    "pine": {
        "in": "#00e5b0", "out": "#ffd166",
        "pie": ["#073b2c", "#00e5b0", "#0e7a5c", "#6cffd4", "#12a37c", "#b6ffe6", "#0a5a44", "#ff6b9d", "#ffd166", "#7c8cff"],
    },
    "pearl": {
        "in": "#1f7ac2", "out": "#e0603a",
        "pie": ["#123f66", "#1f7ac2", "#14508a", "#5fb0e6", "#2a7ab8", "#9ad0ee", "#0f4a7d", "#e0603a", "#7c5cd6", "#d99a2b"],
    },
}

# 老库里的「风格」是配色+形状一体的 key，升级后要拆成 (样式, 色系)：
# 形状取最接近的那套，颜色取最接近的那套，客户升级后不会「一打开变了个样」。
SCREEN_LEGACY_STYLES = {
    "tech-blue": ("command", "deepblue"),
    "tech-cyan": ("command", "deepblue"),
    "neon-cyan": ("command", "deepblue"),
    "command": ("command", "iceblue"),
    "aurora": ("aurora", "iceblue"),
    "nebula-purple": ("aurora", "iceblue"),
    "purple": ("aurora", "iceblue"),
    "ember": ("ember", "gold"),
    "rose": ("rose", "gold"),
    "obsidian": ("rose", "gold"),
    "midnight-gold": ("rose", "gold"),
    "night-gold": ("rose", "gold"),
    "aqua": ("aqua", "pine"),
    "green-data": ("aqua", "pine"),
    "light-clean": ("command", "pearl"),
}

# 老的「高端模板」把排版也绑在风格上（aurora/obsidian/command 各有专属坐标）。
# 排版收敛到五套后，按最接近的排布归并，老库不会因为查不到 key 掉回默认。
SCREEN_LEGACY_LAYOUTS = {"aurora": "classic", "obsidian": "grid", "command": "wide"}


def screen_style_split(name):
    """把任意历史写法拆成 (样式 key, 色系 key)。

    返回的色系可能为空：调用方自己决定「沿用当前色系」还是「用默认」。
    注意顺序：**先查老风格表再查样式表** —— 五个新样式 key（aurora/command/ember/
    rose/aqua）当年就是老风格 key，老库只存了 style、没存色系；先匹配样式表会把
    原来的配色丢掉（以前「指挥舱蓝」升级后变默认深蓝）。新库两个字段都存，
    palette 非空时以 palette 为准，所以这个兜底不会覆盖新选择。
    """
    n = str(name or "").strip()
    if n in SCREEN_LEGACY_STYLES:
        return SCREEN_LEGACY_STYLES[n]
    if n in SCREEN_SKINS:
        return n, ""
    if n == "custom":
        return "aurora", "custom"
    return "command", ""


def screen_style_key(name):
    """样式 key（兼容老调用：以前这个函数返回的就是风格 key）。"""
    return screen_style_split(name)[0]


def screen_palette_key(name):
    """色系 key；老风格 key 也能直接喂进来。"""
    n = str(name or "").strip()
    if n in SCREEN_PALETTES:
        return n
    if n == "custom":
        return "custom"
    pal = screen_style_split(n)[1]
    if pal in SCREEN_PALETTES or pal == "custom":
        return pal
    return "deepblue"


def _screen_style_css(cfg):
    """按色系生成 :root 的 CSS 变量串，并给出深浅标志。

    样式（skin-*）不再携带任何颜色，颜色全部来自这里；custom 仍以「上一次选中的色系」
    为基线，只覆盖客户改过的那几项。
    """
    pal = screen_palette_key(cfg.get("palette"))
    if pal == "custom":
        base = screen_palette_key(cfg.get("palette_base") or "deepblue")
        if base == "custom":
            base = "deepblue"
    else:
        base = pal
    pv = SCREEN_PALETTES.get(base) or SCREEN_PALETTES["deepblue"]
    varset = dict(pv["vars"])
    if pal == "custom":
        cu = cfg.get("style_custom") or {}
        for k in varset:
            v = str(cu.get(k) or "").strip()
            if _is_hex_color(v):
                varset[k] = v
    css = ";".join(f"--{k}:{v}" for k, v in varset.items())
    # 深浅：自定义时以客户填的「页面背景」实测亮度为准（选了浅底就切浅色图表），
    # 预设按色系自带的 light 标记。
    light = _hex_is_light(varset.get("sbg")) if pal == "custom" else bool(pv["light"])
    return css, light


# ------------------------------------------------------------------
# 大屏展示（电视/会议室公共大屏，无需登录）
# ------------------------------------------------------------------
def _gv(v, default=0.0):
    """网格坐标取小数：卡片宽度/高度可以是 2.4 / 2.6 / 2.75 这种非整数格，
    以前一律 int() 会把 2.6 截成 2，管理员保存一次设置，大屏图表就又缩回去了。
    保留两位（12 行除以 4 得 2.75，只留一位会被舍成 2.8，总高就凑不满 12）。"""
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return float(default)


def _sanitize_layout(raw):
    """校验并规整大屏卡片布局（12 列 x 10 行网格）。
    raw: {"mode": "classic"|"custom", "version": "price"|"np",
          "cards": {key: {"x":..,"y":..,"w":..,"h":..}}}
    预设模式下卡片强制取该预设的完整布局；custom 模式取提交值（越界自动收敛）。

    version 决定同一个布局族用哪一版排布：
      price = 有金额版（含金额类卡片）
      np    = 无金额版（取该族的 noprice 坐标，不含金额类卡片）
    老库存的 *_np 模式（classic_np 等）本身没有 noprice 版，等价于「族 + np」，
    这里统一归一成 「mode=族, version=np」，避免两套口径并存。"""
    try:
        mode = str(raw.get("mode") or "classic").strip()
    except Exception:
        mode = "classic"
    _v = str(raw.get("version") or "").strip().lower()
    if _v in ("np", "noprice", "no_price", "none"):
        version = "np"
    elif _v in ("price", "amt", "amount", "with_price"):
        version = "price"
    else:
        version = ""
    if mode.endswith("_np") and mode[:-3] in config.SCREEN_LAYOUTS:
        mode = mode[:-3]
        version = "np"          # 老的无金额预设 = 该族的无金额版
    # 排版收敛到五套：老库里的 aurora / obsidian / command 是「风格绑排版」时代的专属
    # 坐标，直接查不到就会掉回经典。按最接近的一套归并，再走 normal 分支。
    if mode in SCREEN_LEGACY_LAYOUTS:
        mode = SCREEN_LEGACY_LAYOUTS[mode]
    if mode not in config.SCREEN_LAYOUTS and mode != "custom":
        mode = "classic"
    if not version:
        version = "price"
    cards = {}
    noprice = None
    if mode in config.SCREEN_LAYOUTS:
        cards = {k: {"x": _gv(v[0]), "y": _gv(v[1]), "w": _gv(v[2], 1), "h": _gv(v[3], 1)}
                 for k, v in config.SCREEN_LAYOUTS[mode]["cards"].items()}
        np_ = config.SCREEN_LAYOUTS[mode].get("noprice")
        if np_:
            noprice = {k: {"x": _gv(v[0]), "y": _gv(v[1]), "w": _gv(v[2], 1), "h": _gv(v[3], 1)}
                       for k, v in np_.items()}
    elif isinstance(raw.get("cards"), dict):
        for k, v in raw["cards"].items():
            if k not in config.SCREEN_CARDS or not isinstance(v, dict):
                continue
            x = max(0.0, min(_gv(v.get("x", 0)), 11.0))
            y = max(0.0, min(_gv(v.get("y", 0)), 11.0))
            w = max(1.0, min(_gv(v.get("w", 1), 1), 12.0 - x))
            h = max(1.0, min(_gv(v.get("h", 1), 1), 12.0 - y))
            cards[k] = {"x": x, "y": y, "w": w, "h": h}
        # 补齐缺失卡片（回退到经典预设位置）
        for k, v in config.SCREEN_LAYOUTS["classic"]["cards"].items():
            if k not in cards:
                cards[k] = {"x": _gv(v[0]), "y": _gv(v[1]), "w": _gv(v[2], 1), "h": _gv(v[3], 1)}
    return {"mode": mode, "version": version, "cards": cards, "noprice": noprice}


def get_screen_config(conn):
    """读取大屏配置（数据库 sys_settings，管理员可配）"""
    cfg = dict(config.DEFAULT_SCREEN)
    cfg["limits"] = dict(config.DEFAULT_SCREEN["limits"])
    cfg["trend"] = dict(config.DEFAULT_SCREEN["trend"])
    _dl = config.DEFAULT_SCREEN["layout"]
    cfg["layout"] = {"mode": _dl["mode"], "version": _dl.get("version", "price"),
                     "cards": {k: dict(v) for k, v in _dl["cards"].items()},
                     "noprice": {k: dict(v) for k, v in (_dl.get("noprice") or {}).items()}}
    raw = db.get_setting(conn, "screen_config")
    _has_pal_field = False      # 库里到底有没有 palette 字段（老库没有 → 要按老风格推色系）
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                _has_pal_field = bool(str(d.get("palette") or "").strip())
                for k in ("modules", "refresh", "period", "theme", "stale_days", "show_price",
                          "sb_title", "sb_subtitle", "sb_show_logo", "sb_logo_height",
                          "sb_title_size", "sb_subtitle_size", "sb_title_color",
                          "sb_subtitle_color", "sb_font_family", "style",
                          "palette", "palette_base", "style_custom_base",
                          "alert_scroll_speed", "tx_scroll_speed",
                          "layout", "layout_mode", "layout_cards"):
                    if k in d:
                        cfg[k] = d[k]
                # 版本升级：仅补入用户从未见过的全新模块（不覆盖用户手动取消的选择）
                if isinstance(d.get("modules"), list):
                    merged = list(d["modules"])
                    known = d.get("known_modules") or []
                    for m in config.SCREEN_MODULES:
                        if m not in merged and m not in known:
                            merged.append(m)  # 全新模块默认开启
                    cfg["modules"] = merged
                    cfg["known_modules"] = list(set(known + list(config.SCREEN_MODULES.keys())))
                if isinstance(d.get("style_custom"), dict):
                    cu = cfg.get("style_custom") or {}
                    for k, v in d["style_custom"].items():
                        if _is_hex_color(str(v).strip()):
                            cu[k] = str(v).strip().lower()
                    cfg["style_custom"] = cu
                    if k in d:
                        cfg[k] = d[k]
                if isinstance(d.get("limits"), dict):
                    cfg["limits"].update(d["limits"])
                if isinstance(d.get("trend"), dict):
                    cfg["trend"].update(d["trend"])
                if isinstance(d.get("layout"), dict):
                    cfg["layout"] = _sanitize_layout(d["layout"])
        except Exception:
            pass
    cfg["modules"] = [m for m in cfg["modules"] if m in config.SCREEN_MODULES]
    try:
        cfg["refresh"] = max(5, min(int(cfg["refresh"]), 3600))
    except Exception:
        cfg["refresh"] = 30
    if cfg["period"] not in ("day", "week", "month"):
        cfg["period"] = "day"
    if cfg["theme"] not in ("dark", "light"):
        cfg["theme"] = "dark"
    try:
        cfg["show_price"] = 1 if int(cfg.get("show_price") or 0) else 0
    except (TypeError, ValueError):
        cfg["show_price"] = 0
    # 样式 / 色系：v1.0.10 起从「一套风格绑死颜色」拆成两个维度。
    # 老库只有 style（如 tech-blue / obsidian），这里按 SCREEN_LEGACY_STYLES 拆成
    # (样式, 色系)；新库两个字段都有，缺色系时按样式的默认色系补。
    _raw_style = str(cfg.get("style") or "").strip()
    _skin, _pal_from_style = screen_style_split(_raw_style)
    cfg["style"] = _skin
    # 色系优先级：库里存过 palette > 由老风格 key 推出来的色系 > 默认深蓝。
    # 注意不能只看 cfg["palette"] —— 它已经被 DEFAULT_SCREEN 的 "deepblue" 填上了，
    # 那样老库（只存了 style）永远走默认值，升级后「指挥舱蓝」会变成深蓝。
    if _has_pal_field:
        cfg["palette"] = screen_palette_key(cfg.get("palette"))
    elif _pal_from_style:
        cfg["palette"] = screen_palette_key(_pal_from_style)
    else:
        cfg["palette"] = screen_palette_key(cfg.get("palette") or "deepblue")
    # 自定义配色的「起手色系」：客户点过哪套色系，自定义就基于那套改，
    # 老库没有这个字段 → 取当前色系。
    _raw_base = str(cfg.get("palette_base") or "").strip()
    if _raw_base and _raw_base != "custom":
        cfg["palette_base"] = screen_palette_key(_raw_base)
    else:
        cfg["palette_base"] = (cfg["palette"] if cfg["palette"] != "custom" else "deepblue")
    # 布局版本兜底：老库没有 version 字段 → price（与旧行为一致）；
    # 关掉「大屏显示金额」就只能用无金额版，配置页左列同时置灰不可点。
    if not isinstance(cfg.get("layout"), dict):
        cfg["layout"] = {"mode": "classic", "version": "price", "cards": {}, "noprice": {}}
    _ver = str(cfg["layout"].get("version") or "").strip().lower()
    cfg["layout"]["version"] = "np" if _ver == "np" else "price"
    # v1.0.14：金额开关与布局版本双向联动。以前只有「关金额 → 无金额版」这一条，
    # 老库因此可以存着 show_price=1 + version=np，选了「无金额版」却满屏金额。
    # 读配置时就收敛，老库直接覆盖升级也立刻生效，不必先去配置页重新保存一次。
    if cfg["layout"]["version"] == "np":
        cfg["show_price"] = 0
    if not cfg["show_price"]:
        cfg["layout"]["version"] = "np"
    # 显示条数边界
    for k in ("recent_tx", "alerts", "stale"):
        try:
            cfg["limits"][k] = max(3, min(int(cfg["limits"].get(k, 10)), 100))
        except Exception:
            cfg["limits"][k] = 10
    # 趋势回溯长度边界
    for k in ("day", "week", "month"):
        try:
            cfg["trend"][k] = max(2, min(int(cfg["trend"].get(k, config.DEFAULT_SCREEN["trend"][k])), 90))
        except Exception:
            cfg["trend"][k] = config.DEFAULT_SCREEN["trend"][k]
    # 呆滞天数：0=跟随系统设置
    try:
        cfg["stale_days"] = max(0, min(int(cfg.get("stale_days") or 0), 3650))
    except Exception:
        cfg["stale_days"] = 0
    # 大屏标题与 LOGO 边界
    cfg["sb_title"] = (str(cfg.get("sb_title") or "")).strip()[:30]
    cfg["sb_subtitle"] = (str(cfg.get("sb_subtitle") or "")).strip()[:50]
    try:
        cfg["sb_show_logo"] = 1 if int(cfg.get("sb_show_logo", 1)) else 0
    except Exception:
        cfg["sb_show_logo"] = 1
    for k, lo, hi in (("sb_logo_height", 0, 160), ("sb_title_size", 0, 36), ("sb_subtitle_size", 0, 22)):
        try:
            cfg[k] = max(lo, min(int(cfg.get(k) or 0), hi))
        except Exception:
            cfg[k] = 0
    for k in ("sb_title_color", "sb_subtitle_color"):
        v = str(cfg.get(k) or "").strip()
        cfg[k] = v.lower() if _is_hex_color(v) else ""
    if not isinstance(cfg.get("sb_font_family"), str) or len(cfg["sb_font_family"]) > 80:
        cfg["sb_font_family"] = ""
    return cfg


def _layout_tree(cards):
    """把卡片矩形拼成一棵 row/col 嵌套树（递归切分），供大屏模板渲染。

    以前大屏是「同一 y 的卡片算一行」，一行只能等高，卡片**无法跨行**，
    所以「饼图占上下两行」这种排布根本表达不出来（写进配置也会被压回一行）。

    这里按矩形边界递归切：
      1) 先找一条不穿过任何卡片的**横切线**（尽量靠上），把上下分开；
      2) 横着切不开（有卡片跨行，比如那张跨两行的饼）就找**竖切线**，把左右分开；
      3) 两条都切不开才退回「按 y 分行」的老办法，保证任何布局都不会丢卡。
    返回节点：{"t":"col"/"row"/"card", "flex":占父容器的格数, "children"/"key":...}
    """
    cards = [c for c in (cards or []) if c.get("w", 0) > 0 and c.get("h", 0) > 0]
    if not cards:
        return None
    total_h = max(c["y"] + c["h"] for c in cards)
    total_w = 12.0

    def build(sub, x0, y0, x1, y1):
        if len(sub) == 1:
            return {"t": "card", "key": sub[0]["key"]}
        # 1) 横切
        for yc in sorted({round(c["y"], 4) for c in sub} | {round(c["y"] + c["h"], 4) for c in sub}):
            if not (y0 + 1e-6 < yc < y1 - 1e-6):
                continue
            up = [c for c in sub if c["y"] + c["h"] <= yc + 1e-6]
            dn = [c for c in sub if c["y"] >= yc - 1e-6]
            if up and dn and len(up) + len(dn) == len(sub):
                kids = []
                for part, a, b in ((up, y0, yc), (dn, yc, y1)):
                    n = build(part, x0, a, x1, b)
                    n["flex"] = round(b - a, 4)
                    kids.append(n)
                return {"t": "col", "children": kids}
        # 2) 竖切
        for xc in sorted({round(c["x"], 4) for c in sub} | {round(c["x"] + c["w"], 4) for c in sub}):
            if not (x0 + 1e-6 < xc < x1 - 1e-6):
                continue
            lf = [c for c in sub if c["x"] + c["w"] <= xc + 1e-6]
            rt = [c for c in sub if c["x"] >= xc - 1e-6]
            if lf and rt and len(lf) + len(rt) == len(sub):
                kids = []
                for part, a, b in ((lf, x0, xc), (rt, xc, x1)):
                    n = build(part, a, y0, b, y1)
                    n["flex"] = round(b - a, 4)
                    kids.append(n)
                return {"t": "row", "children": kids}
        # 3) 兜底：按 y 分行
        groups = {}
        for c in sub:
            groups.setdefault(round(c["y"], 4), []).append(c)
        kids = []
        for y in sorted(groups):
            row = sorted(groups[y], key=lambda c: c["x"])
            kids.append({"t": "row", "flex": round(max(c["h"] for c in row), 4),
                         "children": [{"t": "card", "key": c["key"], "flex": round(c["w"], 4)}
                                      for c in row]})
        return {"t": "col", "children": kids}

    node = build(cards, 0.0, 0.0, total_w, total_h)
    node["flex"] = round(total_h, 4)   # 根节点撑满 #screen-grid（纵向）
    return node


@app.route("/screen")
def screen_page():
    """大屏展示页（公共只读，无需登录；数据由 /api/screen 提供）
    顶部标题与 LOGO：优先用大屏配置中的值，未设置则跟随系统设置的侧栏品牌"""
    conn = get_db()
    try:
        cfg = get_screen_config(conn)
        brand = get_brand(conn)
        sb = {
            "title": cfg["sb_title"] or brand["title"],
            "subtitle": cfg["sb_subtitle"] or brand["subtitle"],
            "logo_file": brand["logo_file"] if cfg["sb_show_logo"] else "",
            "logo_height": cfg["sb_logo_height"] or brand["logo_height"],
            "title_size": cfg["sb_title_size"] or brand["title_size"],
            "subtitle_size": cfg["sb_subtitle_size"] or brand["subtitle_size"],
            # 仅大屏配置里显式设置标题色/副标题色才覆盖；默认跟随大屏风格（避免浅色品牌色盖住标题）
            "title_color": cfg["sb_title_color"] or "",
            "subtitle_color": cfg["sb_subtitle_color"] or "",
            "font_family": cfg["sb_font_family"] or brand["font_family"],
        }
    finally:
        conn.close()
    style_css, style_light = _screen_style_css(cfg)
    chart_cards = {"qty_trend", "amt_trend", "category",
                   "line_qty", "line_amt", "dev_qty", "dev_amt"}
    # 大屏也要跟随「当前用户」的语言：以前只认 URL 和系统默认，
    # 英文界面点进大屏却是中文。?lang= 仍可显式覆盖（投屏场景）。
    _lang = request.args.get("lang") or _i18n.current_lang()
    # 金额类卡片key。category 不在其中：分类占比卡里同时有「金额饼 + 数量饼」，
    # 关金额时只画数量饼（模板端按 show_price 决定画几个），卡片本身不能整块拿掉，
    # 否则无金额版大屏连一个饼都没有。
    amt_keys = {"stat_in_amt", "stat_out_amt", "amt_trend", "line_amt", "dev_amt"}
    # 根据配置过滤卡片：模块勾选 + 金额开关
    show_price = bool(cfg.get("show_price"))
    enabled_modules = set(cfg.get("modules") or [])
    layout = cfg.get("layout") or {}
    layout_cards = layout.get("cards") or {}
    # 卡片版本：price = 有金额版排布，np = 无金额版排布。
    # 由「大屏显示金额 + ⑧ 里选中的那一版」共同决定：关掉金额就只能是 np。
    layout_ver = str(layout.get("version") or "price").lower()
    use_price_layout = bool(show_price) and layout_ver != "np"
    # 无金额版用 noprice 坐标（老 *_np 预设没有 noprice，它自己的 cards 就是无金额版）
    if not use_price_layout and layout.get("noprice"):
        layout_cards = layout.get("noprice") or layout_cards
    # 流式布局：所有可见卡片按优先级排序，自动填满屏幕
    # 卡片类型优先级：统计卡 > 趋势图 > 列表/预警 > 流水
    if use_price_layout:
        # 有金额版：卡片显示顺序
        card_order = [
            "stat_in_qty", "stat_out_qty", "stat_in_amt", "stat_out_amt",  # 第1排：统计卡
            "pending",                                                      # 第2排：待处理
            "qty_trend", "amt_trend",                                      # 第3排：趋势图
            "alerts", "stale", "category",                                 # 第4排：预警/呆滞/分类
            "line_qty", "line_amt", "dev_qty", "dev_amt",                  # 第5排：线体/设备
            "recent_tx",                                                    # 第6排：流水
        ]
        row_size_map = {  # 每种卡片的默认高度（grid-row span）
            "stat": 1, "pending": 1, "trend": 3, "mid": 2, "line": 2, "tx": 3,
        }
    else:
        card_order = [
            "stat_in_qty", "stat_out_qty",                                  # 第1排：统计卡
            "pending",                                                      # 第2排：待处理
            "qty_trend",                                                    # 第3排：趋势图
            "alerts", "stale", "line_qty", "dev_qty",                      # 第4排：预警/呆滞/线体/设备
            "recent_tx",                                                    # 第5排：流水
        ]
    # 根据布局坐标动态生成卡片行分组：按y值分行，同y按x排序，flex宽度按w分配
    visible_cards = []
    screen_cards = {}
    for k, pos in layout_cards.items():
        v = config.SCREEN_CARDS.get(k, {})
        if v.get("module") not in enabled_modules:
            continue
        if not use_price_layout and k in amt_keys:
            continue
        x = float(pos.get("x", 0))
        y = float(pos.get("y", 0))
        # 宽度允许小数：经典布局的 5 个 KPI 卡各占 2.4 格（12/5），
        # 这里若截成 int，5 张卡只占 10 格，右侧会空出一条
        w = float(pos.get("w", 1))
        h = float(pos.get("h", 1))
        visible_cards.append({"key": k, "x": x, "y": y, "w": w, "h": h})
        _lbl = v["label"]
        if k in ("stat_in_qty", "stat_out_qty", "stat_in_amt", "stat_out_amt"):
            # 标题要跟统计周期一致：按周不能还写「今日入库数量」
            _lbl = _stat_card_label(k, cfg.get("period") or "day")
        screen_cards[k] = {
            "label": _i18n.t(_lbl, _lang),
            "module": v.get("module", ""),
        }
    # 经典布局强制规范 KPI 行：入库数量、出库数量、待处理申请、入库金额、出库金额
    # 压成同一排（宽度 12/n 等分）、高度统一 1 格。
    # 老客户库里可能还存着 3+2 的两排旧坐标，这里统一纠正，避免升级后还是旧样子。
    if str(layout.get("mode") or "classic") in ("classic", "classic_np"):
        kpi_order = [k for k in ("stat_in_qty", "stat_out_qty", "pending", "stock_total",
                                 "stat_in_amt", "stat_out_amt")
                     if any(c["key"] == k for c in visible_cards)]
        if kpi_order:
            kpi_base_y = min(c["y"] for c in visible_cards if c["key"] in kpi_order)
            unit_w = 12.0 / len(kpi_order)
            for i, k in enumerate(kpi_order):
                for c in visible_cards:
                    if c["key"] == k:
                        c["x"], c["y"] = unit_w * i, kpi_base_y
                        c["w"], c["h"] = unit_w, 1
    layout_tree = _layout_tree(visible_cards)
    return render_template("screen.html", screen_brand=sb,
                           screen_style=style_css, screen_light=style_light,
                           screen_cards=screen_cards,
                           layout_tree=layout_tree,
                           screen_chart_cards=chart_cards,
                           screen_lang=_lang,
                           # 样式名单（轮询切主题时 render() 靠它把 skin-* 类补回 body）
                           screen_skins=list(SCREEN_SKINS),
                           # 图表色板（饼图/柱线）跟随配色走，前端轮询切风格时也用它重建
                           screen_charts=SCREEN_CHART_PALETTES,
                           # 大屏的语言可能与当前会话不同（大屏是公共投屏），
                           # 数字体系要跟 _lang 走，不能跟 cur_scale。
                           cur_scale=_i18n.num_scale(_lang),
                           cur_locale=_i18n.locale_tag(_lang),
                           cur_lang=_lang,
                           tr_json=_i18n.tr_json(_lang),
                           cfg=cfg,
                           initial_cfg=json.dumps(cfg, ensure_ascii=False))


@app.route("/api/screen")
def api_screen():
    """大屏聚合数据：按配置返回各模块数据（一次请求）"""
    # 大屏也要跟随「当前用户」的语言：以前只认 URL 和系统默认，
    # 英文界面点进大屏却是中文。?lang= 仍可显式覆盖（投屏场景）。
    _lang = request.args.get("lang") or _i18n.current_lang()
    conn = get_db()
    try:
        cfg = get_screen_config(conn)
        # 缓存有效期跟着「刷新间隔」走：设 5 秒就 5 秒换一次数据，设 30 秒仍按 20 秒复用即可
        global _SCREEN_CACHE_TTL
        try:
            _SCREEN_CACHE_TTL = max(1, min(20, int(cfg.get("refresh") or 30)))
        except (TypeError, ValueError):
            _SCREEN_CACHE_TTL = 20
        period = cfg["period"]
        modules = set(cfg["modules"])
        data = {"config": cfg, "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        # 样式只带形状、色系只带变量：轮询里把变量串一起下发，管理员改完色系
        # 已投屏的那块屏下一次刷新就换色，不用重开页面。
        _css, _light = _screen_style_css(cfg)
        data["config"]["style_css"] = _css
        data["config"]["light"] = 1 if _light else 0
        if "stat_cards" in modules:
            data["cards"] = _period_summary(conn, period)
        if "qty_trend" in modules or "amt_trend" in modules:
            data["series"] = _chart_series(conn, period, cfg["trend"])
        if "category" in modules:
            # 分类占比卡里两个饼：金额占比（只在开金额时返回）+ 数量占比（一直返回）。
            # 数量占比不含任何金额信息，无金额版大屏也照样展示。
            cats_q = conn.execute(
                """SELECT COALESCE(NULLIF(p.category,''),?) c, COALESCE(SUM(i.quantity),0) v
                   FROM parts p LEFT JOIN inventory i ON p.id=i.part_id
                   WHERE p.deleted=0
                   GROUP BY c ORDER BY v DESC""",
                (_i18n.t("未分类", _lang),),
            ).fetchall()
            data["category_qty"] = [{"name": r["c"], "value": round(r["v"], 2)} for r in cats_q]
            if cfg["show_price"]:
                cats = conn.execute(
                    """SELECT COALESCE(NULLIF(p.category,''),?) c, COALESCE(SUM(i.quantity*p.unit_price),0) v
                       FROM parts p LEFT JOIN inventory i ON p.id=i.part_id
                       WHERE p.deleted=0
                       GROUP BY c ORDER BY v DESC""",
                    (_i18n.t("未分类", _lang),),
                ).fetchall()
                data["category"] = [{"name": r["c"], "value": round(r["v"], 2)} for r in cats]
        if "alerts" in modules:
            alerts = _stock_alerts(conn)
            lim = cfg["limits"].get("alerts", 10)
            alerts["low"] = alerts["low"][:lim]
            alerts["high"] = alerts["high"][:lim]
            data["alerts"] = alerts
        if "recent_tx" in modules:
            lim = cfg["limits"].get("recent_tx", 10)
            txs = conn.execute(
                """SELECT t.created_at, p.part_code, p.name, t.tx_type, t.quantity, t.location,
                          COALESCE(u.display_name,'-') op_name
                   FROM transactions t
                   JOIN parts p ON t.part_id=p.id
                   LEFT JOIN users u ON t.operator_id=u.id
                   WHERE t.tx_type IN ('in','out')
                   ORDER BY t.id DESC LIMIT ?""",
                (lim,),
            ).fetchall()
            data["recent_tx"] = [dict(r) for r in txs]
        if "pending" in modules:
            pend = conn.execute(
                "SELECT COUNT(*) c FROM applications WHERE status='pending'"
            ).fetchone()["c"]
            val_out = conn.execute(
                """SELECT COUNT(*) c FROM applications a JOIN parts p ON a.part_id=p.id
                   WHERE a.status='pending' AND a.app_type='out' AND p.is_valuable=1"""
            ).fetchone()["c"]
            data["pending"] = {"all": pend, "valuable_out": val_out}
        # 库存总览：物料种数 / 库存总量 / 库存总值（金额仅「显示金额」开启时返回，避免敏感泄露）
        if "stock_overview" in modules:
            r = conn.execute(
                """SELECT COUNT(DISTINCT p.id) c,
                          COALESCE(SUM(i.quantity),0) q,
                          COALESCE(SUM(i.quantity*p.unit_price),0) v
                   FROM parts p LEFT JOIN inventory i ON p.id=i.part_id
                   WHERE p.deleted=0"""
            ).fetchone()
            data["stock_overview"] = {"parts": r["c"], "qty": round(r["q"], 3),
                                      "amount": round(r["v"], 2) if cfg["show_price"] else None}
        if "stale" in modules:
            sd = cfg["stale_days"] or _stale_days(conn)
            data["stale"] = _stale_summary(conn, sd, cfg["limits"].get("stale", 10))
        # 线体/设备领用柱状图（数量/金额分卡；金额卡仅在开启「显示金额」时返回数据，避免敏感泄露）
        # 统计窗口必须跟「统计周期」走：以前这里直接全表 GROUP BY，
        # 管理员把周期改成「按日」后，KPI 卡变成今日而柱状图仍是历年累计，
        # 看上去就是"周期设置对大屏没生效"。
        usage_specs = [
            ("usage_line_qty",   "line_name",   "line_qty", "领用数量", "q"),
            ("usage_line_amt",   "line_name",   "line_amt", "领用金额", "a"),
            ("usage_device_qty", "device_name", "dev_qty",  "领用数量", "q"),
            ("usage_device_amt", "device_name", "dev_amt",  "领用金额", "a"),
        ]
        if ({"usage_line_qty", "usage_line_amt", "usage_device_qty", "usage_device_amt"} & modules):
            _win_s, _win_e = _screen_period_window(period)
            lim = max(5, min(int(cfg["limits"].get("usage", 10) or 10), 50))
            for mod, col, key, sname, metric in usage_specs:
                if mod not in modules:
                    continue
                if mod.endswith("_amt") and not cfg["show_price"]:
                    continue
                sname = _i18n.t(sname, _lang)
                # 金额卡按金额排、数量卡按数量排（以前统一按数量排，金额榜里会混进
                # 数量大但金额小的线体，看起来像数据错位）
                rows = conn.execute(
                    """SELECT COALESCE(NULLIF(%s,''),?) d, COALESCE(SUM(quantity),0) q,
                              COALESCE(SUM(amount),0) a
                       FROM transactions
                       WHERE tx_type='out' AND IFNULL(out_type,'')<>'stocktake'
                         AND created_at>=? AND created_at<?
                       GROUP BY d ORDER BY %s DESC LIMIT ?""" % (col, "a" if metric == "a" else "q"),
                    (_i18n.t("未分配", _lang),
                     _win_s.strftime("%Y-%m-%d %H:%M:%S"),
                     _win_e.strftime("%Y-%m-%d %H:%M:%S"), lim),
                ).fetchall()
                items = [{"name": r["d"], "qty": round(r["q"], 3), "amount": round(r["a"], 2)} for r in rows]
                data[key] = {"labels": [i["name"] for i in items],
                             "series": [{"name": sname,
                                         "data": [i["qty"] if metric == "q" else i["amount"] for i in items]}]}
        # KPI 卡标题跟随周期：按周时「今日入库数量」要变成「本周入库数量」
        data["stat_labels"] = {
            _k: _i18n.t(_stat_card_label(_k, period), _lang)
            for _k in ("stat_in_qty", "stat_out_qty", "stat_in_amt", "stat_out_amt")
        }
        # v1.0.14：/api/screen 是公共投屏接口（无登录鉴权），关掉「大屏显示金额」
        # 不能只靠前端跳过绘制 —— 以前 cards / series / stale 三处金额数值仍明文下发，
        # 打开 F12 就能翻到。这里在出口统一收敛：保留字段结构、数值一律置 None，
        # 不动被分析页复用的 _period_summary / _chart_series / _stale_summary。
        if not cfg["show_price"]:
            # 注意：cards/series/stale 由带进程内缓存的函数生成，缓存里存的是**同一个对象**。
            # 必须整体换成新 dict/list，不能原地改字段 —— 否则会把缓存也改成 None，
            # 管理员下次打开金额时拿到的是被污染的空数据。
            _kc = data.get("cards")
            if isinstance(_kc, dict):
                _clean = dict(_kc, in_amt=None, out_amt=None)
                if isinstance(_kc.get("prev"), dict):
                    _clean["prev"] = dict(_kc["prev"], in_amt=None, out_amt=None)
                data["cards"] = _clean
            _ks = data.get("series")
            if isinstance(_ks, dict):
                _clean = dict(_ks)
                for _k in ("in_amt", "out_amt"):
                    _clean[_k] = [None] * len(_ks.get(_k) or [])
                data["series"] = _clean
            _kt = data.get("stale")
            if isinstance(_kt, dict):
                _clean = dict(_kt, amount=None)
                _clean["top"] = [dict(r, amount=None) if isinstance(r, dict) else r
                                 for r in (_kt.get("top") or [])]
                data["stale"] = _clean
        return jsonify(data)
    finally:
        conn.close()


def _layout_from_form(form):
    """从表单解析 layout_mode + layout_cards(JSON)"""
    try:
        raw = json.loads(form.get("layout_cards") or "{}")
    except Exception:
        raw = {}
    return _sanitize_layout({"mode": (form.get("layout_mode") or "classic").strip(),
                             "version": (form.get("layout_version") or "price").strip(),
                             "cards": raw})


@app.route("/screen/config", methods=["GET", "POST"])
@login_required
@perm_required("screen_config")
def screen_config():
    if request.method == "POST":
        modules = [k for k in config.SCREEN_MODULES if request.form.get("mod_" + k)]
        refresh = request.form.get("refresh", "30")
        period = request.form.get("period", "day")
        theme = request.form.get("theme", "dark")
        limits = {
            "recent_tx": _int(request.form.get("lim_recent_tx", 10) or 10),
            "alerts": _int(request.form.get("lim_alerts", 10) or 10),
            "stale": _int(request.form.get("lim_stale", 10) or 10),
        }
        trend = {
            "day": _int(request.form.get("trend_day", 14) or 14),
            "week": _int(request.form.get("trend_week", 8) or 8),
            "month": _int(request.form.get("trend_month", 6) or 6),
        }
        stale_days = _int(request.form.get("stale_days", 0) or 0)
        # 表单现在分两个字段：style=样式（形状）、palette=色系（颜色）。
        # 老页面/直连 POST 只带 style 时按老映射拆，不至于把形状随手一改就丢颜色。
        style, _pal_legacy = screen_style_split(request.form.get("style", "aurora"))
        _pal_raw = (request.form.get("palette") or "").strip()
        if _pal_raw:
            palette = screen_palette_key(_pal_raw)
        elif _pal_legacy:
            palette = screen_palette_key(_pal_legacy)
        else:
            palette = "deepblue"
        # 自定义色系的起手 = 上一次选中的色系（前端也会把当前选中的带上来）。
        # 先读旧配置兜底，避免老页面/直连 POST 没带 palette_base 时又退回默认色。
        _prev_conn = get_db()
        try:
            _prev = get_screen_config(_prev_conn)
        finally:
            _prev_conn.close()
        _raw_base = (request.form.get("palette_base") or "").strip()
        if _raw_base and _raw_base != "custom":
            base_palette = screen_palette_key(_raw_base)
        else:
            base_palette = screen_palette_key(_prev.get("palette") or "deepblue")
            if base_palette == "custom":
                base_palette = "deepblue"
        if palette != "custom":
            base_palette = palette     # 直接点了色系：下次自定义就以这套为起点
        style_custom = {}
        for k in SCREEN_PALETTES["deepblue"]["vars"]:
            v = request.form.get("sc_" + k, "").strip()
            if _is_hex_color(v):
                style_custom[k] = v.lower()
        def _sb_int(k):
            try:
                return _int(request.form.get(k, 0) or 0)
            except (TypeError, ValueError):
                return 0
        cfg = {
            "modules": modules,
            "known_modules": list(config.SCREEN_MODULES.keys()),  # 记录所有已知模块
            "refresh": refresh,
            "period": period,
            "theme": theme,
            "show_price": 1 if request.form.get("show_price") else 0,
            "limits": limits,
            "trend": trend,
            "stale_days": stale_days,
            "alert_scroll_speed": float(request.form.get("alert_scroll_speed", 2) or 2),
            "tx_scroll_speed": float(request.form.get("tx_scroll_speed", 2) or 2),
            # 大屏标题与 LOGO（空=跟随系统设置的侧栏品牌）
            "sb_title": request.form.get("sb_title", "").strip()[:30],
            "sb_subtitle": request.form.get("sb_subtitle", "").strip()[:50],
            "sb_show_logo": 1 if request.form.getlist("sb_show_logo") and request.form.getlist("sb_show_logo")[-1] == "1" else 0,
            "sb_logo_height": _sb_int("sb_logo_height"),
            "sb_title_size": _sb_int("sb_title_size"),
            "sb_subtitle_size": _sb_int("sb_subtitle_size"),
            "sb_title_color": request.form.get("sb_title_color", "").strip(),
            "sb_subtitle_color": request.form.get("sb_subtitle_color", "").strip(),
            "sb_font_family": request.form.get("sb_font_family", "").strip(),
            "style": style,
            "palette": palette,
            "palette_base": base_palette,
            # 兼容位：老版本读的是 style_custom_base，留着不碍事
            "style_custom_base": base_palette,
            "style_custom": style_custom,
            "layout": _layout_from_form(request.form),
        }
        # v1.0.14：存进库的永远是自洽的一对 —— 选无金额版 = 关金额，关金额 = 无金额版。
        # 读配置时还有同样的兜底（覆盖老库脏数据），两处口径必须保持一致。
        if cfg["layout"]["version"] == "np":
            cfg["show_price"] = 0
        if not cfg["show_price"]:
            cfg["layout"]["version"] = "np"
        conn = get_db()
        try:
            db.set_setting(conn, "screen_config", json.dumps(cfg, ensure_ascii=False))
            conn.commit()
        finally:
            conn.close()
        # 提示统一交给 _screen_reply：它会按请求类型二选一（AJAX → JSON，普通 → flash）。
        # 这里再 flash 一次就是「一次保存弹两条」的来源，而且 AJAX 时没有模板去消费
        # get_flashed_messages，那条 flash 会一直压在 session 里，等用户下次打开任意
        # 页面（比如系统设置）才突然冒出来 —— 看着像「切页面又弹一次大屏配置」。
        return _screen_reply("大屏配置已保存（大屏页面将按新配置刷新）", "success")
    conn = get_db()
    try:
        cfg = get_screen_config(conn)
    finally:
        conn.close()
    _lang = _i18n.current_lang()
    _lay = {}
    for _mk, _mv in config.SCREEN_LAYOUTS.items():
        if _mv.get("hidden"):
            continue          # 老的高端模板排版已归并到五套，不再出现在选择器里
        _lay[_mk] = dict(_mv)
        _lay[_mk]["name"] = _i18n.t(_mv.get("name", _mk), _lang)
    _sty = {}
    for _sk, _sv in SCREEN_SKINS.items():
        _sty[_sk] = dict(_sv)
        _sty[_sk]["name"] = _i18n.t(_sv.get("name", _sk), _lang)
        _sty[_sk]["tag"] = _i18n.t(_sv.get("tag", ""), _lang)
    _pal = {}
    for _pk, _pv in SCREEN_PALETTES.items():
        _pal[_pk] = dict(_pv)
        _pal[_pk]["name"] = _i18n.t(_pv.get("name", _pk), _lang)
        _pal[_pk]["tag"] = _i18n.t(_pv.get("tag", ""), _lang)
        _pal[_pk]["skin"] = ""      # 色系不带形状，模板只取 vars
    return render_template("screen_config.html", cfg=cfg, SCREEN_SKINS=_sty,
                           SCREEN_PALETTES=_pal,
                           palette_base=cfg.get("palette_base") or "deepblue",
                           SCREEN_LAYOUTS=_lay,
                           SCREEN_CARDS={k: _i18n.t(v["label"], _lang) if isinstance(v, dict) and "label" in v else v for k, v in config.SCREEN_CARDS.items()})


# ------------------------------------------------------------------
# 物料管理（查询 + 信息维护建档）
# ------------------------------------------------------------------
def _query_parts(conn, kw="", cat="", state="", plain_apps=False, sort="", order="asc",
                 search_col="all", filter_code="", filter_name="", filter_category="", filter_spec=""):
    """信息查询公共逻辑（信息查询页 / 信息维护页共用）
    plain_apps=True：适用产线/设备不带「（整线）」「（未配产线）」标注（查询页）
    sort：允许排序的字段白名单
    order：asc/desc
    search_col：搜索列：all=全列，其他值匹配指定列"""
    _search_cols = {
        "all": ["part_code", "name", "category", "spec", "unit"],
        "part_code": ["part_code"],
        "name": ["name"],
        "category": ["category"],
        "spec": ["spec"],
        "unit": ["unit"],
        "unit_price": ["unit_price"],
        "stock": ["stock"],
        "status": ["status"],
        "stock_locs_text": ["stock_locs_text"],
        "apps_text": ["apps_text"],
        "is_valuable": ["is_valuable"],
        "cond_mix": ["cond_mix_text"],
    }
    sql = """SELECT p.*, COALESCE(SUM(i.quantity),0) stock
             FROM parts p LEFT JOIN inventory i ON p.id=i.part_id
             WHERE p.deleted=0"""
    params = []
    if state != "deprecated":
        sql += " AND COALESCE(p.deprecated,0)=0"
    else:
        sql += " AND COALESCE(p.deprecated,0)=1"
    # 区分SQL可过滤字段和后处理字段
    _sql_filter_cols = {"part_code", "name", "category", "spec", "unit", "unit_price"}
    post_kw = None
    post_search_col = search_col
    if kw:
        cols = _search_cols.get(search_col, _search_cols["all"])
        sql_cols = [c for c in cols if c in _sql_filter_cols]
        if sql_cols:
            kw_esc = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like_clauses = [f"p.{c} LIKE ? ESCAPE '\\'" for c in sql_cols]
            params += [f"%{kw_esc}%"] * len(sql_cols)
            sql += " AND (" + " OR ".join(like_clauses) + ")"
        # 非SQL字段的搜索在Python后处理
        non_sql_cols = [c for c in cols if c not in _sql_filter_cols]
        if non_sql_cols:
            post_kw = kw.lower()
    if cat:
        sql += " AND p.category=?"
        params.append(cat)
    # 列级过滤
    if filter_code:
        sql += " AND p.part_code LIKE ?"
        params.append(f"%{filter_code}%")
    if filter_name:
        sql += " AND p.name LIKE ?"
        params.append(f"%{filter_name}%")
    if filter_category:
        sql += " AND p.category LIKE ?"
        params.append(f"%{filter_category}%")
    if filter_spec:
        sql += " AND p.spec LIKE ?"
        params.append(f"%{filter_spec}%")
    sql += " GROUP BY p.id"
    rows = conn.execute(sql, params).fetchall()
    _efs = get_extra_fields(conn)  # 自定义列表头只读一次，逐行归一 extra 用
    loc_map = {l["id"]: l for l in conn.execute("SELECT * FROM locations").fetchall()}
    auto_cfg = _auto_stock_cfg(conn)
    # 逐行查询改成三次整表查询：库存分布 / 适用产线设备 / 建议上下限。
    # 2 万个物料时这里是 6 万条 SQL 与 3 条 SQL 的区别（251s → 亚秒级）。
    _inv_map = {}
    # v1.0.39：多带两列（condition / 库位属性）——列表要显示「良品几件、维修品几件」，
    # 筛选要按「库存落在良品区 / 待维修区 / 待报废区」分组，都靠这两列。
    for r in conn.execute(
            "SELECT i.part_id, i.location, i.quantity, IFNULL(i.repair_qty,0) repair_qty, "
            "       COALESCE(i.condition,'good') cond, COALESCE(l.attribute,'good') attr "
            "FROM inventory i LEFT JOIN locations l ON l.id=i.location_id "
            "ORDER BY i.in_date, i.id").fetchall():
        _inv_map.setdefault(r["part_id"], []).append(
            {"location": r["location"], "quantity": r["quantity"],
             "repair_qty": r["repair_qty"], "cond": r["cond"], "attr": r["attr"]})
    _app_map = {}
    for r in conn.execute(
            """SELECT pa.part_id, pa.line_id, pa.equipment_id, pl.name line_name,
                      e.name equip_name, e.model equip_model
               FROM part_applications pa
               LEFT JOIN production_lines pl ON pa.line_id=pl.id
               LEFT JOIN equipment e ON pa.equipment_id=e.id
               ORDER BY pa.id""").fetchall():
        _app_map.setdefault(r["part_id"], []).append(dict(r))
    _sugg_map = _suggest_map(conn, auto_cfg) if auto_cfg.get("enabled") else {}
    parts = []
    for r in rows:
        d = dict(r)
        # 自动上下限：勾选且全局开启时，用算出来的值覆盖显示；无历史消耗则回退手填
        d["auto_min_on"] = bool(auto_cfg.get("enabled") and d.get("auto_min"))
        if d["auto_min_on"]:
            slo, shi = _sugg_map.get(d["id"], (None, None))
            if slo is not None:
                d["min_stock_disp"] = slo
                d["max_stock_disp"] = shi
                d["auto_fallback"] = 0
            else:
                d["min_stock_disp"] = d["min_stock"]
                d["max_stock_disp"] = d["max_stock"]
                d["auto_fallback"] = 1
        else:
            d["min_stock_disp"] = d["min_stock"]
            d["max_stock_disp"] = d["max_stock"]
            d["auto_fallback"] = 0
        # 整数计数的物料：上下限只显示整数。min_stock 是 REAL 列，库里 100 取回来是 100.0，
        # 直接渲染就成了「100.0/10000.0」，看着像这套类型设置根本没生效。
        if not d.get("qty_decimal"):
            for _k in ("min_stock_disp", "max_stock_disp"):
                if d.get(_k) not in (None, ""):
                    d[_k] = _round_int(d[_k])
        # 状态（低于/超过下限）在下面算出 avail 之后再判——v1.0.40 起上下限只管可用数
        d["location_text"] = db.location_text(loc_map.get(d.get("location_id"))) if d.get("location_id") else ""
        # 所有库存库位（多库位显示；含 0 库存的登记库位，避免「选了库位但列表看不到」）
        sls = _inv_map.get(d["id"], [])
        d["stock_locs_text"] = _i18n.mk("；").join(
            "%s ×%s%s" % (_loc_disp(s["location"]), _fmt_qty_s(s["quantity"]),
                          ("" if not _num(s.get("repair_qty")) else
                           _xl("（维修 {p1}）", p1=_fmt_qty_s(s.get("repair_qty")))))
            for s in sls)
        # v1.0.21：这条物料全仓一共有多少件维修件，列表/详情里直接显示数字
        d["repair_total"] = sum(_num(s.get("repair_qty")) for s in sls)
        # v1.0.39：库存构成——按配件状态统计件数（良品 / 维修品 / 不良品），
        # 以及按库位属性统计（良品区 / 待维修区 / 待报废区），供列表列 + 状态筛选。
        # 原来这一列显示的是 parts.is_repair（「维修件优先扣减」手工标记），
        # 词条键 status_repair 没登记译文，界面上直接是裸键名，看着像坏字段。
        # v1.0.40（用户口径）：构成分四类，且上下限只管控「可用」——
        #   良品     = 良品区 & condition=good
        #   维修品   = 良品区 & condition=repair（修好能用的）
        #   待维修品 = 待维修区库存 + 良品区里标了不良的（还没挪过去）
        #   待报废品 = 待报废区库存
        # 可用 = 良品 + 维修品；不良 = 待维修品 + 待报废品。
        # 原来 status 拿 stock（含不良品）去比上下限，一堆待报废件把库存「填满」，
        # 明明能用的没几件却显示「正常」，该补的货补不上。
        c_stat = {"good": 0.0, "defective": 0.0, "repair": 0.0}
        a_stat = {"good": 0.0, "pending_repair": 0.0, "pending_scrap": 0.0}
        for s in sls:
            _q = _num(s.get("quantity"))
            if _q <= 0:
                continue
            _c = s.get("cond") if s.get("cond") in c_stat else "good"
            _a = s.get("attr") if s.get("attr") in a_stat else "good"
            c_stat[_c] += _q
            a_stat[_a] += _q
        d["cond_stat"] = c_stat
        d["attr_stat"] = a_stat
        b_good = b_repair = b_wait_repair = b_wait_scrap = 0.0
        for s in sls:
            _q = _num(s.get("quantity"))
            if _q <= 0:
                continue
            _c = s.get("cond") if s.get("cond") in c_stat else "good"
            _a = s.get("attr") if s.get("attr") in a_stat else "good"
            if _a == "pending_scrap":
                b_wait_scrap += _q
            elif _a == "pending_repair":
                b_wait_repair += _q
            elif _c == "repair":
                b_repair += _q
            elif _c == "defective":
                b_wait_repair += _q
            else:
                b_good += _q
        d["avail"] = round(b_good + b_repair, 3)      # 可用（良品 + 维修品）
        d["unusable"] = round(b_wait_repair + b_wait_scrap, 3)  # 不良（待维修品 + 待报废品）
        d["wait_repair"] = round(b_wait_repair, 3)    # 待维修品（含良品区里标了不良的）
        d["wait_scrap"] = round(b_wait_scrap, 3)      # 待报废品
        _mix = []
        for _k, _q in (("good", b_good), ("repair", b_repair),
                       ("pending_repair", b_wait_repair), ("pending_scrap", b_wait_scrap)):
            if _q > 0:
                _mix.append({"key": _k,
                             "label": (CONDITION_NAMES[_k] if _k in CONDITION_NAMES
                                       else _xl("待维修品" if _k == "pending_repair" else "待报废品")),
                             "qty": _fmt_qty_s(_q)})
        d["cond_mix"] = _mix
        d["cond_mix_text"] = _i18n.mk("、").join("%s %s" % (m["label"], m["qty"]) for m in _mix)
        # v1.0.40：上下限按可用数判（含维修 OK 的维修品，不含待维修/待报废）
        d["status"] = ("low" if d["avail"] < d["min_stock_disp"]
                       else ("high" if (d["max_stock_disp"] and d["avail"] > d["max_stock_disp"]) else "ok"))
        # 适用产线/设备
        apps = _app_map.get(d["id"], [])
        d["apps_text"] = _i18n.mk("；").join(_apps_label(a, plain=plain_apps) for a in apps)
        d["extra"] = _extra_norm(d.get("extra"), _efs)
        if state == "low" and d["status"] != "low":
            continue
        if state == "high" and d["status"] != "high":
            continue
        if state == "nonzero" and (d.get("stock") or 0) <= 0:
            continue
        # v1.0.39：库存状态按「件在哪类库位 / 什么状态」筛
        #   good          良品（含维修 OK 的维修品）= 可用数 > 0
        #   pending_repair 待维修品——待维修区库存（含良品区标了不良的）
        #   pending_scrap  待报废品——待报废区库存
        if state == "good" and d["avail"] <= 0:
            continue
        if state == "pending_repair" and d["wait_repair"] <= 0:
            continue
        if state == "pending_scrap" and d["wait_scrap"] <= 0:
            continue
        # v1.0.67：内置物料图（随程序发布，data/ 不进包）作为上传图的兜底。
        # 用户上传的 image_file 永远优先；没有内置图才显示「无图」上传入口。
        d["img_url"] = ""
        # 数据层回归会在 app context 外直接调 _query_parts，此时不能构造 URL
        if has_app_context():
            if d.get("image_file"):
                d["img_url"] = url_for("media", fname=d["image_file"])
            else:
                _code = str(d.get("part_code") or "")
                if re.fullmatch(r"[A-Za-z0-9_\-]+", _code):
                    _rel = "partimg/" + _code + ".jpg"
                    if os.path.exists(os.path.join(app.static_folder, "partimg", _code + ".jpg")):
                        d["img_url"] = url_for("static", filename=_rel)
        parts.append(d)
    # 后处理字段过滤（库存/状态/库位/产线/贵重）
    if post_kw:
        cols = _search_cols.get(post_search_col, _search_cols["all"])
        def _match(d):
            for c in cols:
                v = d.get(c) or ""
                if c == "is_valuable":
                    v = "贵重" if v else ""
                if post_kw in str(v).lower():
                    return True
            return False
        parts = [d for d in parts if _match(d)]
    # 表头排序（白名单字段，防止注入；支持自定义extra列）
    _sort_whitelist = {"part_code": "part_code", "name": "name", "category": "category",
                       "spec": "spec", "unit": "unit", "unit_price": "unit_price",
                       # v1.0.40：列表主数字改成「可用」，排序跟着走同一口径
                       "stock": "avail", "avail": "avail", "min_stock": "min_stock", "max_stock": "max_stock",
                       "supplier": "supplier", "remark": "remark", "status": "status",
                       "is_valuable": "is_valuable", "stock_locs_text": "stock_locs_text",
                       "apps_text": "apps_text", "cond_mix_text": "cond_mix_text"}
    if sort in _sort_whitelist:
        key = _sort_whitelist[sort]
        reverse = (order == "desc")
        # 统一转成字符串排序，避免整数/布尔和字符串混排比类型错误
        parts.sort(key=lambda x: str(x.get(key) or ""), reverse=reverse)
    elif sort and sort.startswith("extra_"):
        # 自定义扩展列排序：从extra字典里取对应key
        extra_key = sort[len("extra_"):]
        reverse = (order == "desc")
        parts.sort(key=lambda x: str(x["extra"].get(extra_key) or ""), reverse=reverse)
    return parts


def _categories(conn):
    """分类下拉的选项：字典优先（按后台配置的顺序），后面补库里已有但字典里没有的历史值。
    历史值一定要带上，否则老数据的分类在筛选和编辑框里会「消失」。"""
    out = get_part_categories(conn)
    for r in conn.execute("SELECT DISTINCT category FROM parts WHERE category<>'' AND deleted=0"):
        c = r["category"]
        if c and c not in out:
            out.append(c)
    return out


# 列表页每页条数：只认这四档，默认 10。不做「全部」——
# 2 万条整表铺开是 20MB HTML、几百秒渲染，正是分页要避免的用法；
# 要看全量用导出。会话里可能残留旧版本的 'all'，这里一并挡掉。
_PARTS_PER_PAGE = (10, 20, 50, 100)
_PARTS_PER_PAGE_DEFAULT = 10


def _parts_per_page(session_key):
    """取当前请求的每页条数，并把用户的选择记到会话里（换页时沿用）。"""
    raw = request.args.get("per_page", "").strip()
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = 0
    if val not in _PARTS_PER_PAGE:
        try:
            val = int(session.get(session_key, _PARTS_PER_PAGE_DEFAULT))
        except (TypeError, ValueError):
            val = _PARTS_PER_PAGE_DEFAULT
        if val not in _PARTS_PER_PAGE:
            val = _PARTS_PER_PAGE_DEFAULT
    session[session_key] = str(val)
    return val


@app.route("/parts")
@login_required
@perm_required("query")
def parts_page():
    kw = request.args.get("kw", "").strip()
    cat = request.args.get("category", "").strip()
    state = request.args.get("state", "").strip()  # low/high/all
    sort = request.args.get("sort", "").strip()
    order = request.args.get("order", "asc").strip()
    search_col = request.args.get("search_col", "all").strip()
    filter_code = request.args.get("filter_code", "").strip()
    filter_name = request.args.get("filter_name", "").strip()
    filter_category = request.args.get("filter_category", "").strip()
    filter_spec = request.args.get("filter_spec", "").strip()
    conn = get_db()
    try:
        parts = _query_parts(conn, kw, cat, state, plain_apps=True, sort=sort, order=order,
                             search_col=search_col,
                             filter_code=filter_code, filter_name=filter_name,
                             filter_category=filter_category, filter_spec=filter_spec)
        cats = _categories(conn)
        locations = _all_locations(conn)
        hc_full = get_head_clip(conn)
        hc = dict(hc_full["std"])   # 模板里直接 hc.category / hc.code 取
        extra_fields = get_extra_fields(conn)
        for ef in extra_fields:
            ef["clip"] = hc_full["extra"].get(ef["key"], config.DEFAULT_HEAD_CLIP)
        total_kinds = conn.execute("SELECT COUNT(*) FROM parts WHERE deleted=0").fetchone()[0]
        total_stock = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM inventory").fetchone()[0]
    finally:
        conn.close()
    # 分页：20 万级物料不能整表铺出来（2 万行 ≈ 20MB HTML / 4 分钟）
    parts_total = len(parts)
    per_page = _parts_per_page("parts_per_page")
    parts_pages = max(1, (parts_total + per_page - 1) // per_page)
    try:
        parts_page_no = max(1, _int(request.args.get("page", 1) or 1))
    except (TypeError, ValueError):
        parts_page_no = 1
    parts_page_no = min(parts_page_no, parts_pages)
    shown = parts[(parts_page_no - 1) * per_page: parts_page_no * per_page]
    return render_template("parts.html", parts=shown, categories=cats,
                           kw=kw, cat=cat, state=state, locations=locations,
                           extra_fields=extra_fields,
                           hc=hc,
                           total_kinds=total_kinds, total_stock=total_stock,
                           parts_total=parts_total, parts_page=parts_page_no,
                           parts_pages=parts_pages, parts_per_page=str(per_page),
                           sort=sort, order=order, search_col=search_col,
                           filter_code=filter_code, filter_name=filter_name,
                           filter_category=filter_category, filter_spec=filter_spec)


# ------------------------------------------------------------------
# 物料导入 / 导出（权限：parts_manage 的 import / export 操作级）
# ------------------------------------------------------------------
def _xlsx_col_to_idx(col):
    """列字母 → 0 基序号：A=0, Z=25, AA=26"""
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _xlsx_first_sheet_path(zf):
    """按 workbook.xml 找出「第一张工作表」在包里的真实路径。

    以前写死 xl/worksheets/sheet1.xml：用户在 Excel/WPS 里改过表名、插过表、
    删过表再另存，第一张表的文件名就不一定是 sheet1.xml，于是「用下载的模板
    另存后导入」会直接解析失败，看着像模板坏了。
    """
    from xml.etree import ElementTree as ET
    NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    fallback = "xl/worksheets/sheet1.xml"
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        sheets = wb.find("{%s}sheets" % NS_MAIN)
        if sheets is None or not len(sheets):
            return fallback
        rid = list(sheets)[0].get("{%s}id" % NS_R)
        if not rid:
            return fallback
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rels:
            if rel.get("Id") == rid:
                target = (rel.get("Target") or "").replace("\\", "/").lstrip("/")
                if not target:
                    return fallback
                if not target.startswith("xl/"):
                    target = "xl/" + target
                return target
    except Exception:
        pass
    return fallback


def _read_xlsx_rows(fileobj):
    """纯标准库读取 .xlsx 第一个工作表 → 二维列表（含表头行，空单元格为''）"""
    import zipfile
    from xml.etree import ElementTree as ET
    NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    zf = zipfile.ZipFile(fileobj)
    shared = []
    try:
        with zf.open("xl/sharedStrings.xml") as f:
            root = ET.fromstring(f.read())
        for si in root.iter("{%s}si" % NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % NS)))
    except KeyError:
        pass
    with zf.open(_xlsx_first_sheet_path(zf)) as f:
        root = ET.fromstring(f.read())
    grid_rows = []
    for row in root.iter("{%s}row" % NS):
        cells = {}
        for c in row.findall("{%s}c" % NS):
            ref = c.get("r") or ""
            col = "".join(ch for ch in ref if ch.isalpha())
            idx = _xlsx_col_to_idx(col)
            t = c.get("t") or ""
            v = c.find("{%s}v" % NS)
            is_ = c.find("{%s}is" % NS)
            val = ""
            if t == "s" and v is not None:
                n = int(v.text or 0)
                val = shared[n] if n < len(shared) else ""
            elif t == "inlineStr" and is_ is not None:
                val = "".join(x.text or "" for x in is_.iter("{%s}t" % NS))
            elif v is not None:
                val = v.text or ""
            cells[idx] = val
        if cells:
            grid_rows.append(cells)
    if not grid_rows:
        return []
    max_col = max(max(r.keys()) for r in grid_rows) + 1
    return [[r.get(i, "") for i in range(max_col)] for r in grid_rows]


def _read_csv_rows(fileobj):
    """读取 CSV（自动识别 UTF-8 / GBK 编码）"""
    import csv
    raw = fileobj.read()
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    rows = list(csv.reader(text.splitlines()))
    return rows


def _norm_loc(t):
    """库位文本归一化：全角/各种破折号统一、去空格、忽略大小写"""
    s = str(t or "").strip().upper()
    for ch in ("—", "–", "－", "−"):
        s = s.replace(ch, "-")
    return re.sub(r"\s+", "", s)


def _loc_index(conn):
    """location_text → locations.id 索引（含「三级不带格位」变体）。
    库位各段本身就带 '-'（WH-01 / R-01 / S-01 / B-01），按 '-' 切分取前几段
    永远切不出正确的四段，所以必须先建索引反查，不能靠数段数猜。"""
    idx = {}
    for loc in conn.execute("SELECT * FROM locations"):
        try:
            txt = db.location_text(loc)
        except Exception:
            txt = ""
        key = _norm_loc(txt)
        if key and key not in idx:
            idx[key] = loc["id"]
        base = "%s-%s-%s" % (loc["warehouse"], loc["rack"], loc["shelf"])
        bkey = _norm_loc(base)
        if bkey and bkey not in idx:
            idx[bkey] = loc["id"]
    return idx


def _parse_loc_text(conn, txt, index=None):
    """解析「仓库-货架-货位-格位」文本 → locations.id；无法解析/待定/无 → None"""
    if not txt:
        return None
    txt = str(txt).strip()
    if _norm_loc(txt) in ("待定", "无", "-", "—", "PENDING", "TBD"):
        return None
    # 去掉尾部的 ×数量
    txt = re.sub(r"(?:[×*]|\s+[xX])\s*[\d.]+\s*$", "", txt).strip()
    if not txt:
        return None
    if index is None:
        index = _loc_index(conn)
    hit = index.get(_norm_loc(txt))
    if hit:
        return hit
    # 兜底：段内不含 '-' 的旧式短码（如 WH / R / 01 三段）
    segs = [x.strip() for x in re.split(r"[-—]", txt) if x.strip()]
    if len(segs) >= 3:
        wh, rk, sh = segs[0], segs[1], segs[2]
        bn = segs[3] if len(segs) > 3 else ""
        row = conn.execute(
            "SELECT id FROM locations WHERE warehouse=? AND rack=? AND shelf=? AND COALESCE(bin,'')=?",
            (wh, rk, sh, bn)).fetchone()
        if row:
            return row["id"]
    return None


def _parse_loc_entries(conn, txt, index=None):
    """把「WH-01-...-B-02 ×110; WH-02-...-B-01 ×40」拆成 [(loc_id, 数量或None), ...]
    支持分号/换行分隔多库位，每段可带 ×N 数量（导入导出都按这个格式写）。"""
    if not txt:
        return []
    if index is None:
        index = _loc_index(conn)
    out = []
    for seg in re.split(r"[;；\n]", str(txt)):
        seg = seg.strip()
        if not seg:
            continue
        m = re.search(r"(?:[×*]|\s+[xX])\s*([\d.]+)\s*$", seg)
        q = _num(m.group(1)) if m else None
        lid = _parse_loc_text(conn, seg, index=index)
        if lid:
            out.append((lid, q))
    return out


def _norm_head(h):
    """表头归一化：小写、去括号内容（币种/单位）、标点统一为空格、压缩空白。
    例：'Unit Price (CNY)'→'unit price'，'Mat. No.'→'mat no'，'适用产线/设备'→'适用产线 设备'"""
    s = str(h or "").strip().lower()
    s = re.sub(r"[（(][^（()）]*[)）]", " ", s)
    s = re.sub(r"[.:：、,，/\\|_\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# 导入表头别名：客户手里的表格可能是中文模板、英文模板（系统自己在英文界面下载的就是
# 「Part Code / Qty Type / Min Stock」这套），也可能是各语言模板。以前只认中文列名，
# 非中文表格会被整份拒绝（「表头缺少必填列」）。
_HDR_ALIAS = {
    "code": ("料号", "物料编码", "物料编号", "编号", "品号", "part code", "part no", "part no.",
             "part number", "mat no", "mat no.", "mat code", "mat number", "material no",
             "material code", "item code", "item no", "code", "kode", "品番", "部品番号",
             "품번", "품목코드", "artikelnummer", "artikel nr", "référence", "reference", "réf",
             "código", "codigo", "referencia", "артикул", "код", "รหัส", "รหัสวัสดุ",
             "mã", "mã vật tư"),
    "name": ("名称", "品名", "name", "nama", "nama barang", "bezeichnung", "benennung",
             "désignation", "designation", "libellé", "nombre", "descripción", "описание",
             "наименование", "ชื่อ", "ชื่อวัสดุ", "tên", "tên vật tư"),
    "cat": ("分类", "类别", "category", "kategori", "分類", "분류", "kategorie", "catégorie",
            "categoría", "категория", "หมวดหมู่", "danh mục"),
    "spec": ("规格", "规格型号", "spec", "specification", "spesifikasi", "規格", "규격",
             "spezifikation", "spécification", "especificación", "спецификация",
             "ข้อมูลจำเพาะ", "quy cách"),
    "unit": ("单位", "unit", "satuan", "単位", "단위", "einheit", "unité", "unidad",
             "единица", "единица измерения", "หน่วย", "đơn vị"),
    "dec": ("数量类型", "小数属性", "qty type", "quantity type", "qty decimal", "tipe jumlah",
            "数量タイプ", "수량 유형", "mengentyp", "typ menge", "type de quantité",
            "tipo de cantidad", "тип количества", "ประเภทจำนวน", "loại số lượng"),
    "price": ("单价", "单价(元)", "unit price", "price", "harga satuan", "単価", "단가",
              "stückpreis", "einzelpreis", "preis", "prix unitaire", "prix", "precio unitario",
              "precio", "цена за единицу", "цена", "ราคาต่อหน่วย", "ราคา", "đơn giá", "giá"),
    "min": ("最小库存", "下限", "min stock", "minimum stock", "min", "stok min", "stok minimum",
            "最小在庫", "최소 재고", "mindestbestand", "stock min", "stock minimum",
            "stock mínimo", "мин запас", "минимальный запас", "สต็อกขั้นต่ำ",
            "tồn kho tối thiểu"),
    "max": ("最大库存", "上限", "max stock", "maximum stock", "max", "stok maks", "stok maksimum",
            "最大在庫", "최대 재고", "höchstbestand", "stock max", "stock maximum",
            "stock máximo", "макс запас", "максимальный запас", "สต็อกสูงสุด",
            "tồn kho tối đa"),
    "stock": ("当前库存", "库存数量", "库存", "stock", "current stock", "qty on hand", "on hand",
              "stok saat ini", "stok", "現在庫", "현재 재고", "aktueller bestand", "bestand",
              "stock actuel", "stock actual", "существующий запас", "текущий запас",
              "สต็อกปัจจุบัน", "สต็อก", "tồn kho hiện tại", "tồn kho"),
    "val": ("贵重", "valuable", "is valuable", "barang berharga", "貴重品", "고가품", "wertvoll",
            "précieux", "valioso", "ценный", "ของมีค่า", "hàng giá trị"),
    "loc": ("存放库位", "库位", "放置位置", "storage location", "location", "lokasi rak", "lokasi",
            "保管場所", "格納場所", "보관 위치", "lagerplatz", "emplacement", "ubicación",
            "место хранения", "ตำแหน่งจัดเก็บ", "vị trí lưu trữ"),
    "apps": ("适用产线/设备", "产线/设备", "适用产线", "line/equipment", "applicable line/device",
             "line equipment", "applicable line device", "lin peralatan", "適用ライン/設備",
             "적용 라인/설비", "linie gerät", "ligne équipement", "línea equipo",
             "линия оборудование", "สายการผลิต อุปกรณ์", "dây chuyền thiết bị"),
    "remark": ("备注", "remark", "remarks", "keterangan", "catatan", "備考", "비고", "bemerkung",
               "remarque", "observación", "nota", "примечание", "หมายเหตุ", "ghi chú"),
}


def _extract_xlsx_images(fileobj, conn):
    """提取 xlsx 内嵌图片 → {行号: 图片名}，压缩后写入 images 表。
    图片失败不影响物料数据导入，但必须留痕：以前整段 except 把异常吞掉，
    用户只看到「图明明贴了却没进来」，无从排查。"""
    row_images = {}
    try:
        from openpyxl import load_workbook
        from PIL import Image as PILImage
        import io as _io
        import uuid as _uuid
        fileobj.seek(0)
        wb = load_workbook(fileobj, read_only=False, data_only=True)
        ws = wb.active
        for img in (getattr(ws, "_images", None) or []):
            try:
                row = img.anchor._from.row + 1  # openpyxl 行从 0 开始
            except Exception:
                continue
            if row < 2:  # 跳过表头
                continue
            try:
                raw = img._data()
            except Exception as e:
                app.logger.warning("xlsx 图片读取失败（第 %s 行）：%s", row, e)
                continue
            data = raw
            try:
                # 压缩：最长边缩到 200px，控制库体积
                pil = PILImage.open(_io.BytesIO(raw)).convert("RGB")
                w, h = pil.size
                if w > 200 or h > 200:
                    r = min(200.0 / w, 200.0 / h)
                    pil = pil.resize((max(1, int(w * r)), max(1, int(h * r))),
                                     PILImage.Resampling.LANCZOS)
                out = _io.BytesIO()
                pil.save(out, format="PNG", optimize=True, compress_level=9)
                data = out.getvalue()
            except Exception:
                pass  # 压缩失败就存原图
            fname = "imp_%s.png" % _uuid.uuid4().hex[:12]
            conn.execute("INSERT OR REPLACE INTO images(name, data) VALUES(?,?)", (fname, data))
            row_images[row] = fname
        wb.close()
    except Exception as e:
        import sqlite3 as _sq
        # v1.0.26：图片解析失败只是「这张图没带进来」，可以降级继续；
        # 但数据库/共享盘错误说明写入链路已出问题，绝不能吞掉后继续导物料——
        # 末尾 commit 会把已插入的孤儿图片一起提交，页面还显示「导入成功」。
        if isinstance(e, _sq.Error):
            try:
                import share_guard
                share_guard.mark_unavailable("导入图片写入失败：%s" % e)
            except Exception:
                pass
            raise
        app.logger.warning("xlsx 图片提取失败：%s", e, exc_info=True)
        return {}
    return row_images


def _import_parts_rows(rows, mode="append", row_images=None, conn=None, on_batch=None):
    """导入物料数据：返回 (created, updated, skipped, errs, unmatched)
    表头：料号* 名称* 分类 规格 单位 数量类型 单价 最小库存 最大库存 当前库存 贵重 存放库位 适用产线/设备 备注 + 自定义列
    mode: append 追加导入（已有料号跳过）；overwrite 覆盖导入（更新同料号全部字段与库存）
    row_images: {行号: 图片文件名}，从Excel嵌入图片提取出来
    规则：数量类型填 float=小数、不填或填 int=整数（默认整数）；库存数量未填默认 0；分类/规格/备注等未填默认「无」；
         存放库位被占用或无法解析 → 库存导入「待定」"""
    if row_images is None:
        row_images = {}
    if not rows or len(rows) < 1:
        return 0, 0, 0, [_xl("文件为空")], [], {}
    header = [str(x).strip() for x in rows[0]]

    def col(*names):
        # 精确匹配优先；再按归一化（大小写/括号/标点）匹配。
        # 「单价(元)」「单价(美元)」「单价(CNY)」「Unit Price (CNY)」都是同一个字段，
        # 客户手里的模板可能是哪一版都有，不该因为币种换了或语言不是中文就导不进单价。
        for i, h in enumerate(header):
            if h in names:
                return i
        want = set(_norm_head(n) for n in names)
        for i, h in enumerate(header):
            if _norm_head(h) in want:
                return i
        return -1

    def get(row, i):
        return str(row[i]).strip() if (0 <= i < len(row)) else ""

    i_code = col(*_HDR_ALIAS["code"])
    i_name = col(*_HDR_ALIAS["name"])
    i_cat = col(*_HDR_ALIAS["cat"])
    i_spec = col(*_HDR_ALIAS["spec"])
    i_unit = col(*_HDR_ALIAS["unit"])
    i_dec = col(*_HDR_ALIAS["dec"])
    i_price = col(*_HDR_ALIAS["price"])
    i_min = col(*_HDR_ALIAS["min"])
    i_max = col(*_HDR_ALIAS["max"])
    i_stock = col(*_HDR_ALIAS["stock"])
    i_val = col(*_HDR_ALIAS["val"])
    i_loc = col(*_HDR_ALIAS["loc"])
    i_apps = col(*_HDR_ALIAS["apps"])
    i_remark = col(*_HDR_ALIAS["remark"])
    if i_code < 0 or i_name < 0:
        # 文案保持原样（11 语言已登记），只在实际缺列时出现
        return 0, 0, 0, [_xl("表头缺少必填列「料号」或「名称」。可先在建档页点「下载导入模板」获取标准模板。")], [], {}

    # conn 由调用方传入时复用同一个连接与事务（图片和物料要么一起进库，要么一起回滚）；
    # 不传则自己开自己关。
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    created = updated = skipped = 0
    qty_fixed = 0          # 因「数量类型=整数」被取整的行数（取整不是错误，但要告诉用户）
    qty_sample = []        # 取整示例，最多 3 条，附在导入结果里
    date_hint = 0          # 有日期列认不出时，末尾统一提示一次可用格式（不逐行重复）
    errs = []
    unmatched = set()
    new_cats = []          # 导入时出现的字典外分类，收尾自动补进分类字典
    # v1.0.26：分批提交。400M 含图导入若用单事务，事务会持续数分钟，
    # 共享盘一断就是巨大损坏窗口；每 100 行落一次盘，窗口压到单批时长。
    # 小导入（<100 行）自然只在末尾 commit 一次，仍保持整批原子。
    _batch_rows = 0
    _batch_no = 0
    try:
        lines = {l["name"]: l for l in db.list_lines(conn)}
        equips = {e["name"]: e for e in db.list_equipment(conn)}
        loc_idx = _loc_index(conn)  # 库位反查索引，整个导入过程只建一次
        efs = get_extra_fields(conn)
        ef_idx = {ef["label"]: col(ef["label"]) for ef in efs if ef["label"]}
        ef_clip = get_head_clip(conn)["extra"]
        cat_known = get_part_categories(conn)
        std_lim = part_text_limits(conn)     # 各文本列的录入上限（超长整行跳过）
        for n, row in enumerate(rows[1:], start=2):
            if not row or not any(str(x).strip() for x in row):
                continue
            code = get(row, i_code)
            name = get(row, i_name)
            if str(code).strip().startswith("示例"):
                continue  # 模板内置示例行，跳过不导入
            if not code:
                errs.append(_xl("第{n}行：料号为空，已跳过", n=n))
                skipped += 1
                continue
            if not name:
                errs.append(_xl("第{n}行：名称为空，已跳过", n=n))
                skipped += 1
                continue
            # 表头设置的字数上限是硬约束：超长整行跳过，不再静默截断
            over = None
            for _k, _lab, _v in (("code", "料号", code), ("name", "名称", name),
                                 ("category", "分类", get(row, i_cat) or ""),
                                 ("spec", "规格型号", get(row, i_spec) or "")):
                if len(str(_v).strip()) > std_lim[_k]:
                    over = (_lab, str(_v).strip(), std_lim[_k])
                    break
            if over:
                _lab, _v, _lim = over
                errs.append(_xl("第{n}行：「{label}」超过 {m} 个字符（当前 {c} 个），已跳过",
                                n=n, label=_i18n.t(_lab, _i18n.current_lang()),
                                m=_lim, c=len(_v)))
                skipped += 1
                continue
            # 数字列必须能解析：以前只在覆盖模式校验，追加模式把「12PCS」这类文本
            # 静默当成 0 写库（库存凭空变 0），现在两种模式一起挡。
            bad = False
            for label, idx in (("单价", i_price), ("最小库存", i_min),
                               ("最大库存", i_max), ("当前库存", i_stock)):
                if idx >= 0 and get(row, idx) and _num_strict(get(row, idx)) is None:
                    errs.append(_xl("第{n}行：「{label}」不是有效数字，已跳过", n=n, label=_xl(label)))
                    skipped += 1
                    bad = True
                    break
            if bad:
                continue
            cat = (get(row, i_cat) or "无").strip()
            if cat not in cat_known:
                cat_known.append(cat)
                new_cats.append(cat)
            spec = get(row, i_spec) or "无"
            unit = get(row, i_unit) or "个"
            dec = _qdec(get(row, i_dec))  # 填 float=小数；不填或填 int=整数（默认整数）
            price = _num(get(row, i_price)) or 0
            mn = _num(get(row, i_min)) or 0
            mx = _num(get(row, i_max)) or 0
            qty = _num(get(row, i_stock)) or 0  # 库存数量未填 → 默认 0
            # 负数一律不合法：单价负数会把金额占比/领用金额算反，库存负数是记账错误。
            # 以前这里不校验，Excel 里填 -8 也照样「导入成功」。
            if price < 0:
                errs.append(_xl("第{n}行：单价不能为负数（{p1}），已跳过", n=n, p1=price))
                skipped += 1
                continue
            if mn < 0 or mx < 0:
                errs.append(_xl("第{n}行：库存上下限不能为负数，已跳过", n=n))
                skipped += 1
                continue
            if qty < 0:
                errs.append(_xl("第{n}行：库存数量不能为负数（{p1}），已跳过", n=n, p1=qty))
                skipped += 1
                continue
            # 数量类型不只是界面上的约束，必须落到库里：
            # 整数类物料（默认）的数量一律四舍五入取整。以前这里把 Excel 原值直接写库，
            # 于是「类型=int、当前库存=5000.332」真的进了数据库，之后出库就留下 0.332 零头。
            if not dec:
                _before = (qty, mn, mx)
                qty, mn, mx = _round_int(qty), _round_int(mn), _round_int(mx)
                _after = (qty, mn, mx)
                if tuple(float(x) for x in _before) != tuple(float(x) for x in _after):
                    qty_fixed += 1
                    if len(qty_sample) < 3:
                        # 示例取「第一个真被改动的数值」，只改了上限时也能看出改了什么
                        for _b, _a in zip(_before, _after):
                            if float(_b) != float(_a):
                                qty_sample.append("%s %s→%s" % (code, _fmt_qty(_b), _fmt_qty(_a)))
                                break
            val = 1 if get(row, i_val).lower() in ("是", "1", "y", "yes", "true", "贵重") else 0
            remark = get(row, i_remark) or "无"
            # 自定义列：必须按列 key（f1/f2…）存，界面是按 key 取值的。
            # 以前这里用列名当 key，于是「模板里填了、导入后列表和编辑弹窗都是空的」。
            extra, ex_err = {}, None
            for ef in efs:
                idx = ef_idx.get(ef["label"], -1)
                if idx < 0:
                    continue
                v = get(row, idx)
                if not v:
                    continue
                t = ef.get("type") or config.DEFAULT_EXTRA_TYPE
                ok, cv = _coerce_extra(v, t)
                if not ok:
                    ex_err = ("type", ef, t, v)
                    break
                if t in ("text", "int"):
                    cap = ef_clip.get(ef["key"], config.DEFAULT_HEAD_CLIP)
                    if len(cv) > cap:
                        ex_err = ("len", ef, len(cv), cap)
                        break
                extra[ef["key"]] = cv
            if ex_err:
                if ex_err[0] == "type":
                    if ex_err[2] == "date":
                        # 日期列单独提示：说清「读到的是什么」，格式说明放到末尾统一给一次，
                        # 否则 12 行错误里同一句格式说明重复 12 遍，提示条直接失去可读性。
                        date_hint = 1
                        errs.append(_xl("第{n}行：「{label}」认不出这个日期（{v}），已跳过",
                                        n=n, label=ex_err[1]["label"],
                                        v=str(ex_err[3])[:20]))
                    else:
                        errs.append(_xl("第{n}行：「{label}」需填写{kind}，已跳过", n=n,
                                        label=ex_err[1]["label"],
                                        kind=_i18n.t(_EXTRA_TYPE_LABEL.get(ex_err[2], "文本"),
                                                     _i18n.current_lang())))
                else:
                    errs.append(_xl("第{n}行：「{label}」超过 {m} 个字符（当前 {c} 个），已跳过",
                                    n=n, label=ex_err[1]["label"], m=ex_err[3], c=ex_err[2]))
                skipped += 1
                continue
            # 适用产线/设备：按名称匹配
            pairs = []
            app_txt = get(row, i_apps)
            if app_txt:
                for seg in re.split(r"[;；,，/]", app_txt):
                    seg = seg.strip()
                    if not seg:
                        continue
                    if seg in lines:
                        pairs.append((lines[seg]["id"], None))
                    elif seg in equips:
                        e = equips[seg]
                        pairs.append((e["line_id"], e["id"]))
                    else:
                        unmatched.add(seg)
            # 回收站中的同料号不参与导入（避免误更新/重复建档）
            recycled = conn.execute(
                "SELECT id FROM parts WHERE part_code=? AND deleted=1", (code,)
            ).fetchone()
            if recycled:
                skipped += 1
                errs.append(_xl("料号 {code} 在回收站中，请先在回收站恢复或彻底清除后再导入", code=code))
                continue
            exist = conn.execute(
                "SELECT id FROM parts WHERE part_code=? AND deleted=0", (code,)
            ).fetchone()
            # 覆盖模式：已存在 → 更新全部字段；追加模式：已存在 → 跳过
            if exist and mode == "append":
                skipped += 1
                continue
            try:
                img_file = row_images.get(n, "")
                if exist:
                    conn.execute(
                        """UPDATE parts SET name=?, category=?, spec=?, unit=?, qty_decimal=?, unit_price=?,
                           min_stock=?, max_stock=?, is_valuable=?, remark=?, extra=?, image_file=COALESCE(?, image_file), updated_at=?
                           WHERE id=?""",
                        (name, cat, spec, unit, dec, price, mn, mx, val, remark,
                         json.dumps(extra, ensure_ascii=False), img_file, db.now_str(), exist["id"]),
                    )
                    db.set_part_apps(conn, exist["id"], pairs)
                    pid = exist["id"]
                    updated += 1
                else:
                    cur = conn.execute(
                        """INSERT INTO parts(part_code,name,category,spec,unit,qty_decimal,unit_price,min_stock,max_stock,
                           is_valuable,supplier,remark,location_id,extra,image_file,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,'',?,NULL,?,?,?,?)""",
                        (code, name, cat, spec, unit, dec, price, mn, mx, val, remark,
                         json.dumps(extra, ensure_ascii=False), img_file, db.now_str(), db.now_str()),
                    )
                    db.set_part_apps(conn, cur.lastrowid, pairs)
                    pid = cur.lastrowid
                    created += 1
                # 库存重建：删除旧库存行 → 按导入行写入
                # 支持「WH-01-...-B-02 ×110; WH-02-...-B-01 ×40」这种多库位明细（导出带图就是这个格式）
                conn.execute("DELETE FROM inventory WHERE part_id=?", (pid,))
                entries = _parse_loc_entries(conn, get(row, i_loc), index=loc_idx)
                if not dec:
                    # 库位明细里的数量同样服从数量类型：「×110.5」对整数计数的物料没有意义
                    entries = [(lid, (_round_int(q) if q else q)) for lid, q in entries]
                specified = sum(q for _, q in entries if q)
                total = qty if qty > 0 else specified  # 只填库位明细没填当前库存时，以明细之和为准
                if total > 0:
                    plan = []
                    rest = total - specified
                    for lid, q in entries:
                        if q:
                            plan.append([lid, q])
                        elif rest > 0:
                            plan.append([lid, rest])
                            rest = 0
                    if not plan:
                        plan = [[None, total]]
                    else:
                        # 明细之和与「当前库存」不一致时，差额补到第一行，保证总量不丢
                        gap = total - sum(q for _, q in plan)
                        if abs(gap) > 1e-9 and plan[0][1] + gap > 0:
                            plan[0][1] += gap
                    for lid, q in plan:
                        if q <= 0:
                            continue
                        loc_txt = "待定"
                        if lid:
                            loc = conn.execute("SELECT * FROM locations WHERE id=?", (lid,)).fetchone()
                            loc_txt = db.location_text(loc)
                        conn.execute(
                            "INSERT INTO inventory(part_id, location_id, location, quantity) VALUES(?,?,?,?)",
                            (pid, lid, loc_txt, q),
                        )
                # v1.0.26：满 100 行落一次盘，并在批次边界检查写锁/共享盘状态。
                # on_batch 由调用方注入（LongLock.check），锁一旦丢失立即中止。
                _batch_rows += 1
                if _batch_rows >= 100:
                    conn.commit()
                    _batch_rows = 0
                    _batch_no += 1
                    if on_batch is not None:
                        on_batch(_batch_no)
            except Exception as e:
                import sqlite3 as _sq
                import share_guard
                # v1.0.26：数据库 / 共享盘错误必须整批中止。以前这里一律
                # 当成「第 N 行格式问题」吞掉，循环继续、末尾 conn.commit()
                # 把半成品批次提交，页面还显示「导入成功」——这是断网时
                # 最危险、也最难发现的静默数据不一致。
                if isinstance(e, (_sq.Error, share_guard.ShareUnavailable,
                                  share_guard.LockLost, share_guard.LockBusy)):
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    if isinstance(e, _sq.Error):
                        share_guard.mark_unavailable("导入写入失败：%s" % e)
                    raise
                errs.append(_xl("第{n}行：{err}，已跳过", n=n, err=e))
                skipped += 1
        if new_cats:
            # 导入不该因为分类不在字典里而失败：出现新分类就自动补进字典
            save_part_categories(conn, cat_known)
        conn.commit()
    finally:
        # v1.0.26：异常路径必须显式回滚当前未提交的批次，不能只靠 close()
        # 的隐式回滚（断网时 close 本身也可能失败，留下一堆半成品）。
        try:
            conn.rollback()
        except Exception:
            pass
        if own_conn:
            conn.close()
    return created, updated, skipped, errs[:12], sorted(unmatched), {
        "qty_fixed": qty_fixed, "qty_sample": qty_sample, "err_total": len(errs),
        "date_hint": date_hint, "batch_no": _batch_no, "batched": _batch_no > 0}


@app.route("/parts/import", methods=["POST"])
@login_required
@perm_required("parts_manage", "import")
def parts_import():
    """导入物料（.xlsx / .csv），mode=append 追加 / overwrite 覆盖"""
    file = request.files.get("file")
    mode = request.form.get("mode", "append")
    if mode not in ("overwrite", "append"):
        mode = "append"
    if not file or not file.filename:
        flash(_i18n.t("请选择要导入的 Excel / CSV 文件", _i18n.current_lang()), "danger")
        return redirect(url_for("parts_manage_page"))
    ext = os.path.splitext(file.filename)[1].lower()
    # 全流程一个连接、一个事务：图片和物料要么一起入库，要么一起回滚。
    # 以前图片用临时连接且从不 commit，等于「带图导入」的图片永远进不了库。
    conn = get_db()
    try:
        try:
            if ext == ".csv":
                rows = _read_csv_rows(file)
                row_images = {}
            elif ext in (".xlsx", ".xlsm"):
                rows = _read_xlsx_rows(file)
                row_images = _extract_xlsx_images(file, conn)
            elif ext == ".xls":
                # 老版 .xls 是另一个二进制格式，纯标准库读不了。
                # 单独提示「另存为 xlsx」，比笼统的「仅支持 .xlsx / .csv」有用得多。
                flash(_i18n.t("暂不支持旧版 .xls，请在 Excel / WPS 里「另存为 .xlsx」后再导入",
                              _i18n.current_lang()), "danger")
                return redirect(url_for("parts_manage_page"))
            else:
                flash(_i18n.t("仅支持 .xlsx / .csv 文件", _i18n.current_lang()), "danger")
                return redirect(url_for("parts_manage_page"))
        except Exception as e:
            flash(_i18n.t("文件解析失败：{0}", _i18n.current_lang()).format(e), "danger")
            return redirect(url_for("parts_manage_page"))
        # v1.0.26：覆盖导入 / 含图大导入前先落一份完整备份。
        # 原来只有「整库币种换算」前有备份，导入中途断网没有可回退的快照。
        if mode == "overwrite" or row_images:
            try:
                _do_backup()
            except Exception as _e:  # noqa: BLE001
                app.logger.warning("导入前预备份失败（不阻断导入）：%s", _e)
        try:
            created, updated, skipped, errs, unmatched, notes = _import_parts_rows(
                rows, mode, row_images=row_images, conn=conn,
                on_batch=lambda _n: (g._sg_keepalive.check()
                                     if getattr(g, "_sg_keepalive", None) else None))
        except Exception as e:
            # v1.0.26：整批中止。不能让 DB 错误冒泡成 500 白板，也不能像
            # 以前那样吞掉后继续 commit 半成品、页面还显示「导入成功」。
            try:
                conn.rollback()
            except Exception:
                pass
            import share_guard
            if isinstance(e, share_guard.ShareUnavailable):
                _tip = str(e)
            elif isinstance(e, (share_guard.LockLost, share_guard.LockBusy)):
                _tip = str(e)
            else:
                app.logger.exception("导入失败，已回滚：%s", e)
                _tip = str(e)
            # 用异常自身的文本，避免新增中文词条（11 语言口径）；
            # ShareUnavailable / LockLost 的 message 已是可读中文提示。
            flash(_tip, "danger")
            return redirect(url_for("parts_manage_page"))
        if created == 0 and updated == 0 and skipped == 0 and not errs:
            # 「原样把下载的模板传回去」就是这种结果：示例行被跳过，一条都没导入。
            # 老提示是「追加导入完成：新增 0 条…」，看着像成功又像失败，用户只会说
            # 「模板导入不进去」。这里直接说清楚下一步该做什么。
            flash(_i18n.t("文件里没有可导入的数据：模板自带的示例行（料号以「示例-」开头）会自动跳过，"
                          "请把示例行换成自己的料号后再导入", _i18n.current_lang()), "warning")
            return redirect(url_for("parts_manage_page"))
        mode_txt = "覆盖导入" if mode == "overwrite" else "追加导入"
        msg = _xl("{mode}完成：新增 {created} 条，更新 {updated} 条，跳过 {skipped} 条",
                  mode=_xl(mode_txt), created=created, updated=updated, skipped=skipped)
        # 逐条独立成行：以前把 12 条问题用「；」串成一大段，挤在右上角小气泡里根本读不清
        lines = []
        if notes.get("qty_fixed"):
            # 取整不是错误，但必须让用户看见 —— 否则「我填 5000.332，怎么变 5000 了」
            lines.append(_xl("{n} 行数量按数量类型「整数」四舍五入取整（{sample}）",
                             n=notes["qty_fixed"],
                             sample=_i18n.mk("、", _i18n.current_lang()).join(notes.get("qty_sample") or [])))
        if unmatched:
            lines.append(_xl("以下产线/设备未匹配已忽略：") + _i18n.mk("、", _i18n.current_lang()).join(unmatched[:5]))
        if errs:
            head = _xl("问题（共 {n} 条）：", n=notes.get("err_total") or len(errs))
            lines.append(head + "\n" + "\n".join("· " + e for e in errs))
            if (notes.get("err_total") or 0) > len(errs):
                lines.append(_xl("其余 {n} 条问题已省略，修好后可重新导入", n=notes["err_total"] - len(errs)))
            if notes.get("date_hint"):
                # 格式说明只给一次：逐行重复会让提示条变成一堵墙
                lines.append(_xl("日期列可填 2026-07-01、2026/7/1 或 2026年7月1日"))
        if lines:
            msg += "\n" + "\n".join(lines)
        flash(msg, "success" if skipped == 0 else "warning")
        return redirect(url_for("parts_manage_page"))
    finally:
        # v1.0.26：先显式回滚未提交批次再关连接，不能只依赖 close() 的隐式回滚
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@app.route("/parts/template")
@login_required
@perm_required("parts_manage", "import")
def parts_template():
    """下载物料导入 Excel 模板（标准表头，含自定义列，跟随当前语言）"""
    conn = get_db()
    try:
        efs = get_extra_fields(conn)
    finally:
        conn.close()
    lang = _i18n.current_lang()
    # 模板里的单价列跟随当前币种：币种切到 USD 后模板还写「单价(元)」会误导填写。
    # 导入匹配按「去括号后的列名」找列，所以括号里写什么币种都不影响识别。
    _cur_lab = _i18n.cur_label()
    # 不同语言的表头
    if lang == "id":
        headers = ["Gambar", "Kode", "Nama", "Kategori", "Spesifikasi", "Satuan", "Tipe Jumlah",
                   "Harga Satuan (%s)" % _cur_lab,
                   "Stok Min", "Stok Maks", "Stok Saat Ini", "Barang Berharga", "Lokasi Rak", "Lin/Peralatan"]
        note1 = "Wajib diisi: Kode, Nama; kolom lain boleh dikosongkan, sistem isi default otomatis. Kolom gambar boleh dikosongkan, jika perlu impor dengan gambar, tempelkan gambar di baris terkait. Tipe int = bilangan bulat, Satuan/Kategori/Spesifikasi default 'pcs/tanpa info', Stok default 0."
        note2 = "Kolom Barang Berharga isi 'Ya/Tidak'; Lokasi kosong otomatis jadi 'Menunggu'; Lin/Peralatan pisahkan dengan titik koma; gambar yang ditempel akan otomatis dikompres, tidak perlu kompres manual. Kolom tanggal menerima 2026-07-01, 2026/7/1, 2026年7月1日, atau sel tanggal Excel."
        sheet_name = "Template Impor"
    elif lang == "en":
        headers = ["Image", "Part Code", "Name", "Category", "Spec", "Unit", "Qty Type",
                   "Unit Price (%s)" % _cur_lab,
                   "Min Stock", "Max Stock", "Current Stock", "Valuable", "Storage Location", "Line/Equipment"]
        note1 = "Required: Part Code, Name; other columns can be left blank, system fills defaults. Image column is optional, paste image in the row if needed. Type int = integer; Unit/Category/Spec default 'pcs/None'; Stock default 0."
        note2 = "Valuable column: 'Yes/No'; empty location defaults to 'Pending'; multiple lines separated by semicolon; pasted images are auto-compressed. Date columns accept 2026-07-01, 2026/7/1, 2026年7月1日, or a real Excel date cell."
        sheet_name = "Import Template"
    elif lang == "zh":
        headers = ["图片", "料号", "名称", "分类", "规格", "单位", "数量类型", "单价(%s)" % _cur_lab,
                   "最小库存", "最大库存", "当前库存", "贵重", "存放库位", "适用产线/设备"]
        note1 = "必填：料号、名称；其他列可留空，系统自动填默认值。图片列可留空，需要带图导入时把图片插入到对应行即可。数量类型 int=整数，单位/分类/规格留空默认“个/无”，库存留空默认0。"
        note2 = "贵重列填“是/否”；存放库位留空自动记为“待定”；适用产线/设备多个用分号分隔；插入的图片会自动压缩后存入系统，无需手动压缩。日期列可填 2026-07-01、2026/7/1、2026年7月1日，或直接把单元格设成 Excel 日期格式。"
        sheet_name = "导入模板"
    else:
        # 其余语言（日/韩/俄/法/德/泰/越…）统一给英文模板：
        # 以前这里落到中文，等于「模板没跟语言走」。
        headers = ["Image", "Part Code", "Name", "Category", "Spec", "Unit", "Qty Type",
                   "Unit Price (%s)" % _cur_lab,
                   "Min Stock", "Max Stock", "Current Stock", "Valuable", "Storage Location", "Line/Equipment"]
        note1 = "Required: Part Code, Name; other columns can be left blank, system fills defaults. Image column is optional, paste image in the row if needed. Type int = integer; Unit/Category/Spec default 'pcs/None'; Stock default 0."
        note2 = "Valuable column: 'Yes/No'; empty location defaults to 'Pending'; multiple lines separated by semicolon; pasted images are auto-compressed. Date columns accept 2026-07-01, 2026/7/1, 2026年7月1日, or a real Excel date cell."
        sheet_name = "Import Template"
    for ef in efs:
        if ef["label"]:
            headers.append(ef["label"])
    headers.append(_i18n.t("备注", lang))
    # 两条填写示例（料号以「示例-」开头，导入时自动跳过，不产生物料记录）
    extra_n = len(efs)
    example_rows = [
        ["", "示例-R-0603-10K", "贴片电阻 0603 10KΩ", "电阻", "0603", "PCS", "int", 0.05,
         100, 10000, 5000, _xl("否", lang), "WH-01-R-01-S-01", "SMT-1线"]
        + [""] * extra_n
        + [note1],
        ["", "示例-锡膏SAC305", "无铅锡膏 SAC305", "辅料", "SAC305", "克", "float", 1.2,
         1000, 50000, 20000, _xl("否", lang), _xl("待定", lang), "SMT-2线"]
        + [""] * extra_n
        + [note2],
    ]
    data = _export_xlsx(headers, example_rows, sheet_name, unit_head=False)
    return Response(
        data.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=parts_import_template.xlsx"},
    )

@app.route("/parts/export_with_image")
@login_required
@perm_required("parts_manage", "export")
def parts_export_with_image():
    """导出带图片的物料Excel，格式和导入模板完全一致"""
    conn = get_db()
    try:
        parts = _query_parts(conn, "", "", "")
        efs = get_extra_fields(conn)
    finally:
        conn.close()
    headers = ["料号", "名称", "分类", "规格", "单位", "数量类型", "单价(元)",
               "最小库存", "最大库存", "当前库存", "贵重", "存放库位", "适用产线/设备"]
    for ef in efs:
        if ef["label"]:
            headers.append(ef["label"])
    headers.append("备注")
    # 这个函数走 openpyxl，不经过 _export_xlsx，表头得自己翻
    headers = [_xl(h) for h in headers]
    try:
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image as XLImage
        from openpyxl.utils import get_column_letter
        import os, io
        wb = Workbook()
        ws = wb.active
        ws.title = _sheet_name(_xl("物料清单"))
        # 第一列放图片，后面列顺序和导入模板完全一致
        full_headers = [_xl("图片")] + headers
        ws.append(full_headers)
        ws.column_dimensions['A'].width = 12
        for i, h in enumerate(full_headers, 1):
            ws.cell(row=1, column=i, value=h)
            if i > 1:
                ws.column_dimensions[get_column_letter(i)].width = 15
        conn = get_db()
        for row_idx, p in enumerate(parts, 2):
            ws.row_dimensions[row_idx].height = 50
            if p.get("image_file"):
                try:
                    img_row = conn.execute("SELECT data FROM images WHERE name=?", (p["image_file"],)).fetchone()
                    if img_row and img_row["data"]:
                        img = XLImage(io.BytesIO(img_row["data"]))
                        img.width = 40
                        img.height = 40
                        ws.add_image(img, f"A{row_idx}")
                except Exception:
                    pass
            loc_txt = p["stock_locs_text"] or (_xl("待定") if p["stock"] > 0 else "")
            row_data = [p["part_code"], p["name"], p["category"], p["spec"], p["unit"],
                   "float" if p["qty_decimal"] else "int",
                   p["unit_price"], p["min_stock"], p["max_stock"], p["stock"],
                   _xl("是") if p["is_valuable"] else _xl("否"), loc_txt, p["apps_text"]]
            for ef in efs:
                if ef["label"]:
                    row_data.append(p.get("extra", {}).get(ef["key"], ""))
            row_data.append(p.get("remark", ""))
            for col_idx, val in enumerate(row_data, 2):
                ws.cell(row=row_idx, column=col_idx, value=val)
        conn.close()
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        from urllib.parse import quote
        fname = "Parts_List_With_Images.xlsx"
        return Response(
            buf.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={fname}; filename*=UTF-8''{quote(fname)}"},
        )
    except Exception as e:
        # 出错回退成普通导出，不报错
        rows = []
        for p in parts:
            loc_txt = p["stock_locs_text"] or (_xl("待定") if p["stock"] > 0 else "")
            row = [p["part_code"], p["name"], p["category"], p["spec"], p["unit"],
                   "float" if p["qty_decimal"] else "int",
                   p["unit_price"], p["min_stock"], p["max_stock"], p["stock"],
                   _xl("是") if p["is_valuable"] else _xl("否"), loc_txt, p["apps_text"]]
            for ef in efs:
                if ef["label"]:
                    row.append(p.get("extra", {}).get(ef["key"], ""))
            row.append(p.get("remark", ""))
            rows.append(row)
        data = _export_xlsx(headers, rows, "物料清单")
        return Response(
            data.getvalue(),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=parts_export.xlsx"},
        )


@app.route("/parts/export")
@login_required
@perm_required("parts_manage", "export")
def parts_export():
    """导出物料清单（按当前筛选条件）为 Excel，支持带图片"""
    kw = request.args.get("kw", "").strip()
    cat = request.args.get("category", "").strip()
    state = request.args.get("state", "").strip()
    with_image = request.args.get("with_image", "0") == "1"
    conn = get_db()
    try:
        parts = _query_parts(conn, kw, cat, state)
        efs = get_extra_fields(conn)
    finally:
        conn.close()
    headers = ["料号", "名称", "分类", "规格", "单位", "数量类型", "单价(元)",
               "最小库存", "最大库存", "当前库存", "贵重", "存放库位", "适用产线/设备"]
    for ef in efs:
        if ef["label"]:
            headers.append(ef["label"])
    headers.append("备注")
    rows = []
    for p in parts:
        loc_txt = p["stock_locs_text"] or (_xl("待定") if p["stock"] > 0 else "")
        row = [p["part_code"], p["name"], p["category"], p["spec"], p["unit"],
               "float" if p["qty_decimal"] else "int",
               p["unit_price"], p["min_stock"], p["max_stock"], p["stock"],
               _xl("是") if p["is_valuable"] else _xl("否"), loc_txt, p["apps_text"]]
        for ef in efs:
            if ef["label"]:
                row.append(p.get("extra", {}).get(ef["key"], ""))
        row.append(p.get("remark", ""))
        rows.append(row)
    if with_image:
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image as XLImage
        from openpyxl.utils import get_column_letter
        import os
        wb = Workbook()
        ws = wb.active
        ws.title = _sheet_name(_xl("物料清单"))
        full_headers = [_xl("图片")] + [_xl(h) for h in headers]
        ws.append(full_headers)
        ws.column_dimensions['A'].width = 12
        for i, h in enumerate(full_headers, 1):
            ws.cell(row=1, column=i, value=h)
            if i > 1:
                ws.column_dimensions[get_column_letter(i)].width = 15
        media_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media")
        for row_idx, (p, row_data) in enumerate(zip(parts, rows), 2):
            ws.row_dimensions[row_idx].height = 50
            ws.cell(row=row_idx, column=1, value="")
            if p.get("image_file"):
                img_path = os.path.join(media_dir, p["image_file"])
                if os.path.exists(img_path):
                    try:
                        img = XLImage(img_path)
                        img.width = 40
                        img.height = 40
                        ws.add_image(img, f"A{row_idx}")
                    except Exception:
                        pass
            for col_idx, val in enumerate(row_data, 2):
                ws.cell(row=row_idx, column=col_idx, value=val)
        import io
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        fname = _xl("物料清单_带图片.xlsx")
    else:
        data = _export_xlsx(headers, rows, "物料清单")
        buf = data
        fname = "parts_export.xlsx"
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=" + fname},
    )

@app.route("/parts/manage")
@login_required
@perm_required("parts_manage")
def parts_manage_page():
    """信息维护（建档）：仓管员以上使用，新增/编辑/删除/图片/位置"""
    kw = request.args.get("kw", "").strip()
    cat = request.args.get("category", "").strip()
    state = request.args.get("state", "").strip()
    conn = get_db()
    try:
        parts = _query_parts(conn, kw, cat, state)
        cats = _categories(conn)
        locations = _all_locations(conn)
        lines = db.list_lines(conn)
        equipment = db.list_equipment(conn)
        hc_full = get_head_clip(conn)
        hc = dict(hc_full["std"])   # 模板里直接 hc.category / hc.code 取
        extra_fields = get_extra_fields(conn)
        for ef in extra_fields:
            ef["clip"] = hc_full["extra"].get(ef["key"], config.DEFAULT_HEAD_CLIP)
        total_kinds = conn.execute("SELECT COUNT(*) FROM parts WHERE deleted=0").fetchone()[0]
        total_stock = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM inventory").fetchone()[0]
        auto_min_cfg = _auto_stock_cfg(conn)
    finally:
        conn.close()
    # 分页（同信息查询）：默认 10 条一页，可切 10/20/50/100
    parts_total = len(parts)
    per_page = _parts_per_page("parts_manage_per_page")
    parts_pages = max(1, (parts_total + per_page - 1) // per_page)
    try:
        parts_page_no = max(1, _int(request.args.get("page", 1) or 1))
    except (TypeError, ValueError):
        parts_page_no = 1
    parts_page_no = min(parts_page_no, parts_pages)
    shown = parts[(parts_page_no - 1) * per_page: parts_page_no * per_page]
    return render_template("parts_manage.html", parts=shown, categories=cats,
                           kw=kw, cat=cat, state=state, locations=locations,
                           lines=lines, equipment=equipment,
                           total_kinds=total_kinds, total_stock=total_stock,
                           parts_total=parts_total, parts_page=parts_page_no,
                           parts_pages=parts_pages, parts_per_page=str(per_page),
                           hc=hc, extra_fields=extra_fields,
                           auto_min_cfg=auto_min_cfg)


# --------------------------------------------------------------------------
# 币种：试算 / 换算 / 撤销
#
# 换算范围只有三列 —— parts.unit_price、transactions.unit_price、transactions.amount。
# 没有第四处：申请单不存金额，盘点差异金额是实时算的。这是查全库后确认的。
#
# 每次换算 = 一张 currency_ops 单 + 逐条 currency_op_items 快照。
# 撤销时：快照里有的按快照精确写回，快照里没有的（换算后新产生的业务）按反向汇率回滚。
# 不能反过来只靠「乘以 1/rate」——12.5 × 0.1373 = 1.7163，乘回去是 12.4998，两次就漂了。
# --------------------------------------------------------------------------
_CUR_CONFIRM_WORD = "CONVERT"
_CUR_REVERT_WORD = "REVERT"


def _cur_stats(conn):
    """当前金额规模：物料数 / 库存总额 / 流水数 / 流水总额"""
    p = conn.execute(
        "SELECT COUNT(DISTINCT p.id) n, COALESCE(SUM(i.quantity * p.unit_price), 0) v "
        "FROM parts p LEFT JOIN inventory i ON i.part_id = p.id "
        "WHERE p.deleted = 0"
    ).fetchone()
    t = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(amount), 0) v FROM transactions"
    ).fetchone()
    return {"parts": p["n"] or 0, "stock_value": round(p["v"] or 0, 2),
            "txs": t["n"] or 0, "tx_value": round(t["v"] or 0, 2)}


def _cur_sample(conn, rate, limit=6):
    """抽样对比：按确认之前看到真实数字，比只看一个总额可信"""
    rows = conn.execute(
        "SELECT part_code, name, unit_price FROM parts "
        "WHERE deleted = 0 AND unit_price > 0 ORDER BY unit_price DESC LIMIT ?", (limit,)
    ).fetchall()
    return [{"code": r["part_code"] or "", "name": r["name"] or "",
             "before": round(r["unit_price"] or 0, 2),
             "after": round((r["unit_price"] or 0) * rate, 4)} for r in rows]


def _cur_next_no(conn):
    """调整单号 CUR-20261027-001"""
    pre = "CUR-%s-" % datetime.now().strftime("%Y%m%d")
    row = conn.execute(
        "SELECT op_no FROM currency_ops WHERE op_no LIKE ? ORDER BY op_no DESC LIMIT 1",
        (pre + "%",)
    ).fetchone()
    n = 1
    if row:
        try:
            n = int(str(row["op_no"]).rsplit("-", 1)[1]) + 1
        except (IndexError, ValueError):
            n = 1
    return pre + "%03d" % n


def _cur_snapshot(conn, op_id, table, rate, chunk=5000):
    """逐块给一张表拍快照，返回行数。分块是为了几十万条流水时不把内存撑爆。"""
    is_tx = table == "transactions"
    cols = "id, unit_price, amount" if is_tx else "id, unit_price"
    kind = "tx" if is_tx else "part"
    last, n = 0, 0
    sql = ("INSERT INTO currency_op_items(op_id,kind,row_id,old_num,new_num,old_amt,new_amt) "
           "VALUES(?,?,?,?,?,?,?)" if is_tx else
           "INSERT INTO currency_op_items(op_id,kind,row_id,old_num,new_num) VALUES(?,?,?,?,?)")
    while True:
        rows = conn.execute(
            "SELECT %s FROM %s WHERE id > ? ORDER BY id LIMIT ?" % (cols, table), (last, chunk)
        ).fetchall()
        if not rows:
            break
        if is_tx:
            conn.executemany(sql, [
                (op_id, kind, r["id"], r["unit_price"], round((r["unit_price"] or 0) * rate, 6),
                 r["amount"], round((r["amount"] or 0) * rate, 6)) for r in rows])
        else:
            conn.executemany(sql, [
                (op_id, kind, r["id"], r["unit_price"], round((r["unit_price"] or 0) * rate, 6))
                for r in rows])
        last = rows[-1]["id"]
        n += len(rows)
    return n


def _cur_convert_table(conn, table, rate, to_cur):
    """整表乘率。单价留 6 位小数，显示仍是 2 位 —— 少了会漂，多了没意义。"""
    if table == "transactions":
        conn.execute(
            "UPDATE transactions SET unit_price=ROUND(COALESCE(unit_price,0)*?,6), "
            "amount=ROUND(COALESCE(amount,0)*?,6), currency=?", (rate, rate, to_cur))
    else:
        conn.execute(
            "UPDATE parts SET unit_price=ROUND(COALESCE(unit_price,0)*?,6), currency=?",
            (rate, to_cur))


@app.route("/parts/currency")
@login_required
@role_required("admin")
def parts_currency_page():
    """金额单位：试算器（只读）+ 调整单列表（可撤销）"""
    cfg = currency_cfg(force=True)
    conn = get_db()
    try:
        stats = _cur_stats(conn)
        ops = [dict(r) for r in conn.execute(
            "SELECT * FROM currency_ops ORDER BY id DESC LIMIT 50").fetchall()]
        op_items = {}
        for r in conn.execute(
                "SELECT op_id, COUNT(*) n FROM currency_op_items "
                "WHERE op_id IN (SELECT id FROM currency_ops ORDER BY id DESC LIMIT 50) "
                "GROUP BY op_id").fetchall():
            op_items[r["op_id"]] = r["n"]
        latest = conn.execute(
            "SELECT id FROM currency_ops WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    cur_list = _i18n.currency_list()
    return render_template("parts_currency.html", cfg=cfg, stats=stats, ops=ops,
                           op_items=op_items, currencies=cur_list,
                           latest_active=(latest["id"] if latest else 0),
                           confirm_word=_CUR_CONFIRM_WORD, revert_word=_CUR_REVERT_WORD)


@app.route("/parts/currency/custom", methods=["POST"])
@login_required
@role_required("admin")
def parts_currency_custom():
    """保存内置表之外的自定义币种。

    只登记「长什么样」（代码/符号/中文名/英文名/小数位），**不切换当前币种、
    不动任何金额**。要真的换成它，还得走上面的换算流程（填汇率 + 确认词 +
    整表乘率 + 备份），那是财务动作，不能和「填个符号」混在一起。
    """
    d = request.form or {}
    if (d.get("action") or "") == "remove":
        set_custom_currency_cfg({})
        flash(_xl("已清除自定义币种"), "success")
        return redirect(url_for("parts_currency_page"))
    try:
        meta = set_custom_currency_cfg({
            "code": d.get("code"), "symbol": d.get("symbol"),
            "zh": d.get("zh"), "en": d.get("en"),
            "decimals": d.get("decimals"),
        })
    except ValueError as ex:
        flash(_xl(str(ex)), "danger")
        return redirect(url_for("parts_currency_page"))
    flash(_xl("已保存自定义币种 {code}，现在可以在目标币种里选它", code=meta["code"]), "success")
    return redirect(url_for("parts_currency_page"))


@app.route("/parts/currency/preview", methods=["POST"])
@login_required
@role_required("admin")
def parts_currency_preview():
    """只读试算：一个字节都不写库"""
    to_cur = (request.form.get("to_cur") or "").strip().upper()
    try:
        rate = float(request.form.get("rate") or 0)
    except (TypeError, ValueError):
        rate = 0
    if not to_cur or rate <= 0:
        return jsonify({"ok": False, "msg": _xl("请选择目标币种并填写大于 0 的汇率")})
    if rate > 100000:
        return jsonify({"ok": False, "msg": _xl("汇率数值超出合理范围，请检查是否填反")})
    cur = currency_cfg()["code"]
    conn = get_db()
    try:
        stats = _cur_stats(conn)
        sample = _cur_sample(conn, rate)
    finally:
        conn.close()
    return jsonify({
        "ok": True, "from": cur, "to": to_cur, "rate": rate,
        "parts": stats["parts"], "txs": stats["txs"],
        "stock_before": stats["stock_value"],
        "stock_after": round(stats["stock_value"] * rate, 2),
        "tx_before": stats["tx_value"],
        "tx_after": round(stats["tx_value"] * rate, 2),
        "sample": sample,
    })


@app.route("/parts/currency/apply", methods=["POST"])
@login_required
@role_required("admin")
def parts_currency_apply():
    """正式换算：建单 → 拍快照 → 整表乘率 → 切全局币种，全程一个事务"""
    to_cur = (request.form.get("to_cur") or "").strip().upper()
    try:
        rate = float(request.form.get("rate") or 0)
    except (TypeError, ValueError):
        rate = 0
    word = (request.form.get("confirm") or "").strip().upper()
    if word != _CUR_CONFIRM_WORD:
        flash(_xl("确认词不正确，未做任何改动"), "danger")
        return redirect(url_for("parts_currency_page"))
    if not to_cur or rate <= 0:
        flash(_xl("请选择目标币种并填写大于 0 的汇率"), "danger")
        return redirect(url_for("parts_currency_page"))
    cur = currency_cfg(force=True)["code"]
    if to_cur == cur:
        flash(_xl("目标币种与当前币种相同，无需换算"), "warning")
        return redirect(url_for("parts_currency_page"))

    # 改账前先落一份完整备份。失败不拦（撤销机制本身就是兜底），
    # 但必须让用户知道这次没有备份可退。
    bak = ""
    try:
        bak = os.path.basename(_do_backup() or "")
    except Exception as ex:
        app.logger.warning("换算前自动备份失败：%s", ex)

    conn = get_db()
    try:
        before = _cur_stats(conn)
        op_no = _cur_next_no(conn)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO currency_ops(op_no,from_cur,to_cur,rate,mode,part_count,tx_count,"
            "total_before,total_after,status,operator_id,operator_name,note,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (op_no, cur, to_cur, rate, "exact", before["parts"], before["txs"],
             before["stock_value"], round(before["stock_value"] * rate, 2),
             "active", (current_user() or {}).get("id"),
             (current_user() or {}).get("username", ""),
             (request.form.get("note") or "").strip()[:200], now))
        op_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        n_parts = _cur_snapshot(conn, op_id, "parts", rate)
        n_txs = _cur_snapshot(conn, op_id, "transactions", rate)
        _cur_convert_table(conn, "parts", rate, to_cur)
        _cur_convert_table(conn, "transactions", rate, to_cur)
        conn.execute("UPDATE currency_ops SET part_count=?, tx_count=? WHERE id=?",
                     (n_parts, n_txs, op_id))
        # 审计单独写，不走 _audit()，它内部会 commit 打断事务
        u = current_user() or {}
        conn.execute(
            "INSERT INTO audit_log(obj_type,obj_id,obj_code,action,detail,operator_id,operator_name,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("currency", op_id, op_no, "convert",
             json.dumps({"from": cur, "to": to_cur, "rate": rate,
                         "parts": n_parts, "txs": n_txs,
                         "stock_before": before["stock_value"],
                         "stock_after": round(before["stock_value"] * rate, 2)},
                        ensure_ascii=False),
             u.get("id"), u.get("username", ""), db.now_str()))
        conn.commit()
    except Exception as ex:
        conn.rollback()
        flash(_xl("换算失败，已回滚，数据没有任何改动：{err}", err=str(ex)), "danger")
        return redirect(url_for("parts_currency_page"))
    finally:
        conn.close()
    # 币种切换放在提交之后：万一事务回滚，内存里的币种不能先变
    set_currency_cfg(to_cur, currency_cfg()["form"])
    flash(_xl("已按汇率 {rate} 从 {a} 换算为 {b}，单号 {no}；发现问题可在下方撤销",
              rate=rate, a=cur, b=to_cur, no=op_no), "success")
    if bak:
        flash(_xl("换算前已自动备份：{bak}", bak=bak), "success")
    else:
        flash(_xl("换算前自动备份失败，请自行确认数据可恢复"), "warning")
    return redirect(url_for("parts_currency_page"))


@app.route("/parts/currency/revert", methods=["POST"])
@login_required
@role_required("admin")
def parts_currency_revert():
    """撤销：快照内的精确写回，快照外的（换算后新业务）按反向汇率回滚"""
    try:
        op_id = _int(request.form.get("op_id") or 0)
    except (TypeError, ValueError):
        op_id = 0
    word = (request.form.get("confirm") or "").strip().upper()
    if word != _CUR_REVERT_WORD:
        flash(_xl("确认词不正确，未做任何改动"), "danger")
        return redirect(url_for("parts_currency_page"))
    conn = get_db()
    try:
        op = conn.execute("SELECT * FROM currency_ops WHERE id=?", (op_id,)).fetchone()
        if not op:
            flash(_xl("找不到这张调整单"), "danger")
            return redirect(url_for("parts_currency_page"))
        if op["status"] != "active":
            flash(_xl("这张单已经撤销过了"), "warning")
            return redirect(url_for("parts_currency_page"))
        # 只允许撤销最近一张：连换两次币种后回头撤第一张，会把中间那层币种搞乱
        newer = conn.execute(
            "SELECT COUNT(*) FROM currency_ops WHERE status='active' AND id>?", (op_id,)
        ).fetchone()[0]
        if newer:
            flash(_xl("只能撤销最近一张调整单，请先撤销它后面的 {n} 张", n=newer), "warning")
            return redirect(url_for("parts_currency_page"))
        # 期间是否已有新业务：有的话，那些行不在快照里，只能按反向汇率回
        new_tx = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE created_at > ?", (op["created_at"],)
        ).fetchone()[0]
        rate = float(op["rate"] or 0)
        back = (1.0 / rate) if rate else 1.0

        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _cursnap(rid INTEGER PRIMARY KEY, v1 REAL)")
        conn.execute("DELETE FROM _cursnap")
        conn.execute("INSERT INTO _cursnap SELECT row_id, old_num FROM currency_op_items "
                     "WHERE op_id=? AND kind='part'", (op_id,))
        conn.execute("UPDATE parts SET unit_price=(SELECT v1 FROM _cursnap WHERE _cursnap.rid=parts.id), "
                     "currency=? WHERE id IN (SELECT rid FROM _cursnap)",
                     (op["from_cur"],))
        conn.execute("UPDATE parts SET unit_price=ROUND(COALESCE(unit_price,0)*?,6), currency=? "
                     "WHERE id NOT IN (SELECT rid FROM _cursnap)", (back, op["from_cur"]))

        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _cursnap2(rid INTEGER PRIMARY KEY, v1 REAL, v2 REAL)")
        conn.execute("DELETE FROM _cursnap2")
        conn.execute("INSERT INTO _cursnap2 SELECT row_id, old_num, old_amt FROM currency_op_items "
                     "WHERE op_id=? AND kind='tx'", (op_id,))
        conn.execute("UPDATE transactions SET "
                     "unit_price=(SELECT v1 FROM _cursnap2 WHERE _cursnap2.rid=transactions.id), "
                     "amount=(SELECT v2 FROM _cursnap2 WHERE _cursnap2.rid=transactions.id), "
                     "currency=? WHERE id IN (SELECT rid FROM _cursnap2)", (op["from_cur"],))
        conn.execute("UPDATE transactions SET unit_price=ROUND(COALESCE(unit_price,0)*?,6), "
                     "amount=ROUND(COALESCE(amount,0)*?,6), currency=? "
                     "WHERE id NOT IN (SELECT rid FROM _cursnap2)",
                     (back, back, op["from_cur"]))

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        u = current_user() or {}
        conn.execute(
            "INSERT INTO currency_ops(op_no,from_cur,to_cur,rate,mode,part_count,tx_count,"
            "total_before,total_after,status,revert_of,operator_id,operator_name,note,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_cur_next_no(conn), op["to_cur"], op["from_cur"], back,
             "approx" if new_tx else "exact", op["part_count"], op["tx_count"],
             op["total_after"], op["total_before"], "active", op_id,
             u.get("id"), u.get("username", ""),
             "撤销 %s" % op["op_no"], now))
        rev_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE currency_ops SET status='reverted', reverted_by=? WHERE id=?",
                     (rev_id, op_id))
        conn.execute(
            "INSERT INTO audit_log(obj_type,obj_id,obj_code,action,detail,operator_id,operator_name,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("currency", rev_id, op["op_no"], "revert",
             json.dumps({"revert_of": op_id, "new_tx_during": new_tx, "rate_back": back},
                        ensure_ascii=False),
             u.get("id"), u.get("username", ""), db.now_str()))
        conn.commit()
    except Exception as ex:
        conn.rollback()
        flash(_xl("撤销失败，已回滚，数据没有任何改动：{err}", err=str(ex)), "danger")
        return redirect(url_for("parts_currency_page"))
    finally:
        conn.close()
    set_currency_cfg(op["from_cur"], currency_cfg()["form"])
    if new_tx:
        flash(_xl("已撤销 {no}。期间有 {n} 笔新业务不在快照里，按反向汇率回滚，可能有分位差，请核对报表",
                  no=op["op_no"], n=new_tx), "warning")
    else:
        flash(_xl("已按快照精确撤销 {no}，金额分毫不差", no=op["op_no"]), "success")
    return redirect(url_for("parts_currency_page"))


def _all_locations(conn):
    """全部货架位置，附占用物料信息（供编辑弹窗级联与占用判断）
    排序：空闲（未占用）位置在前，再按仓库-货架-货位-格位
    占用 = 该位置有任一【未删除】物料存放库存（多库位模型，不再依赖 parts.location_id）；
    已删除（回收站）物料的库存行不计占用——删除即释放库位"""
    rows = conn.execute(
        """SELECT l.*, iv.part_id, p.part_code, p.name
           FROM locations l
           LEFT JOIN (
              SELECT location_id, MIN(part_id) part_id FROM inventory
              WHERE location_id IS NOT NULL
                AND part_id IN (SELECT id FROM parts WHERE deleted=0)
              GROUP BY location_id
           ) iv ON iv.location_id=l.id
           LEFT JOIN parts p ON p.id=iv.part_id AND p.deleted=0
           ORDER BY CASE WHEN iv.part_id IS NULL THEN 0 ELSE 1 END,
                    l.warehouse, l.rack, l.shelf, l.bin"""
    ).fetchall()
    return [dict(r) for r in rows]


@app.route("/api/parts/search")
@login_required
def api_parts_search():
    """物料高级搜索（出入库申请/出入库管理/调库管理共用）

    分页：limit（每页条数，硬上限 100）+ offset（跳过的条数）。
    总数走响应头 X-Total-Count，JSON 结构保持数组不变——
    移动盘点页等旧消费方只读数组，改结构会连坐。
    """
    kw = request.args.get("kw", "").strip()
    # v1.0.39：按「库存所在库位属性」筛选——良品 / 待维修 / 待报废。
    # 选料弹窗里料号几百条，不筛根本找不到「哪个在待维修区」。
    attr = request.args.get("attr", "").strip()
    if attr not in ("good", "pending_repair", "pending_scrap"):
        attr = ""
    try:
        limit = min(max(_int(request.args.get("limit", 10)), 1), 100)
    except (TypeError, ValueError):
        limit = 10
    try:
        offset = max(_int(request.args.get("offset", 0) or 0), 0)
    except (TypeError, ValueError):
        offset = 0
    conn = get_db()
    try:
        # v1.0.42：按库位属性筛选时，列表「库存」列要显示该类型的数量，不能还是全库位总数。
        # 四分类口径与 _query_parts 同源：良品区&非 defective = 可用（良品+维修品）；
        # 待维修区 + 良品区 defective = 待维修品；待报废区 = 待报废品。
        # attr_stock 用参数占位，SQL 里出现在 WHERE 之前，故参数顺序为 (attr,attr,attr)+wargs+(limit,offset)。
        base = """SELECT p.id, p.part_code, p.name, p.spec, p.unit, p.unit_price, p.is_valuable,
                         p.qty_decimal,
                         COALESCE(SUM(i.quantity),0) stock,
                         COALESCE(SUM(CASE
                           WHEN ?='good' THEN CASE WHEN COALESCE(l.attribute,'good')='good'
                                 AND COALESCE(i.condition,'good')<>'defective' THEN i.quantity ELSE 0 END
                           WHEN ?='pending_repair' THEN CASE WHEN COALESCE(l.attribute,'good')='pending_repair'
                                 OR (COALESCE(l.attribute,'good')='good' AND COALESCE(i.condition,'good')='defective')
                                 THEN i.quantity ELSE 0 END
                           WHEN ?='pending_scrap' THEN CASE WHEN COALESCE(l.attribute,'good')='pending_scrap'
                                 THEN i.quantity ELSE 0 END
                           ELSE i.quantity END),0) attr_stock,
                         COALESCE(SUM(CASE WHEN (i.location_id IS NOT NULL)
                                            OR (i.location_id IS NULL AND IFNULL(i.location,'') NOT IN ('','待定'))
                                      THEN 1 ELSE 0 END),0) placed
                  FROM parts p LEFT JOIN inventory i ON p.id=i.part_id
                  LEFT JOIN locations l ON l.id=i.location_id
                  WHERE p.deleted=0 %s
                  GROUP BY p.id ORDER BY p.part_code LIMIT ? OFFSET ?"""
        where, wargs = [], []
        if kw:
            where.append("AND (p.part_code LIKE ? OR p.name LIKE ? OR p.spec LIKE ? OR p.category LIKE ?)")
            wargs += ["%{0}%".format(kw)] * 4
        if attr:
            # 只算有库存的行；库位属性为空按良品（与 db.cond_summary 同源口径）
            where.append("AND EXISTS (SELECT 1 FROM inventory i2 "
                         "LEFT JOIN locations l2 ON l2.id=i2.location_id "
                         "WHERE i2.part_id=p.id AND i2.quantity>0 "
                         "AND COALESCE(l2.attribute,'good')=?)")
            wargs.append(attr)
        wsql = " ".join(where)
        rows = conn.execute(base % wsql, (attr, attr, attr) + tuple(wargs) + (limit, offset)).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM parts p WHERE p.deleted=0 " + wsql, tuple(wargs)).fetchone()[0]
    finally:
        conn.close()
    out = [dict(r) for r in rows]
    # 「待定」标记：有库存、但所有库存行都没有放置位置 → 不允许领用。
    # 选择器要据此把这一行置灰，不能让用户选上、提交时才被一条红字打回来。
    for r in out:
        r["pending"] = 1 if (float(r.get("stock") or 0) > 0 and int(r.get("placed") or 0) == 0) else 0
    # 金额敏感：无「物料金额查看」权限（普通用户默认）→ 不返回单价
    if not can(current_user()["role"], "price"):
        for r in out:
            r.pop("unit_price", None)
    # 总数放响应头，不改 JSON 结构（移动盘点页等旧消费方不受影响）
    resp = jsonify(out)
    resp.headers["X-Total-Count"] = str(total)
    return resp


@app.route("/api/parts/<int:pid>")
@login_required
def api_part_detail(pid):
    conn = get_db()
    try:
        d = db.part_detail(conn, pid)
        if d:
            d = dict(d)
            loc = db.get_location(conn, d.get("location_id"))
            d["location_text"] = db.location_text(loc) if loc else ""
            d["apps"] = db.part_apps(conn, pid)
            d["extra"] = _extra_norm(d.get("extra"))
    finally:
        conn.close()
    if not d:
        return jsonify({"error": _xl("物料不存在")}), 404
    # 金额敏感：无「物料金额查看」权限（普通用户默认）→ 不返回单价
    if not can(current_user()["role"], "price"):
        d.pop("unit_price", None)
    # v1.0.70：与 _query_parts 同源的内置物料图回退。v1.0.67 起 30 张内置图随程序
    # 发布（static/partimg/<料号>.jpg），但详情接口只看 image_file，导致批量选料
    # 弹窗里点物料看不到图（列表页有图、点开没了）。用户上传图始终优先。
    d["image_url"] = ""
    if has_app_context():
        if d.get("image_file"):
            d["image_url"] = url_for("media", fname=d["image_file"])
        else:
            _code = str(d.get("part_code") or "")
            if re.fullmatch(r"[A-Za-z0-9_\-]+", _code):
                _rel = "partimg/" + _code + ".jpg"
                if os.path.exists(os.path.join(app.static_folder, "partimg", _code + ".jpg")):
                    d["image_url"] = url_for("static", filename=_rel)
    return jsonify(d)


@app.route("/api/locations/empty")
@login_required
def api_empty_locations():
    """返回所有空库位（未被任何物料占用的），供调库选择"""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT l.id, l.warehouse, l.rack, l.shelf, l.bin,
                      l.attribute,
                      l.warehouse||'-'||l.rack||'-'||l.shelf||'-'||l.bin as full_name
               FROM locations l
               WHERE l.id NOT IN (
                   SELECT DISTINCT location_id FROM inventory WHERE quantity > 0 AND location_id IS NOT NULL
               )
               ORDER BY l.warehouse, l.rack, l.shelf, l.bin"""
        ).fetchall()
        # v1.0.38：整货位（bin 为空）不要再拼出「WH-04-R-01-S-02-」这种尾巴横杠，
        # 下拉里看着像少了一截，现场以为是库位编码错了。
        locs = []
        for r in rows:
            d = dict(r)
            d["full_name"] = "-".join([x for x in (d.get("warehouse"), d.get("rack"),
                                                   d.get("shelf"), d.get("bin"))
                                       if x and str(x).strip()])
            locs.append(d)
        return jsonify({"locations": locs})
    finally:
        conn.close()


@app.route("/parts/add", methods=["POST"])
@login_required
@perm_required("parts_manage", "add")
def part_add():
    f = request.form
    part_code = f.get("part_code", "").strip()
    name = f.get("name", "").strip()
    if not part_code or not name:
        flash(_i18n.t("料号与名称必填", _i18n.current_lang()), "danger")
        return redirect(url_for("parts_manage_page"))
    # 单价不允许负数：金额、占比、领用金额统计全靠它，负数会把图表算反
    if _num_finite(f.get("unit_price", 0)) < 0:
        flash(_i18n.t("单价不能为负数", _i18n.current_lang()), "danger")
        return redirect(url_for("parts_manage_page"))
    # 表头设置里给每列配的字数上限是硬约束：超了不准录入，不能悄悄截断
    len_errs = _too_long_errs(f)
    if len_errs:
        flash(_too_long_msg(len_errs), "danger")
        return redirect(url_for("parts_manage_page"))
    # 自定义表头按列类型校验（整数/小数/日期），填错就拦下来，别静默存空
    extra_vals, extra_errs, extra_len_errs = _form_extra(f)
    if extra_errs:
        flash(_extra_type_error(extra_errs), "danger")
        return redirect(url_for("parts_manage_page"))
    if extra_len_errs:
        flash(_too_long_msg(extra_len_errs, translate=False), "danger")
        return redirect(url_for("parts_manage_page"))
    conn = get_db()
    try:
        if conn.execute("SELECT id FROM parts WHERE part_code=? AND deleted=0", (part_code,)).fetchone():
            flash(_i18n.t("料号 {0} 已存在", _i18n.current_lang()).format(part_code), "danger")
            return redirect(url_for("parts_manage_page"))
        if conn.execute("SELECT id FROM parts WHERE part_code=? AND deleted=1", (part_code,)).fetchone():
            flash(_i18n.t("料号 {0} 在回收站中，请先在回收站恢复或彻底清除后再新增", _i18n.current_lang()).format(part_code), "danger")
            return redirect(url_for("parts_manage_page"))
        extra_json = json.dumps(extra_vals, ensure_ascii=False)
        qdec = _qdec(f.get("qty_decimal"))
        unit = f.get("unit", "个").strip()
        # 最低/最高库存也服从数量类型：整数类物料不许把 100.5 存进库（库里存的是上限本身）
        min_v = max(0.0, _num_finite(f.get("min_stock", 0)))
        max_v = max(0.0, _num_finite(f.get("max_stock", 0)))
        if not qdec:
            min_v, max_v = _round_int(min_v), _round_int(max_v)
        cat = _ensure_category(conn, f.get("category", ""))
        cur = conn.execute(
            """INSERT INTO parts(part_code,name,category,spec,unit,qty_decimal,unit_price,min_stock,max_stock,
               is_valuable,valuable_lvl,auto_min,is_repair,supplier,remark,location_id,extra,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (part_code, name, cat, f.get("spec", "").strip(),
             unit, qdec,
             _num_finite(f.get("unit_price", 0)),
             min_v, max_v,
             1 if f.get("is_valuable") else 0, 1 if f.get("valuable_lvl") else 0,
             1 if f.get("auto_min") else 0, 1 if f.get("is_repair") else 0,
             f.get("supplier", "").strip(),
             f.get("remark", "").strip(), None, extra_json, db.now_str(), db.now_str()),
        )
        conn.commit()
        pid = cur.lastrowid
        _audit("part", pid, part_code, "add", {"f": {
            "part_code": ["", part_code], "name": ["", f.get("name", "").strip()],
            "category": ["", f.get("category", "").strip()], "spec": ["", f.get("spec", "").strip()],
            "unit": ["", unit], "qty_decimal": ["", _fmt_part_val("qty_decimal", qdec)],
            "unit_price": ["", _fmt_part_val("unit_price", _num_finite(f.get("unit_price", 0)))],
            "min_stock": ["", _fmt_part_val("min_stock", min_v)],
            "max_stock": ["", _fmt_part_val("max_stock", max_v)],
            "is_valuable": ["", _fmt_part_val("is_valuable", 1 if f.get("is_valuable") else 0)],
            "is_repair": ["", _fmt_part_val("is_repair", 1 if f.get("is_repair") else 0)],
            "supplier": ["", f.get("supplier", "").strip()], "remark": ["", f.get("remark", "").strip()],
        }})
        # 初始库存：可配置多个库位（位置 id + 数量 并行数组；数量留空=0 仅登记库位）
        inv_locs = f.getlist("inv_loc[]")
        inv_qtys = f.getlist("inv_qty[]")
        added = 0
        for lid, q in zip(inv_locs, inv_qtys):
            qv = max(0, _num(q))
            lid_i = _int(lid, 0)
            if not lid_i:
                continue
            # 整数类物料：库位库存数量必须为整数（前端已限制，后端兜底校验）
            if not qdec and qv != int(qv):
                flash(_i18n.t("该物料按『{0}』整数计数，库位库存数量不能填小数", _i18n.current_lang()).format(unit), "danger")
                continue
            qv = int(qv) if not qdec else qv
            # 位置被其他物料占用时拒绝（与编辑弹窗添加库位同规则）
            occ = conn.execute(
                """SELECT i.part_id, p.part_code FROM inventory i
                   JOIN parts p ON i.part_id=p.id AND p.deleted=0
                   WHERE i.location_id=? AND i.part_id<>? LIMIT 1""",
                (lid_i, pid),
            ).fetchone()
            if occ:
                flash(_xl("位置(id={p1})已配置给物料 [{p2}]，未添加；请先将其调走", p1=lid_i, p2=occ['part_code']), "warning")
                continue
            ok, msg = db.move_stock(conn, pid, lid_i, qv)
            if not ok:
                flash(msg, "warning")
            else:
                added += 1
        if inv_locs and any(x.strip() for x in inv_locs) and added == 0:
            flash(_i18n.t("初始库位未添加：所选位置不存在或已被其他物料占用", _i18n.current_lang()), "warning")
        # 适用产线/设备（只选设备/整线，产线自动带出）
        db.set_part_apps(conn, pid, _form_part_apps(f))
        conn.commit()
        flash(_i18n.t("物料 [{0}] 添加成功", _i18n.current_lang()).format(part_code), "success")
    finally:
        conn.close()
    return redirect(url_for("parts_manage_page"))


@app.route("/parts/<int:pid>/edit", methods=["POST"])
@login_required
@perm_required("parts_manage", "edit")
def part_edit(pid):
    f = request.form
    # 单价不允许负数（导入/编辑两条路都要挡，否则前端 min=0 被绕过就落库了）
    if _num_finite(f.get("unit_price", 0)) < 0:
        flash(_i18n.t("单价不能为负数", _i18n.current_lang()), "danger")
        return redirect(url_for("parts_manage_page", _anchor="part-%d" % pid))
    # 自定义表头按列类型校验（整数/小数/日期），填错就拦下来，别静默存空
    _extra_vals, extra_errs, extra_len_errs = _form_extra(f)
    if extra_errs:
        flash(_extra_type_error(extra_errs), "danger")
        return redirect(url_for("parts_manage_page", _anchor="part-%d" % pid))
    if extra_len_errs:
        flash(_too_long_msg(extra_len_errs, translate=False), "danger")
        return redirect(url_for("parts_manage_page", _anchor="part-%d" % pid))
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT id, part_code, name, category, spec, unit, qty_decimal, unit_price, min_stock,
                      max_stock, is_valuable, valuable_lvl, auto_min, is_repair, deprecated, supplier, remark, extra
               FROM parts WHERE id=? AND deleted=0""", (pid,)).fetchone()
        if not row:
            flash(_i18n.t("物料不存在或已被删除", _i18n.current_lang()), "danger")
            return redirect(url_for("parts_manage_page"))
        # 表头设置的字数上限：改过的值超长就拒绝；值没变的列放过（历史数据不挡路）
        len_errs = _too_long_errs(f, conn, old={
            "code": row["part_code"], "name": row["name"],
            "category": row["category"], "spec": row["spec"]})
        if len_errs:
            flash(_too_long_msg(len_errs), "danger")
            return redirect(url_for("parts_manage_page", _anchor="part-%d" % pid))
        new_dec = _qdec(f.get("qty_decimal"))
        new_extra = _extra_vals
        extra_json = json.dumps(new_extra, ensure_ascii=False)
        new_pairs = _form_part_apps(f)
        # 最低/最高库存服从数量类型：整数类物料库里不该出现 100.5 这样的上限
        min_v = max(0.0, _num_finite(f.get("min_stock", 0)))
        max_v = max(0.0, _num_finite(f.get("max_stock", 0)))
        if not new_dec:
            min_v, max_v = _round_int(min_v), _round_int(max_v)
        # 审计要拿「改之前」的值：适用产线/设备必须在 set_part_apps 之前取，取完再改
        old_apps_text = _apps_text(db.part_apps(conn, pid))
        new_cat = _ensure_category(conn, f.get("category", ""))
        conn.execute(
            """UPDATE parts SET name=?, category=?, spec=?, unit=?, qty_decimal=?, unit_price=?,
               min_stock=?, max_stock=?, is_valuable=?, valuable_lvl=?, auto_min=?, is_repair=?, deprecated=?, deprecated_at=?,
               supplier=?, remark=?, extra=?, updated_at=?
               WHERE id=?""",
            (f.get("name", "").strip(), new_cat,
             f.get("spec", "").strip(), f.get("unit", "个").strip(),
             new_dec,
             _num_finite(f.get("unit_price", 0)), min_v, max_v,
             1 if f.get("is_valuable") else 0,
             1 if f.get("valuable_lvl") else 0,
             1 if f.get("auto_min") else 0,
             1 if f.get("is_repair") else 0,
             1 if f.get("deprecated") else 0,
             db.now_str() if f.get("deprecated") else "",
             f.get("supplier", "").strip(), f.get("remark", "").strip(),
             extra_json, db.now_str(), pid),
        )
        # 数量类型=整数：库存数量自动取整（四舍五入）。不只「小数→整数」那一刻做，
        # 已经标成整数、但库里还留着 0.332 零头的老数据，也在保存时一并修好。
        rounded = 0
        if not new_dec:
            cur = conn.execute(
                "UPDATE inventory SET quantity=ROUND(quantity) WHERE part_id=?", (pid,)
            )
            rounded = cur.rowcount if cur.rowcount > 0 else 0
        conn.commit()
        # 编辑时同步处理图片（上传/更换）
        file = request.files.get("image")
        img_state, img_detail = "", None
        if file and file.filename:
            img_state, img_detail = _save_part_image(conn, pid, file)
            if img_state == "err":
                flash(img_detail, "danger")
                return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))
        # 适用产线/设备（只选设备/整线，产线自动带出）
        db.set_part_apps(conn, pid, new_pairs)
        conn.commit()
        # 操作审计：编辑弹窗里能改的字段全进差异（只记真正变了的）
        try:
            diff, labels = {}, {}
            nv = {
                "name": f.get("name", "").strip(), "category": f.get("category", "").strip(),
                "spec": f.get("spec", "").strip(), "unit": f.get("unit", "个").strip(),
                "qty_decimal": _fmt_part_val("qty_decimal", new_dec),
                "unit_price": _fmt_part_val("unit_price", _num_finite(f.get("unit_price", 0))),
                "min_stock": _fmt_part_val("min_stock", min_v),
                "max_stock": _fmt_part_val("max_stock", max_v),
                "is_valuable": _fmt_part_val("is_valuable", 1 if f.get("is_valuable") else 0),
                "is_repair": _fmt_part_val("is_repair", 1 if f.get("is_repair") else 0),
                "valuable_lvl": _fmt_part_val("valuable_lvl", 1 if f.get("valuable_lvl") else 0),
                "auto_min": _fmt_part_val("auto_min", 1 if f.get("auto_min") else 0),
                "deprecated": _fmt_part_val("deprecated", 1 if f.get("deprecated") else 0),
                "supplier": f.get("supplier", "").strip(), "remark": f.get("remark", "").strip(),
            }
            for k, newv in nv.items():
                oldv = _fmt_part_val(k, row[k])
                if oldv != newv:
                    diff[k] = [oldv, newv]
            # 自定义表头字段：标签是管理员自己起的（不翻译），原样展示
            old_extra = _extra_norm(row["extra"])
            for ef in get_extra_fields(conn):
                k = "extra." + ef["key"]
                oldv = str(old_extra.get(ef["key"], "") or "")
                newv = str(new_extra.get(ef["key"], "") or "")
                if oldv != newv:
                    diff[k] = [oldv, newv]
                    labels[k] = ef["label"]
            # 适用产线/设备：整组对比（增删都看得见）
            new_apps_text = _apps_text_pairs(conn, new_pairs)
            if old_apps_text != new_apps_text:
                diff["apps"] = [old_apps_text or "—", new_apps_text or "—"]
            if diff:
                d = {"f": diff}
                if labels:
                    d["labels"] = labels
                _audit("part", pid, row["part_code"], "edit", d, conn=conn)
        except Exception:
            pass
        if rounded:
            msg = _xl("物料信息已更新，数量类型已改为整数，库存数量已自动取整（{n} 处）", n=rounded)
        else:
            msg = _xl("物料信息已更新")
        # 图片提示并进这一条，不再单独 flash —— 否则一次保存弹两条气泡
        if img_state == "zip":
            msg += _i18n.mk("；") + _i18n.t("物料图片已更新（已自动压缩到 {0}KB 以内）",
                                          _i18n.current_lang()).format(img_detail)
        elif img_state == "ok":
            msg += _i18n.mk("；") + _xl("物料图片已更新")
        flash(msg, "success")
    finally:
        conn.close()
    return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))


def _part_pending(conn, part_id):
    """物料是否处于「待定」状态：**有库存**、但没有任何有放置位置的库存行
    （多库位模型：所有库存行都没有位置时整体待定，不可领用/出库）。

    零库存不算待定 —— 那种情况该报「库存不足」，报「请先调库」会让人对着
    一个根本没货的料号去调库。
    """
    row = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN (location_id IS NOT NULL)
                                     OR (location_id IS NULL AND location IS NOT NULL
                                         AND location != '' AND location != '待定')
                                   THEN 1 ELSE 0 END),0) placed,
                  COALESCE(SUM(quantity),0) total
             FROM inventory WHERE part_id=?""",
        (part_id,),
    ).fetchone()
    if not row:
        return False
    return float(row["total"] or 0) > 0 and int(row["placed"] or 0) == 0


def _part_stock_lines(conn, part_id):
    """物料全部库存行（含待定行），供调库选择源库位"""
    return db.stock_locations(conn, part_id)


# 自定义表头字段类型的中文名（配置里存英文键，界面显示走 tr() 取词）
_EXTRA_TYPE_LABEL = {"text": "文本", "int": "整数", "decimal": "小数", "date": "日期"}

# 日期列允许的写法：2026-07-01 / 2026/7/1 / 2026.7.1 / 2026年7月1日 / 2026年7月 / 20260701 / Excel 日期序列号。
# 以前只认 yyyy-mm-dd，用户在 Excel 里明明填了「2026年7月1日」却被判「需填写日期」，
# 而把单元格设成真正的日期格式又会被读成序列号（46204）同样不过。
def _norm_date(v):
    """把各种常见日期写法归一成 YYYY-MM-DD；认不出来返回 None。"""
    s = (v or "").strip()
    if not s:
        return None
    # Excel 真日期单元格经 str() 后带时间尾：「2026-07-01 00:00:00」
    s = re.sub(r"[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*$", "", s).strip()
    # 中文写法先换成 - ：2026年7月1日 → 2026-7-1；2026年7月 → 2026-7-
    if "年" in s or "月" in s or "日" in s or "号" in s:
        s = s.replace("年", "-").replace("月", "-").replace("日", "").replace("号", "")
        s = s.rstrip("-").strip()
    m = re.fullmatch(r"(\d{4})\s*[-/.]\s*(\d{1,2})(?:\s*[-/.]\s*(\d{1,2}))?", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        d = int(m.group(3)) if m.group(3) else 1
    else:
        m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        elif re.fullmatch(r"\d{5}(?:\.\d+)?", s):
            # Excel 把日期存成序列号（1899-12-30 为 0），未套日期格式时读出来就是这种数字
            try:
                got = datetime(1899, 12, 30) + timedelta(days=float(s))
            except (ValueError, OverflowError):
                return None
            if not (1900 <= got.year <= 2100):
                return None
            y, mo, d = got.year, got.month, got.day
        else:
            return None
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _coerce_extra(v, t):
    """按自定义列的类型规范化一个值，返回 (ok, 规范化后的值)。

    表单录入与 Excel 导入共用同一份规则，避免「页面上填不进去、Excel 里能塞进去」。
    """
    t = t or config.DEFAULT_EXTRA_TYPE
    if t == "int":
        if not re.fullmatch(r"[+-]?\d+", v):
            return False, v
        return True, str(int(v))
    if t == "decimal":
        try:
            d = float(v)
        except (TypeError, ValueError):
            return False, v
        if d != d or d in (float("inf"), float("-inf")):
            return False, v
        return True, "%.10g" % d
    if t == "date":
        nd = _norm_date(v)
        if nd is None:
            return False, v
        return True, nd
    return True, v


def _form_extra(f, conn=None):
    """收集自定义表头字段值，并按该列声明的类型（文本/整数/小数/日期）校验。

    返回 (values, type_errors, len_errors)：
      type_errors = [(列名, 类型)]，len_errors = [(列名, 现长, 上限)]。
    调用方拿到 errors 要拦下来提示，不能静默丢值 —— 以前数量类字段填错是悄悄存成空，
    用户以为保存成功了。类型只约束录入，值仍以字符串存 parts.extra，列表照原样显示。
    文本/整数列还要守住「表头设置」里给该列配的字数上限（超长不准录入）。
    """
    extra, terrs, lerrs = {}, [], []
    lim = get_head_clip(conn)["extra"]
    for ef in get_extra_fields(conn):
        key = ef["key"]
        v = (f.get("extra_" + key) or "").strip()
        if not v:
            continue
        t = ef.get("type") or config.DEFAULT_EXTRA_TYPE
        ok, cv = _coerce_extra(v, t)
        if not ok:
            terrs.append((ef["label"], t))
            continue
        if t in ("text", "int"):
            n = lim.get(key, config.DEFAULT_HEAD_CLIP)
            if len(cv) > n:
                lerrs.append((ef["label"], len(cv), n))
                continue
        extra[key] = cv
    return extra, terrs, lerrs


def extra_type_options():
    """「类型」下拉的选项：[(值, 当前语言显示名)]，给设置页渲染用。"""
    lang = _i18n.current_lang()
    return [(t, _i18n.t(_EXTRA_TYPE_LABEL[t], lang)) for t in config.EXTRA_FIELD_TYPES]


def _extra_fields_with_clip(head_clip=None):
    """设置页用：让每个自定义列带上它自己的字数配置（模板里直接读 ef.clip）。

    以前设置页传的是裸 get_extra_fields()，模板写 ef.clip 取到 Undefined，
    已配置过的列字数框会显示空白，一保存就被改回默认 60。
    """
    hc = head_clip if head_clip is not None else get_head_clip()
    out = get_extra_fields()
    for ef in out:
        ef["clip"] = hc["extra"].get(ef["key"], config.DEFAULT_HEAD_CLIP)
    return out


def _extra_type_error(errs):
    """把第一处类型错误拼成一句可翻译的提示"""
    label, t = errs[0]
    return _xl("自定义表头「{label}」需填写{kind}，请修改后重新保存",
               label=label,
               kind=_i18n.t(_EXTRA_TYPE_LABEL.get(t, "文本"), _i18n.current_lang()))


def _form_part_apps(f):
    """收集物料适用产线/设备：pa_line[] / pa_equip[] 并行数组
    每行可只选设备（产线留空=设备未分配产线）或只选产线（整线适用）
    返回 [(line_id_or_None, equipment_id_or_None), ...]"""
    lines = f.getlist("pa_line[]")
    equips = f.getlist("pa_equip[]")
    pairs = []
    for ln, eq in zip(lines, equips):
        ln = (ln or "").strip()
        eq = (eq or "").strip()
        try:
            lid = int(ln) if ln else None
        except (TypeError, ValueError):
            lid = None
        try:
            eid = int(eq) if eq else None
        except (TypeError, ValueError):
            eid = None
        if not lid and not eid:
            continue
        pairs.append((lid, eid))
    return pairs


def _apps_label(a, plain=False):
    """适用产线/设备的显示文本（统一简洁格式，不带「XX线/」「（整线）」「（未分配产线）」）：
    - 有设备 → 只显示设备名
    - 仅产线 → 显示产线名"""
    line = (a.get("line_name") or "").strip()
    equip = (a.get("equip_name") or "").strip()
    if equip:
        return equip
    if line:
        return line
    return ""


def _upload_compress_kb(conn=None):
    """读取「上传图片自动压缩」阈值（KB，0=不压缩）；随数据库走"""
    try:
        c = conn or get_db()
        raw = db.get_setting(c, "upload_cfg")
        if raw:
            d = json.loads(raw)
            return max(0, min(int(d.get("compress_kb") or 0), 10240))
    except Exception:
        pass
    return 400


def _compress_image(data, max_kb):
    """用 Pillow 把图片压缩到 max_kb KB 以内（尽力而为）；无法处理时原样返回
    返回 (bytes, 是否发生了压缩)"""
    if max_kb <= 0 or len(data) <= max_kb * 1024:
        return data, False
    try:
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(data))
        img.load()
        w, h = img.size
        # 1) 尺寸过大先缩到最长边 4096 以内
        if max(w, h) > 4096:
            s = 4096 / max(w, h)
            img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
        # 2) 统一转 RGB（带透明通道的用白底合成）
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        limit = max_kb * 1024
        # 3) 质量迭代
        for quality in (90, 78, 65, 50, 40):
            buf = _io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= limit:
                return buf.getvalue(), True
        # 4) 仍超：逐级缩小分辨率再压
        for longside in (2048, 1600, 1280, 1024, 800):
            w, h = img.size
            if max(w, h) <= longside:
                continue
            s = longside / max(w, h)
            small = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
            buf = _io.BytesIO()
            small.save(buf, "JPEG", quality=78, optimize=True)
            if buf.tell() <= limit:
                return buf.getvalue(), True
        return data, False
    except Exception:
        return data, False


def _save_part_image(conn, pid, file):
    """保存物料图片（覆盖旧图），供新增/编辑使用——图片二进制存入主数据库 images 表

    返回 (state, detail)，**这里不 flash**：
      ("ok",  "")        已保存
      ("zip", max_kb)    已保存，并压缩到 max_kb 以内
      ("err", 文案)      失败，文案已按当前语言取好，由调用方 flash(danger)

    为什么不自己 flash：编辑物料时这里闪一条「物料图片已更新」，外面再闪一条
    「物料信息已更新」，用户点一次保存会看到两条提示（新增带图同理）。
    提示统一由调用方合并成一句。
    """
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in config.ALLOWED_IMG_EXTS:
        return "err", _i18n.t("不支持的图片格式（支持 jpg/png/gif/bmp/webp）", _i18n.current_lang())
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > config.MAX_IMG_SIZE:
        return "err", _i18n.t("图片超过 5MB 限制", _i18n.current_lang())
    raw = file.read()
    max_kb = _upload_compress_kb(conn)
    data, compressed = _compress_image(raw, max_kb)
    save_name = f"part{pid}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secure_filename(file.filename)}"
    db.image_save(conn, save_name, data)
    old = conn.execute("SELECT image_file FROM parts WHERE id=?", (pid,)).fetchone()
    if old and old["image_file"] and old["image_file"] != save_name:
        db.image_delete(conn, old["image_file"])
        # 兼容旧版：删除历史遗留的同名目录文件（失败忽略，图片已在库中）
        fp = os.path.join(config.resolved_upload_dir(), old["image_file"])
        if os.path.exists(fp):
            try:
                os.remove(fp)
            except OSError:
                pass
    conn.execute("UPDATE parts SET image_file=? WHERE id=?", (save_name, pid))
    # 操作审计：上传/更换物料图片（新增、编辑、单独上传三个入口都经过这里）
    try:
        pc = conn.execute("SELECT part_code FROM parts WHERE id=?", (pid,)).fetchone()
        _audit("part", pid, pc["part_code"] if pc else str(pid), "image", {"note": "上传图片"}, conn=conn)
    except Exception:
        pass
    conn.commit()
    return ("zip", max_kb) if compressed else ("ok", "")


@app.route("/parts/<int:pid>/delete", methods=["POST"])
@login_required
@perm_required("parts_manage", "delete")
def part_delete(pid):
    """删除物料 → 移入回收站（软删除）。库存必须为 0、无未完成申请单；
    回收站仅管理员可见/可恢复，超保留期由后台线程自动彻底清除"""
    conn = get_db()
    try:
        stock = db.total_stock(conn, pid)
        if stock > 0:
            flash(_i18n.t("该物料仍有库存，不能删除；请先调拨或出库至 0", _i18n.current_lang()), "danger")
            return redirect(url_for("parts_manage_page"))
        # 未完成申请单校验：存在待处理/已批准待出库/撤销待审批的申请单时禁止删除
        pnd = conn.execute(
            """SELECT app_no, app_type, status FROM applications
               WHERE part_id=? AND status IN ('pending','approved','cancel_req')
               ORDER BY id LIMIT 1""",
            (pid,),
        ).fetchone()
        if pnd:
            status_name = {"pending": "待处理", "approved": "已批准·待出库", "cancel_req": "撤销待审批"}
            flash(
                _i18n.t("该物料存在未完成申请单 {0}（{1}），不能删除；请先处理该申请单（执行/驳回/撤销）后再删除", _i18n.current_lang())
                .format(pnd['app_no'], status_name.get(pnd['status'], pnd['status'])),
                "danger",
            )
            return redirect(url_for("parts_manage_page"))
        if not conn.execute("SELECT id FROM parts WHERE id=? AND deleted=0", (pid,)).fetchone():
            flash(_i18n.t("物料不存在或已被删除", _i18n.current_lang()), "danger")
            return redirect(url_for("parts_manage_page"))
        p_row = conn.execute("SELECT part_code FROM parts WHERE id=?", (pid,)).fetchone()
        # 软删：释放该物料所有无库存的预留库位（quantity=0），让这些位置立刻可选
        conn.execute("DELETE FROM inventory WHERE part_id=? AND quantity=0", (pid,))
        # 主库位置空，避免主库位一直被占用
        conn.execute("UPDATE parts SET deleted=1, deleted_at=?, location_id=NULL WHERE id=?", (db.now_str(), pid))
        conn.commit()
        try:
            # 审计正文存中文原文（历史记录不该随操作者语言变），渲染时由
            # audit.html 的 tr(d.note) 翻译。这里显式取一次「中文」只是为了让
            # 构建脚本把这句登记进词条表 —— 直接写裸字面量扫不到，英文界面会漏中文。
            _audit("part", pid, p_row["part_code"] if p_row else str(pid), "delete",
                   {"note": _i18n.t("删除物料（移入回收站）", "zh")}, conn=conn)
        except Exception:
            pass
        flash(_i18n.t("物料已移入回收站（管理员可在回收站恢复或彻底删除）", _i18n.current_lang()), "success")
    finally:
        conn.close()
    return redirect(url_for("parts_manage_page"))


@app.route("/parts/<int:pid>/restore_deprecated", methods=["POST"])
@login_required
@perm_required("parts_manage", "edit")
def part_restore_deprecated(pid):
    """恢复被标记停用的物料，将deprecated字段改回0"""
    conn = get_db()
    try:
        p = conn.execute("SELECT part_code FROM parts WHERE id=? AND deleted=0", (pid,)).fetchone()
        conn.execute("UPDATE parts SET deprecated=0 WHERE id=?", (pid,))
        conn.commit()
        # 操作审计：停用 → 正常，也是编辑，不能漏
        try:
            _audit("part", pid, p["part_code"] if p else str(pid), "edit",
                   {"f": {"deprecated": ["是", "否"]}}, conn=conn)
        except Exception:
            pass
        flash(_i18n.t("物料已恢复正常使用状态", _i18n.current_lang()), "success")
    finally:
        conn.close()
    return redirect(url_for("parts_manage_page", state="deprecated"))


# ------------------------------------------------------------------
# 回收站（物料软删除管理，仅管理员）：
#   删除物料 → deleted=1 进回收站；管理员可恢复/彻底删除；
#   保留天数可配置（默认 3 天，sys_settings key=recycle_days），超期由后台线程自动彻底清除
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# 操作审计（物料 / 货架位置：新增、编辑、删除、恢复、彻底清除）
# ------------------------------------------------------------------
PART_FIELD_NAMES = {
    "part_code": "物料编码", "name": "物料名称", "category": "分类", "spec": "规格型号",
    "unit": "单位", "qty_decimal": "数量类型", "unit_price": "单价", "min_stock": "最小库存",
    "max_stock": "最大库存", "is_valuable": "贵重", "supplier": "供应商", "remark": "备注",
    "location_id": "存放位置",
    # 编辑弹窗里能改、以前没进审计的字段
    "valuable_lvl": "强制二级审批", "auto_min": "自动上下限",
    "deprecated": "停用", "apps": "适用产线/设备",
}
# 盘点单审计的 detail 字段标签（展示用；值会再过一遍 tr()）
STOCKTAKE_FIELD_NAMES = {
    "part_code": "物料编码", "location": "库位",
    "book": "账面数量", "actual": "实盘数量", "diff": "差异",
    "unit_price": "单价", "amount": "差异金额",
    "scope": "盘点范围", "picks": "盘点范围", "items": "明细数",
    "level": "审批级别", "review_level": "审批级别", "note": "审批意见",
    "valuable": "贵重",
}


def _fmt_part_val(k, v):
    if k == "qty_decimal":
        return "小数" if v else "整数"
    if k in ("is_valuable", "valuable_lvl", "auto_min", "deprecated", "is_repair"):
        return "是" if v else "否"
    if v is None:
        return ""
    if k in ("unit_price", "min_stock", "max_stock"):
        # 数值列统一成「有小数才带小数点」：否则库里是 100.0、表单是 100，
        # 每次保存都会多写一条「最低库存 100.0 → 100」的假审计记录。
        return _fmt_qty(v)
    return str(v)


def _apps_text(rows):
    """适用产线/设备 → 展示文本：产线·设备(型号)，整线只写产线；多项用「，」连接"""
    out = []
    for r in rows or []:
        ln = (r.get("line_name") or "").strip()
        eq = (r.get("equip_name") or "").strip()
        md = (r.get("equip_model") or "").strip()
        if eq:
            name = f"{eq}({md})" if md else eq
            out.append(f"{ln}·{name}" if ln else name)
        elif ln:
            out.append(ln)
    return "，".join(out)


def _apps_text_pairs(conn, pairs):
    """[(line_id, equip_id)] → 展示文本（落库前用，名称从库里取）"""
    rows = []
    for lid, eid in pairs or []:
        row = {"line_name": "", "equip_name": "", "equip_model": ""}
        if lid:
            r = conn.execute("SELECT name FROM production_lines WHERE id=?", (int(lid),)).fetchone()
            row["line_name"] = (r["name"] if r else "") or ""
        if eid:
            r = conn.execute("SELECT name, model FROM equipment WHERE id=?", (int(eid),)).fetchone()
            row["equip_name"] = (r["name"] if r else "") or ""
            row["equip_model"] = (r["model"] if r else "") or ""
        rows.append(row)
    return _apps_text(rows)

def _audit(obj_type, obj_id, obj_code, action, detail=None, conn=None):
    """写入操作审计记录。detail: dict 或 str"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        u = None
        try:
            u = current_user()
        except Exception:
            u = None
        detail_json = json.dumps(detail, ensure_ascii=False) if isinstance(detail, (dict, list)) else (detail or "")
        conn.execute(
            "INSERT INTO audit_log(obj_type,obj_id,obj_code,action,detail,operator_id,operator_name,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (obj_type, obj_id, obj_code, action, detail_json,
             u.get("id") if u else None, u.get("username") if u else "", db.now_str()),
        )
        conn.commit()
    finally:
        if own:
            conn.close()


def _recycle_days(conn=None):
    """回收站保留天数（默认 3 天）"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        try:
            d = int(db.get_setting(conn, "recycle_days") or 3)
            return d if 1 <= d <= 365 else 3
        except (TypeError, ValueError):
            return 3
    finally:
        if own:
            conn.close()


def _audit_days(conn=None):
    """操作记录保留天数（sys_settings key=audit_days，0=永久保留，默认 0）"""
    own = conn is None
    if own:
        conn = get_db()
    try:
        try:
            return max(0, int(db.get_setting(conn, "audit_days") or 0))
        except (TypeError, ValueError):
            return 0
    finally:
        if own:
            conn.close()


def _audit_cleanup_once():
    """删除超过保留天数的操作审计记录并压缩数据库（返回清理条数；供后台线程与手动清理调用）

    压缩：SQLite 删除只把页挂到空闲链表、文件不会变小，删完跑一次 VACUUM 才真正回收磁盘。
    """
    days = _audit_days()
    if days <= 0:
        return 0
    cut = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        n = conn.execute(
            "DELETE FROM audit_log WHERE created_at IS NOT NULL AND created_at<=?", (cut,)
        ).rowcount
        if n:
            conn.commit()
            try:
                conn.execute("VACUUM")  # 有并发写入时可能报 locked，压缩失败不影响清理结果
            except Exception:
                pass
        return n
    finally:
        conn.close()


def _purge_part(conn, pid):
    """彻底清除一条物料：图片 + 适用关系 + 库存 + 主记录（回收站超期/手动彻底删除用）"""
    p = conn.execute("SELECT image_file FROM parts WHERE id=?", (pid,)).fetchone()
    if p and p["image_file"]:
        db.image_delete(conn, p["image_file"])
        fp = os.path.join(config.resolved_upload_dir(), p["image_file"])
        if os.path.exists(fp):
            try:
                os.remove(fp)
            except OSError:
                pass
    conn.execute("DELETE FROM part_applications WHERE part_id=?", (pid,))
    conn.execute("DELETE FROM inventory WHERE part_id=?", (pid,))
    conn.execute("DELETE FROM parts WHERE id=?", (pid,))


def _recycle_list(conn):
    """回收站列表：deleted=1 的物料 + 到期自动清除时间"""
    days = _recycle_days(conn)
    now = datetime.now()
    rows = conn.execute(
        """SELECT id, part_code, name, category, spec, unit, unit_price, image_file,
                  location_id, created_at, updated_at, deleted_at
           FROM parts WHERE deleted=1 ORDER BY deleted_at DESC"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["expire"] = None
        d["expired"] = False
        deleted_at = d.get("deleted_at")
        if deleted_at:
            try:
                dt = datetime.strptime(deleted_at[:19], "%Y-%m-%d %H:%M:%S")
                d["expire"] = (dt + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                d["expired"] = now >= dt + timedelta(days=days)
            except (ValueError, TypeError):
                pass
        loc = db.get_location(conn, d.get("location_id"))
        d["location_text"] = db.location_text(loc) if loc else ""
        out.append(d)
    return out


@app.route("/parts/recycle")
@login_required
@perm_required("recycle")
def recycle_page():
    """回收站（仅管理员）：按类型分组展示 物料/货架位置/部门/用户；查看/恢复/彻底删除/保留天数设置"""
    conn = get_db()
    try:
        days = _recycle_days(conn)
        groups = []
        _rl = _i18n.current_lang()
        for t, label in (("parts", _i18n.t("物料", _rl)), ("location", _i18n.t("货架位置", _rl)),
                         ("department", _i18n.t("部门", _rl)), ("user", _i18n.t("用户", _rl))):
            items = _recycle_list(conn) if t == "parts" else _recycle_items_list(conn, t)
            groups.append({"type": t, "label": label, "rows": items})
    finally:
        conn.close()
    return render_template("recycle.html", groups=groups, days=days,
                           cover_pending=session.get("cover_pending"))


@app.route("/parts/recycle/restore", methods=["POST"])
@login_required
@perm_required("recycle")
def recycle_restore():
    """恢复回收站物料；原固定库位若已被其他物料占用 → 置为待定（不占位）"""
    try:
        pid = _int(request.form.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    conn = get_db()
    try:
        p = conn.execute("SELECT * FROM parts WHERE id=? AND deleted=1", (pid,)).fetchone()
        if not p:
            flash(_i18n.t("回收站中不存在该物料", _i18n.current_lang()), "danger")
            return redirect(url_for("recycle_page"))
        note = ""
        # 检查该物料原有的所有库位，只要有任意一个已被其他未删除物料占用，整体置待定
        my_locs = conn.execute("SELECT DISTINCT location_id FROM inventory WHERE part_id=? AND location_id IS NOT NULL", (pid,)).fetchall()
        has_conflict = False
        for loc_row in my_locs:
            loc_id = loc_row[0]
            other = conn.execute(
                """SELECT 1 FROM inventory i
                   JOIN parts p2 ON i.part_id=p2.id AND p2.deleted=0
                   WHERE i.location_id=? AND i.part_id<>? LIMIT 1""",
                (loc_id, pid),
            ).fetchone()
            if other:
                has_conflict = True
                break
        if has_conflict:
            # 原库位已被占用，清空该物料所有库位绑定，整体为待定
            conn.execute("DELETE FROM inventory WHERE part_id=?", (pid,))
            conn.execute("UPDATE parts SET location_id=NULL WHERE id=?", (pid,))
            note = "；原库位已被其他物料占用，恢复后状态为【待定】，请手动重新分配空闲库位"
        conn.execute("UPDATE parts SET deleted=0, deleted_at=NULL WHERE id=?", (pid,))
        conn.commit()
        try:
            _audit("part", pid, p["part_code"], "restore", {"note": "恢复物料" + (("；" + note.replace("；", "")) if note else "")}, conn=conn)
        except Exception:
            pass
        flash(_xl("物料 [{p1}] 已恢复{p2}", p1=p['part_code'], p2=note), "success")
    finally:
        conn.close()
    return redirect(url_for("recycle_page"))


@app.route("/parts/recycle/purge", methods=["POST"])
@login_required
@perm_required("recycle")
def recycle_purge():
    """彻底删除回收站中的单条物料（不可恢复）"""
    try:
        pid = _int(request.form.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    conn = get_db()
    try:
        p = conn.execute("SELECT part_code FROM parts WHERE id=? AND deleted=1", (pid,)).fetchone()
        if not p:
            flash(_i18n.t("回收站中不存在该物料", _i18n.current_lang()), "danger")
            return redirect(url_for("recycle_page"))
        try:
            _audit("part", pid, p["part_code"], "purge", {"note": "彻底删除物料"}, conn=conn)
        except Exception:
            pass
        _purge_part(conn, pid)
        conn.commit()
        flash(_xl("物料 [{p1}] 已彻底删除（不可恢复）", p1=p['part_code']), "warning")
    finally:
        conn.close()
    return redirect(url_for("recycle_page"))


@app.route("/parts/recycle/purge-all", methods=["POST"])
@login_required
@perm_required("recycle")
def recycle_purge_all():
    """清空回收站（所有已删除物料彻底清除）"""
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, part_code FROM parts WHERE deleted=1").fetchall()
        for r in rows:
            try:
                _audit("part", r["id"], r["part_code"], "purge", {"note": "清空回收站（彻底删除）"}, conn=conn)
            except Exception:
                pass
            _purge_part(conn, r["id"])
        conn.commit()
        flash(_i18n.t("回收站已清空（{0} 条已彻底删除）", _i18n.current_lang()).format(len(rows)), "warning")
    finally:
        conn.close()
    return redirect(url_for("recycle_page"))


# ============================================================================
# 盘点管理（stocktake）
#   状态机：draft 草稿 → pending1 待一级审批 → pending2 待二级审批(终审) → done 完成
#           任一审批环节可驳回 → 退回 draft 修改后重提；cancelled 取消
#   审批级别：提交时定级 —— 存在贵重物料差异强制两级；
#            否则按设置 stocktake_approval_level（默认 2 两级，可配 1 仅一级）
#   审批人指派：创建时可指定一级/二级审批人（默认带出本部门主管/经理，可在部门配置中设置）；
#            指派后仅被指派者可审批；未指派则任何有权限者可审批
#   差异调整：终审通过后自动校准库存（写 transactions + audit_log）
# ============================================================================
def _form_opt_int(name):
    """表单可选整数（空/0 → None）"""
    v = request.form.get(name, "").strip()
    try:
        return int(v) or None
    except (TypeError, ValueError):
        return None


def _stk_default_approvers(conn, user):
    """盘点/贵重出库默认审批人：一律取创建人所在部门配置（部门配置中指定的
    一级审批人 supervisor_id、二级审批人 manager_id），不由创建时选择。"""
    try:
        dep = (user["department"] or "").strip()
    except (IndexError, KeyError, TypeError):
        dep = ""
    sup1 = sup2 = None
    if dep:
        drow = conn.execute(
            "SELECT supervisor_id, manager_id FROM departments WHERE name=?", (dep,),
        ).fetchone()
        if drow:
            sup1 = drow["supervisor_id"]
            sup2 = drow["manager_id"]
    return sup1, sup2


def _valuable_approval_level(conn, part_row):
    """贵重出库有效审批级别 = max(系统设置级别, 物料级强制二级)。
    系统=二级则一律二级；系统=一级但物料勾选二级，则走二级；
    系统=不审批时，勾选了「强制二级审批」的物料仍强制二级。"""
    sys_lv = int(_settings.load().get("valuable_approval_level", 1))
    try:
        part_lv = 2 if (part_row and part_row["valuable_lvl"]) else 0
    except (IndexError, KeyError, TypeError):
        part_lv = 0
    return max(sys_lv, part_lv)


# ---------------------------------------------------------------------------
# 出库类型扩展（仓管自行出库）：
#   general 领用 / return 退货 / exchange 换货 / internal 内部转移 / giveback 返还
#   / maintenance 维修 / verify 校验·测试
#   - general：与原业务完全一致（线别/设备、贵重走审批），v1.0.16 只改显示名「领用」
#   - return / internal / giveback：真实出库（扣库存）
#   - exchange / maintenance / verify：进借·换·检·修台账（v1.0.19 起办理时实扣库存，还回后回补；到期预警）
#   - 特殊出库默认一级审批，审批设置里可配 不审批(0)/一级(1)/二级(2)；
#     维修 / 校验·测试沿用「换货出库审批级别」，不新增设置项
#   - giveback 的自由文本字段是「备注」（可空），其余是「…原因」（必填）
#   - maintenance / verify 额外登记五项：出库人（只读当前登录人）、目的公司/地址、
#     接收人、联系方式、预计返回时间
# ---------------------------------------------------------------------------
OUT_KINDS = ("return", "exchange", "internal", "giveback", "maintenance", "verify", "scrap")
# v1.0.39：正常出库新增「其它」——领用/退货/内部转移/返还/报废都套不上的出库
# （样机领出、送检、报废前试拆等）。口径按「领用」处理：免目的地/接收人/联系方式、
# 免线别设备，只留领用人 + 扣减库位；贵重仍按贵重审批档走。
# 刻意不放进 OUT_KINDS：那个元组驱动「三字段必填」与台账字段校验，放进去会把
# 其它出库也变成必须填目的地/接收人。合法值集合见 OUT_KINDS_ALL。
OUT_KINDS_ALL = ("general", "misc") + OUT_KINDS
# 走借·换·检·修台账的子类型：登记 borrows、办理时实扣库存、还回后回补（v1.0.19 起）、到期预警
BORROW_OUT_KINDS = ("exchange", "maintenance", "verify")
# 需要五项登记（含只读出库人）的子类型
TRACK5_OUT_KINDS = ("maintenance", "verify")
# v1.0.18：借·换·检·修里「换/检/修」三个子类型的「出库人」一律不用填，
# 后端默认取当前登录用户。与 TRACK5 分开：TRACK5 还决定台账 borrower 的取值口径，
# 换货历史上 borrower = 申请人姓名，不能顺带改掉。
AUTO_OUT_PERSON_KINDS = BORROW_OUT_KINDS  # ("exchange", "maintenance", "verify")
# 业务字段里那段自由文本的名称：特殊出库是「原因」（必填），返还是「备注」（选填）
_OUT_KIND_REASONS = {"return": "退货原因", "exchange": "换货原因",
                     "internal": "转移原因", "giveback": "备注",
                     "maintenance": "维修原因", "verify": "校验/测试原因",
                     "scrap": "报废原因"}
_OUT_KIND_REASON_OPTIONAL = ("giveback",)
# 类型短名（用于提示语/单据备注，如「返还出库」）；不要再从 *_REASONS 里切「原因」二字
_OUT_KIND_NAMES = {"return": "退货", "exchange": "换货",
                   "internal": "内部转移", "giveback": "返还",
                   "maintenance": "维修", "verify": "校验/测试",
                   "scrap": "报废", "misc": "其它"}
_OUT_KIND_LEVEL_KEY = {
    "return": "return_approval_level",
    "exchange": "exchange_approval_level",
    "internal": "internal_approval_level",
    "giveback": "giveback_approval_level",
    "maintenance": "exchange_approval_level",
    "verify": "exchange_approval_level",
    "scrap": "scrap_approval_level",
}

# ---------------------------------------------------------------------------
# 借·换·检·修（v1.0.16）：将原「借用」类型升级为统一台账类型，下含四个子类型：
#   borrow 借(借用) / exchange 换(换货) / verify 检(校验·测试) / maintenance 修(维修)
# - 借(借用)：沿用原借用（借用人 + 预计归还），免审批，登记台账 kind=borrow
# - 换(换货)：从「出库」子类型移入此处；底层仍走 out 特殊类型（审批 + 台账），kind=exchange
# - 检(校验·测试) / 修(维修)：新增；底层走 out 特殊类型，kind=verify/maintenance
#   检/修 多出五项（出库人只读当前登录人、目的公司/地址、接收人、联系方式、预计返回时间），
#   修 额外有「维修品类型」（来自系统设置配件分类）
# 借·换·检·修台账 → 改名为 借·换·检·修台账；四种 kind 全展示、可筛选；出库/还回逻辑不变。
# ---------------------------------------------------------------------------
BORROW_SUBTYPES = ("borrow", "exchange", "verify", "maintenance")
_BORROW_SUB_NAMES = {"borrow": "借(借用)", "exchange": "换(换货)",
                     "verify": "检(校验/测试)", "maintenance": "修(维修)"}
_BORROW_SUB_SHORT = {"borrow": "借", "exchange": "换", "verify": "检", "maintenance": "修"}
BORROW_TYPE_NAME = "借·换·检·修"
# 全部进台账的 four 种 kind（借/换/检/修）
LEDGER_KINDS = ("borrow", "exchange", "verify", "maintenance")




def _norm_borrow_sub(raw):
    s = str(raw or "").strip().lower()
    return s if s in BORROW_SUBTYPES else "borrow"


def _track_out_person(user, ot):
    """借·换·检·修的「换/检/修」：出库人一律取当前登录人。

    v1.0.18 起前端不再有出库人录入框（只读框也去掉），服务端是唯一来源；
    换货也纳入，之前换货的 out_person 留空，台账/导出里显得像没人办理。
    """
    if ot not in AUTO_OUT_PERSON_KINDS:
        return "", None
    try:
        name = user["display_name"] or user["username"] or ""
    except (IndexError, KeyError, TypeError):
        name = ""
    return str(name), user["id"]


def _borrow_out_note(ot, dest, receiver, contact, reason, out_person=""):
    """借·换·检·修台账备注：换货沿用旧句式；维修 / 校验·测试额外带出库人与目的公司/地址。"""
    if ot == "exchange":
        head = _xl("换货出库，目的地：{p1}，接收人：{p2}，联系方式：{p3}",
                   p1=dest, p2=receiver, p3=contact)
    else:
        head = (_OUT_KIND_NAMES[ot] + "出库，出库人：" + (out_person or "") +
                "，目的公司/地址：" + (dest or "") + "，接收人：" + (receiver or "") +
                "，联系方式：" + (contact or ""))
    return (head + _i18n.mk("；") + (reason or "")).strip(_i18n.mk("；"))


# ---------------------------------------------------------------------------
# 入库子类型（v1.0.13，v1.0.28 显示名订正）：采购 / 返修 / 转入 / 借入，默认采购。
# key 仍叫 other（老库零迁移，历史流水不会错位），只是中文/译文显示名改为「借入」。
# 与出库子类型共用 applications.out_type / transactions.out_type 这一列（列语义
# 已泛化为「子类型」）：出库存 general/return/...，入库存 purchase/repair/...，
# 按 tx_type / app_type 区分值域 —— 老库零迁移，无需 ALTER TABLE。
# 历史入库流水该列为空一律按「采购」显示/筛选。入库子类型只做业务分类，
# 不改变加库存的账务口径，也不引入审批。
# ---------------------------------------------------------------------------
# v1.0.37：新增「其它」——采购/返修/转入/借入覆盖不到的入库来源（盘盈、样机退回等），
# 只做业务分类，账务口径与其它入库一致（同样加库存、同样记流水）。
# v1.0.39：新增「产线退回」——产线上用剩/拆下来的件退回仓库，与「其它」分开统计。
IN_KINDS = ("purchase", "repair", "transfer_in", "other", "line_return", "misc")
IN_KIND_DEFAULT = "purchase"
_IN_KIND_NAMES = {"purchase": "采购", "repair": "返修",
                  "transfer_in": "转入", "other": "借入",
                  "line_return": "产线退回", "misc": "其它"}


# 出入库流水的「方向 → 子类型」归属表。
#   入库：采购/返修/转入/借入；出库：领用/退货/换货/内部转移/返还/维修/校验·测试；调库：不细分。
# 以前模板把出库那五个写死在第二个下拉里，跟方向完全无关，于是
# 「方向=入库、子类型=退货」能同时成立；服务端又以子类型优先，筛出来
# 全是出库流水 —— 用户看到的就是「入库把出库的类型带出来了」。
TX_SUBTYPES = {
    "in": IN_KINDS,
    # 出库子类型（v1.0.16）：换货已移入「借·换·检·修」，维修/校验·测试本就是借·换·检·修
    # 子类型，三者都不再作为「出库」方向的可筛子类型；出库只保留 领用/退货/内部转移/返还。
    # 它们仍是合法的 out 交易类型（落在 OUT_KINDS，供事务校验与按原始参数反查），
    # 只是不进这个用户可见的下拉。
    "out": ("general", "misc", "return", "internal", "giveback", "scrap"),
    "transfer": (),
}
# 子类型的中文原文（模板里 tr() 按原文取词，别在这里写译文）
_SUB_KIND_LABELS = {"general": "领用", "return": "退货", "exchange": "换货",
                    "internal": "内部转移", "giveback": "返还",
                    "maintenance": "维修", "verify": "校验/测试", "scrap": "报废",
                    "purchase": "采购", "repair": "返修",
                    "transfer_in": "转入", "other": "借入",
                    "line_return": "产线退回", "misc": "其它"}


# ---------------------------------------------------------------------------
# v1.0.22：配件好坏状态 + 货架属性
# ---------------------------------------------------------------------------
# 配件好坏三态：良品 / 不良品 / 维修品（inventory.condition）
CONDITIONS = ("good", "defective", "repair")
CONDITION_NAMES = {"good": "良品", "defective": "不良品", "repair": "维修品"}
# 货架属性三态：良品 / 待维修 / 待报废（locations.attribute）
# v1.0.23：中文统一为「待维修」「维修品」，与「出维修 / 维修回库」业务叫法一致；内部 key 不变。
LOC_ATTRS = ("good", "pending_repair", "pending_scrap")
LOC_ATTR_NAMES = {"good": "良品", "pending_repair": "待维修", "pending_scrap": "待报废"}
# 库位属性着色：良品绿 / 待维修黄 / 待报废红（前端 CSS 变量名，禁止写死色值）
LOC_ATTR_SKIN = {"good": "good", "pending_repair": "pending_repair",
                 "pending_scrap": "pending_scrap"}

# 约束矩阵：库位属性 → 允许存放的 condition 集合
#   良品区：良品 + 维修品
#   待维修区：不良品
#   待报废区：不良品
# v1.0.37（用户口径）：维修品是「修好可再用」的好件，归属良品范畴，
#   只能入良品库位；待维修 / 待报废是坏件区，维修品一律不进、也不在候选里出现。
_LOC_ACCEPT = {
    "good": ("good", "repair"),
    "pending_repair": ("defective",),
    "pending_scrap": ("defective",),
}


def _row_get(row, key, default=None):
    """sqlite3.Row 没有 .get()，统一安全取值（兼容缺列的老记录）。"""
    if row is None:
        return default
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _cond_attr_allowed(condition, attribute, is_pending=False):
    """condition 能否存入属性为 attribute 的库位（含空属性按良品处理）。

    v1.0.27：is_pending=True 表示目标是「待定」字面占位（location_id NULL，
    不是真实库位、没有 attribute），三种状态都允许暂放；不良品此前只能进
    待维修/待报废真实库位，没有空位时无法入库，本次按需求放行到待定。
    """
    if is_pending:
        return condition in CONDITIONS
    attribute = attribute or "good"
    return condition in _LOC_ACCEPT.get(attribute, ("good", "repair"))


def _loc_attr_of(conn, loc_id):
    """库位属性；无库位（待定/老数据）按良品处理，与 _cond_attr_allowed 同源。"""
    if not loc_id:
        return "good"
    return _row_get(db.get_location(conn, loc_id), "attribute") or "good"


def _is_scrap_to_repair(conn, src_loc_id, to_loc_id):
    """待报废区 → 待维修区：报废件复判可修，转待维修排队送修。
    注意判的是「库位属性」而非配件状态——两处都放 defective，普通状态矩阵放行。
    """
    if not src_loc_id or not to_loc_id:
        return False
    return (_loc_attr_of(conn, src_loc_id) == "pending_scrap"
            and _loc_attr_of(conn, to_loc_id) == "pending_repair")


def _scrap_reassess_err(conn, user, src_loc_id, to_loc_id, reason, lang=None, check_perm=True):
    """待报废→待维修门禁：复判原因必填 + 复判审批权限。
    返回错误文案或 None；check_perm=False 用于申请提交（申请人不要求仓库审批权限，
    执行时再卡）。三条执行链共用，不能只挡前端。
    """
    if not _is_scrap_to_repair(conn, src_loc_id, to_loc_id):
        return None
    lang = lang or _i18n.current_lang()
    if not str(reason or "").strip():
        return _i18n.t("待报废转待维修必须填写复判原因", lang)
    if check_perm and not (user and can(user["role"], "stock_inout", "reassess")):
        return _i18n.t("待报废转待维修需要复判审批权限", lang)
    return None


def _scrap_reassess_mark(reason, lang=None):
    """流水固定标记 + 复判原因，便于事后审计「谁把报废件转去修」。"""
    mark = _i18n.t("待报废转待维修（复判可修）", lang or _i18n.current_lang())
    r = str(reason or "").strip()
    return mark + (_xl("：{p1}", p1=r) if r else "")


def _transfer_move_inv_row(conn, part_id, src, to_loc_id, qty, lang=None):
    """v1.0.70 批次行级调库核心：只搬指定的 inventory 行。

    旧版按库位找「第一行」扣减，同库位多批次时选了批次 2 实际搬走批次 B-LEGACY，
    且目标行只写 part/location/quantity，批次、状态、维修件数、入库日期全丢。
    这里改成：
      · 原子扣减 `UPDATE ... WHERE id=? AND quantity>=?`（并发安全，不够就报错）；
      · 目标按「物料 + 库位 + 批次 + 状态」找同款行合并，没有就新插；
      · 新行原样继承批次号、状态、维修件数、入库日期（v1.0.24 调库继承状态基线）。
    返回 (from_txt, to_txt, batch_no, err)；err 非空表示没有动库。
    """
    lang = lang or _i18n.current_lang()
    src_id = int(src["id"])
    qty = float(qty or 0)
    cond = str(_row_get(src, "condition") or "good")
    if cond not in CONDITIONS:
        cond = "good"
    batch = str(_row_get(src, "batch_no") or "")
    carry = min(float(_row_get(src, "repair_qty") or 0), qty)
    if carry < 0:
        carry = 0
    in_date = str(_row_get(src, "in_date") or "")
    from_txt = _row_get(src, "location") or "待定"
    if to_loc_id:
        to_loc = db.get_location(conn, to_loc_id)
        to_attr = _row_get(to_loc, "attribute") or "good"
        if not _cond_attr_allowed(cond, to_attr):
            return None, None, None, _xl(
                "该库位属性 [{p1}] 不允许存放 [{p2}]",
                p1=_i18n.t(LOC_ATTR_NAMES.get(to_attr, "良品"), lang),
                p2=_i18n.t(CONDITION_NAMES.get(cond, "良品"), lang))
        to_txt = db.location_text(to_loc) if to_loc else ""
    else:
        to_txt = "待定"
    # 原子扣减：只动这一行；数量不足时不产生任何写入
    cur = conn.execute(
        "UPDATE inventory SET quantity=quantity-? WHERE id=? AND quantity>=?",
        (qty, src_id, qty))
    if cur.rowcount != 1:
        return None, None, None, _xl(
            "该批次只有 {p1} 件，不足调出 {p2}", p1=_row_get(src, "quantity"), p2=qty)
    rem = conn.execute("SELECT quantity FROM inventory WHERE id=?", (src_id,)).fetchone()
    if not rem or float(rem["quantity"] or 0) < 1e-9:
        conn.execute("DELETE FROM inventory WHERE id=?", (src_id,))
    # 目标同款行：批次 + 状态都要一致，绝不能把不良品并进良品行
    if to_loc_id:
        tgt = conn.execute(
            """SELECT id FROM inventory WHERE part_id=? AND location_id=?
               AND COALESCE(batch_no,'')=? AND COALESCE(condition,'good')=?
               ORDER BY id LIMIT 1""",
            (part_id, to_loc_id, batch, cond)).fetchone()
    else:
        tgt = conn.execute(
            """SELECT id FROM inventory WHERE part_id=? AND location_id IS NULL
               AND COALESCE(batch_no,'')=? AND COALESCE(condition,'good')=?
               ORDER BY id LIMIT 1""",
            (part_id, batch, cond)).fetchone()
    if tgt:
        conn.execute(
            "UPDATE inventory SET quantity=quantity+?, "
            "repair_qty=MIN(quantity+?, IFNULL(repair_qty,0)+?) WHERE id=?",
            (qty, qty, carry, tgt["id"]))
    else:
        conn.execute(
            """INSERT INTO inventory(part_id, location_id, location, quantity,
               repair_qty, batch_no, condition, in_date) VALUES(?,?,?,?,?,?,?,?)""",
            (part_id, to_loc_id, to_txt, qty, carry, batch, cond, in_date))
    return from_txt, to_txt, batch, None


def _parse_inv_source(conn, part_id, from_key):
    """解析调库源：优先 inv_<库存行id>（批次行级），兼容老 loc_<库位id>/loc_NULL。

    返回 (src_row, is_batch)；src_row 为 sqlite3.Row 或 None。
    """
    key = str(from_key or "").strip()
    lines = _part_stock_lines(conn, part_id)
    if key.startswith("inv_"):
        try:
            iid = int(key[4:])
        except (TypeError, ValueError):
            iid = 0
        return next((l for l in lines if int(l["inv_id"]) == iid), None), True
    if key.startswith("loc_"):
        raw = key[4:]
        if raw == "NULL":
            return next((l for l in lines if l["location_id"] is None), None), False
        try:
            fid = int(raw)
        except (TypeError, ValueError):
            fid = None
        return next((l for l in lines if l["location_id"] == fid), None), False
    return None, False


def _parse_transfer_source(conn, part_id, from_key):
    """v1.0.78：调库源统一解析。

    返回 (rows, mode, pseudo)：
      · rows  = 该源下的库存行（整库位可能多行）
      · mode  = whole(整库位 locall_<库位id>) / batch(单批 inv_<行id>) / loc(老库位 loc_<库位id>)
      · pseudo= 统一的行字典，quantity 为合计、condition 为合并口径，供校验/展示复用
    老草稿的 loc_<id> 仍只取一行，行为完全不变。
    """
    key = str(from_key or "").strip()
    lines = _part_stock_lines(conn, part_id)
    if key.startswith("locall_"):
        raw = key[7:]
        rows = [l for l in lines
                if ("NULL" if l["location_id"] is None else str(l["location_id"])) == raw]
        if not rows:
            return [], "whole", None
        total = sum(float(_row_get(r, "quantity") or 0) for r in rows)
        conds = {str(_row_get(r, "condition") or "good") for r in rows}
        pseudo = dict(rows[0])
        pseudo.update({"quantity": total, "condition": "defective" if "defective" in conds
                       else next(iter(conds)), "inv_id": None, "batch_no": ""})
        return rows, "whole", pseudo
    src, is_batch = _parse_inv_source(conn, part_id, key)
    if src is None:
        return [], ("batch" if key.startswith("inv_") else "loc"), None
    return [src], ("batch" if is_batch else "loc"), src


def _transfer_move_whole_location(conn, part_id, rows, to_loc_id, qty, lang=None):
    """v1.0.78：整库位调库——把该库位所有库存行搬到目标，逐行保留批次/状态/维修数/入库日。

    qty 小于合计时按入库日从早到晚搬（与出库 FIFO 同口径）；任一行属性不合法时整体报错。
    返回 (from_txt, to_txt, batch_text, err)。
    """
    lang = lang or _i18n.current_lang()
    rows = list(rows or [])
    if not rows:
        return None, None, None, _i18n.t("源库位无库存，无法调库", lang)
    to_loc = db.get_location(conn, to_loc_id) if to_loc_id else None
    for r in rows:
        cond = str(_row_get(r, "condition") or "good")
        if cond not in CONDITIONS:
            cond = "good"
        if to_loc_id and not _cond_attr_allowed(cond, _row_get(to_loc, "attribute") or "good"):
            return None, None, None, _xl(
                "该库位属性 [{p1}] 不允许存放 [{p2}]",
                p1=_i18n.t(LOC_ATTR_NAMES.get(_row_get(to_loc, "attribute") or "good", "良品"), lang),
                p2=_i18n.t(CONDITION_NAMES.get(cond, "良品"), lang))
    remain = float(qty or 0)
    from_txt = _row_get(rows[0], "location") or "待定"
    to_txt = db.location_text(to_loc) if to_loc else "待定"
    batches, done = [], []
    ordered = sorted(rows, key=lambda r: (str(_row_get(r, "in_date") or ""),
                                          int(_row_get(r, "id") or 0)))
    for r in ordered:
        if remain <= 1e-9:
            break
        take = min(float(_row_get(r, "quantity") or 0), remain)
        if take <= 1e-9:
            continue
        _frm, to_txt, bat, terr = _transfer_move_inv_row(conn, part_id, r, to_loc_id, take, lang=lang)
        if terr:
            return None, None, None, terr
        if bat:
            batches.append(str(bat))
        done.append(take)
        remain -= take
    if remain > 1e-9:
        return None, None, None, _xl("整库位可调数量不足，还差 {p1}", p1=remain, lang=lang)
    return from_txt, to_txt, "、".join(batches), None


def _is_pending_loc(loc_key, loc_text):
    """是否「待定」字面占位：没有真实库位 id，location 文本为待定类。"""
    if isinstance(loc_key, int):
        return False
    return str(loc_text or "").strip() in ("待定", "无", "-", "—", "PENDING", "TBD")


def _loc_out_allowed(attribute, out_type):
    """某库位的库存允许以哪种出库类型出库（按库位属性限制）——全系统唯一口径。

    - 待维修区：仅「维修 maintenance」可出库（领用/退货/报废/换货/借用/校验一律禁止）
    - 待报废区：「报废 scrap」或「维修 maintenance」可出库
    - 良品区：除「维修 maintenance」「报废 scrap」外全部出库类型均可
      v1.0.31：维修严格只针对不良品——良品不送修，维修只能从待维修/待报废库位出。
      旧口径「良品也可送修」已按用户本次反馈收紧（前端维修台账只列待维修/待报废库位）。
      v1.0.32：良品不能报废——良品区库存禁止 scrap 出库；报废必须来自待报废库位，
      与 _out_cond_error 的 has_pending_scrap 口径一致。
    attribute 为空（旧库位）按良品处理，不阻断历史数据。

    v1.0.24 界面口径：出库主流程的类型子项按属性置灰——待报废区只剩「报废」
    可选、待维修区全部置灰（根本不能走「出库」）；维修一律走「借·换·检·修」
    →「修(维修)」，也就是 out_type=maintenance 只由借换检修台账链产生。
    绕过前端直接 POST 同样在 _parse_exec_location / _stock_out_auto 被拦住。
    """
    attribute = attribute or "good"
    if attribute == "pending_repair":
        return out_type == "maintenance"
    if attribute == "pending_scrap":
        return out_type in ("scrap", "maintenance")
    # v1.0.31：良品区不再允许维修出库（良品不送修）
    # v1.0.32：良品区也不允许报废——报废只针对待报废库位里的不良品
    if out_type in ("maintenance", "scrap"):
        return False
    return True


_ALL_LOC_ATTRS = ("good", "pending_repair", "pending_scrap")


def _loc_attrs_allowed(out_type):
    """反查：某种出库类型允许从哪些属性的库位扣库存（与 _loc_out_allowed 同源）。

    供 _stock_out_auto 跨库位自动补足时过滤。以前只看 i.quantity>0，
    良品区不够时会继续从待维修/待报废区扣不良品——领用 5 件里有 3 件是坏件，
    属于静默业务事故。v1.0.24 起自动补足也严格遵守库位属性。
    """
    return [a for a in _ALL_LOC_ATTRS if _loc_out_allowed(a, out_type)]


# ---------------------------------------------------------------------------
# v1.0.28：只有良品与维修品可以走正常出库（规格表业务规则）。
#   不良品（inventory.condition='defective'）只允许走「报废 scrap」和
#   「修 maintenance」两条专门链，且仍受 _loc_out_allowed 的库位属性约束。
# 这是服务端唯一真源：_stock_out_auto 的「总库存口径」与「候选行口径」都读它，
# 覆盖出库主流程、出库调库申请、批量出入库、借·换·检·修四条链（它们都经
# _stock_out_auto 扣减）。以前只看库位属性，v1.0.27 新增的「待定(不良品)」行
# location_id=NULL 被折算成 good 属性，普通领用会把不良品静默扣走。
# ---------------------------------------------------------------------------
_NORMAL_OUT_CONDS = ("good", "repair")
_BAD_OK_OUT_TYPES = ("scrap", "maintenance")


def _out_cond_allow(out_type):
    """本次出库允许扣减的配件状态；None 表示不限制（仅兼容 out_type 缺失的老调用）。

    报废 / 维修不限制状态（由库位属性与 _out_cond_error 兜底），其余一律只扣
    良品 + 维修品，包括四类普通出库与借·换·检·修的借 / 换 / 检。
    """
    if out_type is None:
        return None
    if str(out_type) in _BAD_OK_OUT_TYPES:
        return None
    return _NORMAL_OUT_CONDS


def _out_cond_where(conds):
    """把状态白名单拼成可移植的 SQL 片段（COALESCE 兼容老库缺列/空值）。"""
    if not conds:
        return ""
    return " AND COALESCE(i.condition,'good') IN (" + ",".join("?" for _ in conds) + ")"


def _borrow_in_batches(conn, part_id):
    """v1.0.28：本物料「借入」子类型登记过的批次号集合（去重）。

    入库子类型 other 在 v1.0.28 起显示名为「借入」，历史流水 out_type='other'
    就是借入；out_type 为空按采购理解，不会被误收。用于「返还」子模式的
    批次优先提示与扣减顺序，SQL 只用标准 DISTINCT / IS NOT NULL。
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT batch_no FROM transactions "
            "WHERE part_id=? AND tx_type='in' AND out_type='other' "
            "AND batch_no IS NOT NULL AND batch_no<>''",
            (part_id,)).fetchall()
        return [str(r[0]) for r in rows if r[0]]
    except Exception:
        return []


def _giveback_hint_rows(conn, part_id):
    """v1.0.28：「返还」子模式的可返还批次 / 库位清单（老回归兼容入口）。

    每行 {batch_no, location, location_id, quantity, borrowed, repair}。
    v1.0.77 曾作为零引用死代码删除，但 check_v1028 用它核对返还批次口径；
    业务路径不调用，仅保留函数供回归断言。
    """
    batches = set(_borrow_in_batches(conn, part_id))
    try:
        rows = conn.execute(
            """SELECT COALESCE(i.batch_no,'') AS batch_no,
                      i.location AS location, i.location_id AS location_id,
                      i.quantity AS quantity,
                      CASE WHEN IFNULL(i.repair_qty,0)>0
                                OR IFNULL(i.condition,'')='repair'
                           THEN 1 ELSE 0 END AS repair,
                      COALESCE(i.in_date,'') AS in_date, i.id AS inv_id
               FROM inventory i
               LEFT JOIN locations l ON l.id=i.location_id
               WHERE i.part_id=? AND i.quantity>0
                 AND COALESCE(i.condition,'good') IN ('good','repair')
                 AND CASE WHEN l.attribute IN ('pending_repair','pending_scrap')
                          THEN l.attribute ELSE 'good' END='good'
               ORDER BY i.in_date, i.id""",
            (part_id,)).fetchall()
    except Exception:
        return []
    return [{"batch_no": str(r["batch_no"] or ""),
             "location": str(r["location"] or ""),
             "location_id": r["location_id"],
             "quantity": float(r["quantity"] or 0),
             "borrowed": str(r["batch_no"] or "") in batches,
             "repair": bool(r["repair"])} for r in rows]


def _inout_sub_options(f_type):
    """当前方向可选的子类型；调库返回空列表 → 模板只渲染「全部类型」。"""
    return [{"value": v, "label": _SUB_KIND_LABELS[v]}
            for v in TX_SUBTYPES.get(str(f_type or "").strip(), ())]


def _inout_filter(where, args, f_type, f_sub):
    """把「方向 + 子类型」解析成 WHERE 片段，返回规整后的 (f_type, f_sub)。

    两条规则：
      1. 子类型只属于它自己的方向 —— 方向不匹配就丢掉子类型，绝不跨方向生效
      2. 只给子类型不给方向（老链接/bookmark）时按出库理解
    列表页和导出共用这一份，避免「页面筛得对、导出还是全量」。
    """
    f_type = (f_type or "").strip()
    f_sub = (f_sub or "").strip()
    if f_sub and not f_type:
        # 老链接/bookmark 只给子类型：按值域反推方向，别一律当成出库
        f_type = "in" if f_sub in IN_KINDS else "out"
    if f_sub and f_sub not in TX_SUBTYPES.get(f_type, ()):
        f_sub = ""
    if f_sub:
        where.append("t.tx_type=?")
        args.append(f_type)
        if f_type == "in":
            # 历史入库流水 out_type 为空 = 采购（默认值），要和显式 purchase 一起捞出
            if f_sub == IN_KIND_DEFAULT:
                where.append("IFNULL(t.out_type,'') IN ('',?)")
                args.append(IN_KIND_DEFAULT)
            else:
                where.append("t.out_type=?")
                args.append(f_sub)
        elif f_sub == "general":
            where.append("IFNULL(t.out_type,'') IN ('','general')")
        else:
            where.append("t.out_type=?")
            args.append(f_sub)
    elif f_type in ("in", "out", "transfer", "borrow"):
        where.append("t.tx_type=?")
        args.append(f_type)
    return f_type, f_sub


def _norm_out_type(raw):
    """出库类型归一化：空/非法值一律视为 general（领用），历史数据零迁移。

    注意：这是「读路径 / 历史数据」的宽松归一化。写入路径必须用 _strict_out_type，
    否则前端隐藏的非法 out_type 会被静默改成「领用」，绕过物料级状态规则。
    """
    ot = str(raw or "").strip().lower()
    return ot if ot in OUT_KINDS_ALL else "general"


def _strict_out_type(raw):
    """v1.0.27 写入路径：空值仍按 general（历史零迁移），非法值返回 None 由调用方拒绝。"""
    ot = str(raw or "").strip().lower()
    if not ot:
        return "general"
    return ot if ot in OUT_KINDS_ALL else None


# v1.0.31：正常出库（含申请单出库、批量出库、直接出库）不再接受 maintenance。
# 返修/维修(maintenance) 只能走「借·换·检·修」→「修(维修)」台账链，
# 从正常出库移除——前端 cond_rules.js 已不含此项，这里防止直接 POST 绕过。
# 注意：exchange / verify 等历史特殊出库子类型仍合法（旧脚本与台账依赖），
# 这里只黑名单 maintenance，不用白名单误伤其它类型。
_NORMAL_OUT_BLOCKED = ("maintenance",)


def _normal_out_type_ok(ot):
    """正常出库子类型是否合法（v1.0.31 起仅排除 maintenance）。"""
    return str(ot or "general") not in _NORMAL_OUT_BLOCKED


def _out_cond_error(conn, pid, out_type, loc_attr, lang):
    """v1.0.27 出库主流程：物料级状态规则的服务端同源校验。

    与前端 cond_summary 共用 db.cond_summary（同一真源），不信任客户端传来的判定。
    边界（刻意保留，避免破坏 v1.0.24 既有业务与 18 例回归矩阵）：
    - 只约束出库主流程可见子项；借·换·检·修台账链里的 maintenance 是既有送修入口，
      良品也可送修，不在此处加 has_defective 限制。
    - 库位属性约束仍由 _loc_out_allowed / _parse_exec_location 负责，本函数不重复改写。
    - 纯良品物料的报废沿用旧行为，不受「含不良品」规则影响。
    """
    s = db.cond_summary(conn, pid)
    # v1.0.28 规格表「只有良品与维修品可以走正常出库」：全不良物料四类普通
    # 出库一律拒绝（v1.0.27 只禁领用/内部转移，退货/返还会被 _stock_out_auto
    # 的白名单挡成「库存不足」，这里给出更明确的业务原因）。
    # v1.0.39：出库「其它」与「领用」同档——全不良物料同样不能走
    if s["all_defective"] and out_type in ("general", "internal", "misc"):
        return _i18n.t("该物料全部为不良品，不能办理领用或内部转移", lang)
    if s["all_defective"] and out_type in ("return", "giveback"):
        return _i18n.t("该物料全部为不良品，不能办理退货或返还", lang)
    if out_type == "scrap":
        # v1.0.32：良品不能报废——报废只针对待报废库位里的不良品，
        # 不再像旧版那样「纯良品物料可报废」放行（此前仅 has_defective 时才校验）。
        # 选中了扣减库位就按该库位属性判定；申请页/自动扣减没有库位时，
        # 退回到「该物料是否存在待报废库位库存」这一同源口径。
        if loc_attr is not None:
            if loc_attr != "pending_scrap":
                return _i18n.t("含不良品的物料只能从「待报废」库位报废", lang)
        elif not s["has_pending_scrap"]:
            return _i18n.t("含不良品的物料只能从「待报废」库位报废", lang)
    return None


def _out_loc_attr(conn, raw):
    """从出库行解析「实际扣减库位」的属性，解析不到返回 None（由 cond_summary 兜底）。

    兼容三种历史写法：纯数字 location_id、批量页的 loc_<id>，以及申请页不带库位。
    """
    s = str(raw or "").strip()
    if not s or s in ("__NEW__", "loc_NULL", "0"):
        return None
    if s.startswith("loc_"):
        s = s[4:]
    try:
        lid = int(s)
    except (TypeError, ValueError):
        return None
    if not lid:
        return None
    loc = db.get_location(conn, lid)
    return (_row_get(loc, "attribute") or "good") if loc else None


def _norm_in_type(raw):
    """入库类型归一化：空/非法值一律视为 purchase（采购），历史数据零迁移"""
    it = str(raw or "").strip().lower()
    return it if it in IN_KINDS else IN_KIND_DEFAULT


def _norm_sub_type(tx_type, raw):
    """按方向归一化子类型：in→入库四项、out→出库七项、borrow→借·换·检·修四项、其余不带。"""
    if tx_type == "in":
        return _norm_in_type(raw)
    if tx_type == "out":
        return _norm_out_type(raw)
    if tx_type == "borrow":
        return _norm_borrow_sub(raw)
    return ""


def _out_kind_level(conn, ot, part_row=None):
    """新出库类型的有效审批级别（0 不审批 / 1 一级 / 2 二级）。
    贵重物料勾选「强制二级审批」时仍强制二级（与新类型自身的档位取大）。"""
    key = _OUT_KIND_LEVEL_KEY.get(ot or "")
    try:
        lv = int(_settings.load().get(key, 1))
    except (TypeError, ValueError):
        lv = 1
    if lv not in (0, 1, 2):
        lv = 1
    try:
        if part_row and part_row["is_valuable"] and part_row["valuable_lvl"]:
            lv = max(lv, 2)
    except (IndexError, KeyError, TypeError):
        pass
    return lv


def _scrap_approval_mode():
    """v1.0.47：报废审批两模式。配置值 1=系统审批 / 2=系统+纸档审批。
    返回内部 doc_mode（2=系统审批自动扣库 / 3=纸档+附件闸门自动扣库），
    与现有审批闸门、自动扣库的条件保持一致，不用改那两处判断。"""
    try:
        v = int(_settings.load().get("scrap_approval_mode", 1))
    except (TypeError, ValueError):
        v = 1
    return 3 if v == 2 else 2


def _stk_approver_candidates(conn, perm):
    """有指定审批权限的候选用户（指派审批人下拉用）"""
    rows = conn.execute(
        """SELECT id, display_name, username, role, department FROM users
           WHERE active=1 ORDER BY department, display_name"""
    ).fetchall()
    out = []
    for r in rows:
        if can(r["role"], "stocktake", perm):
            out.append(dict(r))
    return out


def _stk_visible_cond(user):
    """盘点单可见性 SQL 条件（配合 LEFT JOIN users u ON u.id=s.created_by_id）：

    - 系统管理员全可见；
    - **已完成的盘点单全员可见**（status='done'）—— 盘点是库存凭证，仓管要按它调账、
      部门管理要按它核差异，锁在「创建人+同部门」里会出现「ADMIN 盘完了别人查不到」；
    - 未完成的单据仍只看 本人 / 同部门 / 指派给自己审批的，避免半成品被围观。
    """
    if user.get("role") == "admin":
        return "", []
    dep = (user.get("department") or "").strip()
    conds, args = ["s.status='done'", "s.created_by_id=?"], [user["id"]]
    if dep:
        conds.append("u.department=?")
        args.append(dep)
    conds.append("s.approver1_id=?")
    conds.append("s.approver2_id=?")
    args += [user["id"], user["id"]]
    return "(" + " OR ".join(conds) + ")", args


def _stk_can_view(user, st):
    """盘点单可见性（Python 侧，st 需带 created_dep）

    注意：st 可能是 sqlite3.Row（没有 .get()）。以前这里写成 st.get("status")，
    结果只有管理员能打开报告的「完成」分支，其他人（仓管/经理）一进来就
    AttributeError → 500，看起来像「别人打开都报错」。
    """
    def g(k):
        try:
            return st[k]
        except (IndexError, KeyError, TypeError):
            return None

    if user.get("role") == "admin":
        return True
    if (g("status") or "") == "done":
        return True
    if g("created_by_id") == user["id"]:
        return True
    dep = (user.get("department") or "").strip()
    if dep and g("created_dep") and g("created_dep") == dep:
        return True
    if g("approver1_id") == user["id"] or g("approver2_id") == user["id"]:
        return True
    return False
def _stk_cycle_window(conn):
    """当前盘点期：应盘日、容差窗口、周期天数。

    期次 = 锚点 + k×周期，按「离应盘日最近」归期；窗口 = 应盘日 ±(周期×容差)。
    """
    days, tol, anchor = _stk_cycle_cfg(conn)
    today = datetime.now().date()
    # v1.0.54：与看板同用「就近归期」，否则建单存的 cycle_key 跟柱子对不上
    k = _stk_period_k(today, anchor, days)
    if k is None or k < 0:
        k = 0
    base = anchor + timedelta(days=k * days)
    td = int(round(days * tol))
    return {"cycle_key": base.strftime("%Y-%m-%d"),
            "base": base,
            "from": (base - timedelta(days=td)).strftime("%Y-%m-%d"),
            "to": (base + timedelta(days=td)).strftime("%Y-%m-%d"),
            "tol_days": td, "days": days, "tolerance": tol}


def _stk_counted_clause(conn, cycle_from=None, alias="i"):
    """「这一行本期已经盘过」的排除子句 + 参数。

    三条来源都要排除，否则会重复盘点：
      1) 进行中的单（draft/pending1/pending2/applying）——防止同一批料开出两张并行单
      2) 本期及以后已完成的单——防止上期盘过、本期又盘一次（防重盘的核心）
      3) 匹配键按 inv_id 精确命中；只有老单据没有 inv_id 时才回退库位文本。
         v1.0.23 起同一库位会按配件状态分成多行，若文本回退无条件生效，
         盘过一行就会把同库位其它状态行一起判成「已盘」，覆盖率虚高且漏盘。
    """
    args = []
    sql = """ NOT EXISTS (
                SELECT 1 FROM stocktake_items si
                JOIN stocktakes s ON s.id = si.stocktake_id
                WHERE si.part_id = {a}.part_id
                  AND (si.inv_id = {a}.id
                       OR (si.inv_id IS NULL
                           AND IFNULL(si.location,'') = IFNULL({a}.location,'')))
                  AND ( s.status IN ('draft','pending1','pending2','applying')
                        OR ( s.status = 'done' AND substr(COALESCE(NULLIF(s.done_at,''), s.created_at),1,10) >= ? )
                  )
              )""".format(a=alias)
    args.append(cycle_from or "0000-00-00")
    return sql, args


def _stk_prior_map(conn):
    """v1.0.21：本期内「已由别的单据盘过」的行 → 键 → {no, at, qty}。

    只认真正盘过的行（actual_qty 有值）且单据状态已进流程；同一行被盘过多次时取最后一次。
    建单时把结果快照到 stocktake_items.prior_* 上，之后别人再盘别的单不会让这张单中途变色。
    v1.0.23：键优先用 inv_id（同库位按配件状态分行后不能再用「物料+库位」合并），
    只有明细没有 inv_id 的老单据才用库位文本；两类键分开存，避免互相误命中。
    """
    win = _stk_cycle_window(conn)
    rows = conn.execute(
        """SELECT si.inv_id, si.part_id, IFNULL(si.location,'') loc, s.no,
                  si.actual_qty, COALESCE(NULLIF(s.done_at,''), s.updated_at, s.created_at) at
             FROM stocktake_items si JOIN stocktakes s ON s.id = si.stocktake_id
            WHERE s.status IN ('pending1','pending2','applying','done')
              AND si.actual_qty IS NOT NULL
              AND COALESCE(s.cycle_key, substr(s.created_at,1,10)) >= ?
            ORDER BY s.id""", (win["from"],)).fetchall()
    m = {}
    for r in rows:
        rec = {"no": r["no"] or "", "at": r["at"] or "", "qty": r["actual_qty"]}
        if r["inv_id"]:
            m[("id", r["inv_id"])] = rec
        else:
            m[("loc", r["part_id"], r["loc"] or "")] = rec
    return m


def _stk_snapshot(conn, mode, picks):
    """按范围生成盘点明细快照（book_qty=当前库存，按物料+库位逐条）。

    mode: all / warehouse / rack / category
    v1.0.21：范围内所有在库行都拉进来，**不再建单时跳过本期已盘行**。
    已盘/未盘改为在单据明细里用页签区分（已盘行锁灰只读，要改得点「重盘」），
    这样进单就能看到全貌，而不是只看到剩下没盘的那几十行。
    """
    sql = """SELECT i.id AS inv_id, i.part_id, p.part_code, p.name AS part_name,
                    i.location_id, i.location, i.quantity, p.is_valuable, p.unit_price
             FROM inventory i JOIN parts p ON p.id = i.part_id
             LEFT JOIN locations l ON l.id = i.location_id
             WHERE p.deleted = 0"""
    conds, args = [], []
    if mode == "warehouse" and picks:
        conds.append("l.warehouse IN ({0})".format(",".join("?" * len(picks)))); args += picks
    elif mode == "rack" and picks:
        # picks 为 "仓库｜货架" 组合，按仓库+货架联合匹配（不同仓库同名货架互不混淆）
        ors = []
        for pk in picks:
            if "｜" in pk:
                wh, rk = pk.split("｜", 1)
                ors.append("(l.warehouse=? AND l.rack=?)"); args += [wh, rk]
            else:
                ors.append("l.rack=?"); args.append(pk)
        if ors:
            conds.append("(" + " OR ".join(ors) + ")")
    elif mode == "category" and picks:
        conds.append("p.category IN ({0})".format(",".join("?" * len(picks)))); args += picks
    if conds:
        sql += " AND " + " AND ".join(conds)
    if mode == "remain":
        # 兜底补盘入口（未盘清单页的「只盘剩余行」）：只取本期还没盘过的行。
        # 正常建单不走这个模式，建单一律全量进单、靠明细页页签区分。
        win = _stk_cycle_window(conn)
        clause, cargs = _stk_counted_clause(conn, win["from"], "i")
        sql += " AND " + clause
        args += cargs
    # 同一物料同一库位不能同时躺在两张在途单里（否则两边都能提交，重复校准）。
    # v1.0.23：同库位按配件状态分行后，防重必须落到 inv_id；仅老单据（无 inv_id）
    # 才回退库位文本，否则同库位多行会被互相排掉，明细数和分母对不上。
    sql += """ AND NOT EXISTS (
                SELECT 1 FROM stocktake_items si
                JOIN stocktakes s ON s.id = si.stocktake_id
                WHERE s.status IN ('draft','pending1','pending2','applying')
                  AND si.part_id = i.part_id
                  AND (si.inv_id = i.id
                       OR (si.inv_id IS NULL
                           AND IFNULL(si.location,'') = IFNULL(i.location,'')))
              )"""
    sql += " ORDER BY i.location, p.part_code"
    prior = _stk_prior_map(conn)
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(r)
        if d.get("inv_id"):
            p = prior.get(("id", d["inv_id"]))
        else:
            p = prior.get(("loc", d["part_id"], d["location"] or ""))
        d["prior_st"] = p["no"] if p else ""
        d["prior_qty"] = p["qty"] if p else None
        d["prior_at"] = p["at"] if p else ""
        out.append(d)
    return out


def _stk_uncovered_rows(conn, cycle_key=None):
    """本期未盘清单：在库行 − 归属于本期的已盘行（inv_id 精确命中）。

    v1.0.54：归属规则与看板柱子完全同源——按「离哪个应盘日最近」归期。
    旧实现用「done_at >= 窗口起日」筛，窗口之前提前完成的单据被漏掉，
    于是看板说已盘 72、点进清单却说未盘 72（现场 72/0 互相打架）。
    cycle_key 可传名义应盘日，也可传窗口起日（都会就近归到同一期）。
    """
    days, tol, anchor = _stk_cycle_cfg(conn)
    tol_days = int(round(days * tol))
    kd = _stk_dt(cycle_key) if cycle_key else None
    if kd is None:
        k = _stk_period_k(datetime.now().date(), anchor, days)
    else:
        k = _stk_period_k(kd, anchor, days)
    if k is None:
        k = 0
    nominal = anchor + timedelta(days=k * days)
    # 「本期开始」沿用旧口径：上一期窗口起日。只用于给未盘行标原因，不参与已盘判定。
    cycle_from = (nominal - timedelta(days=days + tol_days)).strftime("%Y-%m-%d")

    # 已盘行：与 _stk_cycle_series 相同的状态集合 + 同样的就近归期 + 同样的 cell 键
    counted_ids, counted_txt = set(), set()
    try:
        for r in conn.execute(
                """SELECT s.status st, s.done_at s_done, s.updated_at s_upd,
                          s.created_at s_created,
                          si.part_id pid, si.inv_id iid, IFNULL(si.location,'') loc
                   FROM stocktakes s JOIN stocktake_items si ON si.stocktake_id=s.id
                   WHERE s.status IN ('pending1','pending2','applying','done')
                     AND si.actual_qty IS NOT NULL""").fetchall():
            if _stk_period_k(_stk_doc_ref(r["s_done"], r["s_created"], r["s_upd"]),
                             anchor, days) != k:
                continue
            if r["iid"] is not None:
                try:
                    counted_ids.add(int(r["iid"]))
                except (TypeError, ValueError):
                    pass
            else:
                counted_txt.add((int(r["pid"]), r["loc"] or ""))
    except Exception:
        pass
    # 进行中的单（草稿/审批中）虽不算「已盘」，但已占用的行不必再派工，和旧口径一致不进清单
    try:
        for r in conn.execute(
                """SELECT si.part_id pid, si.inv_id iid, IFNULL(si.location,'') loc
                   FROM stocktake_items si JOIN stocktakes s ON s.id=si.stocktake_id
                   WHERE s.status IN ('draft','pending1','pending2','applying')"""):
            if r["iid"] is not None:
                try:
                    counted_ids.add(int(r["iid"]))
                except (TypeError, ValueError):
                    pass
            else:
                counted_txt.add((int(r["pid"]), r["loc"] or ""))
    except Exception:
        pass

    sql = """SELECT i.id AS inv_id, i.part_id, p.part_code, p.name, p.spec, p.unit, p.category,
                    i.location, i.location_id, i.quantity, COALESCE(i.in_date,'') in_date,
                    COALESCE(l.warehouse,'') warehouse, COALESCE(l.rack,'') rack,
                    (SELECT MAX(substr(COALESCE(NULLIF(s2.done_at,''), s2.created_at),1,10))
                       FROM stocktake_items si2 JOIN stocktakes s2 ON s2.id = si2.stocktake_id
                      WHERE s2.status='done' AND si2.part_id=i.part_id
                        AND IFNULL(si2.location,'')=IFNULL(i.location,'')) last_done
             FROM inventory i JOIN parts p ON p.id = i.part_id
             LEFT JOIN locations l ON l.id = i.location_id
             WHERE p.deleted = 0
             ORDER BY i.location, p.part_code"""
    # v1.0.40：每行标「为什么还没盘」——new 本期窗口开始后才入库；noup 本期没有单据覆盖到。
    out = []
    for r in conn.execute(sql).fetchall():
        d = dict(r)
        iid = d.get("inv_id")
        if iid is not None and int(iid) in counted_ids:
            continue
        if iid is None and (int(d["part_id"]), d["location"] or "") in counted_txt:
            continue
        if (d.get("in_date") or "")[:10] >= cycle_from[:10]:
            d["why"] = "new"
        else:
            d["why"] = "noup"
        out.append(d)
    return out


def _stk_coerce_qty(conn, part_id, q):
    """实盘数量按物料数量类型对齐（int 取整 / float 保留）
    实盘数不能为负，也不能是 NaN/Inf —— 负实盘会算出「账面 10、实盘 -5、差异 -15」，
    审批时按 -15 校准库存直接把账面扣穿。"""
    try:
        v = float(q)
    except (TypeError, ValueError):
        return None
    if v != v or v == float("inf") or v == float("-inf"):
        return None
    if v < 0:
        return None
    row = conn.execute("SELECT qty_decimal FROM parts WHERE id=?", (part_id,)).fetchone()
    if row and not row["qty_decimal"]:
        v = int(v)
    return v


def _stk_insert_row(conn, part_id, loc_txt, qty):
    """v1.0.23：盘点盘盈补库存行 —— 继承库位 id 与配件状态。

    旧实现只 INSERT(part_id, location, quantity)，默认值 condition='good'、
    location_id=NULL，会把待维修/待报废区补成「良品」，既违反库位属性约束，
    又让后续按库位 id 出库、筛选、盘点定位全部失效。
    """
    lid, cond, txt = None, "good", (loc_txt or "").strip()
    if txt:
        lr = conn.execute(
            "SELECT id, attribute FROM locations WHERE "
            "warehouse||'-'||rack||'-'||shelf=? OR "
            "warehouse||'-'||rack||'-'||shelf||'-'||bin=? LIMIT 1",
            (txt, txt)).fetchone()
        if lr:
            lid = lr["id"]
            if (_row_get(lr, "attribute") or "good") in ("pending_repair", "pending_scrap"):
                cond = "defective"
    conn.execute(
        "INSERT INTO inventory(part_id, location, location_id, quantity, condition) "
        "VALUES(?,?,?,?,?)", (part_id, txt, lid, qty, cond))


def _stk_apply(conn, sid, user):
    """终审通过：校准库存 + 流水 + 审计，返回汇总 dict"""
    st = conn.execute("SELECT * FROM stocktakes WHERE id=?", (sid,)).fetchone()
    items = conn.execute(
        """SELECT it.*, p.unit_price, p.is_valuable, ap.unit_price actual_price
           FROM stocktake_items it
           JOIN parts p ON p.id = it.part_id
           LEFT JOIN parts ap ON ap.id = it.actual_part_id
           WHERE it.stocktake_id=?""", (sid,),
    ).fetchall()
    total_book = total_act = total_diff = 0.0
    amount = 0.0
    v_count = 0
    clamped_count = 0
    for it in items:
        clamped = False
        actual = it["actual_qty"] or 0
        book = it["book_qty"] or 0
        # 错料：实盘实物为其它料号 → 原料号移出该库位、实物料号盘入该库位
        if it["actual_part_id"] and it["actual_part_id"] != it["part_id"]:
            loc = it["location"] or ""
            # v1.0.23：同库位按状态分行后，错料移出必须按 inv_id 精确落在盘点那一行，
            # 否则会改到另一状态的库存行；盘入也只加在唯一一行上，不能整组多行一起加。
            inv = None
            if it["inv_id"]:
                inv = conn.execute(
                    "SELECT id, quantity FROM inventory WHERE id=?", (it["inv_id"],)).fetchone()
            if inv is None:
                inv = conn.execute(
                    "SELECT id, quantity FROM inventory WHERE part_id=? AND location=? "
                    "ORDER BY id LIMIT 1", (it["part_id"], loc),
                ).fetchone()
            if inv and (inv["quantity"] or 0) > 0:
                newq = max(0.0, (inv["quantity"] or 0) - book)
                conn.execute("UPDATE inventory SET quantity=? WHERE id=?", (newq, inv["id"]))
            # 2) 实物料号盘入该库位 actual
            if actual:
                tgt = conn.execute(
                    "SELECT id FROM inventory WHERE part_id=? AND location=? ORDER BY id LIMIT 1",
                    (it["actual_part_id"], loc),
                ).fetchone()
                if tgt:
                    conn.execute("UPDATE inventory SET quantity=quantity+? WHERE id=?",
                                 (actual, tgt["id"]))
                else:
                    _stk_insert_row(conn, it["actual_part_id"], loc, actual)
            # 3) 流水：原料号盘亏出（错料移出）、实物料号盘盈入
            conn.execute(
                """INSERT INTO transactions(tx_no, part_id, tx_type, quantity, unit_price, amount,
                   location, out_type, ref_type, ref_id, operator_id, applicant_id, line_name, device_name,
                   remark, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (db.gen_no("TX", conn, "transactions", "tx_no"), it["part_id"], "out", book,
                 it["unit_price"] or 0, book * (it["unit_price"] or 0), loc, "stocktake",
                 "stocktake", sid, user["id"], user["id"], "", "",
                 "盘点错料移出 " + st["no"], db.now_str()),
            )
            if actual:
                apr = conn.execute(
                    "SELECT unit_price FROM parts WHERE id=?", (it["actual_part_id"],)
                ).fetchone()
                ap_price = apr["unit_price"] or 0 if apr else 0
                conn.execute(
                    """INSERT INTO transactions(tx_no, part_id, tx_type, quantity, unit_price, amount,
                       location, out_type, ref_type, ref_id, operator_id, applicant_id, line_name, device_name,
                       remark, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (db.gen_no("TX", conn, "transactions", "tx_no"), it["actual_part_id"], "in", actual,
                     ap_price, actual * ap_price, loc, "stocktake",
                     "stocktake", sid, user["id"], user["id"], "", "",
                     "盘点错料盘入 " + st["no"], db.now_str()),
                )
            # 金额口径集中在 _stk_item_amount：错料行以前把账面金额也「加」进
            # 差异金额，一条错料能顶出几百块（见该函数注释）
            amount += _stk_item_amount(it)
            _audit("stocktake", sid, st["no"], "adjust",
                   detail={"part_code": it["part_code"], "location": loc, "book": book,
                           "actual": actual, "mismatch_to": it["actual_part_code"] or "",
                           "diff": actual - book}, conn=conn)
            total_book += book
            total_act += actual
            total_diff += actual - book
            continue
        if not it["diff"]:
            total_book += book
            total_act += actual
            continue
        diff = it["diff"]
        # 1) 校准库存：按审批时实时库存做增量调整，避免盘点期间出入库被覆盖丢失
        #    新库存 = 当前实时库存 + (实盘 - 盘点时账面)
        #    v1.0.20：优先按 inv_id 定位（库位改名后按文本匹配会 INSERT 出幽灵库存行，
        #    同料同库位多行时也只改得到第一行，见 P2-8），取不到再回退 part_id + location。
        inv_row = None
        if it["inv_id"]:
            inv_row = conn.execute(
                "SELECT id, quantity FROM inventory WHERE id=?", (it["inv_id"],)).fetchone()
        if inv_row is None:
            inv_row = conn.execute(
                "SELECT id, quantity FROM inventory WHERE part_id=? AND location=?",
                (it["part_id"], it["location"] or ""),
            ).fetchone()
        cur_qty = float(inv_row["quantity"] or 0) if inv_row else 0.0
        new_qty = max(0.0, cur_qty + (actual - book))
        if inv_row:
            conn.execute("UPDATE inventory SET quantity=? WHERE id=?", (new_qty, inv_row["id"]))
        else:
            _stk_insert_row(conn, it["part_id"], it["location"] or "", new_qty)
        # 2) 流水：**按实际落地变化量记账，不按理论差异**（P0-3）。
        #    账面 10、实盘 9、盘点期间已被出库扣到 0 → 库存只能落在 0，落地量是 0 而不是 -1；
        #    若仍按 abs(diff)=1 记一笔盘亏，账上就永久多扣一件，越盘越偏。
        delta = new_qty - cur_qty
        clamped = abs(abs(delta) - abs(diff)) > 1e-9
        if clamped:
            clamped_count += 1
        if abs(delta) > 1e-9:
            qty = abs(delta)
            tx_type = "in" if delta > 0 else "out"
            remark = "盘点调整 " + st["no"]
            if clamped:
                remark += "（差异 %s 未落地，库存已为 0）" % round(abs(diff) - abs(delta), 3)
            conn.execute(
                """INSERT INTO transactions(tx_no, part_id, tx_type, quantity, unit_price, amount,
                   location, out_type, ref_type, ref_id, operator_id, applicant_id, line_name, device_name,
                   remark, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (db.gen_no("TX", conn, "transactions", "tx_no"), it["part_id"], tx_type, qty,
                 it["unit_price"] or 0, qty * (it["unit_price"] or 0),
                 it["location"] or "", "stocktake", "stocktake", sid, user["id"], user["id"],
                 "", "", remark, db.now_str()),
            )
        # 3) 审计
        _audit("stocktake", sid, st["no"], "adjust",
               detail={"part_code": it["part_code"], "location": it["location"] or "",
                       "book": it["book_qty"], "actual": actual, "diff": diff,
                       "applied": round(delta, 3), "clamped": 1 if clamped else 0,
                       "unit_price": it["unit_price"] or 0},
               conn=conn)
        total_book += it["book_qty"] or 0
        total_act += actual
        total_diff += diff
        amount += _stk_item_amount(it)
        if it["is_valuable"]:
            v_count += 1
    summary = {"book": total_book, "actual": total_act, "diff": total_diff,
               "amount": amount, "v_count": v_count, "clamped": clamped_count}
    conn.execute(
        "UPDATE stocktakes SET status='done', summary=?, updated_at=?, done_at=? WHERE id=?",
        (json.dumps(summary, ensure_ascii=False), db.now_str(), db.now_str(), sid),
    )
    return summary


def _stk_attach_report_urls(cycle):
    """给看板节点补统一报告入口：有完成单 -> 本期合并报告；否则 -> 记录页筛选。"""
    for t in cycle.get("timeline", []):
        _docs = t.get("docs") or []
        _has_done = any(d.get("status") == "done" and d.get("id") for d in _docs)
        if _has_done:
            _url = url_for("stocktake_period_report", cycle=t.get("cycle_key") or "")
        else:
            _url = url_for("stocktake_reports", cycle=t.get("cycle_key") or "")
        t["report_url"] = _url
    return cycle


@app.route("/stocktakes")
@login_required
@perm_required("stocktake")
def stocktakes_page():
    kw = request.args.get("kw", "").strip()
    st = request.args.get("status", "").strip()
    conn = get_db()
    try:
        user = current_user()
        sql = """SELECT s.*,
                        COALESCE(u1.display_name,'') approver1_name,
                        COALESCE(u2.display_name,'') approver2_name,
                        (SELECT COUNT(*) FROM stocktake_items i WHERE i.stocktake_id=s.id) item_cnt,
                        (SELECT COUNT(*) FROM stocktake_items i WHERE i.stocktake_id=s.id AND i.actual_qty IS NOT NULL) done_cnt
                 FROM stocktakes s
                 LEFT JOIN users u ON u.id=s.created_by_id
                 LEFT JOIN users u1 ON u1.id=s.approver1_id
                 LEFT JOIN users u2 ON u2.id=s.approver2_id"""
        conds, args = [], []
        vis, vis_args = _stk_visible_cond(user)
        if vis:
            conds.append(vis); args += vis_args
        if st:
            conds.append("s.status=?"); args.append(st)
        if kw:
            conds.append("(s.no LIKE ? OR s.scope_text LIKE ? OR s.created_by_name LIKE ?)")
            args += ["%" + kw + "%"] * 3
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY s.id DESC"
        rows = conn.execute(sql, args).fetchall()
        # v1.0.18：盘点管理页展示「节点周期图表」与「改善行动任务监控」，
        # 报告统一收进「盘点记录」页。周期天数来自系统设置。
        try:
            cycle_days = int(_settings.load().get("stocktake_cycle_days", 30))
        except (TypeError, ValueError):
            cycle_days = 30
        # v1.0.41：横轴按自然时间等距后，若仍固定 6 个节点，6 期永远铺满画布，
        # 改周期只有日期变、疏密不变（现场反馈「看不到图形变化」）。
        # 改为固定约 180 天时间窗：30 天一期看 6 期，15 天一期看 12 期，
        # 周期一改，柱子和虚线的疏密立刻跟着变；容差改则虚线窗口宽度跟着变。
        chart_node_count = max(6, min(24, int(round(180.0 / max(1, cycle_days)))))
        cycle = _stk_attach_report_urls(
            _stk_cycle_series(conn, cycle_days, nodes=chart_node_count))
        # v1.0.62：默认连已完成任务一起展示（旧版只显示已完成，让人以为任务没生成）。
        # 勾选「隐藏已完成任务」才退回只看未完成；旧 task_done 参数继续兼容。
        _hide_done = request.args.get("task_hide_done", "") in ("1", "on")
        monitor = _stk_task_monitor(
            conn, kw="", only_overdue=request.args.get("task_overdue", "") in ("1", "on"),
            show_done=(request.args.get("task_done", "1") not in ("0", "off"))
                      and not _hide_done)
    finally:
        conn.close()
    # v1.0.33：下发给看板图的节点只含绘图字段。cause/action 自动汇总文本里带
    # 逐行「计划完成日」，整段 tojson 会把「只看超期」筛掉的任务日期又带进页面。
    # v1.0.51：下发 cycle_key/docs/uncovered 供柱子点击跳「该期报表」与未盘数提示。
    # 有盘点单 → 进该期第一张单的报告页；未盘/未来期 → 进盘点记录页按 cycle 筛选。
    chart_nodes = []
    for t in cycle.get("timeline", []):
        _n = {k: t[k] for k in ("label", "plan_base", "from", "to", "state",
                                "done_date", "bar_date", "ok", "bad", "current",
                                "cycle_key", "docs", "total")}
        # v1.0.55：与 timeline 同源；未来期不记未盘
        _n["uncovered"] = t.get("uncovered", 0)
        # v1.0.57：柱子/节点统一进「本期合并报告」，不再挑某一张原单。
        # 原单与逐单报告仍可在合并页「来源单据」和盘点记录里逐张打开。
        _n["report_url"] = t.get("report_url") or url_for(
            "stocktake_reports", cycle=t.get("cycle_key") or "")
        chart_nodes.append(_n)
    ctx = dict(rows=rows, kw=kw, st=st, cycle=cycle, cycle_days=cycle_days,
               chart_nodes=chart_nodes)
    ctx.update(monitor)
    return render_template("stocktakes.html", **ctx)


@app.route("/stocktakes/cycle-note", methods=["GET", "POST"])
@login_required
@perm_required("stocktake")
def stocktake_cycle_note_save():
    """v1.0.68：未盘期原因在后台填写页登记，看板只展示。

    GET 渲染后台填写页；POST 写周期快照表 stocktake_cycles.missed_reason。
    与已完成单据的 delay_reason 分开，不能为了写一句话造一张假盘点单。
    """
    lang = _i18n.current_lang()
    key = ((request.form if request.method == "POST" else request.args)
           .get("cycle_key") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
        flash(_i18n.t("无效的盘点期", lang), "warning")
        return redirect(url_for("stocktakes_page"))
    if request.method == "GET":
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT missed_reason FROM stocktake_cycles WHERE cycle_key=?",
                (key,)).fetchone()
        finally:
            conn.close()
        return render_template("stocktake_cycle_note.html", cycle_key=key,
                               missed_reason=(row["missed_reason"] if row else "") or "")
    note = (request.form.get("missed_reason") or "").strip()[:400]
    now = db.now_str()
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO stocktake_cycles(cycle_key, missed_reason, created_at, updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(cycle_key) DO UPDATE SET
                 missed_reason=excluded.missed_reason, updated_at=excluded.updated_at""",
            (key, note, now, now))
        _audit("stocktake", 0, key, "missed_reason",
               detail={"cycle_key": key, "missed_reason": note}, conn=conn)
        conn.commit()
    finally:
        conn.close()
    flash(_i18n.t("未盘点原因已保存", lang), "success")
    return redirect(url_for("stocktakes_page"))


@app.route("/api/stocktakes/cycle")
@login_required
@perm_required("stocktake")
def api_stocktake_cycle():
    """v1.0.20：周期统计原始数据（图表 / 未盘清单 / 导出同源）。

    抽成接口是为了让「图、清单、导出、回归」四处都读同一份数字，
    避免 v1.0.18 那种「图上说 12 单、列表说 9 单」的口径分裂。
    """
    try:
        nodes = int(request.args.get("nodes", 12))
    except (TypeError, ValueError):
        nodes = 12
    nodes = nodes if 3 <= nodes <= 60 else 12
    conn = get_db()
    try:
        data = _stk_attach_report_urls(_stk_cycle_series(conn, nodes=nodes))
    finally:
        conn.close()
    return jsonify(data)


@app.route("/stocktakes/uncovered")
@login_required
@perm_required("stocktake")
def stocktake_uncovered():
    """v1.0.20：本期未盘清单（在库行 − 本期已盘行，跨单去重）。

    「盘了一部分」的落点就在这里：图上说未盘 372，点进来就是这 372 行，
    按库位排好直接派工；建单时勾「只盘剩余」就不会重复盘已经盘过的。
    """
    cyc = (request.args.get("cycle") or "").strip()
    kw = (request.args.get("kw") or "").strip()
    wh = (request.args.get("wh") or "").strip()
    want_csv = request.args.get("export") in ("1", "csv")
    conn = get_db()
    try:
        win = _stk_cycle_window(conn)
        # v1.0.54：统一传名义 cycle_key（与看板柱子点击链接一致），不再传窗口起日
        rows = [dict(r) for r in _stk_uncovered_rows(conn, cyc or win["cycle_key"])]
        live, live_qty = _stk_live_stock_rows(conn)
        whs = sorted({r["warehouse"] for r in rows if r["warehouse"]})
    finally:
        conn.close()
    if wh:
        rows = [r for r in rows if r["warehouse"] == wh]
    if kw:
        _k = kw.lower()
        rows = [r for r in rows
                if _k in (r.get("part_code") or "").lower()
                or _k in (r.get("name") or "").lower()
                or _k in (r.get("location") or "").lower()]
    if want_csv:
        import csv
        import io as _io
        buf = _io.StringIO()
        wr = csv.writer(buf)
        wr.writerow([_i18n.t("仓库", _i18n.current_lang()), _i18n.t("货架", _i18n.current_lang()),
                     _i18n.t("库位", _i18n.current_lang()), _i18n.t("物料编码", _i18n.current_lang()),
                     _i18n.t("物料名称", _i18n.current_lang()), _i18n.t("规格", _i18n.current_lang()),
                     _i18n.t("单位", _i18n.current_lang()), _i18n.t("账面数量", _i18n.current_lang()),
                     _i18n.t("上次盘点", _i18n.current_lang())])
        for r in rows:
            wr.writerow([r.get("warehouse") or "", r.get("rack") or "", r.get("location") or "",
                         r.get("part_code") or "", r.get("name") or "", r.get("spec") or "",
                         r.get("unit") or "", r.get("quantity") or 0, r.get("last_done") or ""])
        return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 "attachment; filename=stocktake_uncovered_%s.csv"
                                 % datetime.now().strftime("%Y%m%d")})
    info = dict(win)
    info.update({"total": live, "uncovered": len(rows),
                 "counted": max(0, live - len(rows)),
                 "live_qty": round(live_qty, 3),
                 "cycle_query": cyc or win["cycle_key"]})
    return render_template("stocktake_uncovered.html", rows=rows, kw=kw, wh=wh,
                           whs=whs, info=info)


@app.route("/stocktakes/new", methods=["GET", "POST"])
@login_required
@perm_required("stocktake", "create")
def stocktake_new():
    if request.method == "POST":
        mode = request.form.get("scope_mode", "all")
        picks = request.form.getlist("picks")
        note = request.form.get("note", "").strip()
        # v1.0.21：范围回到「全部 / 按仓库 / 按货架 / 按分类」，范围内行全量进单；
        # 哪些盘过哪些没盘，进单后由明细页的页签区分，不再在建单时偷偷跳过。
        if mode not in ("all", "warehouse", "rack", "category", "remain"):
            mode = "all"
        if mode != "all" and not picks:
            flash(_i18n.t("请选择盘点范围", _i18n.current_lang()), "danger")
            return redirect(url_for("stocktake_new"))
        conn = get_db()
        try:
            items = _stk_snapshot(conn, mode, picks)
            if not items:
                flash(_i18n.t("所选范围没有可盘点的库存", _i18n.current_lang()), "danger")
                return redirect(url_for("stocktake_new"))
            win = _stk_cycle_window(conn)
            scope = {"mode": mode, "picks": picks}
            scope_txt = _stk_scope_text(mode, picks)
            no = db.gen_no("ST", conn, "stocktakes", "no")
            user = current_user()
            # 审批人自动按创建人所在部门配置取（一级=部门主管，二级=部门经理），不再让用户选
            a1, a2 = _stk_default_approvers(conn, user)
            cur = conn.execute(
                """INSERT INTO stocktakes(no, scope, scope_text, status, review_level,
                   approver1_id, approver2_id,
                   created_by_id, created_by_name, created_at, updated_at, cycle_key)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (no, json.dumps(scope, ensure_ascii=False), scope_txt, "draft", 2,
                 a1, a2,
                 user["id"], user["username"], db.now_str(), db.now_str(), win["cycle_key"]),
            )
            sid = cur.lastrowid
            prior_n = 0
            for it in items:
                if it.get("prior_st"):
                    prior_n += 1
                conn.execute(
                    """INSERT INTO stocktake_items(stocktake_id, part_id, part_code, part_name,
                       location_id, location, inv_id, book_qty, actual_qty, diff,
                       prior_st, prior_qty, prior_at)
                       VALUES(?,?,?,?,?,?,?,?,NULL,0,?,?,?)""",
                    (sid, it["part_id"], it["part_code"], it["part_name"],
                     it["location_id"], it["location"] or "", it["inv_id"], it["quantity"] or 0,
                     it.get("prior_st") or "", it.get("prior_qty"), it.get("prior_at") or ""),
                )
            # 本期分母快照 + 覆盖范围（图上「本期覆盖：A仓」就靠它，别让 26% 被读成系统算错）
            _stk_cycle_ensure(conn, win["cycle_key"], win["days"], win["tolerance"])
            try:
                _sc = conn.execute(
                    "SELECT scope_note FROM stocktake_cycles WHERE cycle_key=?",
                    (win["cycle_key"],)).fetchone()
                _old = ((_sc["scope_note"] if _sc else "") or "").strip()
                _add = _stk_scope_text(mode, picks)
                if _add and _add not in _old:
                    conn.execute(
                        "UPDATE stocktake_cycles SET scope_note=?, updated_at=? WHERE cycle_key=?",
                        ((_old + "、" + _add) if _old else _add, db.now_str(), win["cycle_key"]))
            except Exception:
                pass
            # 存码不存文案：存 scope_txt 的话，英文界面建单会永久留下
            # 「Scope: 全部库存」这种半截中文（历史数据只能凑合看）
            _audit("stocktake", sid, no, "add",
                   detail={"mode": mode, "picks": picks or [],
                           "scope": scope_txt, "items": len(items)}, conn=conn)
            conn.commit()
            if prior_n:
                flash(_i18n.t("盘点单已创建：{0}（{1} 条明细，其中 {2} 条本期已盘、已锁定）",
                              _i18n.current_lang()).format(no, len(items), prior_n), "success")
            else:
                flash(_i18n.t("盘点单已创建：{0}（{1} 条明细）", _i18n.current_lang()).format(no, len(items)), "success")
            return redirect(url_for("stocktake_detail", sid=sid))
        finally:
            conn.close()
    conn = get_db()
    try:
        # v1.0.20：本期在库覆盖率，建单页顶部直接告诉仓管「已盘多少、还剩多少」
        _win = _stk_cycle_window(conn)
        _unc = _stk_uncovered_rows(conn, _win["cycle_key"])
        _live, _live_qty = _stk_live_stock_rows(conn)
        cycle_info = dict(_win)
        cycle_info.update({"total": _live, "uncovered": len(_unc),
                           "counted": max(0, _live - len(_unc)),
                           "live_qty": round(_live_qty, 3)})
        warehouses = conn.execute(
            "SELECT DISTINCT warehouse FROM locations WHERE warehouse<>'' ORDER BY warehouse").fetchall()
        racks = conn.execute(
            "SELECT DISTINCT warehouse, rack FROM locations WHERE rack<>'' ORDER BY warehouse, rack").fetchall()
        rack_options = [f"{r['warehouse']}｜{r['rack']}" for r in racks]
        cats = conn.execute(
            "SELECT DISTINCT category FROM parts WHERE category<>'' AND deleted=0 ORDER BY category").fetchall()
        user = current_user()
        dep_a1, dep_a2 = _stk_default_approvers(conn, user)
        cand1 = _stk_approver_candidates(conn, "approve1")
        cand2 = _stk_approver_candidates(conn, "approve2")
        cand1 = [dict(r) for r in cand1]
        cand2 = [dict(r) for r in cand2]
        # 最近用过的盘点审批人（本人历史创建单中指派过的，供选择器"常用"分组）
        recent_a1_ids, recent_a2_ids = [], []
        for col in ("approver1_id", "approver2_id"):
            _rc = conn.execute(
                f"""SELECT {col} v, MAX(id) mid FROM stocktakes
                    WHERE created_by_id=? AND {col} IS NOT NULL
                    GROUP BY {col} ORDER BY mid DESC LIMIT 8""", (user["id"],),
            ).fetchall()
            _ids = [r["v"] for r in _rc]
            if col == "approver1_id":
                recent_a1_ids = _ids
            else:
                recent_a2_ids = _ids
        dep1_name = dep2_name = ""
        if dep_a1:
            r = conn.execute("SELECT display_name FROM users WHERE id=?", (dep_a1,)).fetchone()
            dep1_name = r["display_name"] if r else ""
        if dep_a2:
            r = conn.execute("SELECT display_name FROM users WHERE id=?", (dep_a2,)).fetchone()
            dep2_name = r["display_name"] if r else ""
    finally:
        conn.close()
    try:
        try:
            approval_level = int(_settings.load().get("stocktake_approval_level", 1))
        except (TypeError, ValueError):
            approval_level = 1
        if approval_level not in (0, 1, 2):
            approval_level = 1
    except (TypeError, ValueError):
        approval_level = 1
    return render_template("stocktake_new.html", warehouses=warehouses, racks=racks, cats=cats,
                           rack_options=rack_options, cycle_info=cycle_info,
                           dep_a1=dep_a1, dep_a2=dep_a2,
                           dep1_name=dep1_name, dep2_name=dep2_name,
                           cand1=cand1, cand2=cand2,
                           recent_a1_ids=recent_a1_ids, recent_a2_ids=recent_a2_ids,
                           approval_level=approval_level)


def _stk_scope_text(mode, picks):
    if mode == "all":
        return _i18n.t("全部库存", _i18n.current_lang())
    if mode == "remain":
        return _i18n.t("本期剩余", _i18n.current_lang())
    if mode == "warehouse":
        return _i18n.t("仓库", _i18n.current_lang()) + ": " + ",".join(picks)
    if mode == "rack":
        return _i18n.t("货架", _i18n.current_lang()) + ": " + ",".join(picks)
    return _i18n.t("分类", _i18n.current_lang()) + ": " + ",".join(picks)


# 盘点差异原因：库里只存码，界面按当前语言取词。
# 存中文标签的话，俄语同事填的「丢失」会在中文界面原样冒出来，切语言也不跟着变。
_STK_REASONS = [
    ("lost",            "丢失"),
    ("unrecorded_out",  "未登记出库"),
    ("unrecorded_in",   "未登记入库"),
    ("misplaced",       "错放库位"),
    ("count_error",     "计量/计数误差"),
    ("supplier_short",  "供应商短装"),
    ("return_unlogged", "退料未登账"),
    ("other",           "其他"),
]
_STK_REASON_LABEL = dict(_STK_REASONS)
_STK_REASON_CODES = frozenset(_STK_REASON_LABEL)


def _stk_reason_label(code):
    """原因码 → 当前语言文案（未知码原样返回，兼容将来新增的码）"""
    if not code:
        return ""
    return _i18n.t(_STK_REASON_LABEL.get(code, code), _i18n.current_lang())


def _stk_reason_options():
    """模板用：[(码, 当前语言文案)]"""
    lang = _i18n.current_lang()
    return [(c, _i18n.t(z, lang)) for c, z in _STK_REASONS]


def _rowv(row, key, default=""):
    """安全取列：老库/非 SQLite 上迁移没跑成功时列可能不存在，取不到就当空。

    报告页是终点页面，不能因为一个可选列缺失就 500。
    """
    try:
        v = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if v is None else v


def _stk_can_analyze(user, st):
    """能否填写原因分析/改善行动：**只认盘点单创建人**。

    以前只判「有没有盘点模块权限」，于是任何有盘点权限的人都能替别人的单子
    写结论（反馈里的「我不盘点，分析我却能填」就是这么来的）。报告是追责凭证，
    谁盘谁写才算留痕；管理员也不放开 —— 要改别人的单就用创建人的账号，
    避免「有权限就能改历史结论」。
    """
    def g(k):
        try:
            return st[k]
        except (IndexError, KeyError, TypeError):
            return None

    try:
        return g("created_by_id") == user.get("id")
    except Exception:
        return False



def _stk_item_amount(it):
    """盘点单行的差异金额（三处共用：落库汇总 / 报告页 / 导出 Excel）。

    普通行   = 差异数量 × 单价           （盘亏为负、盘盈为正）
    错料行   = 盘入料金额 - 账面料金额    （料号换了，数量可能相同但价值不同）

    这三个地方以前各写一遍，错料金额的口径就不一致了 —— 落库把账面料金额
    也「加」了进去，报告按 差异×单价 算成 0，导出又跟落库不一样。
    规则只留这一份，改一处三处一起变。
    """
    def g(k):
        try:
            return it[k]
        except (IndexError, KeyError, TypeError):
            return None

    up = g("unit_price") or 0
    book = g("book_qty") or 0
    actual = g("actual_qty") or 0
    if g("actual_part_id") and g("actual_part_id") != g("part_id"):
        return actual * (g("actual_price") or 0) - book * up
    return (g("diff") or 0) * up


def _stk_diff_summary(items):
    """报告页与导出 Excel 共用的结论计算。

    返回 (rows, stats, reason_rows)：
      rows       逐行补上 diff_amt / is_wrong / reason_label
      stats      盘盈、盘亏、错料、无差异的条数与数量金额
      reason_rows 按原因码归因后的 条数/数量/金额

    页面和 Excel 各算一遍迟早会漂（页面上合计 1,000、导出 980 这种），
    所以结论只在这里算一次，两边都拿这份结果去排版。
    金额口径与 _stk_apply 一致：普通行 diff×单价，错料行 = 盘入料金额 - 账面料金额。
    """
    gain = loss = mismatch = same = abnormal = 0
    gain_qty = loss_qty = 0.0
    gain_amt = loss_amt = 0.0
    rows = []
    reason_map = {}
    diff_cnt = 0
    no_reason_cnt = 0
    for it in items:
        r = dict(it)
        r["diff_amt"] = _stk_item_amount(it)
        r["is_wrong"] = bool(it["actual_part_id"] and it["actual_part_id"] != it["part_id"])
        # v1.0.74：合并行可能「最终数量已正常、但本期抓到过异常」
        r["abnormal"] = bool(_rowv(it, "merged_abnormal", 0)) or r["is_wrong"] \
            or _stk_item_is_diff(it)
        if r["abnormal"]:
            abnormal += 1
        r["reason_label"] = _stk_reason_label(it["reason"] if "reason" in it.keys() else "")
        wrong = r["is_wrong"]
        if wrong:
            mismatch += 1
        d = it["diff"] or 0
        if d > 0:
            gain += 1
            gain_qty += d
            gain_amt += r["diff_amt"]
        elif d < 0:
            loss += 1
            loss_qty += -d
            loss_amt += r["diff_amt"]
        else:
            same += 1
        if d or wrong or r["abnormal"]:
            diff_cnt += 1
            code = (it["reason"] if "reason" in it.keys() else "") or ""
            if code:
                g = reason_map.setdefault(code, {"code": code, "cnt": 0,
                                                 "qty": 0.0, "amt": 0.0})
                g["cnt"] += 1
                g["qty"] += d if d else 0
                g["amt"] += r["diff_amt"]
            else:
                no_reason_cnt += 1
        rows.append(r)
    reason_rows = []
    for code, g in reason_map.items():
        g["label"] = _stk_reason_label(code)
        reason_rows.append(g)
    reason_rows.sort(key=lambda x: (-x["cnt"], x["label"]))
    stats = {"gain": gain, "gain_qty": gain_qty, "gain_amt": gain_amt,
             "loss": loss, "loss_qty": loss_qty, "loss_amt": loss_amt,
             "mismatch": mismatch, "same": same, "abnormal": abnormal,
             "total": len(items),
             "done": sum(1 for it in items if it["actual_qty"] is not None),
             "diff_cnt": diff_cnt, "no_reason": no_reason_cnt}
    return rows, stats, reason_rows


def _stk_scope_display(row):
    """盘点范围文案：优先按 scope 里的码用当前语言重建。

    scope_text 是建单那刻写死的展示文案 —— 英文界面建的单，切回中文看
    还是「Category: IC」。所以渲染一律以 scope 码为准，scope_text 只兜底
    （历史数据 / 老包导入的数据里可能只有 scope_text）。
    """
    try:
        raw = row["scope"]
        sc = json.loads(raw) if raw else None
        if isinstance(sc, dict) and sc.get("mode"):
            return _stk_scope_text(sc["mode"], sc.get("picks") or [])
    except Exception:
        pass
    try:
        return row["scope_text"] or ""
    except Exception:
        return ""


def _fmt_q(v):
    """数量展示：整数不带小数点，小数最多 3 位。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v if v is not None else "")
    if f == int(f):
        return str(int(f))
    return ("%.3f" % f).rstrip("0").rstrip(".")


def _stk_audit_text(a):
    """把盘点审计的 detail 渲染成一句人能读的话。

    detail 在库里是 JSON 串（见 _audit），以前模板直接 {{ a.detail }} 输出，
    界面上就是 {"scope": "Category: IC", "items": 4} 这种原始数据。
    历史数据可能是纯文本（驳回意见），也要能吃。
    """
    lang = _i18n.current_lang()

    def T(s, **kw):
        return _i18n.t(s, lang, **kw) if kw else _i18n.t(s, lang)

    action = a["action"]
    raw = (a["detail"] or "").strip()
    d = {}
    if raw.startswith("{"):
        try:
            v = json.loads(raw)
            d = v if isinstance(v, dict) else {}
        except Exception:
            d = {}
    if not d:
        # 非 JSON 的自由文本（驳回意见等）原样显示；纯空 {} 显示占位符
        if raw and not raw.startswith("{"):
            return raw
        return T("—") if action == "cancel" else ""

    if action == "add":
        # 新数据存了 mode/picks → 按当前语言重建；老数据只有 scope 文案，原样显示
        if d.get("mode"):
            scope_txt = _stk_scope_text(d["mode"], d.get("picks") or [])
        else:
            scope_txt = d.get("scope") or T("全部库存")
        return T("范围：{scope} · 明细 {n} 条",
                 scope=scope_txt, n=d.get("items", 0))
    if action == "submit":
        bits = [T("审批级别：{n} 级", n=d.get("review_level", 2))]
        if d.get("valuable"):
            bits.append(T("贵重差异 {n} 条", n=d["valuable"]))
        return " · ".join(bits)
    if action in ("approve1", "approve2", "reject"):
        note = d.get("note") or ""
        key = "驳回意见：{note}" if action == "reject" else "意见：{note}"
        txt = T(key, note=note or T("（未填写）"))
        if d.get("diff") is not None:
            txt += " · " + T("差异 {diff}", diff=_fmt_q(d["diff"]))
        if d.get("amount") is not None:
            txt += " · " + T("差异金额 {amount}", amount="%.2f" % float(d["amount"]))
        return txt
    if action == "adjust":
        txt = T("{code} @ {loc}：账面 {book} → 实盘 {actual}",
                code=d.get("part_code", ""), loc=d.get("location") or T("无库位"),
                book=_fmt_q(d.get("book")), actual=_fmt_q(d.get("actual")))
        txt += " · " + T("差异 {diff}", diff=_fmt_q(d.get("diff")))
        if d.get("mismatch_to"):
            txt += " · " + T("错料 → {code}", code=d["mismatch_to"])
        return txt
    if action == "reassign":
        return T("一级审批人 → {a1} · 二级审批人 → {a2}",
                 a1=d.get("approver1_name") or ("#" + str(d.get("approver1_id") or "")),
                 a2=d.get("approver2_name") or ("#" + str(d.get("approver2_id") or "")))
    if action == "analyze":
        bits = []
        if d.get("cause"):
            bits.append(T("已填原因分析"))
        if d.get("action"):
            bits.append(T("已填改善行动"))
        if d.get("reasons"):
            bits.append(T("标注差异原因 {n} 条", n=d["reasons"]))
        if d.get("owner"):
            bits.append(T("责任人：{name}", name=d["owner"]))
        if d.get("due"):
            bits.append(T("计划完成：{date}", date=d["due"]))
        return " · ".join(bits) if bits else T("清空了分析内容")
    return ""


def _stk_audit_rows(audits):
    """审计记录转 dict 并附上人类可读文案，供模板直接渲染。"""
    out = []
    for a in audits:
        r = dict(a)
        r["text"] = _stk_audit_text(a)
        out.append(r)
    return out


# 列表页按行取范围文案（sqlite3.Row 不能就地加字段，走模板函数最省事）
app.jinja_env.globals["stk_scope"] = _stk_scope_display


@app.route("/stocktakes/<int:sid>")
@login_required
def stocktake_detail(sid):
    """盘点详情：可见性 = 创建人/同部门/指派审批人（管理员全可见），不依赖模块权限
    ——被指派的部门主管可能是普通角色，须能进入详情完成审批"""
    conn = get_db()
    try:
        user = current_user()
        st = conn.execute(
            """SELECT s.*, u.department created_dep FROM stocktakes s
               LEFT JOIN users u ON u.id=s.created_by_id WHERE s.id=?""", (sid,),
        ).fetchone()
        if not st:
            abort(404)
        vis, vis_args = _stk_visible_cond(user)
        if vis:
            cnt = conn.execute(
                """SELECT COUNT(*) c FROM stocktakes s LEFT JOIN users u ON u.id=s.created_by_id
                   WHERE s.id=? AND """ + vis, [sid] + vis_args,
            ).fetchone()["c"]
            if not cnt:
                abort(403)
        _items = conn.execute(
            """SELECT it.*, p.unit_price, p.is_valuable, p.qty_decimal
               FROM stocktake_items it JOIN parts p ON p.id=it.part_id
               WHERE it.stocktake_id=? ORDER BY it.id""", (sid,),
        ).fetchall()
        # v1.0.17：老库万一没跑成迁移也不能让详情页 500，逐行分析列统一补默认
        items = []
        for _r in _items:
            _d = dict(_r)
            for _k in ("cause", "action", "action_owner", "action_due",
                       "action_status", "reason", "actual_part_code"):
                _d.setdefault(_k, "")
            items.append(_d)
        audits = conn.execute(
            "SELECT * FROM audit_log WHERE obj_type='stocktake' AND obj_id=? ORDER BY id",
            (sid,),
        ).fetchall()
        approvals = conn.execute(
            """SELECT * FROM audit_log WHERE obj_type='stocktake' AND obj_id=? AND action IN ('approve1','approve2','reject') ORDER BY id""",
            (sid,),
        ).fetchall()
        # 指派审批人姓名
        ap1 = ap2 = None
        if st["approver1_id"]:
            r = conn.execute("SELECT display_name FROM users WHERE id=?", (st["approver1_id"],)).fetchone()
            ap1 = r["display_name"] if r else ""
        if st["approver2_id"]:
            r = conn.execute("SELECT display_name FROM users WHERE id=?", (st["approver2_id"],)).fetchone()
            ap2 = r["display_name"] if r else ""
        cand1 = _stk_approver_candidates(conn, "approve1")
        cand2 = _stk_approver_candidates(conn, "approve2")
        cur_role = user["role"]
        # v1.0.15：提交审批弹窗要逐行差异原因下拉 + 整单分析，报告页的四字段回显
        reasons = _stk_reason_options()
        analysis = {"cause": _rowv(st, "cause"), "action": _rowv(st, "action"),
                    "owner": _rowv(st, "action_owner"), "due": _rowv(st, "action_due"),
                    "at": _rowv(st, "analyzed_at"), "by": _rowv(st, "analyzed_by")}
        # v1.0.43：有差异口径统一走 _stk_item_is_diff，页面只展示问题行时用同一把尺子
        diff_flags = [_stk_item_is_diff(it) for it in items]
        has_diff = any(diff_flags)
    finally:
        conn.close()
    prior_cnt = sum(1 for it in items if (it.get("prior_st") or "").strip())
    done_here_cnt = sum(1 for it in items
                        if (it.get("actual_qty") is not None)
                        and not (it.get("prior_st") or "").strip())
    diff_cnt = sum(1 for f in diff_flags if f)
    return render_template("stocktake_detail.html", st=st, items=items,
                           audits=_stk_audit_rows(audits),
                           approvals=_stk_audit_rows(approvals),
                           ap1=ap1, ap2=ap2, scope_disp=_stk_scope_display(st),
                           cand1=cand1, cand2=cand2, cur_role=cur_role, cur_uid=user["id"],
                           reasons=reasons, analysis=analysis, has_diff=has_diff,
                           prior_cnt=prior_cnt, done_here_cnt=done_here_cnt,
                           undo_cnt=len(items) - prior_cnt - done_here_cnt,
                           diff_cnt=diff_cnt, item_cnt=len(items))


@app.route("/api/stocktake/<int:sid>/save", methods=["POST"])
@login_required
@perm_required("stocktake", "create")
def stocktake_save(sid):
    conn = get_db()
    try:
        user = current_user()
        st = conn.execute(
            """SELECT s.*, u.department created_dep FROM stocktakes s
               LEFT JOIN users u ON u.id=s.created_by_id WHERE s.id=?""", (sid,),
        ).fetchone()
        if not st:
            return jsonify(ok=False, msg=_i18n.t("盘点单不存在", _i18n.current_lang()))
        if not _stk_can_view(user, st):
            return jsonify(ok=False, msg=_i18n.t("无权查看该盘点单", _i18n.current_lang()))
        if st["status"] != "draft":
            return jsonify(ok=False, msg=_i18n.t("仅草稿状态可录入实盘数量", _i18n.current_lang()))
        data = request.get_json(silent=True) or {}
        rows = data.get("items") or []
        lang = _i18n.current_lang()
        saved = 0
        invalid = []
        pairs = {}
        for r in rows:
            iid = r.get("id")
            if not iid:
                continue
            it = conn.execute("SELECT * FROM stocktake_items WHERE id=? AND stocktake_id=?",
                              (iid, sid)).fetchone()
            if not it:
                continue
            # v1.0.17：逐行分析可以独立于实盘数量保存（只改原因、不改数量也算有效保存）
            fields = {k: r.get(k) for k in _STK_ITEM_ANA_KEYS if k in r}
            if fields:
                try:
                    pairs[int(iid)] = fields
                except (TypeError, ValueError):
                    pass
            raw = r.get("actual_qty")
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                continue
            qty = _stk_coerce_qty(conn, it["part_id"], raw)
            if qty is None:
                # 负数 / NaN / 非数字：不能静默丢弃 —— 否则前端以为存进去了，
                # 审批时按缺失值处理，账面差异凭空消失
                invalid.append(it["part_code"] or "")
                continue
            # 错料：实盘发现实物为其它料号（actual_part_id 与原 part_id 不同才记录）
            ap_id = r.get("actual_part_id") or None
            ap_code = ""
            if ap_id:
                try:
                    ap_id = int(ap_id)
                except (TypeError, ValueError):
                    ap_id = None
                if ap_id and ap_id != it["part_id"]:
                    apr = conn.execute(
                        "SELECT part_code FROM parts WHERE id=? AND deleted=0", (ap_id,)
                    ).fetchone()
                    if apr:
                        ap_code = apr["part_code"]
                    else:
                        ap_id = None
            conn.execute(
                """UPDATE stocktake_items SET actual_qty=?, diff=?, note=?,
                   actual_part_id=?, actual_part_code=? WHERE id=?""",
                (qty, qty - it["book_qty"], (r.get("note") or "").strip(),
                 ap_id, ap_code, iid),
            )
            saved += 1
        if invalid:
            conn.rollback()   # 整批不写：不能出现「一半存进去、界面说成功」
            msg = _i18n.t("盘点数量不能为负数，请检查标红的输入框", lang)
            shown = [c for c in invalid[:5] if c]
            if shown:
                msg += " " + ", ".join(shown) + ("…" if len(invalid) > 5 else "")
            return jsonify(ok=False, msg=msg, bad=invalid)
        # 逐行分析落库后立刻刷新报告级自动汇总，提交弹窗/报告页当场看到的是最新结论
        _stk_save_item_analysis(conn, sid, pairs)
        _stk_refresh_summary(conn, sid)
        conn.execute("UPDATE stocktakes SET updated_at=? WHERE id=?", (db.now_str(), sid))
        conn.commit()
        return jsonify(ok=True, saved=saved)
    finally:
        conn.close()


@app.route("/api/stocktake/<int:sid>/reset-all", methods=["POST"])
@login_required
@perm_required("stocktake", "create")
def stocktake_reset_all(sid):
    """v1.0.56：整体重盘——把本单所有明细的实盘数/差异/逐行分析一次清空，
    回到全未盘状态重盘。仅草稿 + 创建人可操作（与逐行重盘同一口径）。"""
    conn = get_db()
    try:
        user = current_user()
        st = conn.execute(
            """SELECT s.*, u.department created_dep FROM stocktakes s
               LEFT JOIN users u ON u.id=s.created_by_id WHERE s.id=?""", (sid,),
        ).fetchone()
        if not st:
            return jsonify(ok=False, msg=_i18n.t("盘点单不存在", _i18n.current_lang()))
        if not _stk_can_view(user, st):
            return jsonify(ok=False, msg=_i18n.t("无权操作该盘点单", _i18n.current_lang()))
        if st["status"] != "draft":
            return jsonify(ok=False, msg=_i18n.t("仅草稿状态可整体重盘", _i18n.current_lang()))
        if not _stk_can_analyze(user, st):
            return jsonify(ok=False, msg=_i18n.t("只有盘点单创建人可以整体重盘", _i18n.current_lang()))
        n = conn.execute(
            """SELECT COUNT(*) c FROM stocktake_items WHERE stocktake_id=?""", (sid,),
        ).fetchone()["c"]
        conn.execute(
            """UPDATE stocktake_items
               SET actual_qty=NULL, diff=0, actual_part_id=NULL, actual_part_code=NULL,
                   reason='', cause='', action='', action_owner='', action_due='',
                   action_status='open', action_done_by='', action_done_at=''
               WHERE stocktake_id=?""", (sid,),
        )
        _stk_refresh_summary(conn, sid, force=True)
        conn.execute("UPDATE stocktakes SET updated_at=? WHERE id=?", (db.now_str(), sid))
        _audit("stocktake", sid, st["no"], "reset_all", detail={"rows": n}, conn=conn)
        conn.commit()
        return jsonify(ok=True, reset=n)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route("/stocktakes/<int:sid>/submit", methods=["POST"])
@login_required
@perm_required("stocktake", "create")
def stocktake_submit(sid):
    conn = get_db()
    try:
        user = current_user()
        st = conn.execute(
            """SELECT s.*, u.department created_dep FROM stocktakes s
               LEFT JOIN users u ON u.id=s.created_by_id WHERE s.id=?""", (sid,),
        ).fetchone()
        if not st:
            abort(404)
        if not _stk_can_view(user, st):
            abort(403)
        if st["status"] != "draft":
            flash(_i18n.t("仅草稿状态可提交", _i18n.current_lang()), "danger")
            return redirect(url_for("stocktake_detail", sid=sid))
        items = conn.execute(
            """SELECT it.*, p.is_valuable FROM stocktake_items it JOIN parts p ON p.id=it.part_id
               WHERE it.stocktake_id=?""", (sid,),
        ).fetchall()
        if not items:
            flash(_i18n.t("无盘点明细，无法提交", _i18n.current_lang()), "danger")
            return redirect(url_for("stocktake_detail", sid=sid))
        # v1.0.20 / v1.0.72：允许「盘一部分」先提交，但未盘行只在**所有校验通过、
        # 提交真正成功**时才剔除。原来一进路由就删，校验失败重定向后虽然事务回滚，
        # 却先闪了「已跳过 N 条」的成功提示，用户以为提交过了。
        #   · 不能留在单里 —— _stk_apply 用 `it["actual_qty"] or 0`，留着会被当成实盘 0，
        #     整批算成盘亏，把库存扣穿；
        #   · 剔掉也不算「本期已盘」—— 它们仍在未盘清单里，下一张单继续盘（就是要的效果）。
        unbooked = [it for it in items if it["actual_qty"] is None]
        booked = [it for it in items if it["actual_qty"] is not None]
        if not booked:
            flash(_i18n.t("本单还没有任何已盘明细，请先录入实盘数量再提交", _i18n.current_lang()),
                  "danger")
            return redirect(url_for("stocktake_detail", sid=sid))
        valuable_diff = any(it["is_valuable"] and (it["diff"] or 0) != 0 for it in booked)
        # 负数实盘一律不许提交：审批时会按 diff 校准库存，负数能把账面扣穿
        neg = [it["part_code"] or "" for it in items
               if it["actual_qty"] is not None and it["actual_qty"] < 0]
        if neg:
            msg = _i18n.t("盘点数量不能为负数，请检查标红的输入框", _i18n.current_lang())
            shown = [c for c in neg[:5] if c]
            if shown:
                msg += " " + ", ".join(shown) + ("…" if len(neg) > 5 else "")
            flash(msg, "danger")
            return redirect(url_for("stocktake_detail", sid=sid))
        # 审批级别：设置里 None / 非法值一律按默认一级（1）解析（settings.load 已归一化）
        try:
            lv = int(_settings.load().get("stocktake_approval_level", 1))
        except (TypeError, ValueError):
            lv = 1
        if lv not in (0, 1, 2):
            lv = 1
        # 校验审批人是否配置：创建人所在部门必须有主管（一级审批人）
        if not st["created_dep"]:
            flash(_i18n.t("提交失败：您的账号未分配部门，无法确定审批人。请先到「用户管理」为您分配部门后再提交", _i18n.current_lang()), "danger")
            return redirect(url_for("stocktake_detail", sid=sid))
        if lv >= 1:
            dep = conn.execute("SELECT supervisor_id, manager_id FROM departments WHERE name=?", (st["created_dep"],)).fetchone()
            if not dep or not dep["supervisor_id"]:
                flash(_i18n.t("提交失败：您所在部门 [{0}] 未配置一级审批人（主管），请先到「部门配置」设置主管后再提交", _i18n.current_lang()).format(st["created_dep"]), "danger")
                return redirect(url_for("stocktake_detail", sid=sid))
            # 两级审批时，二级审批人（经理）也必须配置
            if lv == 2 and not dep["manager_id"]:
                flash(_i18n.t("提交失败：系统配置为两级审批，但您所在部门 [{0}] 未配置二级审批人（经理），请先到「部门配置」设置经理后再提交", _i18n.current_lang()).format(st["created_dep"]), "danger")
                return redirect(url_for("stocktake_detail", sid=sid))
            # 自动指派审批人：一级=部门主管，二级=部门经理
            approver1 = dep["supervisor_id"]
            approver2 = dep["manager_id"] if lv == 2 else None
        else:
            # 不审批：跳过审批人校验与指派，提交即按盘点结果校准库存并完成
            approver1 = None
            approver2 = None
        user = current_user()
        # v1.0.17 / v1.0.72：弹窗里补填的内容先落库再校验，且失败也 commit 保住，
        # 否则重定向后用户刚填的原因/改善处理全没了，只能回主清单重填一遍。
        # v1.0.56 起前端口径：选了具体差异原因即原因已明确，原因分析可留空；
        # 只有「其他」(other) 或「未标注」(空) 才必须另填原因分析；改善处理始终必填。
        # 后端这里原来仍强制「原因分析 + 改善处理」双必填，和提示语、前端校验矛盾，
        # 按提示填好的内容永远提交不了 —— 本次把三处（UI/前端/后端）统一。
        _stk_save_reason_codes(conn, sid, request.form, {it["id"] for it in items})
        _stk_save_item_analysis(conn, sid, _stk_item_pairs_from_form(request.form))
        items = conn.execute(
            """SELECT it.*, p.is_valuable FROM stocktake_items it JOIN parts p ON p.id=it.part_id
               WHERE it.stocktake_id=? ORDER BY it.id""", (sid,),
        ).fetchall()
        missing = []
        for it in items:
            if not _stk_item_is_diff(it):
                continue
            reason = (_rowv(it, "reason") or "").strip()
            need_cause = (not reason or reason == "other")
            if (not (_rowv(it, "action") or "").strip()
                    or (need_cause and not (_rowv(it, "cause") or "").strip())):
                missing.append((_rowv(it, "part_code") or ""))
        if missing:
            conn.commit()   # 原因码 + 逐行分析保留，重开弹窗不用重填
            flash(_i18n.t("每条差异都要填写原因分析和改善处理，还差 {0} 条",
                          _i18n.current_lang()).format(len(missing)), "danger")
            return redirect(url_for("stocktake_detail", sid=sid))
        if unbooked:
            conn.execute(
                "DELETE FROM stocktake_items WHERE stocktake_id=? AND actual_qty IS NULL", (sid,))
            items = [it for it in items if it["actual_qty"] is not None]
            flash(_i18n.t("已跳过 {n} 条未盘明细，本次只提交已盘的 {m} 条；未盘行仍留在本期未盘清单里",
                          _i18n.current_lang()).format(n=len(unbooked), m=len(items)), "success")
        # 逐行内容自动总结成报告级「原因分析 / 改善行动」，再补落责任人等元数据
        _stk_refresh_summary(conn, sid)
        _stk_save_analysis_form(conn, sid, user, request.form, items, compose=True)
        if lv == 0:
            summary = _stk_apply(conn, sid, user)
            _audit("stocktake", sid, st["no"], "submit",
                   detail={"review_level": 0, "auto_done": True, "diff": summary["diff"],
                           "amount": summary["amount"]}, conn=conn)
            conn.commit()
            flash(_i18n.t("审批设置为「不审批」：盘点已提交并按结果校准库存，直接完成", _i18n.current_lang()), "success")
            return redirect(url_for("stocktake_detail", sid=sid))
        conn.execute(
            "UPDATE stocktakes SET status='pending1', review_level=?, approver1_id=?, approver2_id=?, updated_at=? WHERE id=?",
            (lv, approver1, approver2, db.now_str(), sid),
        )
        _audit("stocktake", sid, st["no"], "submit",
               detail={"review_level": lv, "valuable": valuable_diff, "unbooked": 0},
               conn=conn)
        conn.commit()
        flash(_i18n.t("已提交，等待审批（{0}）", _i18n.current_lang())
              .format(_i18n.t("两级审批", _i18n.current_lang()) if lv == 2 else _i18n.t("一级审批", _i18n.current_lang())),
              "success")
        return redirect(url_for("stocktake_detail", sid=sid))
    finally:
        conn.close()


@app.route("/stocktakes/<int:sid>/approve", methods=["POST"])
@login_required
def stocktake_approve(sid):
    """盘点审批：指派制（被指派者/管理员可批）+ 未指派（同部门有权限者/管理员可批）。
    不用 perm_required 装饰器——被指派者可能无 stocktake 模块权限（如部门主管为普通角色），
    指派即授权；未指派时函数内按权限矩阵判断。"""
    user = current_user()
    action = request.form.get("action", "")
    # v1.0.15：审批单是唯一审批入口，从那里提交后要回到那里，不要跳去盘点详情
    nxt = (request.form.get("next") or "").strip()

    def back():
        return redirect(url_for("approval_page") if nxt == "approval"
                        else url_for("stocktake_detail", sid=sid))

    lv = 1
    try:
        lv = _int(request.form.get("level", 1), 1)
    except (TypeError, ValueError):
        lv = 1
    conn = get_db()
    try:
        st = conn.execute("SELECT * FROM stocktakes WHERE id=?", (sid,)).fetchone()
        if not st:
            abort(404)
        if st["status"] == "pending1" and lv != 1:
            flash(_i18n.t("当前处于一级审批阶段", _i18n.current_lang()), "danger")
            return back()
        if st["status"] == "pending2" and lv != 2:
            flash(_i18n.t("当前处于二级审批（终审）阶段", _i18n.current_lang()), "danger")
            return back()
        if st["status"] not in ("pending1", "pending2"):
            flash(_i18n.t("当前状态不可审批", _i18n.current_lang()), "danger")
            return back()
        perm = "approve1" if lv == 1 else "approve2"
        # 指派制：指派了审批人则仅被指派者可审批（系统管理员可代审批）
        assigned_id = st["approver1_id"] if lv == 1 else st["approver2_id"]
        # 检查指派的审批人是否还存在且启用，不存在则放开（管理员兜底）
        assigned_valid = False
        if assigned_id:
            assigned_user = conn.execute("SELECT id, active FROM users WHERE id=?", (assigned_id,)).fetchone()
            assigned_valid = assigned_user and assigned_user["active"]
        if assigned_valid:
            if user["role"] != "admin" and user["id"] != assigned_id:
                flash(_i18n.t("该环节已指派给指定审批人，您无权审批", _i18n.current_lang()), "danger")
                return back()
        elif not can(user["role"], "stocktake", perm):
            abort(403)
        else:
            # 未指派（或审批人已失效）：仅同部门有权限者可审批（系统管理员例外）
            if user["role"] != "admin":
                created_dep = conn.execute(
                    """SELECT u.department FROM stocktakes s LEFT JOIN users u ON u.id=s.created_by_id
                       WHERE s.id=?""", (sid,),
                ).fetchone()
                if ((user.get("department") or "") != (created_dep["department"] or "") if created_dep else True):
                    flash(_i18n.t("仅本部门审批人可审批该盘点单", _i18n.current_lang()), "danger")
                    return back()
        note = (request.form.get("note") or "").strip()
        if action == "reject":
            conn.execute("UPDATE stocktakes SET status='draft', updated_at=? WHERE id=?",
                         (db.now_str(), sid))
            _audit("stocktake", sid, st["no"], "reject",
                   detail={"level": lv, "note": note or _i18n.t("未填写驳回意见", _i18n.current_lang())},
                   conn=conn)
            conn.commit()
            flash(_i18n.t("已驳回，盘点单退回草稿，可修改后重新提交", _i18n.current_lang()), "success")
            return back()
        # 通过
        if st["review_level"] == 2 and lv == 1:
            conn.execute("UPDATE stocktakes SET status='pending2', updated_at=? WHERE id=?",
                         (db.now_str(), sid))
            _audit("stocktake", sid, st["no"], "approve1",
                   detail={"level": 1, "note": note}, conn=conn)
            conn.commit()
            flash(_i18n.t("一级审批通过，等待管理员终审", _i18n.current_lang()), "success")
        else:
            # v1.0.20：先原子抢占状态，再校准库存（P0-1）。
            # 双击 / 网络重试 / 审批中心与详情页同时点，会发出两个都读到 pending 的请求；
            # 不加这道锁，两个请求都会跑完整套 _stk_apply，库存被调两次、流水记两组。
            # SQLite 写锁排他，第二个请求的 UPDATE 会等第一个提交，然后 rowcount=0。
            cur = conn.execute(
                "UPDATE stocktakes SET status='applying', updated_at=? "
                "WHERE id=? AND status IN ('pending1','pending2')",
                (db.now_str(), sid))
            if cur.rowcount != 1:
                conn.rollback()
                flash(_i18n.t("该盘点单已被处理，请刷新后查看", _i18n.current_lang()), "danger")
                return back()
            try:
                summary = _stk_apply(conn, sid, user)
            except Exception:
                # 回滚后状态自动退回 pending1/pending2，不会卡在 applying
                conn.rollback()
                raise
            _audit("stocktake", sid, st["no"], "approve2" if lv == 2 else "approve1",
                   detail={"level": lv, "note": note, "diff": summary["diff"],
                           "amount": summary["amount"], "clamped": summary.get("clamped", 0)},
                   conn=conn)
            conn.commit()
            _msg = _i18n.t("审批通过，库存已按盘点结果校准，盘点完成", _i18n.current_lang())
            if summary.get("clamped"):
                _msg += _i18n.t("（{n} 行差异因库存已为 0 未能完全落地，详见报告）",
                                _i18n.current_lang()).replace("{n}", str(summary["clamped"]))
            flash(_msg, "success")
        return back()
    finally:
        conn.close()


@app.route("/stocktakes/<int:sid>/cancel", methods=["POST"])
@login_required
@perm_required("stocktake", "create")
def stocktake_cancel(sid):
    conn = get_db()
    try:
        user = current_user()
        st = conn.execute(
            """SELECT s.*, u.department created_dep FROM stocktakes s
               LEFT JOIN users u ON u.id=s.created_by_id WHERE s.id=?""", (sid,),
        ).fetchone()
        if not st:
            abort(404)
        if not _stk_can_view(user, st):
            abort(403)
        if st["status"] not in ("draft", "pending1", "pending2"):
            flash(_i18n.t("当前状态不可取消", _i18n.current_lang()), "danger")
            return redirect(url_for("stocktake_detail", sid=sid))
        user = current_user()
        conn.execute("UPDATE stocktakes SET status='cancelled', updated_at=? WHERE id=?",
                     (db.now_str(), sid))
        _audit("stocktake", sid, st["no"], "cancel", detail={}, conn=conn)
        conn.commit()
        flash(_i18n.t("盘点单已取消", _i18n.current_lang()), "success")
        return redirect(url_for("stocktakes_page"))
    finally:
        conn.close()


@app.route("/stocktakes/<int:sid>/reassign", methods=["POST"])
@login_required
@role_required("admin")
def stocktake_reassign(sid):
    """管理员改派审批人：解决审批人不在/离职导致的卡单（仅草稿/待审批状态可改）"""
    conn = get_db()
    try:
        st = conn.execute("SELECT * FROM stocktakes WHERE id=?", (sid,)).fetchone()
        if not st:
            abort(404)
        if st["status"] not in ("draft", "pending1", "pending2"):
            flash(_i18n.t("仅草稿或待审批状态的盘点单可改派审批人", _i18n.current_lang()), "danger")
            return redirect(url_for("stocktake_detail", sid=sid))
        a1 = _form_opt_int("approver1_id")
        a2 = _form_opt_int("approver2_id")
        # 校验：所选审批人必须存在且启用
        for aid in (a1, a2):
            if aid:
                if not conn.execute(
                        "SELECT id FROM users WHERE id=? AND active=1", (aid,)).fetchone():
                    flash(_i18n.t("所选审批人不存在或已停用", _i18n.current_lang()), "danger")
                    return redirect(url_for("stocktake_detail", sid=sid))
        conn.execute(
            "UPDATE stocktakes SET approver1_id=?, approver2_id=?, updated_at=? WHERE id=?",
            (a1, a2, db.now_str(), sid),
        )
        # 记姓名：只存 id 的话，详情页只能显示 #3，#3 是谁得再去查用户表
        def _uname(uid):
            if not uid:
                return ""
            r = conn.execute("SELECT display_name FROM users WHERE id=?", (uid,)).fetchone()
            return (r["display_name"] if r else "") or ""

        _audit("stocktake", sid, st["no"], "reassign",
               detail={"approver1_id": a1, "approver2_id": a2,
                       "approver1_name": _uname(a1), "approver2_name": _uname(a2)},
               conn=conn)
        conn.commit()
        flash(_i18n.t("审批人已改派", _i18n.current_lang()), "success")
    finally:
        conn.close()
    return redirect(url_for("stocktake_detail", sid=sid))


@app.route("/stocktakes/<int:sid>/delete", methods=["POST"])
@login_required
@role_required("admin")
def stocktake_delete(sid):
    """管理员删除盘点单：仅未执行（草稿/待审批/已取消）的单可删；已完成单不可删"""
    conn = get_db()
    try:
        st = conn.execute("SELECT * FROM stocktakes WHERE id=?", (sid,)).fetchone()
        if not st:
            abort(404)
        if st["status"] in ("pending1", "pending2"):
            # 待审批删除前检查：未产生任何盘点校准流水（ref_type='stocktake'）
            ref = conn.execute(
                "SELECT COUNT(*) c FROM transactions WHERE ref_type='stocktake' AND ref_id=?",
                (sid,),
            ).fetchone()["c"]
            if ref:
                flash(_i18n.t("该盘点单已产生库存流水，不可删除", _i18n.current_lang()), "danger")
                return redirect(url_for("stocktake_detail", sid=sid))
        if st["status"] not in ("draft", "pending1", "pending2", "cancelled"):
            flash(_i18n.t("已完成状态的盘点单不可删除", _i18n.current_lang()), "danger")
            return redirect(url_for("stocktake_detail", sid=sid))
        conn.execute("DELETE FROM stocktake_items WHERE stocktake_id=?", (sid,))
        conn.execute("DELETE FROM stocktakes WHERE id=?", (sid,))
        conn.commit()
        flash(_i18n.t("盘点单已删除", _i18n.current_lang()), "success")
    finally:
        conn.close()
    return redirect(url_for("stocktakes_page"))


@app.route("/stocktakes/<int:sid>/report")
@login_required
@perm_required("stocktake")
def stocktake_report(sid):
    conn = get_db()
    try:
        user = current_user()
        st = conn.execute(
            """SELECT s.*, u.department created_dep FROM stocktakes s
               LEFT JOIN users u ON u.id=s.created_by_id WHERE s.id=?""", (sid,),
        ).fetchone()
        if not st:
            abort(404)
        if not _stk_can_view(user, st):
            abort(403)
        # 报告是「结论」，没走完流程就进来只会看到 0 和「已完成」徽章，等于骗人
        if st["status"] != "done":
            flash(_i18n.t("盘点报告在审批通过、库存调整完成后生成，当前单据还没走完流程",
                          _i18n.current_lang()), "warning")
            return redirect(url_for("stocktake_detail", sid=sid))
        _items = conn.execute(
            """SELECT it.*, p.unit_price, p.is_valuable, ap.unit_price actual_price
               FROM stocktake_items it
               JOIN parts p ON p.id=it.part_id
               LEFT JOIN parts ap ON ap.id=it.actual_part_id
               WHERE it.stocktake_id=? ORDER BY it.id""", (sid,),
        ).fetchall()
        items = []
        for _r in _items:
            _d = dict(_r)
            for _k in ("cause", "action", "action_owner", "action_due",
                       "action_status", "reason", "actual_part_code"):
                _d.setdefault(_k, "")
            items.append(_d)
        approvals = conn.execute(
            """SELECT * FROM audit_log WHERE obj_type='stocktake' AND obj_id=? AND action IN ('approve1','approve2','reject','submit') ORDER BY id""",
            (sid,),
        ).fetchall()
        ap1 = ap2 = ""
        if st["approver1_id"]:
            r = conn.execute("SELECT display_name FROM users WHERE id=?",
                             (st["approver1_id"],)).fetchone()
            ap1 = r["display_name"] if r else ""
        if st["approver2_id"]:
            r = conn.execute("SELECT display_name FROM users WHERE id=?",
                             (st["approver2_id"],)).fetchone()
            ap2 = r["display_name"] if r else ""
        editable = _stk_can_analyze(user, st)
        # v1.0.62：未结束的改善任务允许责任人改写本行原因与改善行动。
        # 已完成的任务继续锁定；创建人可改本单所有未结束任务的行。
        _uname = (user.get("display_name") or user.get("username") or "").strip()
        item_edit_ids = set()
        for _it in items:
            if not (_it.get("action") or "").strip():
                continue
            if (_it.get("action_status") or "open") == "done":
                continue
            _owner = (_it.get("action_owner") or "").strip()
            if editable or (_uname and _owner == _uname):
                item_edit_ids.add(int(_it["id"]))
        # v1.0.41：本期是否延期 = 完成日晚于「应盘日 + 容差天数」。
        # 已写过说明的老单据即使规则改了也保留入口，避免历史说明看不到/改不了。
        delay_flag = False
        try:
            _days, _tol, _anchor = _stk_cycle_cfg(conn)
            _nominal = _stk_dt(_rowv(st, "cycle_key")) or _anchor
            _done = _stk_dt(_rowv(st, "done_at"))
            if _nominal and _done:
                delay_flag = _done > (_nominal + timedelta(days=int(round(_days * _tol))))
        except Exception:
            delay_flag = False
        if _rowv(st, "delay_reason"):
            delay_flag = True
    finally:
        conn.close()

    rows, stats, reason_rows = _stk_diff_summary(items)
    return render_template("stocktake_report.html", st=st, items=rows,
                           approvals=_stk_audit_rows(approvals),
                           stats=stats, scope_disp=_stk_scope_display(st),
                           printed_at=db.now_str(), ap1=ap1, ap2=ap2,
                           reasons=_stk_reason_options(), reason_rows=reason_rows,
                           editable=editable, delay_flag=delay_flag,
                           item_edit_ids=item_edit_ids,
                           analysis={"delay_reason": _rowv(st, "delay_reason"),
                                     "cause": _rowv(st, "cause"),
                                     "action": _rowv(st, "action"),
                                     "owner": _rowv(st, "action_owner"),
                                     "due": _rowv(st, "action_due"),
                                     "at": _rowv(st, "analyzed_at"),
                                     "by": _rowv(st, "analyzed_by"),
                                     "manual": bool(_rowv(st, "summary_manual", 0)),
                                     "cause_auto": _rowv(st, "cause_auto"),
                                     "action_auto": _rowv(st, "action_auto")})


@app.route("/stocktakes/period-report")
@login_required
@perm_required("stocktake")
def stocktake_period_report():
    """v1.0.57：盘点节点点击进入的「本期合并报告」。

    同一期可能先全盘、再对几颗料复盘；逐单报告各自保留，合并页按库存行
    取最终结论，并把每一张来源单据列出来，避免「点柱子只看到 2 行复盘」
    或「点上一期看到另一天的全盘报告」。
    """
    cycle = (request.args.get("cycle") or "").strip()
    if not _stk_dt(cycle):
        abort(404)
    conn = get_db()
    try:
        data = _stk_period_report_data(conn, cycle)
        if data is None:
            flash(_i18n.t("该期还没有已完成的盘点报告", _i18n.current_lang()), "warning")
            return redirect(url_for("stocktake_reports", cycle=cycle))
        docs = []
        for d in data["docs"]:
            sid = int(d["id"])
            cnt = conn.execute(
                """SELECT COUNT(*) c,
                          SUM(CASE WHEN ABS(COALESCE(diff,0))>0 OR actual_part_id IS NOT NULL
                                   THEN 1 ELSE 0 END) bad
                   FROM stocktake_items WHERE stocktake_id=? AND actual_qty IS NOT NULL""",
                (sid,)).fetchone()
            docs.append({
                "id": sid, "no": d.get("no") or "",
                "date": d["_ref_date"].strftime("%Y-%m-%d"),
                "created_by_name": d.get("created_by_name") or "",
                "scope": _stk_scope_display(d),
                "item_cnt": int(cnt["c"] or 0),
                "diff_cnt": int(cnt["bad"] or 0),
                "report_url": url_for("stocktake_report", sid=sid),
                "detail_url": url_for("stocktake_detail", sid=sid),
            })
        approvers = []
        for d in data["docs"]:
            for field in ("approver1_id", "approver2_id"):
                aid = d.get(field)
                if not aid:
                    continue
                r = conn.execute("SELECT display_name FROM users WHERE id=?", (aid,)).fetchone()
                nm = r["display_name"] if r else ""
                if nm and nm not in approvers:
                    approvers.append(nm)
    finally:
        conn.close()
    rows, stats, reason_rows = _stk_diff_summary(data["items"])
    return render_template(
        "stocktake_period_report.html",
        period=data, docs=docs, items=rows, stats=stats, reason_rows=reason_rows,
        approvers=approvers, printed_at=db.now_str(),
        reasons=_stk_reason_options())


@app.route("/stocktakes/period-report/export")
@login_required
@perm_required("stocktake", "export")
def stocktake_period_export():
    """合并报告导出：一张汇总页 + 一张扁平明细，不改动任何原始单据。"""
    cycle = (request.args.get("cycle") or "").strip()
    if not _stk_dt(cycle):
        abort(404)
    conn = get_db()
    try:
        data = _stk_period_report_data(conn, cycle)
    finally:
        conn.close()
    if data is None:
        abort(404)
    lang = _i18n.current_lang()
    L = lambda s: _xl(s, lang)  # noqa: E731
    rows_full, stats, reason_rows = _stk_diff_summary(data["items"])
    s = data["summary"]
    W = [6, 15, 22, 22, 10, 10, 9, 17, 10, 12, 13, 9]
    R, MG = [], []

    def add(cells, h=None):
        R.append({"h": h, "cells": cells})

    def span(r, c1, c2):
        col = lambda n: chr(64 + n) if n <= 26 else "A"  # noqa: E731
        MG.append("%s%d:%s%d" % (col(c1), r, col(c2), r))

    def section(title):
        rr = len(R) + 1
        add([(title, "sec")] + [("", "sec")] * 11, 22)
        span(rr, 1, 12)

    add([(L("本期合并报告") + "  " + cycle, "title")] + [("", "def")] * 11, 30)
    span(1, 1, 8)
    R[0]["cells"][8] = (L("制表时间") + "：" + db.now_str(), "sub")
    span(1, 9, 12)
    add([("", "def")], 6)
    section(L("盘点结论"))
    labels = [L("账面总数"), L("实盘总数"), L("差异总数"), L("差异金额(元)"), L("贵重差异条数")]
    rr = len(R) + 1
    cells = []
    for lb in labels:
        cells += [(lb, "statl"), ("", "statl")]
    add(cells, 20)
    for i in range(len(labels)):
        span(rr, i * 2 + 1, i * 2 + 2)
    rr += 1
    vals = [(s.get("book", 0), "statn"), (s.get("actual", 0), "statn"),
            (s.get("diff", 0), "statn"), (s.get("amount", 0), "stat2"),
            (s.get("v_count", 0), "statn")]
    cells = []
    for v, sty in vals:
        cells += [(v, sty), ("", sty)]
    add(cells, 30)
    for i in range(len(vals)):
        span(rr, i * 2 + 1, i * 2 + 2)
    section(L("盘点明细"))
    heads = ["#", L("料号"), L("名称"), L("库位"), L("账面数量"), L("实盘数量"),
             L("差异"), L("实盘料号"), L("单价(元)"), L("差异金额"), L("差异原因"), L("贵重")]
    add([(h, "th") for h in heads], 24)
    for i, it in enumerate(rows_full, 1):
        d = it["diff"] or 0
        dsty = "pos" if d > 0 else ("neg" if d < 0 else "numg")
        add([(i, "tdc"), (it["part_code"], "td"), (it["part_name"], "td"),
             (it["location"] or "—", "td"), (it["book_qty"], "numg"),
             (it["actual_qty"] if it["actual_qty"] is not None else "—", "numg"),
             (d, dsty),
             (("%s → %s" % (L("错料"), it["actual_part_code"] or ""))
              if it["is_wrong"] else "—", "tdc"),
             (it["unit_price"] or 0, "num2"), (it["diff_amt"], "num2"),
             ((L("异常") + " · " if it.get("abnormal") else "")
              + (it["reason_label"] or "—"), "tdc"),
             (L("贵重") if it["is_valuable"] else "—", "tdc")], 21)
    rr = len(R) + 1
    add([(L("合计"), "totl"), ("", "totl"), ("", "totl"), ("", "totl"),
         (s.get("book", 0), "totg"), (s.get("actual", 0), "totg"),
         (s.get("diff", 0), "totg"), ("", "totl"), ("", "totl"),
         (s.get("amount", 0), "tot2"), ("", "totl"), (s.get("v_count", 0), "totg")], 22)
    span(rr, 1, 4)
    flat_heads = ["料号", "名称", "库位", "账面数量", "实盘数量", "差异", "实盘料号",
                  "单价(元)", "差异金额", "差异原因", "贵重",
                  L("原因分析"), L("改善处理"), L("责任人"), L("计划完成日期"), L("来源单据")]
    flat = []
    for it in rows_full:
        # v1.0.74：每次异常压进明细单元格，最终数量仍取最后一次盘点值
        process = "；".join(
            "%s %s %s" % (h.get("source_date") or "", h.get("source_no") or "",
                          h.get("diff") or 0)
            for h in (it.get("hist") or []))
        cause_text = ((L("盘点过程") + "：" + process + "\n") if process else "") \
            + _rowv(it, "cause")
        flat.append([(it["part_code"], "td"), (it["part_name"], "td"),
                     (it["location"] or "", "td"), (it["book_qty"], "numg"),
                     (it["actual_qty"] if it["actual_qty"] is not None else "", "numg"),
                     (it["diff"] or 0, "numg"),
                     ((it["actual_part_code"] or "") if it["is_wrong"] else "", "td"),
                     (it["unit_price"] or 0, "num2"), (it["diff_amt"], "num2"),
                     (it["reason_label"] or "", "tdc"),
                     (L("是") if it["is_valuable"] else L("否"), "tdc"),
                     (cause_text, "td"), (_rowv(it, "action"), "td"),
                     (_rowv(it, "action_owner"), "td"), (_rowv(it, "action_due"), "td"),
                     (it.get("source_no") or "", "td")])
    data_xlsx = _xlsx_styled([
        {"name": L("本期合并报告"), "cols": W, "rows": R, "merges": MG,
         "print": True, "grid": False},
        {"name": L("盘点明细"),
         "cols": [16, 24, 18, 11, 11, 9, 16, 11, 12, 14, 9, 30, 30, 14, 16, 18],
         "rows": [{"cells": [(h, "th") for h in flat_heads], "h": 24}] + flat,
         "print": True},
    ])
    return Response(
        data_xlsx.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 "attachment; filename=stocktake_period_%s.xlsx" % cycle})


# ───────── v1.0.17：逐行差异分析 → 自动汇总 → 改善行动任务 ─────────
# 设计要点：
#  1) 逐行的「原因分析 / 改善处理」是自由文本，和原有的「差异原因码」并存：
#     原因码继续供「按原因汇总」做数字统计，文本供报告结论与任务监控。
#  2) 汇总文本只用序号、料号、库位、±数量、箭头这些**符号与数据**拼接，
#     不拼自然语言前缀后缀 —— 11 语言下拼接式文案必翻车。
#  3) 报告级 cause/action 默认跟着逐行内容自动刷新；一旦人工改写
#     （summary_manual=1）就不再被覆盖，另给「重新汇总」强制还原。
_STK_ITEM_ANA_KEYS = ("cause", "action", "action_owner", "action_due")
_STK_ITEM_FORM_PREFIX = (("icause_", "cause"), ("iaction_", "action"),
                         ("iowner_", "action_owner"), ("idue_", "action_due"))


def _stk_item_is_diff(it):
    """一行是否算「有差异」：数量不符或错料。提交必填 / 汇总 / 任务统一口径。"""
    aq = _rowv(it, "actual_qty", None)
    if aq is not None:
        try:
            if abs(float(aq or 0) - float(_rowv(it, "book_qty", 0) or 0)) > 1e-9:
                return True
        except (TypeError, ValueError):
            pass
    ap = _rowv(it, "actual_part_id", None)
    return bool(ap and ap != _rowv(it, "part_id", None))


def _stk_item_label(it):
    """汇总里的行定位：料号 · 库位（±差异）或（→ 实盘料号）。纯数据，零翻译。"""
    code = (_rowv(it, "part_code") or "").strip() or "-"
    loc = (_rowv(it, "location") or "").strip()
    head = code + (" · " + loc if loc else "")
    ap = _rowv(it, "actual_part_id", None)
    if ap and ap != _rowv(it, "part_id", None):
        tail = "→ " + ((_rowv(it, "actual_part_code") or "").strip() or "?")
    else:
        try:
            tail = "%+g" % float(_rowv(it, "diff", 0) or 0)
        except (TypeError, ValueError):
            tail = "+0"
    return "%s（%s）" % (head, tail)


def _stk_compose_summary(items):
    """把逐行分析去重汇总成 (原因分析, 改善行动) 两段文本。

    同文案归为一条并合并涉及的料号，这才叫「总结」而不是把明细抄一遍；
    改善行动按 (行动, 责任人, 计划完成日) 三元组分组，任务归属不串味。
    v1.0.18 起每条以「问题定位」开头，再写结论，便于先看是哪颗料出了问题。
    """
    cause_groups = {}
    action_groups = {}
    for it in items:
        if not _stk_item_is_diff(it):
            continue
        c = (_rowv(it, "cause") or "").strip()
        a = (_rowv(it, "action") or "").strip()
        if not c and not a:
            continue
        label = _stk_item_label(it)
        if c:
            cause_groups.setdefault(c, []).append(label)
        if a:
            key = (a, (_rowv(it, "action_owner") or "").strip(),
                   (_rowv(it, "action_due") or "").strip())
            action_groups.setdefault(key, []).append(label)

    def _join(groups, with_meta):
        lines = []
        for idx, (key, labels) in enumerate(groups.items(), 1):
            # v1.0.18：问题（料号 · 库位 · ±差异）写在前面，原因分析 / 改善处理
            # 的结论文本跟在后面；改善行动的责任人与计划完成日放最后当补充信息。
            parts = ["、".join(labels)]
            if with_meta:
                text, owner, due = key
                parts.append(text)
                meta = " · ".join(x for x in (owner, due) if x)
                if meta:
                    parts.append(meta)
            else:
                parts.append(key)
            lines.append("%d. %s" % (idx, "  ｜ ".join(parts)))
        return "\n".join(lines)[:4000]

    return _join(cause_groups, False), _join(action_groups, True)


def _stk_item_pairs_from_form(form):
    """提交弹窗 / 报告页表单 → {明细id: {字段: 值}}，别单 id 在写库时再挡一次。"""
    pairs = {}
    for key in form:
        for prefix, field in _STK_ITEM_FORM_PREFIX:
            if not key.startswith(prefix):
                continue
            try:
                iid = int(key[len(prefix):])
            except (TypeError, ValueError):
                break
            pairs.setdefault(iid, {})[field] = form.get(key) or ""
            break
    return pairs


def _stk_save_item_analysis(conn, sid, pairs):
    """写逐行分析。只认本单明细 id；无差异行也允许暂存（汇总时自动跳过）。"""
    if not pairs:
        return 0
    valid = {r["id"] for r in conn.execute(
        "SELECT id FROM stocktake_items WHERE stocktake_id=?", (sid,)).fetchall()}
    hits = 0
    for iid, fields in pairs.items():
        if iid not in valid:
            continue
        sets, vals = [], []
        for k in _STK_ITEM_ANA_KEYS:
            if k not in fields:
                continue
            v = (fields.get(k) or "").strip()
            if k == "action_due" and v and not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
                v = ""
            v = v[:4000] if k in ("cause", "action") else v[:60]
            sets.append("%s=?" % k)
            vals.append(v)
        if not sets:
            continue
        conn.execute("UPDATE stocktake_items SET %s WHERE id=? AND stocktake_id=?"
                     % ",".join(sets), vals + [iid, sid])
        hits += 1
    return hits


def _stk_refresh_summary(conn, sid, force=False):
    """按当前逐行分析重算整单结论，尊重「人工改写」标记。返回 (cause, action)。"""
    st = conn.execute(
        "SELECT cause, action, summary_manual FROM stocktakes WHERE id=?", (sid,)
    ).fetchone()
    if not st:
        return "", ""
    items = conn.execute(
        "SELECT * FROM stocktake_items WHERE stocktake_id=? ORDER BY id", (sid,)
    ).fetchall()
    ca, aa = _stk_compose_summary(items)
    manual = bool(_rowv(st, "summary_manual", 0)) and not force
    if manual:
        # 人工改过：只更新系统快照，页面上的结论保持人写的那份
        conn.execute("UPDATE stocktakes SET cause_auto=?, action_auto=? WHERE id=?",
                     (ca, aa, sid))
        return _rowv(st, "cause"), _rowv(st, "action")
    conn.execute(
        """UPDATE stocktakes SET cause=?, action=?, cause_auto=?, action_auto=?,
           summary_manual=0 WHERE id=?""", (ca, aa, ca, aa, sid))
    return ca, aa


def _stk_action_task_rows(conn, sid=None, only_open=True, kw=""):
    """归档页 / 角标共用的改善行动任务行。口径：已归档单 + 有改善处理文本 + 未完成。"""
    sql = """SELECT i.id item_id, i.stocktake_id, i.part_code, i.part_name, i.location,
                    i.book_qty, i.actual_qty, i.diff, i.cause, i.action,
                    i.action_owner, i.action_due, i.action_status,
                    i.action_done_at, i.action_done_by,
                    s.no st_no, s.updated_at st_done_at, s.created_by_name
             FROM stocktake_items i JOIN stocktakes s ON s.id=i.stocktake_id
             WHERE s.status='done' AND COALESCE(i.action,'')<>''"""
    args = []
    if only_open:
        sql += " AND COALESCE(i.action_status,'open')<>'done'"
    if sid:
        sql += " AND i.stocktake_id=?"
        args.append(sid)
    if kw:
        sql += (" AND (s.no LIKE ? OR i.part_code LIKE ? OR i.part_name LIKE ?"
                " OR i.action LIKE ? OR i.action_owner LIKE ?)")
        args += ["%" + kw + "%"] * 5
    sql += " ORDER BY COALESCE(NULLIF(i.action_due,''),'9999-12-31'), s.id DESC, i.id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


_STK_CYCLE_STATES = ("early", "on_time", "late", "approving", "draft",
                     "in_window", "pending", "missed", "not_due")


def _stk_dt(s):
    """把 'YYYY-MM-DD ...' 解析成 date，失败返回 None（盘点周期里到处要用）。"""
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _stk_period_k(d, anchor, days):
    """v1.0.54：按「离哪个应盘日最近」把日期归到周期序号 k。

    看板柱子、KPI、未盘清单、建单 cycle_key、下次盘点窗口必须共用这一条规则。
    旧实现柱子用 round、当前期用 floor，同一张盘点单会一边算已盘、一边算未盘。
    """
    if d is None:
        return None
    return int(round((d - anchor).days / float(max(1, days))))


def _stk_doc_ref(done_at, created_at, updated_at=None):
    """单据归期用的参考日；过滤明显不可能的历史脏日期。

    现场库里存在 done_at='2000-01-01' 的旧单，照它归期会把看板窗口
    拉到几百期之外（只剩 24 期也被撑满）。只挡两类脏值：
      1) 2001 年以前的占位日期；2) 比建单日早五年以上的日期。
    单纯早几天/几个月的完成日要照常保留（回归里就有回填完成日的场景）。
    """
    dd, cd, ud = _stk_dt(done_at), _stk_dt(created_at), _stk_dt(updated_at)
    if dd is not None:
        try:
            too_old = dd < datetime(2001, 1, 1).date()
        except Exception:
            too_old = False
        way_before = (cd is not None and (cd - dd).days > 365 * 5)
        if too_old or way_before:
            dd = None
    return dd or ud or cd


def _stk_period_docs(conn, cycle_key, days=None, tol=None, anchor=None):
    """v1.0.57：按看板同一套「完成日就近归期」取该期的全部已完成单。

    看板柱子、合并报告、盘点记录的 cycle 筛选必须共用这一条归属规则。
    旧实现看板按完成日归期、记录页按单据原始 cycle_key 精确筛，
    于是「11/20 全盘单原始写着 10/28」会在 10 月节点里冒出来。
    """
    days, tol, anchor_d = _stk_cycle_cfg(conn, days, tol, anchor)
    key_date = _stk_dt(cycle_key)
    if key_date is None:
        return [], None
    target_k = _stk_period_k(key_date, anchor_d, days)
    out = []
    try:
        rows = conn.execute(
            """SELECT s.*, COALESCE(u.display_name,'') created_by_name
               FROM stocktakes s LEFT JOIN users u ON u.id=s.created_by_id
               WHERE s.status='done' ORDER BY s.id"""
        ).fetchall()
    except Exception:
        rows = []
    for r in rows:
        ref = _stk_doc_ref(r["done_at"], r["created_at"], r["updated_at"])
        if ref is None or _stk_period_k(ref, anchor_d, days) != target_k:
            continue
        d = dict(r)
        d["_ref_date"] = ref
        out.append(d)
    out.sort(key=lambda d: (d["_ref_date"],
                            d.get("done_at") or "", d.get("updated_at") or "",
                            d.get("created_at") or "", int(d.get("id") or 0)))
    return out, target_k


def _stk_period_report_data(conn, cycle_key):
    """把同一期的多张完成单合并成一份报告数据（不改动任何原始单据）。

    合并口径（v1.0.74 统一）：
      · 覆盖率：所有完成单按库存行取并集；
      · 明细是「汇总」而不是「逐单罗列」：同一库存行多次盘点只出一行；
        每次都正常 → 取最近一次正常的账实结论；
        只要有一次抓到异常 → 该行状态记为「异常」，实盘数量与差异净额取**最后一次**
        （例：10 → 11 → 10，最终数量 10、状态异常，分析仍保留异常那次的原因）；
      · v1.0.62：同一行多次异常各自留痕（hist），盘亏/盘盈原因不互相抵消；
      · 差异来源单号取异常那一张，最后单据另记 final_no；原始逐单报告一律保留。
    原始单据与逐单报告全部保留，合并页只做只读汇总。
    """
    docs, k = _stk_period_docs(conn, cycle_key)
    if not docs:
        return None
    doc_ids = [int(d["id"]) for d in docs]
    marks = ",".join("?" for _ in doc_ids)
    snap = conn.execute(
        "SELECT total_rows FROM stocktake_cycles WHERE cycle_key=?",
        (cycle_key,)).fetchone()
    try:
        period_total = int(snap["total_rows"] or 0) if snap else 0
    except (TypeError, ValueError):
        period_total = 0

    rows = conn.execute(
        """SELECT it.*, p.unit_price, p.is_valuable, ap.unit_price actual_price
           FROM stocktake_items it
           JOIN parts p ON p.id=it.part_id
           LEFT JOIN parts ap ON ap.id=it.actual_part_id
           WHERE it.stocktake_id IN (%s) AND it.actual_qty IS NOT NULL
           ORDER BY it.stocktake_id, it.id""" % marks, doc_ids).fetchall()
    per_doc = {}
    for r in rows:
        per_doc.setdefault(int(r["stocktake_id"]), []).append(dict(r))
    if period_total <= 0:
        period_total = max([len(v) for v in per_doc.values()] or [0])

    # v1.0.74：一次盘点一个库存行只出一行汇总。
    #   · 全期都正常 → 最后一次正常的账实结论；
    #   · 出现过异常 → 状态「异常」+ 保留每次异常的原因链，但数量/净额取最后一张单
    #     （复盘把数量改回来，不等于这件事没发生过）。
    # 与看板 bad_cells 同源：按 inv_id 精确分组，没有 inv_id 的老单回退到 物料+库位。
    grouped, diff_hist = {}, {}
    for d in docs:
        sid = int(d["id"])
        its = per_doc.get(sid, [])
        date_s = d["_ref_date"].strftime("%Y-%m-%d")
        for it in its:
            iid = it.get("inv_id")
            key = ("inv", iid, it.get("part_id"), it.get("location")) if iid is not None \
                else ("txt", it.get("part_id"), it.get("location"))
            enriched = dict(it)
            enriched["source_sid"] = sid
            enriched["source_no"] = d.get("no") or ""
            enriched["source_date"] = date_s
            grouped.setdefault(key, []).append(enriched)
            if _stk_item_is_diff(it):
                # 每一次异常单独留痕，盘亏/盘盈原因不相互抵消
                h = dict(enriched)
                h["reason_label"] = _stk_reason_label(it.get("reason") or "")
                diff_hist.setdefault(key, []).append(h)

    effective = {}
    for key, occ in grouped.items():
        hist = diff_hist.get(key) or []
        if hist:
            hist.sort(key=lambda r: (r.get("source_date") or "",
                                     int(r.get("source_sid") or 0), int(r.get("id") or 0)))
            merged = dict(occ[-1])                 # 最终数量 / 最终净额
            merged["merged_abnormal"] = 1
            # 原因分析取最后一次异常（最终那张正常单一般没填原因，不能拿它盖掉）
            last = hist[-1]
            for field in ("reason", "cause", "action", "action_owner", "action_due",
                          "action_status", "action_done_by", "action_done_at"):
                merged[field] = last.get(field) or ""
            merged["reason_label"] = last.get("reason_label") or ""
            merged["merged_diff_source"] = last.get("source_no") or ""
            merged["hist"] = hist
        else:
            merged = dict(occ[-1])
            merged["merged_abnormal"] = 0
            merged["hist"] = []
            merged["merged_diff_source"] = ""
        effective[key] = merged

    gross_gain = gross_loss = 0.0
    gross_gain_cnt = gross_loss_cnt = 0
    for key, hist in diff_hist.items():
        hist.sort(key=lambda r: (r.get("source_date") or "",
                                 int(r.get("source_sid") or 0), int(r.get("id") or 0)))
        for h in hist:
            try:
                dv = float(h.get("diff") or 0)
            except (TypeError, ValueError):
                continue
            if dv > 0:
                gross_gain += dv
                gross_gain_cnt += 1
            elif dv < 0:
                gross_loss += -dv
                gross_loss_cnt += 1
    items = [effective.get(key, it) for key, it in grouped.items()]
    items.sort(key=lambda it: (it.get("location") or "", it.get("part_code") or "",
                               it.get("id") or 0))

    book = actual = amount = 0.0
    diff = 0.0
    v_count = 0
    abnormal = 0
    for it in items:
        try:
            book += float(it.get("book_qty") or 0)
        except (TypeError, ValueError):
            pass
        try:
            actual += float(it.get("actual_qty") or 0)
        except (TypeError, ValueError):
            pass
        try:
            diff += float(it.get("diff") or 0)
        except (TypeError, ValueError):
            pass
        amount += float(_stk_item_amount(it) or 0)
        if it.get("merged_abnormal"):
            abnormal += 1
        if (abs(float(it.get("diff") or 0)) > 1e-9 or it.get("merged_abnormal")) \
                and it.get("is_valuable"):
            v_count += 1
    summary = {"book": round(book, 3), "actual": round(actual, 3),
               "diff": round(diff, 3), "amount": round(amount, 2),
               "v_count": v_count, "abnormal": abnormal, "clamped": 0}
    # v1.0.61：差异来源单号（去重、按日期），供报告解释「红柱为什么红」；
    # 最后一张完成单的账实结论用于说明「差异后来是否已账面修正」。
    diff_sources = []
    for it in items:
        if it.get("merged_abnormal") or _stk_item_is_diff(it):
            src = it.get("merged_diff_source") or it.get("source_no") or ""
            if src and src not in diff_sources:
                diff_sources.append(src)
    final_bad = 0
    if docs:
        last_sid = int(docs[-1]["id"])
        final_bad = sum(1 for it in per_doc.get(last_sid, []) if _stk_item_is_diff(it))
    dates = [d["_ref_date"] for d in docs]
    last_date = max(dates) if dates else None
    days, tol, anchor_d = _stk_cycle_cfg(conn)
    nominal = _stk_dt(cycle_key) or anchor_d
    tol_days = int(round(days * tol))
    delay = bool(last_date and nominal and
                 (last_date - nominal).days > tol_days)
    causes, actions, owners, dues, delays = [], [], [], [], []
    for d in docs:
        for text, bucket in ((d.get("cause"), causes), (d.get("action"), actions),
                             (d.get("delay_reason"), delays)):
            text = (text or "").strip()
            if text and text not in bucket:
                bucket.append(text)
    for it in items:
        if (abs(float(it.get("diff") or 0)) < 1e-9 and not _stk_item_is_diff(it)
                and not it.get("merged_abnormal")):
            continue
        for text, bucket in ((it.get("cause"), causes), (it.get("action"), actions)):
            text = (text or "").strip()
            if text and text not in bucket:
                bucket.append(text)
        for field, bucket in (("action_owner", owners), ("action_due", dues)):
            text = (it.get(field) or "").strip()
            if text and text not in bucket:
                bucket.append(text)
    return {
        "cycle_key": cycle_key,
        "docs": docs,
        "items": items,
        "summary": summary,
        "last_date": last_date.strftime("%Y-%m-%d") if last_date else "",
        "from_date": (nominal - timedelta(days=tol_days)).strftime("%Y-%m-%d"),
        "to_date": (nominal + timedelta(days=tol_days)).strftime("%Y-%m-%d"),
        "period_total": period_total,
        "diff_sources": diff_sources,
        # v1.0.62：本期异常累计（含已被后来盘点修正的部分），用于净额红字说明
        "gross_gain": round(gross_gain, 3),
        "gross_loss": round(gross_loss, 3),
        "gross_gain_cnt": gross_gain_cnt,
        "gross_loss_cnt": gross_loss_cnt,
        "repeat_cnt": sum(1 for h in diff_hist.values() if len(h) > 1),
        "final_bad": final_bad,
        "final_no": (docs[-1].get("no") or "") if docs else "",
        "delay": delay,
        "delay_reason": "；".join(delays)[:2000],
        "cause": "；".join(causes)[:4000],
        "action": "；".join(actions)[:4000],
        "owner": "；".join(owners)[:200],
        "due": "；".join(dues)[:200],
    }


def _stk_start_md_date(value, today=None):
    """v1.0.43：把「盘点启始日」M/D 解析成具体年份的日期。

    用户填的是每年重复的月日（默认 1/1），不是某一年的完整日期。
    解析出的日期若晚于今天，自动退回上一年，保证「今天」落在当前期里。
    老数据里若仍存着完整 YYYY-MM-DD，这里也兼容，按原样返回。
    """
    today = today or datetime.now().date()
    s = (value or "").strip()
    if not s:
        s = "1/1"
    parts = re.split(r"[/-]", s)
    try:
        if len(parts) >= 3 and len(parts[0]) == 4:
            return datetime(int(parts[0]), int(parts[1]), int(parts[2])).date()
        m, d = int(parts[0]), int(parts[1])
        out = datetime(today.year, m, d).date()
        if out > today:
            try:
                out = datetime(today.year - 1, m, d).date()
            except ValueError:       # 2/29 遇上平年，退到 2/28
                out = datetime(today.year - 1, m, 28).date()
        return out
    except (TypeError, ValueError, IndexError):
        return datetime(today.year, 1, 1).date()


def _stk_cycle_cfg(conn, days=None, tol=None, anchor=None):
    """盘点周期配置：周期天数、容差比例（±20%）、启始日/锚点日。

    v1.0.43 起以「盘点启始日」(M/D，默认 1/1) 为第一优先；老库里已存的
    完整「周期锚点日」仍兼容，避免升级后历史期次整体漂移。
    显式传入 anchor 的调用（接口/回归）优先级最高。
    """
    cfg = {}
    try:
        cfg = _settings.load() or {}
    except Exception:
        cfg = {}
    if days is None:
        try:
            days = int(cfg.get("stocktake_cycle_days", 30) or 30)
        except (TypeError, ValueError):
            days = 30
    days = days if 1 <= days <= 365 else 30
    if tol is None:
        try:
            tol = float(cfg.get("stocktake_cycle_tolerance", 0.20))
        except (TypeError, ValueError):
            tol = 0.20
    tol = 0.0 if tol < 0 else (0.5 if tol > 0.5 else tol)
    ad = None
    if anchor is not None:
        ad = _stk_dt(anchor)
    else:
        start_md = (cfg.get("stocktake_start_md") or "").strip()
        if start_md:
            ad = _stk_start_md_date(start_md)
        else:
            legacy = (cfg.get("stocktake_cycle_anchor") or "").strip()
            ad = _stk_dt(legacy) if legacy else None
    # v1.0.54：设置页显示「留空默认 1/1」，计算就必须真的按 1/1 走。
    # 旧版留空时偷偷回退到「第一张盘点单建立日」：打开设置页什么都不改、
    # 一点保存就把锚点从 11/18 跳成 1/1，柱子左右横跳、下次窗口报「已超期 143 天」。
    if ad is None:
        ad = _stk_start_md_date("1/1")
    return days, tol, ad


def _stk_live_stock_rows(conn):
    """分母口径：在库库存行（物料 × 库位）。

    不用格位（locations）——空格位在 inventory 里没有行，永远进不了盘点单，
    要统计就得让仓管逐个点「确认空格」，现场不会执行。库存行天然就是盘点明细的一行。
    不过滤 quantity>0：账上 0 的行也可能是实物有货（盘盈来源），必须一起盘；
    而且建单快照就是全量取，分母若单独过滤就会和明细行数对不上。
    """
    try:
        r = conn.execute(
            """SELECT COUNT(*) c, COALESCE(SUM(i.quantity),0) q
               FROM inventory i JOIN parts p ON p.id=i.part_id
               WHERE p.deleted=0""").fetchone()
        return int(r["c"] or 0), float(r["q"] or 0)
    except Exception:
        return 0, 0.0


def _stk_cycle_ensure(conn, cycle_key, days, tol):
    """确保该期在 stocktake_cycles 里有分母快照（幂等）。

    分母必须**快照**：若每次实时 COUNT，日常出入库会让历史柱高来回变，
    同一张报表隔天打开数字不一样，没法复盘也没法考核。
    """
    try:
        row = conn.execute(
            "SELECT total_rows FROM stocktake_cycles WHERE cycle_key=?", (cycle_key,)).fetchone()
        if row and int(row["total_rows"] or 0) > 0:
            return
        base = _stk_dt(cycle_key)
        if base is None:
            return
        td = int(round(days * tol))
        n = _stk_live_stock_rows(conn)
        conn.execute(
            """INSERT INTO stocktake_cycles(cycle_key, due_from, due_to, total_rows, total_qty,
                                            scope_note, created_at, updated_at)
               VALUES(?,?,?,?,?,'',?,?)
               ON CONFLICT(cycle_key) DO UPDATE SET
                 due_from=excluded.due_from, due_to=excluded.due_to,
                 total_rows=CASE WHEN COALESCE(total_rows,0)>0 THEN total_rows ELSE excluded.total_rows END,
                 total_qty=CASE WHEN COALESCE(total_rows,0)>0 THEN total_qty ELSE excluded.total_qty END,
                 updated_at=excluded.updated_at""",
            (cycle_key,
             (base - timedelta(days=td)).strftime("%Y-%m-%d"),
             (base + timedelta(days=td)).strftime("%Y-%m-%d"),
             n[0], n[1], db.now_str(), db.now_str()))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _stk_cycle_series(conn, days=None, nodes=12, tol=None, anchor=None):
    """v1.0.33：盘点周期序列（一个节点两根柱：数量无误 / 数量有误）。

    沿革：
      · v1.0.20 柱高 = 在库覆盖率（已盘库存行 ÷ 在库总行），归属按完成日就近归期。
      · v1.0.33 按草图改双柱：蓝=已盘中数量无误的物料行，红=已盘中数量有误的物料行；
        同一行多单结论冲突时「有误」优先。cycle_key 仍是名义应盘日（快照/明细归属不变），
        另出 plan_base = 链式顺延后的实际计划日。
      · 链式顺延：某期超期完成（晚于其计划窗口）后，后续各期计划日 = 上期最后盘点日 + 周期，
        不再回到固定锚点节奏；shift 只增不回退。
      · 状态八种：提前 / 按期 / 超期 / 审批中 / 草稿 / 窗口内待盘 / 待盘 / 漏盘。
    """
    days, tol, anchor_d = _stk_cycle_cfg(conn, days, tol, anchor)
    today = datetime.now().date()
    tol_days = int(round(days * tol))

    # v1.0.54：当前期与单据归期统一走「就近归期」。旧版这里用 floor、单据用 round，
    # 同一张单在柱子上算已盘、在 KPI / 未盘清单里却算未盘（现场 72 与 0 互相打架）。
    K = _stk_period_k(today, anchor_d, days)
    if K is None or K < 0:
        K = 0
    _n = max(1, int(nodes or 6))
    # v1.0.54：先把所有单据的归属期算出来，窗口必须覆盖到近期每一次盘点——
    # 「近期多次盘点，所有盘点的柱子都要有」，不能因为窗口只留 6 期就把柱子裁掉；
    # 窗口外完成的单据照样按「离哪个期近归哪个期」落进对应柱子。
    doc_ks = set()
    try:
        for _r in conn.execute(
                """SELECT COALESCE(done_at,'') done_at, created_at, updated_at
                   FROM stocktakes WHERE status<>'cancelled'""").fetchall():
            _k = _stk_period_k(
                _stk_doc_ref(_r["done_at"], _r["created_at"], _r["updated_at"]),
                anchor_d, days)
            if _k is not None:
                doc_ks.add(_k)
    except Exception:
        pass
    # 只认近期单据；几百期前的老账不撑爆看板窗口（「近期多次盘点」）
    doc_ks = {k for k in doc_ks if K - 23 <= k <= K + 1}
    # v1.0.47~49：不足 4 期时最左一列钉死启始日，右侧补未来期（现场既定口径，不动）。
    if K < 4:
        k_start = 0
        k_end = k_start + _n - 1
        # v1.0.54：只在确实有晚于窗口的盘点单时向右扩，保证柱子不丢；启始日之前不补空期
        if doc_ks and max(doc_ks) >= k_end:
            k_end = max(doc_ks) + 1
    else:
        k_end = K + 1
        k_start = k_end - _n + 1
        # v1.0.54：当期右数第二列是常规布局；有单据落在窗口外时按就近归期扩窗
        if doc_ks:
            k_end = max(k_end, max(doc_ks) + 1)
            k_start = min(k_start, min(doc_ks))
    if k_end - k_start + 1 > 24:      # 防御：异常老单不能把图画到几百期
        k_start = k_end - 23
    periods = list(range(k_start, k_end + 1))

    # 分母快照
    snaps = {}
    try:
        for r in conn.execute("SELECT * FROM stocktake_cycles").fetchall():
            snaps[r["cycle_key"]] = r
    except Exception:
        snaps = {}
    live_rows, live_qty = _stk_live_stock_rows(conn)
    cur_key = (anchor_d + timedelta(days=K * days)).strftime("%Y-%m-%d")
    if cur_key not in snaps:
        # 当前期第一次打开图表时就把分母钉下来（等价于「本期第一次建单时快照」）
        _stk_cycle_ensure(conn, cur_key, days, tol)
        try:
            r = conn.execute("SELECT * FROM stocktake_cycles WHERE cycle_key=?", (cur_key,)).fetchone()
            if r:
                snaps[cur_key] = r
        except Exception:
            pass

    # v1.0.33：cells 拆成「数量无误 / 数量有误」两套（物料×库位），同一格两单结论冲突时以有误为准；
    # causes/actions 收集该期完成单的原因分析 / 改善行动，供图下方展示。
    buckets = {k: {"docs": [], "cells": set(), "ok_cells": set(), "bad_cells": set(),
                   "diff_rows": 0, "amount": 0.0, "causes": [], "actions": [],
                   "delay_reasons": [], "last_done": None}
               for k in periods}

    def _period_of(d):
        k = _stk_period_k(d, anchor_d, days)
        return k if k in buckets else None

    # 单据：取消的不参与任何统计
    try:
        docs = conn.execute(
            """SELECT s.id, s.no, s.status, COALESCE(s.done_at,'') done_at,
                      COALESCE(s.cycle_key,'') cycle_key, s.created_at, s.updated_at, s.summary,
                      COALESCE(s.cause,'') cause, COALESCE(s.action,'') action,
                      COALESCE(s.delay_reason,'') delay_reason,
                      COALESCE(d.cnt,0) diff_cnt
               FROM stocktakes s
               LEFT JOIN (SELECT stocktake_id, COUNT(*) cnt FROM stocktake_items
                          WHERE ABS(COALESCE(diff,0))>0 OR actual_part_id IS NOT NULL
                          GROUP BY stocktake_id) d ON d.stocktake_id=s.id
               WHERE s.status<>'cancelled' ORDER BY s.id""").fetchall()
    except Exception:
        docs = []
    for d in docs:
        k = _period_of(_stk_doc_ref(d["done_at"], d["created_at"], d["updated_at"]))
        if k is None:
            continue
        buckets[k]["docs"].append(d)
        if d["status"] == "done":
            try:
                amt = json.loads(d["summary"] or "{}").get("amount") or 0
            except Exception:
                amt = 0
            buckets[k]["amount"] += float(amt)
            # v1.0.33：该期「最后盘点日」取已审单最晚完成日（链式顺延的锚），原因/改善按单收集
            dd = _stk_doc_ref(d["done_at"], d["created_at"], d["updated_at"])
            if dd is not None:
                prev = buckets[k]["last_done"]
                if prev is None or dd > prev:
                    buckets[k]["last_done"] = dd
            for txt, key in ((d["cause"], "causes"), (d["action"], "actions"),
                             (d["delay_reason"], "delay_reasons")):
                txt = (txt or "").strip()
                if txt and txt not in buckets[k][key]:
                    buckets[k][key].append(txt)

    # 已盘行：只认已提交/已完成的单（草稿里填了数还没交，不算盘过）
    # v1.0.52：cell 键必须用 inv_id 精确命中，与分母 stocktake_cycles.total_rows
    #（按库存行计）及 _stk_prior_map / _stk_counted_clause 同源。旧实现按
    # (物料,库位文本) 去重，而同一库位会按配件状态分成多行（良品/待维修等），
    # 于是「全盘了」仍显示未盘（实测 70 行被并成 67，凭空多出未盘 3）。
    # 老单据没有 inv_id 时才回退库位文本，兼容历史数据。
    try:
        items = conn.execute(
            """SELECT s.id sid, s.status st, s.done_at s_done,
                      s.updated_at s_upd, s.created_at s_created,
                      si.part_id pid, si.inv_id iid, IFNULL(si.location,'') loc,
                      COALESCE(si.diff,0) diff
               FROM stocktakes s JOIN stocktake_items si ON si.stocktake_id=s.id
               WHERE s.status IN ('pending1','pending2','applying','done')
                 AND si.actual_qty IS NOT NULL""").fetchall()
    except Exception:
        items = []
    for it in items:
        k = _period_of(_stk_doc_ref(it["s_done"], it["s_created"], it["s_upd"]))
        if k is None:
            continue
        cell = ("inv", it["iid"]) if it["iid"] is not None else ("txt", it["pid"], it["loc"])
        buckets[k]["cells"].add(cell)
        # v1.0.33：同一格多单时「有误」优先（曾抓到差异就不能再算无误）
        if abs(float(it["diff"] or 0)) > 1e-9:
            buckets[k]["bad_cells"].add(cell)
            buckets[k]["ok_cells"].discard(cell)
            if it["st"] == "done":
                buckets[k]["diff_rows"] += 1
        else:
            if cell not in buckets[k]["bad_cells"]:
                buckets[k]["ok_cells"].add(cell)

    labels, ratios, counted_l, total_l, states, offsets = [], [], [], [], [], []
    done_dates, diff_rows, amounts, doc_ids, uncovered, estimated = [], [], [], [], [], []
    ok_l, bad_l, plan_bases, causes_l, actions_l = [], [], [], [], []
    timeline = []
    kpi_counted = kpi_total = 0
    next_due = None
    # v1.0.33：链式顺延累计天数。某一期超期完成（超出容差窗口）后，
    # 之后每一期的计划日 = 上一期最后盘点日 + 周期，不再回到固定锚点节奏。
    shift = 0

    for k in periods:
        nominal = anchor_d + timedelta(days=k * days)
        base = nominal + timedelta(days=shift)      # 顺延后的实际计划日（用于显示/窗口/状态）
        key = nominal.strftime("%Y-%m-%d")          # cycle_key 仍是名义日期：快照、明细归属、未盘清单链接都靠它，不能动
        b = buckets[k]
        done_docs = [d for d in b["docs"] if d["status"] == "done"]
        appr_docs = [d for d in b["docs"] if d["status"] in ("pending1", "pending2", "applying")]
        draft_docs = [d for d in b["docs"] if d["status"] == "draft"]

        offset = None
        done_date = ""
        if done_docs:
            # v1.0.55：归期、状态、展示的完成日必须同用 _stk_doc_ref。
            # 旧写法状态用过滤后的参考日、卡片日期却读原始 done_at，现场
            # done_at=2000-01-01 的脏单就出现「提前完成 (-9793d)」，
            # 实际按建单日落在窗口之后，应该是延期/超期。
            # v1.0.57：柱子日期取本期最后一次有效盘点（复盘日），不是最早一张。
            # 现场 11/20 全盘、11/21 复盘，柱子必须落在 11/21；
            # 旧写法取最早一张，柱子和报告日期都停在 11/18。
            last = max(done_docs, key=lambda d: (_stk_doc_ref(
                d["done_at"], d["created_at"], d["updated_at"]) or datetime.min.date()))
            dd = _stk_doc_ref(last["done_at"], last["created_at"], last["updated_at"])
            done_date = dd.strftime("%Y-%m-%d") if dd else ""
            offset = (dd - base).days if dd else 0
            if offset < -tol_days:
                state = "early"
            elif offset > tol_days:
                state = "late"
            else:
                state = "on_time"
        elif appr_docs:
            state = "approving"
        elif draft_docs:
            state = "draft"
        elif today < base - timedelta(days=tol_days):
            # v1.0.55：未来期「未到盘点期」，不能提前记一笔未盘；
            # 只有真正进入盘点窗口仍未盘（in_window/missed）才叫未盘。
            state = "not_due" if k > K else "pending"
        elif today <= base + timedelta(days=tol_days):
            state = "in_window"
        else:
            state = "missed"

        # 本期超期 → 下一期基准 = 本期最后盘点日 + 周期（相对名义日期的偏移即新 shift）
        # shift 只增不回退：后续各期继承，直到某期给出更晚的顺延锚。
        if state == "late" and b["last_done"] is not None:
            new_shift = (b["last_done"] - nominal).days
            if new_shift > shift:
                shift = new_shift

        snap = snaps.get(key)
        if snap is not None and int(snap["total_rows"] or 0) > 0:
            total = int(snap["total_rows"] or 0)
            est = False
        else:
            total = live_rows
            est = True
        counted = len(b["cells"])
        ok_n = len(b["ok_cells"])
        bad_n = len(b["bad_cells"])
        ratio = (float(counted) / float(total)) if total > 0 else None
        cause_txt = "；".join(b["causes"])[:400]
        action_txt = "；".join(b["actions"])[:400]
        delay_txt = "；".join(b["delay_reasons"])[:400]
        # v1.0.67：未盘期没有报告，原因直接记在周期表上，与单据的 delay_reason 分开
        missed_txt = ((snap["missed_reason"] if snap is not None else "") or "").strip()[:400]

        labels.append(base.strftime("%m-%d"))
        ratios.append(None if ratio is None else round(min(1.0, ratio), 4))
        counted_l.append(counted)
        total_l.append(total)
        states.append(state)
        offsets.append(offset)
        done_dates.append(done_date)
        diff_rows.append(b["diff_rows"])
        amounts.append(round(b["amount"], 2))
        doc_ids.append([d["id"] for d in b["docs"]])
        # 未来期还没到，不存在「未盘点」，只有到期未盘才算
        uncovered.append(0 if state == "not_due" else max(0, total - counted))
        estimated.append(est)
        ok_l.append(ok_n)
        bad_l.append(bad_n)
        plan_bases.append(base.strftime("%Y-%m-%d"))
        causes_l.append(cause_txt)
        actions_l.append(action_txt)
        timeline.append({
            "cycle_key": key,
            "base": key,
            "plan_base": base.strftime("%Y-%m-%d"),
            "shift_days": shift,
            "from": (base - timedelta(days=tol_days)).strftime("%Y-%m-%d"),
            "to": (base + timedelta(days=tol_days)).strftime("%Y-%m-%d"),
            "label": base.strftime("%m-%d"),
            "state": state,
            "done_date": done_date,
            # v1.0.55：柱子画在实际盘点日；未盘期仍画在计划日
            "bar_date": done_date or base.strftime("%Y-%m-%d"),
            "offset": offset,
            "counted": counted,
            "total": total,
            "uncovered": 0 if state == "not_due" else max(0, total - counted),
            "ok": ok_n,
            "bad": bad_n,
            "cause": cause_txt,
            "action": action_txt,
            "delay_reason": delay_txt,
            "missed_reason": missed_txt,
            "last_done": b["last_done"].strftime("%Y-%m-%d") if b["last_done"] else "",
            "ratio": None if ratio is None else round(min(1.0, ratio), 4),
            "diff_rows": b["diff_rows"],
            "amount": round(b["amount"], 2),
            "docs": [{"id": d["id"], "no": d["no"], "status": d["status"],
                      "diff_cnt": int(d["diff_cnt"] or 0)} for d in b["docs"]],
            "estimated": est,
            "current": k == K,
        })

        if k == K:
            kpi_counted, kpi_total = counted, total

    # v1.0.54：下次窗口只认「当前期 / 未来期里还没盘完的那一期」。
    # 旧写法从最左节点顺着找第一个 pending/missed，启始日一变就把半年前
    # 的漏盘点当成「下次盘点窗口」，还给出「已超期 143 天」这种荒唐提示。
    for kk, n in zip(periods, timeline):
        if kk < K or n["state"] not in ("pending", "in_window", "draft", "approving"):
            continue
        next_due = {
            "cycle_key": n["cycle_key"], "base": n["plan_base"],
            "from": n["from"], "to": n["to"],
            "left_days": (_stk_dt(n["plan_base"]) - today).days,
            "state": n["state"],
        }
        break

    # 当期及可见期都已盘完 → 下次窗口是下一期（同样套用链式顺延）
    if next_due is None and periods:
        nb_nominal = anchor_d + timedelta(days=(K + 1) * days)
        nb = nb_nominal + timedelta(days=shift)
        next_due = {
            "cycle_key": nb_nominal.strftime("%Y-%m-%d"), "base": nb.strftime("%Y-%m-%d"),
            "from": (nb - timedelta(days=tol_days)).strftime("%Y-%m-%d"),
            "to": (nb + timedelta(days=tol_days)).strftime("%Y-%m-%d"),
            "left_days": (nb - today).days, "state": "pending",
        }

    missed_total = sum(1 for s in states if s == "missed")
    done_total = sum(1 for s in states if s in ("early", "on_time", "late"))
    return {
        # 新口径
        "labels": labels,
        "ratio": ratios,
        "counted": counted_l,
        "total": total_l,
        "state": states,
        "offset": offsets,
        "done_date": done_dates,
        "diff_rows": diff_rows,
        "loss_amount": amounts,
        "doc_ids": doc_ids,
        "uncovered": uncovered,
        "estimated": estimated,
        # v1.0.33 双柱 + 链式顺延 + 原因
        "ok": ok_l,
        "bad": bad_l,
        "plan_bases": plan_bases,
        "causes": causes_l,
        "actions": actions_l,
        "timeline": timeline,
        "days": days,
        "tolerance": tol,
        "tol_days": tol_days,
        "anchor": anchor_d.strftime("%Y-%m-%d"),
        "nodes": len(periods),
        "next_due": next_due,
        "kpi": {
            "total": kpi_total, "counted": kpi_counted,
            "uncovered": max(0, kpi_total - kpi_counted),
            # v1.0.73：分子按已提交单的库存行 inv_id 去重，包含盘点后已出库/调走的行；
            # 分母是本期在库行快照。库存中途增减时分子可大于分母，柱子早就按 100% 封顶，
            # KPI 也封顶，避免出现「在库 77 / 已盘 80 / 覆盖率 104%」这种自相矛盾的展示。
            "ratio": (round(min(1.0, float(kpi_counted) / kpi_total), 4) if kpi_total > 0 else None),
            "live_rows": live_rows, "live_qty": round(live_qty, 3),
        },
        # 兼容 v1.0.18 字段（模板改完后可删）
        "planned": [1] * len(labels),
        "completed": counted_l,
        "missed": [1 if s == "missed" else 0 for s in states],
        "planned_total": len(labels),
        "completed_total": done_total,
        "missed_total": missed_total,
    }


def _stk_task_monitor(conn, kw="", only_overdue=False, show_done=False):
    """v1.0.18：改善行动任务监控数据（盘点管理页 / 侧栏角标同源）。

    从归档页抽出来成为公共函数：统计卡 + 按盘点单分组的列表。
    """
    all_tasks = _stk_action_task_rows(conn, only_open=False)
    today = db.now_str()[:10]
    for t in all_tasks:
        t["is_done"] = (t.get("action_status") or "open") == "done"
        t["overdue"] = bool(not t["is_done"] and t.get("action_due")
                            and t["action_due"] < today)
    open_tasks = [t for t in all_tasks if not t["is_done"]]
    done_cnt = len(all_tasks) - len(open_tasks)
    overdue_cnt = sum(1 for t in open_tasks if t["overdue"])
    # v1.0.62：show_done 表示「也显示已完成」，不再等于「只看已完成」
    view = all_tasks if show_done else open_tasks
    if only_overdue:
        view = [t for t in view if t["overdue"]]
    if kw:
        low = kw.lower()
        view = [t for t in view
                if any(low in str(t.get(f) or "").lower()
                       for f in ("st_no", "part_code", "part_name", "action", "action_owner"))]
    groups = {}
    for t in view:
        g = groups.setdefault(t["st_no"],
                              {"no": t["st_no"], "by": t.get("created_by_name") or "",
                               "rows": []})
        g["rows"].append(t)
    return {"task_groups": list(groups.values()),
            "task_open": len(open_tasks), "task_done": done_cnt,
            "task_overdue": overdue_cnt, "today": today,
            "only_overdue": only_overdue, "show_done": show_done}


def _stk_save_reason_codes(conn, sid, form, valid_ids=None):
    """只写逐行差异原因码（reason_<明细id>）。提交校验前与报告保存共用同一口径。

    原因码白名单不要信任前端；别单的明细 id 直接忽略。返回实际变更条数。
    """
    hits = 0
    for key in form:
        if not key.startswith("reason_"):
            continue
        try:
            iid = int(key[7:])
        except (TypeError, ValueError):
            continue
        if valid_ids is not None and iid not in valid_ids:
            continue
        code = (form.get(key) or "").strip()
        if code and code not in _STK_REASON_CODES:
            code = ""
        it = conn.execute(
            "SELECT id, reason FROM stocktake_items WHERE id=? AND stocktake_id=?",
            (iid, sid)).fetchone()
        if it and (it["reason"] or "") != code:
            conn.execute("UPDATE stocktake_items SET reason=? WHERE id=?", (code, iid))
            hits += 1
    return hits


def _stk_save_analysis_form(conn, sid, user, form, items=None, compose=False,
                            allow_item_edit=True):
    """差异分析保存（提交审批 + 报告页共用）：整单四字段 + 逐行原因码 + 逐行分析文本。

    只接受白名单原因码；别单的明细直接忽略。返回 (cause, action, owner, due, hits)，
    供调用方写审计。v1.0.15 起提交审批就调用一次，审批人当场能看到结论。
    v1.0.17：
      compose=True（提交场景）——整单 cause/action 交给 _stk_refresh_summary 自动汇总，
        这里只落 owner/due/analyzed_* 与逐行字段，不覆盖自动结论；
      compose=False（报告页场景）——整单以表单为准，与最新系统汇总不一致即标记
        summary_manual=1，此后逐行变更不再冲掉人工结论。
    v1.0.18：
      allow_item_edit=False（报告页，单据已审批）——逐行原因码与逐行分析全部只读，
        只让盘点人补充整单结论；直接伪造 POST 也在后端被忽略。
    """
    cause = (form.get("cause") or "").strip()[:4000]
    action = (form.get("action") or "").strip()[:4000]
    owner = (form.get("action_owner") or "").strip()[:60]
    due = (form.get("action_due") or "").strip()[:10]
    if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        due = ""
    # v1.0.41：延期原因说明是报告页独立字段。只有表单真的带了这个键才写，
    # 否则（提交弹窗/旧客户端/非延期期）绝不把已填的说明清掉。
    delay_reason = None
    if "delay_reason" in form:
        delay_reason = (form.get("delay_reason") or "").strip()[:2000]
        try:
            conn.execute("UPDATE stocktakes SET delay_reason=?, updated_at=? WHERE id=?",
                         (delay_reason, db.now_str(), sid))
        except Exception:
            # 老库还没跑迁移时不能让整份报告保存失败
            delay_reason = None
    valid_ids = {it["id"] for it in items} if items is not None else None
    # 已审批的报告页逐行明细锁定：整单 cause/action 仍可由盘点人补充，
    # 但任何伪造的 reason_ / icause_ / iaction_ / iowner_ / idue_ 一律忽略。
    # v1.0.72：原因码保存抽成共用函数，提交校验前也要先落库。
    hits = _stk_save_reason_codes(conn, sid, form if allow_item_edit else (), valid_ids)
    # v1.0.17：逐行「原因分析 / 改善处理 / 责任人 / 计划完成日」随表单一起落库。
    # v1.0.18：报告页（已审批）不允许再改逐行，只有提交弹窗/草稿阶段才写。
    if allow_item_edit:
        hits += _stk_save_item_analysis(conn, sid, _stk_item_pairs_from_form(form))
    who = user.get("display_name") or user.get("username") or ""
    if compose:
        # cause/action 由调用方先跑 _stk_refresh_summary 写好，这里绝不覆盖。
        # v1.0.18：提交弹窗已去掉整单责任人/日期，没带这两个键时保留库里原值，
        # 免得驳回后重新提交把盘点人之前补的整单元数据清掉。
        if "action_owner" in form or "action_due" in form:
            conn.execute(
                """UPDATE stocktakes SET action_owner=?, action_due=?,
                   analyzed_at=?, analyzed_by=?, updated_at=? WHERE id=?""",
                (owner, due, db.now_str(), who, db.now_str(), sid))
        else:
            conn.execute(
                """UPDATE stocktakes SET analyzed_at=?, analyzed_by=?, updated_at=? WHERE id=?""",
                (db.now_str(), who, db.now_str(), sid))
        cur = conn.execute("SELECT cause, action FROM stocktakes WHERE id=?", (sid,)).fetchone()
        cause = _rowv(cur, "cause") if cur else cause
        action = _rowv(cur, "action") if cur else action
    else:
        fresh = conn.execute(
            "SELECT * FROM stocktake_items WHERE stocktake_id=? ORDER BY id", (sid,)
        ).fetchall()
        ca, aa = _stk_compose_summary(fresh)
        manual = 1 if (cause != ca or action != aa) else 0
        conn.execute(
            """UPDATE stocktakes SET cause=?, action=?, cause_auto=?, action_auto=?,
               summary_manual=?, action_owner=?, action_due=?,
               analyzed_at=?, analyzed_by=?, updated_at=? WHERE id=?""",
            (cause, action, ca, aa, manual, owner, due, db.now_str(), who, db.now_str(), sid))
    return cause, action, owner, due, hits


@app.route("/stocktakes/<int:sid>/analysis", methods=["POST"])
@login_required
@perm_required("stocktake")
def stocktake_analysis(sid):
    """保存盘点报告的原因分析与改善行动（报告级 + 明细逐行归因）。

    表单直接提交（无 JS 也能用）：文本域 + 每行一个 reason_<明细id> 下拉。
    只接受白名单里的原因码，未知值一律清空 —— 这个字段会打进纸质凭证，
    不能让脏值漏进去。
    """
    lang = _i18n.current_lang()
    conn = get_db()
    try:
        user = current_user()
        st = conn.execute(
            """SELECT s.*, u.department created_dep FROM stocktakes s
               LEFT JOIN users u ON u.id=s.created_by_id WHERE s.id=?""", (sid,),
        ).fetchone()
        if not st:
            abort(404)
        if not _stk_can_view(user, st):
            abort(403)
        # v1.0.62：创建人可写整单；未结束改善任务的责任人可改写**本行**原因与改善。
        # 直接 POST 也按同样的白名单挡，伪造别人行的字段在下面过滤掉。
        _uname = (user.get("display_name") or user.get("username") or "").strip()
        _creator = _stk_can_analyze(user, st)
        _open_rows = conn.execute(
            """SELECT id, action_owner FROM stocktake_items WHERE stocktake_id=?
               AND COALESCE(action,'')<>'' AND COALESCE(action_status,'open')<>'done'""",
            (sid,)).fetchall()
        _allowed = set()
        for _r in _open_rows:
            if _creator or (_uname and (_r["action_owner"] or "").strip() == _uname):
                _allowed.add(int(_r["id"]))
        _item_prefixes = tuple(p for p, _ in _STK_ITEM_FORM_PREFIX) + ("reason_",)
        _has_item_edit = any(k.startswith(_item_prefixes) for k in request.form.keys())
        # 责任人只能改自己那几行；只伪造整单字段（cause/action）仍按越权拒绝
        if not _creator and (not _allowed or not _has_item_edit):
            flash(_i18n.t("只有盘点单创建人或未结束任务的责任人可以填写原因分析", lang), "warning")
            return redirect(url_for("stocktake_report", sid=sid))
        if st["status"] != "done":
            flash(_i18n.t("盘点完成后才能填写原因分析", lang), "warning")
            return redirect(url_for("stocktake_detail", sid=sid))
        # v1.0.18：报告页是已审批单据，逐行明细一律锁定，只有盘点人能补充整单结论。
        # 前端不渲染逐行输入框，这里也不再接受伪造的逐行字段。
        if (request.form.get("op") or "").strip() == "resummarize" and st["status"] != "done":
            _stk_refresh_summary(conn, sid, force=True)
            _audit("stocktake", sid, st["no"], "analyze",
                   detail={"resummarize": True}, conn=conn)
            conn.commit()
            flash(_i18n.t("已按逐行分析重新汇总", lang), "success")
        else:
            # 判定「人工改写」要拿表单值与**保存前**的系统快照比：
            # 相等 = 用户没动整单字段，逐行分析变了就让它自动跟随新汇总；
            # 不等 = 人工修订，保留人写的那份（_stk_save_analysis_form 会打标记）。
            # 以前这里用 summary_manual 判断，导致报告页第一次人工改写永远被冲掉。
            fresh = conn.execute(
                "SELECT * FROM stocktake_items WHERE stocktake_id=? ORDER BY id", (sid,)
            ).fetchall()
            ca, aa = _stk_compose_summary(fresh)
            form = dict(request.form)
            # v1.0.56：已完成审批的报告，整单原因分析/改善行动/责任人/日期一律不可改。
            # 前端已撤掉录入框；这里再把伪造 POST 的这四个字段强制写回库中原值，
            # 只有「超期原因备注」delay_reason 允许继续维护。
            for _lock in ("cause", "action", "action_owner", "action_due"):
                form[_lock] = _rowv(st, _lock)
            if (form.get("cause") or "").strip() == (_rowv(st, "cause_auto") or "").strip():
                form["cause"] = ca
            if (form.get("action") or "").strip() == (_rowv(st, "action_auto") or "").strip():
                form["action"] = aa
            # v1.0.62：逐行字段只放行「本人负责的未结束任务」，其余一律不入库
            # 报告页只开放「原因 / 改善行动 / 原因码」；责任人与计划日期是派工信息，
            # 不随这次改写变动，仍按原值锁定（伪造 iowner_/idue_ 一律丢弃）。
            _report_editable = ("icause_", "iaction_", "reason_")
            edit_form = {k: v for k, v in form.items() if not k.startswith(_item_prefixes)}
            for _k, _v in form.items():
                for _p in _item_prefixes:
                    if not _k.startswith(_p):
                        continue
                    try:
                        _iid = int(_k[len(_p):])
                    except (TypeError, ValueError):
                        break
                    if _iid in _allowed and _p in _report_editable:
                        edit_form[_k] = _v
                    break
            cause, action, owner, due, reason_hits = _stk_save_analysis_form(
                conn, sid, user, edit_form, allow_item_edit=bool(_allowed))
            _audit("stocktake", sid, st["no"], "analyze",
                   detail={"cause": bool(cause), "action": bool(action),
                           "owner": owner, "due": due, "reasons": reason_hits,
                           "item_edit": len(_allowed),
                           "delay_reason": "delay_reason" in form}, conn=conn)
            conn.commit()
            if not reason_hits and "delay_reason" in request.form:
                flash(_i18n.t("超期原因备注已保存", lang), "success")
            else:
                flash(_i18n.t("原因分析与改善行动已保存", lang), "success")
    finally:
        conn.close()
    return redirect(url_for("stocktake_report", sid=sid))


@app.route("/stocktakes/reports")
@login_required
@perm_required("stocktake")
def stocktake_reports():
    """盘点记录（v1.0.33）：全状态盘点单列表 + 已完成报告清单同页。

    管理页只保留看板图与任务监控；盘点单列表（含草稿/在途/已完成）搬到这里。
    报告查看/导出仍统一收在本页；可见范围用 _stk_visible_cond 与管理页同源。
    """
    kw = request.args.get("kw", "").strip()
    # v1.0.51：盘点图柱子点击带 cycle 进来，按应盘日筛该期盘点单/报告
    cycle = request.args.get("cycle", "").strip()
    # v1.0.33：盘点单列表从管理页搬来，状态筛选参数随行搬过来（st）
    st = request.args.get("st", "").strip()
    only_missing = request.args.get("missing", "").strip() in ("1", "on")
    only_tasks = request.args.get("tasks", "").strip() in ("1", "on")
    only_overdue = request.args.get("overdue", "").strip() in ("1", "on")
    show_done = request.args.get("done", "").strip() in ("1", "on")
    conn = get_db()
    try:
        user = current_user()
        # v1.0.57：cycle 筛选与看板归期同源。旧写法按单据原始 cycle_key 精确匹配，
        # 11/20 完成的单即使原始写着 10/28，也会在 10 月节点里露出。
        cycle_ids = []
        if cycle:
            _docs, _ = _stk_period_docs(conn, cycle)
            cycle_ids = [int(d["id"]) for d in _docs]
        sql = """SELECT s.*,
                        (SELECT COUNT(*) FROM stocktake_items i
                          WHERE i.stocktake_id=s.id) item_cnt,
                        (SELECT COUNT(*) FROM stocktake_items i
                          WHERE i.stocktake_id=s.id
                            AND ((i.diff IS NOT NULL AND i.diff<>0)
                                 OR (i.actual_part_id IS NOT NULL AND i.actual_part_id<>i.part_id))) diff_cnt,
                        (SELECT COUNT(*) FROM stocktake_items i
                          WHERE i.stocktake_id=s.id AND i.reason IS NOT NULL AND i.reason<>'') reason_cnt
                 FROM stocktakes s LEFT JOIN users u ON u.id=s.created_by_id"""
        conds, args = ["s.status='done'"], []
        if cycle:
            conds.append("s.id IN (%s)" % (
                ",".join("?" for _ in cycle_ids) if cycle_ids else "0"))
            args.extend(cycle_ids)
        # 归档 = 全公司已完成盘点的历史结论，不再按「本人/本部门/审批人」过滤：
        # 以前这里套了 _stk_visible_cond，结果仓管打开归档只看得到自己那几张，
        # 同事的盘点单既不在列表里、点进去还 500（Row.get 的锅，另一处已修）。
        if kw:
            conds.append("(s.no LIKE ? OR s.scope_text LIKE ? OR s.created_by_name LIKE ?)")
            args += ["%" + kw + "%"] * 3
        if only_missing:
            conds.append("(s.cause IS NULL OR s.cause='')")
        _TASK_OPEN = ("EXISTS (SELECT 1 FROM stocktake_items i2 WHERE i2.stocktake_id=s.id"
                      " AND COALESCE(i2.action,'')<>''"
                      " AND COALESCE(i2.action_status,'open')<>'done')")
        if only_tasks:
            conds.append(_TASK_OPEN)
        if only_overdue:
            conds.append("EXISTS (SELECT 1 FROM stocktake_items i3 WHERE i3.stocktake_id=s.id"
                         " AND COALESCE(i3.action,'')<>''"
                         " AND COALESCE(i3.action_status,'open')<>'done'"
                         " AND i3.action_due<>'' AND i3.action_due<?)")
            args.append(db.now_str()[:10])
        sql += " WHERE " + " AND ".join(conds) + " ORDER BY s.updated_at DESC, s.id DESC"
        rows = conn.execute(sql, args).fetchall()
        # v1.0.18：任务监控已移到盘点管理页；这里只保留统计数字供角标/兼容，
        # 模板不再渲染任务表。URL 上的 tasks/overdue/done 参数仍可解析，不报错。
        monitor = _stk_task_monitor(conn, only_overdue=only_overdue,
                                    show_done=show_done)
        # v1.0.33：盘点单列表（全状态）从管理页搬来，与报告清单同页。
        # 可见范围口径与管理页同源 _stk_visible_cond，避免仓管看到别人部门的单。
        lsql = """SELECT s.*,
                         COALESCE(u1.display_name,'') approver1_name,
                         COALESCE(u2.display_name,'') approver2_name,
                         (SELECT COUNT(*) FROM stocktake_items i WHERE i.stocktake_id=s.id) item_cnt,
                         (SELECT COUNT(*) FROM stocktake_items i WHERE i.stocktake_id=s.id AND i.actual_qty IS NOT NULL) done_cnt,
                         (SELECT COUNT(*) FROM stocktake_items i WHERE i.stocktake_id=s.id
                           AND ((i.diff IS NOT NULL AND i.diff<>0)
                                OR (i.actual_part_id IS NOT NULL AND i.actual_part_id<>i.part_id))) diff_cnt
                  FROM stocktakes s
                  LEFT JOIN users u ON u.id=s.created_by_id
                  LEFT JOIN users u1 ON u1.id=s.approver1_id
                  LEFT JOIN users u2 ON u2.id=s.approver2_id"""
        lconds, largs = [], []
        lvis, lvis_args = _stk_visible_cond(user)
        if lvis:
            lconds.append(lvis); largs += lvis_args
        if st:
            lconds.append("s.status=?"); largs.append(st)
        if cycle:
            lconds.append("s.id IN (%s)" % (
                ",".join("?" for _ in cycle_ids) if cycle_ids else "0"))
            largs.extend(cycle_ids)
        if kw:
            lconds.append("(s.no LIKE ? OR s.scope_text LIKE ? OR s.created_by_name LIKE ?)")
            largs += ["%" + kw + "%"] * 3
        if lconds:
            lsql += " WHERE " + " AND ".join(lconds)
        lsql += " ORDER BY s.id DESC"
        list_rows = conn.execute(lsql, largs).fetchall()
        try:
            _cdays, _ctol, _ = _stk_cycle_cfg(conn)
        except Exception:
            _cdays, _ctol = 30, 0.2
    finally:
        conn.close()

    # v1.0.62：状态列旁的红/黄标 —— 有差异=红，未在计划时间内=黄。
    # 归期口径与看板/合并报告同源，不按单据原始 cycle_key 直接相减。
    list_out = []
    _limit_days = int(round(_cdays * _ctol))
    for r in list_rows:
        d = dict(r)
        d["_diff_cnt"] = int(d.get("diff_cnt") or 0)
        d["_late_days"] = d["_early_days"] = 0
        if d.get("status") == "done":
            _ref = _stk_doc_ref(d.get("done_at"), d.get("created_at"), d.get("updated_at"))
            _nominal = _stk_dt(d.get("cycle_key"))
            if _ref and _nominal:
                _delta = (_ref - _nominal).days
                if _delta > _limit_days:
                    d["_late_days"] = _delta - _limit_days
                elif _delta < -_limit_days:
                    d["_early_days"] = -_limit_days - _delta
        list_out.append(d)

    out = []
    total_amt = 0.0
    missing = 0
    for r in rows:
        d = dict(r)
        try:
            summ = json.loads(r["summary"]) if r["summary"] else {}
        except Exception:
            summ = {}
        d["diff_amt"] = float(summ.get("amount") or 0)
        d["analyzed"] = bool(_rowv(r, "cause").strip())
        total_amt += d["diff_amt"]
        if not d["analyzed"] and (r["diff_cnt"] or 0):
            missing += 1
        out.append(d)
    # v1.0.18：改善行动任务监控在盘点管理页；v1.0.33：盘点单列表与报告清单同页
    ctx = dict(rows=out, list_rows=list_out, kw=kw, st=st, cycle=cycle,
               only_missing=only_missing,
               total=len(out), missing=missing, total_amt=total_amt,
               only_tasks=only_tasks, today=monitor["today"])
    ctx.update(monitor)
    return render_template("stocktake_reports.html", **ctx)


@app.route("/stocktakes/action-tasks/<int:iid>", methods=["POST"])
@login_required
@perm_required("stocktake")
def stocktake_action_task(iid):
    """改善行动任务：标记完成 / 撤销完成。留痕到 audit_log，可追溯谁关的。"""
    lang = _i18n.current_lang()
    undo = request.form.get("undo", "").strip() in ("1", "on")
    conn = get_db()
    try:
        user = current_user()
        row = conn.execute(
            """SELECT i.id, i.stocktake_id, i.part_code, i.action, s.no, s.status
               FROM stocktake_items i JOIN stocktakes s ON s.id=i.stocktake_id
               WHERE i.id=?""", (iid,)).fetchone()
        if not row or row["status"] != "done" or not (row["action"] or "").strip():
            abort(404)
        if undo:
            conn.execute(
                """UPDATE stocktake_items SET action_status='open',
                   action_done_at='', action_done_by='' WHERE id=?""", (iid,))
        else:
            conn.execute(
                """UPDATE stocktake_items SET action_status='done',
                   action_done_at=?, action_done_by=? WHERE id=?""",
                (db.now_str(), user.get("display_name") or user.get("username") or "", iid))
        _audit("stocktake", row["stocktake_id"], row["no"],
               "action_done" if not undo else "action_undo",
               detail={"item_id": iid, "part": row["part_code"] or ""}, conn=conn)
        conn.commit()
        flash(_i18n.t("已撤销完成标记", lang) if undo
              else _i18n.t("改善行动已标记完成", lang), "success")
    finally:
        conn.close()
    # v1.0.18：任务监控移到盘点管理页，标记完成后要跳回原来的页面，
    # 否则从盘点管理页点一下就被弹到盘点记录页。
    # v1.0.40：操作入口收口到盘点报告页（管理页那张表只读），要跳回同一张报告。
    if request.form.get("from", "").strip() == "report":
        try:
            _sid = int(request.form.get("sid") or 0)
        except (TypeError, ValueError):
            _sid = 0
        if _sid:
            return redirect(url_for("stocktake_report", sid=_sid))
    if request.form.get("from", "").strip() == "manage":
        args = {}
        if request.form.get("task_overdue", ""):
            args["task_overdue"] = "1"
        if request.form.get("task_done", ""):
            args["task_done"] = "1"
        return redirect(url_for("stocktakes_page", **args))
    args = {}
    for k in ("kw", "tasks", "overdue", "done"):
        v = request.form.get(k, "")
        if v:
            args[k] = v
    return redirect(url_for("stocktake_reports", **args))


@app.route("/stocktakes/<int:sid>/export")
@login_required
@perm_required("stocktake", "export")
def stocktake_export(sid):
    """导出盘点报告 Excel —— 版式与报告页一致（抬头 / 基本info / 结论 / 归因 / 明细 / 分析 / 签署）。

    以前这里只把明细和一段「项目-内容」倒进两张裸表：没有抬头、没有分区、
    没有边框，主管拿到后还得自己排版。现在改成「页面上长什么样，表就长什么样」，
    数字用同一份 _stk_diff_summary 算，页面与 Excel 的合计永远对得上。
    第二张表保留扁平明细数据，方便再做透视或导入别的系统。
    """
    conn = get_db()
    try:
        st = conn.execute("SELECT * FROM stocktakes WHERE id=?", (sid,)).fetchone()
        if not st:
            abort(404)
        items = conn.execute(
            """SELECT it.*, p.unit_price, p.is_valuable, ap.unit_price actual_price
               FROM stocktake_items it
               JOIN parts p ON p.id=it.part_id
               LEFT JOIN parts ap ON ap.id=it.actual_part_id
               WHERE it.stocktake_id=? ORDER BY it.id""", (sid,),
        ).fetchall()
        ap1 = ap2 = ""
        for _field, _target in (("approver1_id", "ap1"), ("approver2_id", "ap2")):
            _aid = st[_field] if _field in st.keys() else None
            if _aid:
                _u = conn.execute("SELECT display_name FROM users WHERE id=?",
                                  (_aid,)).fetchone()
                _nm = _u["display_name"] if _u else ""
                if _target == "ap1":
                    ap1 = _nm
                else:
                    ap2 = _nm
    finally:
        conn.close()

    lang = _i18n.current_lang()
    L = lambda s: _xl(s, lang)                      # noqa: E731  导出文案统一走当前语言
    rows_full, stats, reason_rows = _stk_diff_summary(items)
    try:
        summ = json.loads(st["summary"]) if st["summary"] else {}
    except Exception:
        summ = {}
    scope_disp = _stk_scope_display(st)
    printed = db.now_str()
    _at = _rowv(st, "analyzed_at")
    _cause = _rowv(st, "cause")
    _action = _rowv(st, "action")
    _owner = _rowv(st, "action_owner")
    _due = _rowv(st, "action_due")
    _by = _rowv(st, "analyzed_by")
    _prog = "%s / %s %s" % (stats["done"], stats["total"], L("条"))
    _lvl = L("两级审批") if int(_rowv(st, "review_level") or 1) == 2 else L("一级审批")
    _appr = " / ".join(x for x in (
        ("%s %s" % (L("一级"), ap1)) if ap1 else "",
        ("%s %s" % (L("二级"), ap2)) if ap2 else "") if x) or "—"

    # ── 表格骨架：12 列，列宽按内容定（料号/名称/库位/实盘料号要宽） ──
    W = [6, 15, 22, 22, 10, 10, 9, 17, 10, 12, 13, 9]
    R = []          # 每行 {"h":.., "cells":[(值,样式)]}
    MG = []         # 合并单元格

    def add(cells, h=None):
        R.append({"h": h, "cells": cells})

    def span(r, c1, c2):
        """A1 记法：第 r 行第 c1..c2 列合并"""
        col = lambda n: chr(64 + n) if n <= 26 else "A"   # noqa: E731  最多 L 列
        MG.append("%s%d:%s%d" % (col(c1), r, col(c2), r))

    # 抬头
    add([(L("盘点报告") + "  " + st["no"], "title")] + [("", "def")] * 11, 30)
    span(1, 1, 8)
    R[0]["cells"][8] = (L("制表时间") + "：" + printed, "sub")
    for i in range(9, 11):
        R[0]["cells"][i] = ("", "sub")
    span(1, 9, 12)
    add([("", "def")], 6)

    # 基本信息：两栏四行（与页面 rpt-meta 的表头/值同一套底色）
    meta = [(L("盘点单号"), st["no"], L("盘点范围"), scope_disp),
            (L("创建人"), st["created_by_name"] or "—", L("创建时间"), st["created_at"] or "—"),
            (L("完成时间"), st["updated_at"] or "—", L("审批人"), _appr),
            (L("盘点进度"), _prog, L("审批级别"), _lvl)]
    r = len(R) + 1
    for a, b, c, d in meta:
        add([(a, "mlab"), ("", "mlab"), (b, "mval"), ("", "mval"), ("", "mval"),
             ("", "mval"), (c, "mlab"), ("", "mlab"), (d, "mval"), ("", "mval"),
             ("", "mval"), ("", "mval")], 20)
        span(r, 1, 2); span(r, 3, 6); span(r, 7, 8); span(r, 9, 12)
        r += 1

    add([("", "def")], 8)

    def section(title):
        """分区标题条：深底白字，跨满 12 列"""
        rr = len(R) + 1
        add([(title, "sec")] + [("", "sec")] * 11, 22)
        span(rr, 1, 12)

    def strip(labels, values, note_row=None):
        """指标条：标签行 + 数值行（5 项按 2 列一格，4 项按 3 列一格）"""
        n = len(labels)
        per = 2 if n == 5 else 3
        rr = len(R) + 1
        cells = []
        for i, lb in enumerate(labels):
            cells += [(lb, "statl")] + [("", "statl")] * (per - 1)
        while len(cells) < 12:
            cells.append(("", "statl"))
        add(cells, 20)
        for i in range(n):
            span(rr, i * per + 1, i * per + per)
        rr += 1
        cells = []
        for i, (v, sty) in enumerate(values):
            cells += [(v, sty)] + [("", sty)] * (per - 1)
        while len(cells) < 12:
            cells.append(("", "statl"))
        add(cells, 30)
        for i in range(n):
            span(rr, i * per + 1, i * per + per)

    section(L("盘点结论"))
    strip([L("账面总数"), L("实盘总数"), L("差异总数"), L("差异金额(元)"), L("贵重差异条数")],
          [(summ.get("book", 0), "statn"), (summ.get("actual", 0), "statn"),
           (summ.get("diff", 0), "statn"), (summ.get("amount", 0), "stat2"),
           (summ.get("v_count", 0), "statn")])

    section(L("差异构成"))
    strip([L("盘盈"), L("盘亏"), L("错料"), L("无差异")],
          [(stats["gain"], "statn"), (stats["loss"], "statn"),
           (stats["mismatch"], "statn"), (stats["same"], "statn")])
    rr = len(R) + 1
    _notes = ["%s+%g · %.2f %s" % (L("盘盈"), stats["gain_qty"], stats["gain_amt"], L("元")),
              "%s-%g · %.2f %s" % (L("盘亏"), stats["loss_qty"], abs(stats["loss_amt"]), L("元")),
              L("实盘料号与账面不符"), L("账实相符")]
    add([((_notes[i // 3] if i // 3 < len(_notes) else ""), "stats") if i % 3 == 0
         else ("", "stats") for i in range(12)], 18)
    span(rr, 1, 3); span(rr, 4, 6); span(rr, 7, 9); span(rr, 10, 12)

    # ── 按原因汇总 ──
    section(L("按原因汇总"))
    rr = len(R) + 1
    add([(L("原因"), "th"), ("", "th"), ("", "th"), ("", "th"),
         (L("差异条数"), "th"), ("", "th"), (L("数量合计"), "th"), ("", "th"),
         (L("差异金额"), "th"), ("", "th"), ("", "th"), ("", "th")], 20)
    span(rr, 1, 4); span(rr, 5, 6); span(rr, 7, 8); span(rr, 9, 12)
    if reason_rows:
        for g in reason_rows:
            rr = len(R) + 1
            add([(g["label"], "td"), ("", "td"), ("", "td"), ("", "td"),
                 (g["cnt"], "numg"), ("", "numg"), (g["qty"], "numg"), ("", "numg"),
                 (g["amt"], "num2"), ("", "num2"), ("", "num2"), ("", "num2")], 18)
            span(rr, 1, 4); span(rr, 5, 6); span(rr, 7, 8); span(rr, 9, 12)
        if stats["no_reason"]:
            rr = len(R) + 1
            add([(L("未标注"), "td"), ("", "td"), ("", "td"), ("", "td"),
                 (stats["no_reason"], "numg"), ("", "numg"), ("—", "tdc"), ("", "tdc"),
                 ("—", "tdc"), ("", "tdc"), ("", "tdc"), ("", "tdc")], 18)
            span(rr, 1, 4); span(rr, 5, 6); span(rr, 7, 8); span(rr, 9, 12)
    else:
        rr = len(R) + 1
        tip = L("本次盘点无差异，无需归因") if not stats["diff_cnt"] else L("差异还没标注原因")
        add([(tip, "td")] + [("", "td")] * 11, 18)
        span(rr, 1, 12)

    # ── 盘点明细（12 列与页面表头一一对应）──
    section(L("盘点明细"))
    heads = ["#", L("料号"), L("名称"), L("库位"), L("账面数量"), L("实盘数量"),
             L("差异"), L("实盘料号"), L("单价(元)"), L("差异金额"), L("差异原因"), L("贵重")]
    add([(h, "th") for h in heads], 24)
    for i, it in enumerate(rows_full, start=1):
        d = it["diff"] or 0
        dsty = "pos" if d > 0 else ("neg" if d < 0 else "numg")
        asty = "pos2" if it["diff_amt"] > 0 else ("neg2" if it["diff_amt"] < 0 else "num2")
        if it["is_wrong"]:
            wrong_txt = "%s → %s" % (L("错料"), it["actual_part_code"] or "")
        else:
            wrong_txt = "—"
        add([(i, "tdc"), (it["part_code"], "td"), (it["part_name"], "td"),
             (it["location"] or "—", "td"),
             (it["book_qty"], "numg"),
             (it["actual_qty"] if it["actual_qty"] is not None else "—", "numg"),
             (d, dsty), (wrong_txt, "val" if it["is_wrong"] else "tdc"),
             (it["unit_price"] or 0, "num2"), (it["diff_amt"], asty),
             (it["reason_label"] or "—", "tdc"),
             (L("贵重") if it["is_valuable"] else "—", "tdc")], 21)
    rr = len(R) + 1
    add([(L("合计"), "totl"), ("", "totl"), ("", "totl"), ("", "totl"),
         (summ.get("book", 0), "totg"), (summ.get("actual", 0), "totg"),
         (summ.get("diff", 0), "totg"), ("", "totl"), ("", "totl"),
         (summ.get("amount", 0), "tot2"), ("", "totl"), (summ.get("v_count", 0), "totg")], 22)
    span(rr, 1, 4)

    add([("", "def")], 8)

    # ── 差异原因分析：文本块给足行高，打印时才不会被截断 ──
    section(L("差异原因分析"))
    for lab, txt in ((L("原因分析"), _cause), (L("改善行动"), _action)):
        rr = len(R) + 1
        add([(lab, "mlab"), ("", "mlab"), (txt or L("尚未填写"), "box"), ("", "box"),
             ("", "box"), ("", "box"), ("", "box"), ("", "box"), ("", "box"),
             ("", "box"), ("", "box"), ("", "box")], 66)
        span(rr, 1, 2); span(rr, 3, 12)
    rr = len(R) + 1
    add([(L("责任人"), "mlab"), ("", "mlab"), (_owner or "—", "mval"), ("", "mval"),
         ("", "mval"), ("", "mval"), (L("计划完成日期"), "mlab"), ("", "mlab"),
         (_due or "—", "mval"), ("", "mval"), ("", "mval"), ("", "mval")], 20)
    span(rr, 1, 2); span(rr, 3, 6); span(rr, 7, 8); span(rr, 9, 12)
    rr = len(R) + 1
    add([(L("分析填写人"), "mlab"), ("", "mlab"),
         (("%s · %s" % (_by or "—", _at)) if _at else L("尚未填写"), "mval"),
         ("", "mval"), ("", "mval"), ("", "mval"), ("", "mval"), ("", "mval"),
         ("", "mval"), ("", "mval"), ("", "mval"), ("", "mval")], 18)
    span(rr, 1, 2); span(rr, 3, 12)

    add([("", "def")], 8)

    # ── 签署确认 + 页脚说明（打印后手签归档用）──
    section(L("签署确认"))
    add([(L("盘点人"), "sign"), ("", "blank"), ("", "blank"),
         (L("复核人"), "sign"), ("", "blank"), ("", "blank"),
         (L("审批人"), "sign"), ("", "blank"), ("", "blank"),
         (L("日期"), "sign"), ("", "blank"), ("", "blank")], 34)
    rr = len(R) + 1
    add([(L("本报告由系统在盘点审批通过、库存自动调整完成后生成，明细与库存流水一一对应，可导出 Excel 备查。"),
          "stats")] + [("", "stats")] * 11, 30)
    span(rr, 1, 12)

    # ── 第二张表：扁平明细（给透视/导入用，表头固定在首行）──
    flat_heads = ["料号", "名称", "库位", "账面数量", "实盘数量", "差异", "实盘料号",
                  "单价(元)", "差异金额", "差异原因", "贵重",
                  L("原因分析"), L("改善处理"), L("责任人"), L("计划完成日期")]
    flat = []
    for it in rows_full:
        flat.append([
            (it["part_code"], "td"), (it["part_name"], "td"),
            (it["location"] or "", "td"),
            (it["book_qty"], "numg"),
            (it["actual_qty"] if it["actual_qty"] is not None else "", "numg"),
            (it["diff"] or 0, "numg"),
            ((it["actual_part_code"] or "") if it["is_wrong"] else "", "td"),
            (it["unit_price"] or 0, "num2"),
            (it["diff_amt"], "num2"),
            (it["reason_label"] or "", "tdc"),
            (L("是") if it["is_valuable"] else L("否"), "tdc"),
            (_rowv(it, "cause"), "td"), (_rowv(it, "action"), "td"),
            (_rowv(it, "action_owner"), "td"), (_rowv(it, "action_due"), "td"),
        ])
    data = _xlsx_styled([
        {"name": L("盘点报告"), "cols": W, "rows": R, "merges": MG,
         "print": True, "grid": False},
        {"name": L("盘点明细"),
         "cols": [16, 24, 18, 11, 11, 9, 16, 11, 12, 14, 9, 30, 30, 14, 16],
         "rows": [{"cells": [(h, "th") for h in flat_heads], "h": 24}] + flat,
         "print": True},
    ])
    return Response(
        data.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=stocktake_{0}.xlsx".format(st["no"])},
    )


@app.route("/m/stocktake/<int:sid>")
@login_required
@perm_required("stocktake", "create")
def stocktake_mobile(sid):
    conn = get_db()
    try:
        st = conn.execute("SELECT * FROM stocktakes WHERE id=?", (sid,)).fetchone()
        if not st:
            abort(404)
        items = [dict(r) for r in conn.execute(
            """SELECT it.*, p.qty_decimal, p.is_valuable FROM stocktake_items it
               JOIN parts p ON p.id=it.part_id WHERE it.stocktake_id=? ORDER BY it.id""", (sid,),
        ).fetchall()]
        # v1.0.17：老库缺列时给 JS 补默认，手机页 render() 不会读到 undefined
        for _d in items:
            for _k in ("cause", "action", "action_owner", "action_due", "action_status"):
                _d.setdefault(_k, "")
    finally:
        conn.close()
    return render_template("stocktake_mobile.html", st=st, items=items)



@app.route("/parts/recycle/settings", methods=["POST"])
@login_required
@perm_required("recycle")
def recycle_settings():
    """回收站保留天数设置（默认 3 天，超期自动彻底清除）"""
    try:
        days = max(1, min(_int(request.form.get("days", 3), 3), 365))
    except (TypeError, ValueError):
        days = 3
    conn = get_db()
    try:
        db.set_setting(conn, "recycle_days", str(days))
        conn.commit()
    finally:
        conn.close()
    flash(_i18n.t("回收站保留天数已设置为 {0} 天（超期自动彻底清除）", _i18n.current_lang()).format(days), "success")
    return redirect(url_for("recycle_page"))


# ------------------------------------------------------------------
# 通用回收站（货架位置 / 部门 / 用户）：
#   删除 = 物理删除 + 记录快照进 recycle_items（唯一键随即释放，可新建同名）；
#   恢复 = 从快照重建（保留原 id，库存/流水引用自动重连）；
#   恢复时若已存在相同信息 → 提示「有相同信息已存在，恢复时会覆盖已有信息，请确认」，
#   确认后覆盖（删除现有同名，恢复旧记录）。
# ------------------------------------------------------------------
_RECYCLE_TYPES = {"location": "货架位置", "department": "部门", "user": "用户"}

_RECYCLE_KEY_SQL = {
    "location":   "SELECT id FROM locations WHERE warehouse=? AND rack=? AND shelf=? AND bin=?",
    "department": "SELECT id FROM departments WHERE name=?",
    "user":       "SELECT id FROM users WHERE username=?",
}

_RECYCLE_KEY_VALUES = {
    "location":   lambda d: (d["warehouse"], d["rack"], d["shelf"], d["bin"]),
    "department": lambda d: (d["name"],),
    "user":       lambda d: (d["username"],),
}

_RECYCLE_INSERT_SQL = {
    "location":   "INSERT INTO locations(id, warehouse, rack, shelf, bin, remark, created_at) VALUES(?,?,?,?,?,?,?)",
    "department": "INSERT INTO departments(id, name, remark, created_at) VALUES(?,?,?,?)",
    "user":       "INSERT INTO users(id, username, password_hash, display_name, role, department, active, created_at) VALUES(?,?,?,?,?,?,?,?)",
}

_RECYCLE_ITEM_LABEL = {
    "location":   lambda d: f"{d['warehouse']}-{d['rack']}-{d['shelf']}-{d['bin'] or _i18n.t('整货位')}",
    "department": lambda d: d["name"],
    "user":       lambda d: d["username"],
}


def _recycle_add(conn, item_type, item_id, row):
    """把被物理删除的记录存入通用回收站快照"""
    conn.execute(
        "INSERT INTO recycle_items(item_type, item_id, data, deleted_at) VALUES(?,?,?,?)",
        (item_type, item_id, json.dumps(dict(row), ensure_ascii=False), db.now_str()),
    )


def _recycle_items_list(conn, item_type):
    """通用回收站列表：item_type 全部快照 + 到期自动清除时间"""
    days = _recycle_days(conn)
    now = datetime.now()
    rows = conn.execute(
        "SELECT * FROM recycle_items WHERE item_type=? ORDER BY id DESC", (item_type,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["data_obj"] = json.loads(d["data"] or "{}")
        except (TypeError, ValueError):
            d["data_obj"] = {}
        d["label"] = _RECYCLE_ITEM_LABEL[item_type](d["data_obj"])
        d["expire"] = None
        d["expired"] = False
        if d.get("deleted_at"):
            try:
                dt = datetime.strptime(d["deleted_at"][:19], "%Y-%m-%d %H:%M:%S")
                d["expire"] = (dt + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                d["expired"] = now >= dt + timedelta(days=days)
            except (ValueError, TypeError):
                pass
        out.append(d)
    return out


def _recycle_find_dup(conn, item_type, data_obj):
    """返回与快照唯一键相同的现存记录 id（无冲突返回 None）"""
    sql = _RECYCLE_KEY_SQL[item_type]
    vals = _RECYCLE_KEY_VALUES[item_type](data_obj)
    row = conn.execute(sql, vals).fetchone()
    return row["id"] if row else None


def _recycle_restore_other(conn, item_type, item_id, force=False):
    """恢复一条通用回收站记录。返回 (ok, msg, need_confirm)
    - 无同名冲突：直接恢复（保留原 id）
    - 有同名冲突且未 force：返回 need_confirm=True，等待用户确认覆盖
    - force=True：删除现有同名后恢复旧记录（覆盖），位置被占时顺带把库存引用改回原 id"""
    r = conn.execute(
        "SELECT * FROM recycle_items WHERE item_type=? AND item_id=?",
        (item_type, item_id),
    ).fetchone()
    if not r:
        return False, _xl("回收站中不存在该记录"), False
    try:
        data_obj = json.loads(r["data"] or "{}")
    except (TypeError, ValueError):
        return False, _xl("回收站记录数据损坏，无法恢复"), False
    dup_id = _recycle_find_dup(conn, item_type, data_obj)
    if dup_id is not None and not force:
        return False, "", True   # 需确认：有相同信息已存在
    if dup_id is not None:
        # 覆盖：删除现有同名记录；位置的库存引用改指回原 id（避免悬空）
        if item_type == "location":
            conn.execute(
                "UPDATE inventory SET location_id=? WHERE location_id=?",
                (item_id, dup_id),
            )
            conn.execute("UPDATE parts SET location_id=? WHERE location_id=?", (item_id, dup_id))
        conn.execute("DELETE FROM locations WHERE id=?" if item_type == "location"
                     else "DELETE FROM departments WHERE id=?" if item_type == "department"
                     else "DELETE FROM users WHERE id=?", (dup_id,))
    if item_type == "location":
        try:
            _audit("rack", item_id, _RECYCLE_ITEM_LABEL[item_type](data_obj), "restore", {"note": "从回收站恢复货架位置"}, conn=conn)
        except Exception:
            pass
    sql = _RECYCLE_INSERT_SQL[item_type]
    if item_type == "location":
        conn.execute(sql, (item_id, data_obj.get("warehouse", ""), data_obj.get("rack", ""),
                           data_obj.get("shelf", ""), data_obj.get("bin", "") or "",
                           data_obj.get("remark", "") or "", data_obj.get("created_at") or db.now_str()))
    elif item_type == "department":
        conn.execute(sql, (item_id, data_obj.get("name", ""), data_obj.get("remark", "") or "",
                           data_obj.get("created_at") or db.now_str()))
    else:
        conn.execute(sql, (item_id, data_obj.get("username", ""), data_obj.get("password_hash", ""),
                           data_obj.get("display_name", ""), data_obj.get("role", "user"),
                           data_obj.get("department", "") or "", data_obj.get("active", 1),
                           data_obj.get("created_at") or db.now_str()))
    conn.execute("DELETE FROM recycle_items WHERE id=?", (r["id"],))
    conn.commit()
    return True, _xl("[{label}] 已恢复", label=_RECYCLE_ITEM_LABEL[item_type](data_obj)), False


@app.route("/recycle/restore-other", methods=["POST"])
@login_required
@perm_required("recycle")
def recycle_restore_other():
    """恢复货架位置/部门/用户；有相同信息已存在时需确认覆盖"""
    item_type = request.form.get("type", "")
    try:
        item_id = _int(request.form.get("item_id", 0))
    except (TypeError, ValueError):
        item_id = 0
    force = request.form.get("force") == "1"
    if item_type not in _RECYCLE_TYPES:
        flash(_i18n.t("无效的回收站类型", _i18n.current_lang()), "danger")
        return redirect(url_for("recycle_page"))
    conn = get_db()
    try:
        ok, msg, need_confirm = _recycle_restore_other(conn, item_type, item_id, force)
    finally:
        conn.close()
    if need_confirm:
        # 有相同信息已存在：提示确认覆盖，待用户在页面确认后以 force 提交
        session["cover_pending"] = {"item_type": item_type, "item_id": item_id}
        flash(_i18n.t("有相同信息已存在，恢复时会覆盖已有信息，请确认", _i18n.current_lang()), "warning")
        return redirect(url_for("recycle_page"))
    if ok:
        session.pop("cover_pending", None)
        flash(msg, "success")
    else:
        flash(msg, "danger")
    return redirect(url_for("recycle_page"))


@app.route("/recycle/cover-confirm", methods=["POST"])
@login_required
@perm_required("recycle")
def recycle_cover_confirm():
    """用户确认覆盖后，强制恢复（覆盖现有同名记录）"""
    pend = session.pop("cover_pending", None)
    if not pend or pend.get("item_type") not in _RECYCLE_TYPES:
        flash(_i18n.t("没有待确认的覆盖恢复操作", _i18n.current_lang()), "warning")
        return redirect(url_for("recycle_page"))
    conn = get_db()
    try:
        ok, msg, _ = _recycle_restore_other(conn, pend["item_type"], pend.get("item_id", 0), force=True)
    finally:
        conn.close()
    if ok:
        flash(_xl("{msg}（已覆盖同名信息）", msg=msg), "success")
    else:
        flash(msg, "danger")
    return redirect(url_for("recycle_page"))


@app.route("/recycle/cover-cancel", methods=["POST"])
@login_required
@perm_required("recycle")
def recycle_cover_cancel():
    """取消待确认的覆盖恢复"""
    pend = session.pop("cover_pending", None)
    if pend and pend.get("item_type") in _RECYCLE_TYPES:
        flash(_i18n.t("已取消覆盖恢复", _i18n.current_lang()), "warning")
    return redirect(url_for("recycle_page"))


@app.route("/recycle/purge-other", methods=["POST"])
@login_required
@perm_required("recycle")
def recycle_purge_other():
    """彻底删除一条通用回收站记录（不可恢复）"""
    item_type = request.form.get("type", "")
    try:
        rid = _int(request.form.get("rid", 0))
    except (TypeError, ValueError):
        rid = 0
    if item_type not in _RECYCLE_TYPES or not rid:
        flash(_i18n.t("无效的回收站记录", _i18n.current_lang()), "danger")
        return redirect(url_for("recycle_page"))
    conn = get_db()
    try:
        rrow = conn.execute("SELECT * FROM recycle_items WHERE id=? AND item_type=?", (rid, item_type)).fetchone()
        if rrow and item_type == "location":
            try:
                dobj = json.loads(rrow["data"] or "{}")
                _audit("rack", rrow["item_id"], _RECYCLE_ITEM_LABEL[item_type](dobj), "purge", {"note": "彻底删除货架位置"}, conn=conn)
            except Exception:
                pass
        conn.execute("DELETE FROM recycle_items WHERE id=? AND item_type=?", (rid, item_type))
        conn.commit()
    finally:
        conn.close()
    flash(_i18n.t("记录已彻底删除（不可恢复）", _i18n.current_lang()), "warning")
    return redirect(url_for("recycle_page"))


@app.route("/recycle/purge-all-other", methods=["POST"])
@login_required
@perm_required("recycle")
def recycle_purge_all_other():
    """清空指定类型的通用回收站记录"""
    item_type = request.form.get("type", "")
    if item_type not in _RECYCLE_TYPES:
        flash(_i18n.t("无效的回收站类型", _i18n.current_lang()), "danger")
        return redirect(url_for("recycle_page"))
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM recycle_items WHERE item_type=?", (item_type,))
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()
    flash(_i18n.t("已彻底删除 {0} 条记录（不可恢复）", _i18n.current_lang()).format(n), "warning")
    return redirect(url_for("recycle_page"))


def _recycle_cleanup_once():
    """彻底清除所有超过保留天数的已删除物料与通用回收站记录（返回清除条数；供后台线程与测试调用）"""
    if not config.DB_PATH or not os.path.exists(config.DB_PATH):
        return 0
    days = _recycle_days()
    cut = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id FROM parts WHERE deleted=1 AND deleted_at IS NOT NULL AND deleted_at<=?",
            (cut,),
        ).fetchall()
        for r in rows:
            _purge_part(conn, r["id"])
        n = len(rows)
        # 通用回收站（位置/部门/用户）超期自动彻底清除
        n2 = conn.execute(
            "DELETE FROM recycle_items WHERE deleted_at IS NOT NULL AND deleted_at<=?", (cut,)
        ).rowcount
        if rows or n2:
            conn.commit()
        return n + n2
    finally:
        conn.close()


def _remind_worker():
    """审批超时催办线程：每10分钟扫一次贵重出库审批单，
    提交后超过首催小时数未审 → 发第一次提醒；之后每隔间隔小时数再催一次。"""
    log = logging.getLogger("remind")
    while True:
        try:
            import time as _t
            _t.sleep(600)  # 10分钟
            st = _settings.load()
            if not (st.get("notify_email_enabled") or st.get("notify_imp_enabled")):
                continue
            first_h = float(st.get("remind_first_hours", 4))
            interval_h = float(st.get("remind_interval_hours", 24))
            conn = get_db()
            try:
                rows = conn.execute(
                    """SELECT a.id, a.app_no, a.part_id, a.status, a.created_at, a.last_remind_at,
                              p.part_code, p.name,
                              CASE WHEN a.status='pending' THEN a.assigned_approver_id
                                   ELSE a.approver2_id END AS uid
                       FROM applications a JOIN parts p ON p.id=a.part_id
                       WHERE a.app_type='out' AND p.is_valuable=1
                         AND a.status IN ('pending','pending2')"""
                ).fetchall()
                now = datetime.now()
                for r in rows:
                    try:
                        created = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        continue
                    age_h = (now - created).total_seconds() / 3600.0
                    if age_h < first_h:
                        continue
                    last = r["last_remind_at"] or ""
                    if last:
                        try:
                            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                            if (now - last_dt).total_seconds() / 3600.0 < interval_h:
                                continue
                        except Exception:
                            pass
                    u = conn.execute("SELECT display_name,email,enterprise_id,lang FROM users WHERE id=?",
                                     (r["uid"],)).fetchone()
                    if not u:
                        continue
                    try:
                        import notify as _notify
                        _notify.reload_settings()
                        # 关键：按收件人语言渲染，而不是按「当前请求语言」——
                        # 这里是后台线程，没有请求上下文，取不到任何人的会话。
                        with _i18n.lang_scope(u["lang"] or ""):
                            _notify.notify_approver(
                                dict(u),
                                _i18n.t("审批超时提醒"),
                                _i18n.t("单据 {no}（{part}）提交已超过 {h} 小时未审批，请尽快处理。",
                                        no=r["app_no"], part=r["part_code"], h="%g" % first_h))
                    except Exception:
                        pass
                    conn.execute("UPDATE applications SET last_remind_at=? WHERE id=?",
                                 (now.strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
                    conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.warning("催办扫描异常：%s", e)


def _pending_release_worker():
    """待出库超时处理线程：每30分钟扫一次已批准待出库的申请单，
    超过提醒时长发提醒；超过释放时长自动取消申请单，释放锁定库存。"""
    log = logging.getLogger("pending")
    while True:
        try:
            import time as _t
            _t.sleep(1800)  # 30分钟
            st = _settings.load()
            remind_h = float(st.get("pending_remind_hours", 24))
            release_h = float(st.get("pending_release_hours", 36))
            conn = get_db()
            try:
                rows = conn.execute(
                    """SELECT a.id, a.app_no, a.part_id, a.quantity, a.processed_at, a.created_at, a.last_remind_at,
                              p.part_code, p.name, p.unit, p.is_valuable,
                              u.display_name AS applicant_name, u.email AS applicant_email,
                              u.enterprise_id, u.lang
                       FROM applications a
                       JOIN parts p ON p.id=a.part_id
                       JOIN users u ON u.id=a.applicant_id
                       WHERE a.app_type='out' 
                         AND ((p.is_valuable=1 AND a.status='approved') 
                           OR (p.is_valuable=0 AND a.status='pending'))"""
                ).fetchall()
                now = datetime.now()
                for r in rows:
                    try:
                        # 普通料：从提交时间开始算；贵重料：从审批通过时间开始算
                        if r["is_valuable"]:
                            base_time = r["processed_at"] or r["created_at"]
                        else:
                            base_time = r["created_at"]
                        approved_dt = datetime.strptime(base_time, "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        continue
                    age_h = (now - approved_dt).total_seconds() / 3600.0
                    # 超过释放时长：自动取消
                    if age_h >= release_h:
                        # 落库的备注存「事件码」，不存中文文案：
                        # 中文一旦入库，历史记录切语言永远是中文。
                        auto_key = ("notify.release_valuable" if r["is_valuable"]
                                    else "notify.release_normal")
                        auto_note = "%s|%g" % (auto_key, release_h)
                        conn.execute(
                            "UPDATE applications SET status='cancelled', approval_note=?, operator_id=? WHERE id=?",
                            (auto_note, None, r["id"]),
                        )
                        log.info("待出库超时取消：%s（%s x%s）已超时 %.1f 小时", r["app_no"], r["part_code"], r["quantity"], age_h)
                        continue
                    # 超过提醒时长：发提醒（只发一次）
                    if age_h >= remind_h and not r["last_remind_at"]:
                        try:
                            import notify as _notify
                            _notify.reload_settings()
                            # 按申请人自己的语言写这封信（r["lang"] 来自 users.lang）
                            with _i18n.lang_scope(r["lang"] or ""):
                                if r["is_valuable"]:
                                    msg = _i18n.t("您的出库申请单 {no}（{part} x{qty}）审批通过已超过 {h} 小时未出库，请尽快到仓库领货，超时将自动取消。",
                                                  no=r["app_no"], part=r["part_code"],
                                                  qty=r["quantity"], h="%g" % remind_h)
                                else:
                                    msg = _i18n.t("您的出库申请单 {no}（{part} x{qty}）提交后已超过 {h} 小时未出库，请尽快到仓库领货，超时将自动取消。",
                                                  no=r["app_no"], part=r["part_code"],
                                                  qty=r["quantity"], h="%g" % remind_h)
                                subject = _i18n.t("待出库超时提醒")
                            _notify.notify_approver(dict(r), subject, msg)
                        except Exception:
                            pass
                        conn.execute("UPDATE applications SET last_remind_at=? WHERE id=?",
                                     (now.strftime("%Y-%m-%d %H:%M:%S"), r["id"]))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.warning("待出库超时扫描异常：%s", e)


def _recycle_worker():
    """回收站自动清理线程：每小时检查一次，彻底清除超过保留天数的已删除物料"""
    log = logging.getLogger("recycle")
    while True:
        try:
            n = _recycle_cleanup_once()
            if n:
                log.info("回收站自动清理 %s 条", n)
            na = _audit_cleanup_once()
            if na:
                log.info("操作记录自动清理 %s 条", na)
        except Exception as e:
            log.warning("回收站自动清理失败：%s", e)
        time.sleep(3600)


@app.route("/parts/<int:pid>/image", methods=["POST"])
@login_required
@perm_required("parts_manage", "edit")
def part_upload_image(pid):
    file = request.files.get("image")
    if not file or not file.filename:
        flash(_i18n.t("请选择图片文件", _i18n.current_lang()), "danger")
        return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))
    conn = get_db()
    try:
        state, detail = _save_part_image(conn, pid, file)
    finally:
        conn.close()
    if state == "err":
        flash(detail, "danger")
    elif state == "zip":
        flash(_i18n.t("物料图片已更新（已自动压缩到 {0}KB 以内）",
                      _i18n.current_lang()).format(detail), "success")
    else:
        flash(_i18n.t("物料图片已更新", _i18n.current_lang()), "success")
    return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))


@app.route("/parts/<int:pid>/image/delete", methods=["POST"])
@login_required
@perm_required("parts_manage", "edit")
def part_delete_image(pid):
    """删除物料图片（编辑物料中操作）"""
    conn = get_db()
    try:
        p = conn.execute("SELECT image_file FROM parts WHERE id=?", (pid,)).fetchone()
        if not p:
            flash(_i18n.t("物料不存在", _i18n.current_lang()), "danger")
            return redirect(url_for("parts_manage_page"))
        if p["image_file"]:
            db.image_delete(conn, p["image_file"])
            fp = os.path.join(config.resolved_upload_dir(), p["image_file"])
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
            conn.execute("UPDATE parts SET image_file='' WHERE id=?", (pid,))
            conn.commit()
            # 操作审计：删除物料图片
            try:
                pc = conn.execute("SELECT part_code FROM parts WHERE id=?", (pid,)).fetchone()
                _audit("part", pid, pc["part_code"] if pc else str(pid), "image_del", {"note": "删除图片"}, conn=conn)
                conn.commit()
            except Exception:
                pass
            flash(_i18n.t("物料图片已删除", _i18n.current_lang()), "success")
        else:
            flash(_i18n.t("该物料暂无图片", _i18n.current_lang()), "warning")
    finally:
        conn.close()
    return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))


@app.route("/media/<path:fname>")
def media(fname):
    """提供图片：优先从主数据库 images 表读取（图片已入库），
    旧版遗留的 upload_dir 目录文件仍可回退读取（兼容历史数据）"""
    from flask import Response, send_file
    fname = os.path.basename(fname)  # 只允许单文件名，防路径穿越
    if not db_configured():
        abort(404)
    conn = get_db()
    try:
        data = db.image_load(conn, fname)
        if data is not None:
            mime = "image/png"
            ext = os.path.splitext(fname)[1].lower()
            if ext in (".jpg", ".jpeg"):
                mime = "image/jpeg"
            elif ext == ".gif":
                mime = "image/gif"
            elif ext == ".bmp":
                mime = "image/bmp"
            elif ext == ".webp":
                mime = "image/webp"
            return Response(data, mimetype=mime, headers={"Cache-Control": "private, max-age=3600"})
    finally:
        conn.close()
    # 回退：旧版目录文件
    full = os.path.realpath(os.path.join(config.resolved_upload_dir(), fname))
    base = os.path.realpath(config.resolved_upload_dir())
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        abort(404)
    return send_file(full)


# ------------------------------------------------------------------
# 物料多库位配置（信息维护 → 编辑物料）：一个物料可占用多个货位
# ------------------------------------------------------------------
@app.route("/parts/<int:pid>/locations/add", methods=["POST"])
@login_required
@perm_required("parts_manage", "edit")
def part_location_add(pid):
    """为物料新增一个库存库位（位置 + 数量，数量可空=0 仅登记位置）；位置被其他物料占用时拒绝"""
    f = request.form
    qty = _num(f.get("qty", 0))
    if qty < 0:
        qty = 0
    lid = _int(f.get("location_id"), 0)
    if not lid:
        flash(_i18n.t("请选择库位（仓库号-货架号-货位号-格位号）", _i18n.current_lang()), "danger")
        return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))
    conn = get_db()
    try:
        part = conn.execute(
            "SELECT qty_decimal, unit FROM parts WHERE id=? AND deleted=0", (pid,)
        ).fetchone()
        if part and not part["qty_decimal"]:
            if qty != int(qty):
                flash(_xl("该物料按『{p1}』整数计数，库存数量不能填小数", p1=part['unit']), "danger")
                return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))
            qty = int(qty)
        loc = conn.execute("SELECT * FROM locations WHERE id=?", (lid,)).fetchone()
        if not loc:
            flash(_i18n.t("所选位置不存在，请先在货架管理中建立", _i18n.current_lang()), "danger")
            return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))
        occ = conn.execute(
            """SELECT i.part_id, p.part_code FROM inventory i
               JOIN parts p ON i.part_id=p.id AND p.deleted=0
               WHERE i.location_id=? AND i.part_id<>? LIMIT 1""",
            (lid, pid),
        ).fetchone()
        if occ:
            flash(_xl("该位置已配置给物料 [{p1}]，不能重复配置；请先将其调走", p1=occ['part_code']), "danger")
            return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))
        ok, msg = db.move_stock(conn, pid, lid, qty)
        if not ok:
            flash(msg, "danger")
            return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))
        conn.commit()
        if qty > 0:
            flash(_i18n.t("已为物料增加库位 [{0}]，库存 +{1}", _i18n.current_lang()).format(db.location_text(loc), qty), "success")
        else:
            flash(_i18n.t("已为物料登记库位 [{0}]（库存 0，可后续入库）", _i18n.current_lang()).format(db.location_text(loc)), "success")
    finally:
        conn.close()
    return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))


@app.route("/parts/<int:pid>/locations/<int:inv_id>/delete", methods=["POST"])
@login_required
@perm_required("parts_manage", "edit")
def part_location_delete(pid, inv_id):
    """删除物料的一个库存库位（仅该库位库存为 0 时可删）"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM inventory WHERE id=? AND part_id=?", (inv_id, pid)
        ).fetchone()
        if not row:
            flash(_i18n.t("该库存库位不存在", _i18n.current_lang()), "danger")
            return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))
        if row["quantity"] > 0:
            flash(_xl("库位 [{p1}] 仍有库存 {p2}，不能删除；请先出库/调拨清零", p1=row['location'], p2=row['quantity']), "danger")
            return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))
        conn.execute("DELETE FROM inventory WHERE id=?", (inv_id,))
        conn.commit()
        flash(_xl("库位 [{p1}] 已删除", p1=row['location']), "success")
    finally:
        conn.close()
    return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))


@app.route("/parts/<int:pid>/locations/<int:inv_id>/set-primary", methods=["POST"])
@login_required
@perm_required("parts_manage", "edit")
def part_location_set_primary(pid, inv_id):
    """（已废弃：多库位平等，无主放置位置概念）仅兼容旧入口，直接返回"""
    return redirect(url_for("parts_manage_page", _anchor=f"part-{pid}"))


# ------------------------------------------------------------------
# 货架位置管理（仓库号-货架号-货位号-格位号，仓管员以上）
# ------------------------------------------------------------------
@app.route("/locations")
@login_required
@perm_required("parts_manage")
def locations_page():
    conn = get_db()
    try:
        rows = _all_locations(conn)
    finally:
        conn.close()
    # 分页：与信息查询/信息维护同一套口径（默认 10，可切 10/20/50/100）
    total = len(rows)
    per_page = _parts_per_page("locations_per_page")
    pages = max(1, (total + per_page - 1) // per_page)
    try:
        page_no = max(1, _int(request.args.get("page", 1) or 1))
    except (TypeError, ValueError):
        page_no = 1
    page_no = min(page_no, pages)
    shown = rows[(page_no - 1) * per_page: page_no * per_page]
    return render_template("locations.html", locations=shown,
                           locations_total=total, locations_page=page_no,
                           locations_pages=pages, locations_per_page=str(per_page),
                           loc_attr_names={k: _i18n.t(v, _i18n.current_lang())
                                           for k, v in LOC_ATTR_NAMES.items()},
                           loc_attrs=LOC_ATTRS)


def _loc_code(row):
    """库位码（与 inventory.location 一致的连字符格式，扫码盘点用）"""
    w = (row.get("warehouse") or "").strip()
    r = (row.get("rack") or "").strip()
    s = (row.get("shelf") or "").strip()
    b = (row.get("bin") or "").strip()
    code = f"{w}-{r}-{s}"
    if b:
        code += f"-{b}"
    return code


@app.route("/locations/codes")
@login_required
@perm_required("parts_manage")
def location_codes_page():
    """库位码打印页：为每个库位生成二维码（扫码进入盘点定位）"""
    conn = get_db()
    try:
        rows = _all_locations(conn)
        codes = [dict(r, loc_code=_loc_code(r)) for r in rows]
    finally:
        conn.close()
    return render_template("location_codes.html", codes=codes)


@app.route("/audit")
@login_required
def audit_page():
    """操作审计查询：物料 / 货架位置 / 盘点单 的新增、编辑、删除、审批等

    可见性：以前只判登录，任何账号直接敲 /audit 就能看到全公司谁改了什么。
    现在与入口保持同一口径——只有管理员、能维护物料档案、或能管盘点的人可看。
    """
    _u = current_user()
    _role = (_u["role"] if _u else "") or ""
    if not (_role == "admin" or can(_role, "parts_manage") or can(_role, "stocktake")):
        abort(403)
    t = request.args.get("type", "part")
    if t not in ("part", "rack", "stocktake"):
        t = "part"
    try:
        page = max(1, _int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    per = 20
    conn = get_db()
    try:
        tot = conn.execute("SELECT COUNT(*) FROM audit_log WHERE obj_type=?", (t,)).fetchone()[0]
        pages = max(1, (tot + per - 1) // per)
        page = min(page, pages)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log WHERE obj_type=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (t, per, (page - 1) * per),
        ).fetchall()]
        for r in rows:
            try:
                r["d"] = json.loads(r.get("detail") or "{}")
            except (TypeError, ValueError):
                r["d"] = {}
        audit_total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        audit_bytes = _audit_size_bytes(conn)
    finally:
        conn.close()
    if audit_bytes >= 1048576:
        audit_size = f"{audit_bytes / 1048576:.1f} MB"
    elif audit_bytes >= 1024:
        audit_size = f"{audit_bytes / 1024:.0f} KB"
    else:
        audit_size = f"{audit_bytes} B"
    return render_template("audit.html", rows=rows, t=t, page=page, pages=pages, tot=tot,
                           audit_total=audit_total, audit_size=audit_size, audit_days=_audit_days(),
                           PART_FIELD_NAMES=PART_FIELD_NAMES,
                           STK_FIELD_NAMES=STOCKTAKE_FIELD_NAMES,
                           _fmt_part_val=_fmt_part_val)


def _audit_size_bytes(conn):
    """audit_log 实际占用的磁盘字节数（dbstat 可用时精确统计，否则按条数估算）"""
    try:
        n = conn.execute("SELECT SUM(pgsize) FROM dbstat WHERE name='audit_log'").fetchone()[0]
        if n:
            return int(n)
    except Exception:
        pass
    total = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    return int(total) * 600  # 估算：每条约 0.6KB


@app.route("/audit/settings", methods=["POST"])
@login_required
@role_required("admin")
def audit_settings():
    """操作记录保留天数（0=永久保留）与立即清理"""
    t = request.form.get("type", "part")
    if t not in ("part", "rack", "stocktake"):
        t = "part"
    if request.form.get("do") == "clean":
        if _audit_days() <= 0:
            flash(_i18n.t("当前设置为永久保留，无需清理", _i18n.current_lang()), "warning")
        else:
            n = _audit_cleanup_once()
            flash(_i18n.t("已清理 {0} 条过期操作记录并压缩数据库", _i18n.current_lang()).format(n), "success")
        return redirect(url_for("audit_page", type=t))
    try:
        days = max(0, min(_int(request.form.get("days", 0)), 3650))
    except (TypeError, ValueError):
        days = 0
    conn = get_db()
    try:
        db.set_setting(conn, "audit_days", str(days))
        conn.commit()
    finally:
        conn.close()
    if days:
        flash(_i18n.t("已设置操作记录保留 {0} 天，超期记录将自动清理", _i18n.current_lang()).format(days), "success")
    else:
        flash(_i18n.t("操作记录将永久保留", _i18n.current_lang()), "success")
    return redirect(url_for("audit_page", type=t))


@app.route("/locations/add", methods=["POST"])
@login_required
@perm_required("parts_manage")
def location_add():
    f = request.form
    warehouse = f.get("warehouse", "").strip()
    rack = f.get("rack", "").strip()
    shelf = f.get("shelf", "").strip()
    bin_ = f.get("bin", "").strip()   # 格位号可空：空 = 整个货位
    remark = f.get("remark", "").strip()
    attribute = f.get("attribute", "").strip() or "good"
    if attribute not in LOC_ATTRS:
        attribute = "good"
    if not (warehouse and rack and shelf):
        flash(_i18n.t("仓库号、货架号、货位号为必填项（格位号可空）", _i18n.current_lang()), "danger")
        return redirect(url_for("locations_page"))
    if len(warehouse) > 20 or len(rack) > 20 or len(shelf) > 20 or len(bin_) > 20:
        flash(_i18n.t("仓库号/货架号/货位号/格位号长度不能超过 20 字符", _i18n.current_lang()), "danger")
        return redirect(url_for("locations_page"))
    conn = get_db()
    try:
        if conn.execute(
            "SELECT id FROM locations WHERE warehouse=? AND rack=? AND shelf=? AND bin=?",
            (warehouse, rack, shelf, bin_),
        ).fetchone():
            flash(_xl("位置 [{p1}-{p2}-{p3}-{p4}] 已存在", p1=warehouse, p2=rack, p3=shelf, p4=bin_ or '(整货位)'), "danger")
            return redirect(url_for("locations_page"))
        cur = conn.execute(
            "INSERT INTO locations(warehouse,rack,shelf,bin,remark,attribute,created_at) VALUES(?,?,?,?,?,?,?)",
            (warehouse, rack, shelf, bin_, remark, attribute, db.now_str()),
        )
        conn.commit()
        try:
            _audit("rack", cur.lastrowid, f"{warehouse}-{rack}-{shelf}-{bin_ or ''}", "add", {"note": "建立货架位置"}, conn=conn)
        except Exception:
            pass
        flash(_xl("位置 [{p1}-{p2}-{p3}-{p4}] 已建立", p1=warehouse, p2=rack, p3=shelf, p4=bin_ or '(整货位)'), "success")
    finally:
        conn.close()
    return redirect(url_for("locations_page"))


@app.route("/locations/<int:lid>/delete", methods=["POST"])
@login_required
@perm_required("parts_manage")
def location_delete(lid):
    conn = get_db()
    try:
        loc = conn.execute("SELECT * FROM locations WHERE id=?", (lid,)).fetchone()
        if not loc:
            flash(_i18n.t("位置不存在", _i18n.current_lang()), "danger")
            return redirect(url_for("locations_page"))
        occ = conn.execute(
            """SELECT p.part_code FROM inventory i JOIN parts p ON i.part_id=p.id AND p.deleted=0
               WHERE i.location_id=? AND i.quantity>0 LIMIT 1""", (lid,)
        ).fetchone()
        if occ:
            flash(_xl("该位置正被物料 [{p1}] 存放库存，请先将其调走/出库", p1=occ['part_code']), "danger")
            return redirect(url_for("locations_page"))
        _recycle_add(conn, "location", lid, loc)
        try:
            _audit("rack", lid, f"{loc['warehouse']}-{loc['rack']}-{loc['shelf']}-{loc['bin'] or ''}", "delete", {"note": "删除货架位置（进入回收站）"}, conn=conn)
        except Exception:
            pass
        conn.execute("DELETE FROM locations WHERE id=?", (lid,))
        conn.commit()
        flash(_i18n.t("位置 [{0}-{1}-{2}-{3}] 已删除并进入回收站（有权限的用户可恢复）", _i18n.current_lang())
              .format(loc['warehouse'], loc['rack'], loc['shelf'], loc['bin'] or _i18n.t('整货位')), "success")
    finally:
        conn.close()
    return redirect(url_for("locations_page"))


@app.route("/locations/<int:lid>/attribute", methods=["POST"])
@login_required
@perm_required("parts_manage")
def location_set_attribute(lid):
    """库位属性创建时选定后即锁定，不接受任何修改（v1.0.24：静默拒绝）。

    保留路由只是为了兼容旧页面、旧书签与回归调用。按用户口径：页面不展示
    「不可修改」字样，点了也不弹错误提示——直接原样返回列表，属性保持不变。
    原因：属性决定该库位能放什么状态、能走什么出库类型，事后改会让库存状态
    与库位属性对不上（例如良品区被改成待报废区，里面的良品就领不出来）。
    确需换属性：先调走库存 → 删除库位 → 按新属性重建。
    """
    return redirect(url_for("locations_page"))


# ------------------------------------------------------------------
# 直接出入库（仓库人员）
# ------------------------------------------------------------------
# 「已到仓管、可以动手」的申请条件（待处理列表与菜单角标必须同源，否则又会出现
# 「列表有单、角标没数」或相反的假象）：
#   · approved      —— 审批已通过，等仓库执行
#   · cancel_req    —— 用户发起撤销，等仓库/审批处理
#   · pending 且没有指派人 —— 本就不需要审批，提交后直达仓库（普通领用/调库/借出）
# 还在一级/二级审批中的行（pending/pending2 且已指派审批人）不列出来：
# 流程没到仓管，仓管也不该看到、更不该能点「执行」（点了只会被状态校验挡回）。
_INOUT_READY_WHERE = (
    "(a.status IN ('approved','cancel_req')"
    " OR (a.status='pending' AND IFNULL(a.assigned_approver_id,0)=0))"
)


@app.route("/inout")
@login_required
@perm_required("stock_inout")
def inout_page():
    """出入库管理：直接出入库 + 待处理的申请单（执行/驳回/撤销请求审批）+ 可按时间/类型筛选流水"""
    user = current_user()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    time_from = request.args.get("time_from", "").strip()
    time_to = request.args.get("time_to", "").strip()
    f_type = request.args.get("f_type", "").strip()
    f_sub = request.args.get("f_sub", "").strip()
    conn = get_db()
    try:
        where = []
        args = []
        dt_where(where, args, "t.created_at", date_from, date_to, time_from, time_to)
        # 方向 + 子类型必须自洽：子类型只在自己的方向里生效（入库/调库没有子类型）
        f_type, f_sub = _inout_filter(where, args, f_type, f_sub)
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        # 流水分页：每页条数可在 5/10/20/100/全部 间切换（默认 10，记忆在当前电脑）
        per_page_raw = request.args.get("per_page", "")
        if per_page_raw not in ("5", "10", "20", "100", "all"):
            per_page_raw = str(session.get("inout_per_page", 10))
        session["inout_per_page"] = per_page_raw
        per_page = None if per_page_raw == "all" else int(per_page_raw)
        page = max(1, request.args.get("page", 1, type=int))
        total = conn.execute(
            "SELECT COUNT(*) c FROM transactions t" + cond, args
        ).fetchone()["c"]
        pages = 1
        if per_page:
            pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, pages)
            txs = conn.execute(
                """SELECT t.*,
                          CASE WHEN t.tx_type='in' AND COALESCE(t.out_type,'')=''
                                    AND t.ref_type='borrow' AND t.remark LIKE '%回补%'
                               THEN 'borrow_return' ELSE t.out_type END disp_out_type,
                          p.part_code, p.name, u.display_name op_name
                   FROM transactions t
                   LEFT JOIN parts p ON t.part_id=p.id
                   LEFT JOIN users u ON t.operator_id=u.id"""
                + cond + " ORDER BY t.id DESC LIMIT ? OFFSET ?",
                args + [per_page, (page - 1) * per_page],
            ).fetchall()
        else:
            txs = conn.execute(
                """SELECT t.*,
                          CASE WHEN t.tx_type='in' AND COALESCE(t.out_type,'')=''
                                    AND t.ref_type='borrow' AND t.remark LIKE '%回补%'
                               THEN 'borrow_return' ELSE t.out_type END disp_out_type,
                          p.part_code, p.name, u.display_name op_name
                   FROM transactions t
                   LEFT JOIN parts p ON t.part_id=p.id
                   LEFT JOIN users u ON t.operator_id=u.id"""
                + cond + " ORDER BY t.id DESC",
                args,
            ).fetchall()
        # v1.0.64：历史回补流水的备注按原借·换·检·修子类型展示（只改展示副本）
        txs = [_borrow_return_remark_display(conn, t) for t in txs]
        # 待处理申请单（供仓管员执行/驳回；撤销请求供审批）
        # 只有真正能「执行出入库」的角色才看得到别人的单据。部门管理/普通用户
        # 即便通过 URL 进来，也不该看到全公司的申请明细（截图里部门管理能看到
        # 张三的单，就是这里没过滤）。
        can_exec = can(user["role"], "stock_inout", "execute")
        if can_exec:
            pending_apps = conn.execute(
                """SELECT a.*, p.part_code, p.name, p.unit, p.is_valuable, u.display_name app_name,
                          CASE WHEN l.bin='' THEN l.warehouse||'-'||l.rack||'-'||l.shelf
                               ELSE l.warehouse||'-'||l.rack||'-'||l.shelf||'-'||l.bin END AS location_text,
                          CASE WHEN fl.bin='' THEN fl.warehouse||'-'||fl.rack||'-'||fl.shelf
                               ELSE fl.warehouse||'-'||fl.rack||'-'||fl.shelf||'-'||fl.bin END AS from_loc_text,
                          CASE WHEN tl.bin='' THEN tl.warehouse||'-'||tl.rack||'-'||tl.shelf
                               ELSE tl.warehouse||'-'||tl.rack||'-'||tl.shelf||'-'||tl.bin END AS to_loc_text
                   FROM applications a
                   LEFT JOIN parts p ON a.part_id=p.id
                   LEFT JOIN locations l ON p.location_id=l.id
                   LEFT JOIN locations fl ON a.from_location_id=fl.id
                   LEFT JOIN locations tl ON a.to_location_id=tl.id
                   LEFT JOIN users u ON a.applicant_id=u.id
                   WHERE """ + _INOUT_READY_WHERE + """
                   ORDER BY a.id DESC LIMIT 200"""
            ).fetchall()
        else:
            pending_apps = []
        # 多库位：待处理列表显示该物料所有存放库位
        pending_apps = [dict(r) for r in pending_apps]
        for a in pending_apps:
            sls = db.stock_locations(conn, a["part_id"])
            a["all_locs_text"] = _i18n.mk("；").join(
                f"{s['location']} ×{s['quantity']}" for s in sls if s["quantity"])
            # v1.0.70：把申请锁定的源批次信息带到执行弹窗，仓管能一眼看清搬哪一行
            a["from_batch"] = ""
            a["from_qty"] = 0
            a["from_cond"] = "good"
            if a["app_type"] == "transfer" and _row_get(a, "whole_loc"):
                _sl = [s for s in sls
                       if (None if s["location_id"] is None else int(s["location_id"])) == a["from_location_id"]]
                a["from_batch"] = (_i18n.t("整库位", _i18n.current_lang()) + "（" + str(len(_sl))
                                   + _i18n.t("批次", _i18n.current_lang()) + "）")
                a["from_qty"] = sum(float(s.get("quantity") or 0) for s in _sl)
                a["from_cond"] = "defective" if any(str(s.get("condition") or "good") == "defective"
                                                    for s in _sl) else "good"
            elif a["app_type"] == "transfer" and _row_get(a, "from_inv_id"):
                _srow = next((s for s in sls if int(s["inv_id"]) == int(a["from_inv_id"])), None)
                if _srow:
                    a["from_batch"] = str(_srow.get("batch_no") or "")
                    a["from_qty"] = float(_srow.get("quantity") or 0)
                    a["from_cond"] = str(_srow.get("condition") or "good")
        # 按申请单（doc_no）分组展示：一张单多行明细，逐行执行
        pending_docs, _order = {}, []
        for a in pending_apps:
            key = a["doc_no"] or a["app_no"]
            if key not in pending_docs:
                pending_docs[key] = {"doc_no": key, "app_type": a["app_type"],
                                     "reason": a["reason"] or "", "created_at": a["created_at"],
                                     "app_name": a["app_name"], "rows": []}
                _order.append(key)
            pending_docs[key]["rows"].append(a)
        pending_docs = [pending_docs[k] for k in _order]
        for d in pending_docs:
            d["n_rows"] = len(d["rows"])
            d["has_cancel_req"] = any(r["status"] == "cancel_req" for r in d["rows"])
        locations = _all_locations(conn)
        # 直出表单数据：申请人（出库行需记录领用人）/ 产线设备
        # v1.0.50：guest 是 AD 白名单外自动进入的内置访客占位账号，不能当领用人/审批人候选
        users = [dict(r) for r in conn.execute(
            "SELECT id, display_name, department, role FROM users WHERE active=1 AND role<>'guest' ORDER BY department, id"
        ).fetchall()]
        lines = db.list_lines(conn)
        equipment = db.list_equipment(conn)
        _lm = {l["id"]: l["name"] for l in lines}
        for _e in equipment:
            _e["line_name"] = _lm.get(_e["line_id"], "")
    finally:
        conn.close()
    # 前端切方向时要重建下拉：把「方向 → [(值, 译文)]」整张表交给页面，
    # 译文在服务端取（tr 只能翻原文，JS 里没有词表）
    _lang_now = _i18n.current_lang()
    sub_map = {k: [[v, _i18n.t(_SUB_KIND_LABELS[v], _lang_now)] for v in vs]
               for k, vs in TX_SUBTYPES.items()}
    return render_template("inout.html", txs=txs, pending_docs=pending_docs,
                           locations=locations, cur_uid=user["id"], cur_role=user["role"],
                           users=users,
                           lines=lines, equipment=equipment,
                           date_from=date_from, date_to=date_to, f_type=f_type,
                           f_sub=f_sub,
                           sub_options=_inout_sub_options(f_type),
                           sub_map=sub_map,
                           time_from=time_from, time_to=time_to,
                           tx_total=total, tx_page=page, tx_pages=pages,
                           tx_per_page=per_page_raw,
                           fifo_enabled=int(_settings.load().get("fifo_enabled", 1)),
                           # v1.0.78：入库摆放提示（默认文案走四路 i18n，自定义文案原样返回）
                           # v1.0.80：关闭先进先出时不下发提示文案，页面与执行窗口都不显示
                           fifo_in_tip=(_i18n.t(str(_settings.load().get("fifo_in_tip") or ""),
                                                _lang_now)
                                        if int(_settings.load().get("fifo_enabled", 1)) == 1 else ""),
                           cond_matrix=_LOC_ACCEPT,
                           can_reassess=can(user["role"], "stock_inout", "reassess"),
                           condition_names={k: _i18n.t(v, _i18n.current_lang())
                                            for k, v in CONDITION_NAMES.items()})


@app.route("/inout/export")
@login_required
@perm_required("stock_inout", "export")
def inout_export():
    """按当前筛选条件导出出入库流水为 Excel（.xlsx，纯标准库生成）"""
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    time_from = request.args.get("time_from", "").strip()
    time_to = request.args.get("time_to", "").strip()
    f_type = request.args.get("f_type", "").strip()
    f_sub = request.args.get("f_sub", "").strip()
    conn = get_db()
    try:
        where = []
        args = []
        dt_where(where, args, "t.created_at", date_from, date_to, time_from, time_to)
        f_type, f_sub = _inout_filter(where, args, f_type, f_sub)
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            """SELECT t.*, p.part_code, p.name, p.unit, u.display_name op_name
               FROM transactions t
               LEFT JOIN parts p ON t.part_id=p.id
               LEFT JOIN users u ON t.operator_id=u.id"""
            + cond + " ORDER BY t.id DESC LIMIT 10000",
            args,
        ).fetchall()
    finally:
        conn.close()
    type_name = {"in": _xl("入库"), "out": _xl("出库"), "transfer": _xl("调库")}
    headers = ["单号", "时间", "料号", "名称", "单位", "类型", "库位", "数量",
               "单价", "金额(元)", "操作人", "备注"]
    data = []
    for r in rows:
        lab = type_name.get(r["tx_type"], r["tx_type"])
        _ot = (r["out_type"] or "").strip()
        if r["tx_type"] == "out" and _ot in OUT_KINDS:
            lab = lab + " / " + _xl(_OUT_KIND_NAMES[_ot])
        elif r["tx_type"] == "in":
            # 历史空值一律显示「采购」，与页面徽章/筛选口径同源
            # 默认值要取中文原文再交给 _xl 翻，别把英文 key 直接塞进去（会导出裸 purchase）
            lab = lab + " / " + _xl(_IN_KIND_NAMES.get(_ot, _IN_KIND_NAMES[IN_KIND_DEFAULT]))
        data.append([
            r["tx_no"], r["created_at"] or "", r["part_code"] or "", r["name"] or "",
            r["unit"] or "", lab,
            r["location"] or "", r["quantity"],
            round(r["unit_price"], 4), round(r["amount"], 2),
            r["op_name"] or "", r["remark"] or "",
        ])
    buf = _export_xlsx(headers, data, "出入库流水")
    return send_file(buf, as_attachment=True,
                     download_name=_i18n.t("出入库流水_{ts}.xlsx", _i18n.current_lang(),
                                           ts=datetime.now().strftime("%Y%m%d_%H%M%S")),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ------------------------------------------------------------------
# 呆滞物料管理（以料号为锚点：超过 N 天未领用即呆滞，默认 90 天）
# ------------------------------------------------------------------
def _stale_rows(conn, stale_days, kw="", only_stale=True, ignore_borrowed=None):
    """呆滞物料列表：有库存的物料，参考日=最后领用日(未领用/未借用过=建档日)，距今超 N 天=呆滞

    参数 ignore_borrowed 名字是历史遗留（设置键 stale_ignore_borrowed 同名字），语义要按
    「借用是否计入」来读：True = 借用记入呆滞判断（默认），False = 只看领用出库日期。
    None 表示读系统设置。

    两个口径细节（2026-11-03 修的，之前会误判）：
    · 「领用」只认真实出库。盘点盘亏 / 错料移出也写 tx_type='out'（ref_type='stocktake'），
      若算进来，一颗躺着没动、只是被盘亏调过账的料会显示「刚领用过」，呆滞漏报。
    · 还回换库位写的是 tx_type='transfer'，本就不在 out 里，这里不用再排除。
    """
    if ignore_borrowed is None:
        ignore_borrowed = _stale_ignore_borrowed(conn)
    where = []
    args = []
    if kw:
        where.append("(p.part_code LIKE ? OR p.name LIKE ?)")
        args += [f"%{kw}%", f"%{kw}%"]
    sql = """SELECT p.id, p.part_code, p.name, p.unit, p.unit_price, p.qty_decimal, p.created_at,
                    (SELECT MAX(t.created_at) FROM transactions t
                      WHERE t.part_id=p.id AND t.tx_type='out'
                        AND COALESCE(t.ref_type,'')<>'stocktake') last_out,
                    (SELECT MAX(b.borrowed_at) FROM borrows b
                      WHERE b.part_id=p.id AND COALESCE(b.status,'')!='cancelled') last_borrow
             FROM parts p
             WHERE p.deleted=0
               AND p.id IN (SELECT part_id FROM inventory GROUP BY part_id HAVING SUM(quantity)>0)
               AND """ + (" AND ".join(where) if where else "1=1") + """
             ORDER BY p.part_code"""
    rows = conn.execute(sql, args).fetchall()
    today = datetime.now().date()
    out = []
    for r in rows:
        last_out = r["last_out"]
        last_borrow = r["last_borrow"] if ignore_borrowed else None
        # 参考日 = 最后出库日 与 最后借用日 中较晚的一个；两者都没有才按建档日
        refs = [x for x in (last_out, last_borrow) if x]
        ref = max(refs) if refs else (r["created_at"] or today.strftime("%Y-%m-%d"))
        try:
            ref_date = datetime.strptime(str(ref)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            ref_date = today
        days = (today - ref_date).days
        if days < 0:
            days = 0
        is_stale = days >= stale_days
        if only_stale and not is_stale:
            continue
        d = dict(r)
        d["last_out"] = last_out
        d["last_borrow"] = r["last_borrow"]
        d["ref_by_borrow"] = bool(last_borrow and (not last_out or str(last_borrow) > str(last_out)))
        d["ref_date"] = str(ref_date)
        d["days"] = days
        d["stale"] = is_stale
        d["total_stock"] = db.total_stock(conn, r["id"])
        d["amount"] = round(d["total_stock"] * (d["unit_price"] or 0), 2)
        d["locs"] = _i18n.mk("；").join("%s ×%s" % (_loc_disp(s["location"]), s["quantity"]) for s in db.stock_locations(conn, r["id"]) if s["quantity"])
        out.append(d)
    out.sort(key=lambda x: (x["stale"] is False, -x["days"]))
    return out


def _stale_summary(conn, stale_days, top=10):
    """呆滞汇总：呆滞料号数、呆滞库存金额、呆滞 Top 列表（按未领用天数降序）"""
    rows = _stale_rows(conn, stale_days, "", only_stale=True)
    count = len(rows)
    amount = round(sum(r["amount"] for r in rows), 2)
    top_rows = rows[: max(1, top)]
    return {
        "days": stale_days,
        "count": count,
        "amount": amount,
        "top": top_rows,
    }


@app.route("/stale")
@login_required
@perm_required("stale")
def stale_page():
    """呆滞物料管理：按料号统计未领用天数，超过阈值（系统设置可配，默认90天）判定为呆滞"""
    conn = get_db()
    try:
        stale_days = _stale_days(conn)
        ignore_borrowed = _stale_ignore_borrowed(conn)
        kw = request.args.get("q", "").strip()
        f_stale = request.args.get("f_stale", "all").strip()
        rows = _stale_rows(conn, stale_days, kw, only_stale=(f_stale == "stale"),
                           ignore_borrowed=ignore_borrowed)
        if f_stale == "normal":
            rows = [r for r in rows if not r["stale"]]
    finally:
        conn.close()
    return render_template("stale.html", rows=rows, stale_days=stale_days,
                           q=kw, f_stale=f_stale, ignore_borrowed=ignore_borrowed)


@app.route("/stale/export")
@login_required
@perm_required("stale", "export")
def stale_export():
    """导出呆滞物料清单为 Excel"""
    conn = get_db()
    try:
        stale_days = _stale_days(conn)
        ignore_borrowed = _stale_ignore_borrowed(conn)
        kw = request.args.get("q", "").strip()
        f_stale = request.args.get("f_stale", "all").strip()
        rows = _stale_rows(conn, stale_days, kw, only_stale=(f_stale == "stale"),
                           ignore_borrowed=ignore_borrowed)
        if f_stale == "normal":
            rows = [r for r in rows if not r["stale"]]
    finally:
        conn.close()
    headers = ["料号", "名称", "单位", "当前库存", "库位分布", "最后领用日期", "最后借用日期",
               "参考日期", "未领用天数",
               _i18n.t("呆滞阈值(天)={d}", _i18n.current_lang(), d=stale_days),
               "状态", "单价(元)", "库存金额(元)"]
    data = []
    for r in rows:
        data.append([
            r["part_code"], r["name"], r["unit"], r["total_stock"], r["locs"],
            r["last_out"] or _xl("从未出库"),
            r.get("last_borrow") or _xl("从未借用"),
            r["ref_date"], r["days"], stale_days,
            _xl("呆滞") if r["stale"] else _xl("正常"), round(r["unit_price"] or 0, 4), r["amount"],
        ])
    buf = _export_xlsx(headers, data, "呆滞物料清单")
    return send_file(buf, as_attachment=True,
                     download_name=_i18n.t("呆滞物料清单_{ts}.xlsx", _i18n.current_lang(),
                                           ts=datetime.now().strftime("%Y%m%d_%H%M%S")),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/api/part_batches")
@login_required
def api_part_batches():
    """返回某物料在指定库位的批次明细（按入库先后=先进先出顺序），供出库提示仓管员取哪批。

    v1.0.28 mode=giveback：按规格表把「借入批次」排在最前，其次维修品，再先进先出；
    同时标出 borrowed / unborrowed，前端据此提示「未借入的批次库位」，避免把采购件
    当成借入件返还。批次真源是入库流水（tx_type='in' + out_type='other'），
    与 _stock_out_auto 的返还扣减顺序同源，不改表结构、老库零迁移。
    """
    pid = request.args.get("part_id", type=int)
    lid = request.args.get("location_id", type=int)
    mode = (request.args.get("mode") or "").strip()
    if not pid:
        return {"ok": False, "batches": []}
    conn = get_db()
    try:
        sql = """SELECT i.id, i.batch_no, i.quantity, i.in_date,
                        i.location, i.location_id,
                        CASE WHEN IFNULL(i.repair_qty,0)>0
                                  OR IFNULL(i.condition,'')='repair'
                             THEN 1 ELSE 0 END AS repair
                 FROM inventory i
                 JOIN parts p ON p.id=i.part_id
                 LEFT JOIN locations l ON l.id=i.location_id
                 WHERE i.part_id=? AND i.quantity>0"""
        args = [pid]
        if lid:
            sql += " AND i.location_id=?"
            args.append(lid)
        borrow_set = _borrow_in_batches(conn, pid) if mode == "giveback" else []
        if borrow_set:
            sql += (" ORDER BY CASE WHEN i.batch_no IN (" +
                    ",".join("?" for _ in borrow_set) + ") THEN 0 ELSE 1 END, "
                    "CASE WHEN p.is_repair=1 THEN 0 ELSE 1 END, i.in_date, i.id")
            args = args + borrow_set
        else:
            sql += " ORDER BY CASE WHEN p.is_repair=1 THEN 0 ELSE 1 END, i.in_date, i.id"
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    borrowed = set(borrow_set)
    return {"ok": True, "batches": [
        {"id": r["id"], "batch_no": r["batch_no"] or "B-LEGACY",
         "quantity": r["quantity"], "in_date": r["in_date"] or "",
         "location": r["location"] or "", "location_id": r["location_id"],
         "repair": bool(r["repair"]),
         "borrowed": (r["batch_no"] or "B-LEGACY") in borrowed,
         "unborrowed": mode == "giveback" and (r["batch_no"] or "B-LEGACY") not in borrowed}
        for r in rows
    ]}


@app.route("/inout/do", methods=["POST"])
@login_required
@perm_required("stock_inout", "execute")
def inout_do():
    """直接出入库：
    - 入库：仓库直接入库（不经过申请单）
    - 出库：与「出入库调库申请」中的出库业务一致——记录数量、申请人、使用线别、使用设备、原因；
      贵重物料出库自动转入审批流（审批经理审批通过后由仓库执行），非贵重直接扣库存"""
    f = request.form
    # 用户输入一律安全解析：以前 int("1 OR 1=1") 直接抛 ValueError → 500 + 日志里的
    # 完整 traceback（等于把源码路径外泄），任何登录用户都能低成本刷出来
    pid = _int(f.get("part_id"), 0)
    tx_type = f.get("tx_type", "")
    quantity = _num(f.get("quantity", 0))
    reason = (f.get("reason", "") or f.get("remark", "")).strip()
    if not pid or tx_type not in ("in", "out", "transfer"):
        flash(_i18n.t("参数错误", _i18n.current_lang()), "danger")
        return redirect(url_for("inout_page"))
    if quantity <= 0:
        flash(_i18n.t("数量必须大于 0", _i18n.current_lang()), "danger")
        return redirect(url_for("inout_page"))
    user = current_user()
    conn = get_db()
    try:
        p = conn.execute("SELECT * FROM parts WHERE id=? AND deleted=0", (pid,)).fetchone()
        if not p:
            flash(_i18n.t("物料不存在", _i18n.current_lang()), "danger")
            return redirect(url_for("inout_page"))
        ok, msg = _qty_valid(p, quantity)
        if not ok:
            flash(msg, "danger")
            return redirect(url_for("inout_page"))
        if tx_type == "out" and _part_pending(conn, pid):
            flash(_i18n.t("该物料当前处于「待定」状态（所有库存均无放置位置），不允许领用；请先执行调库到具体位置", _i18n.current_lang()), "danger")
            return redirect(url_for("inout_page"))
        if tx_type == "transfer":
            # ================= 直接调库：仓管员操作，库存总量不变 =================
            # v1.0.70：可传 from_inv_id 锁定批次行；老表单只传 location_id 时照旧
            from_inv_id = _int(f.get("from_inv_id") or 0)
            from_loc_id = _int(f.get("location_id") or 0)
            to_loc_id_raw = f.get("to_location_id", "").strip()
            to_mode = f.get("to_mode", "loc")
            try:
                to_loc_id = int(to_loc_id_raw) if to_loc_id_raw and to_mode == "loc" else None
            except (TypeError, ValueError):
                to_loc_id = None
            # 查源库存行
            if from_inv_id:
                src = conn.execute("SELECT * FROM inventory WHERE id=? AND part_id=?",
                                   (from_inv_id, pid)).fetchone()
            elif from_loc_id:
                src = conn.execute("SELECT * FROM inventory WHERE part_id=? AND location_id=? ORDER BY id LIMIT 1", (pid, from_loc_id)).fetchone()
            else:
                src = conn.execute("SELECT * FROM inventory WHERE part_id=? AND location_id IS NULL ORDER BY id LIMIT 1", (pid,)).fetchone()
            if not src:
                flash(_i18n.t("所选批次不存在或已变化，请刷新重选", _i18n.current_lang()) if from_inv_id
                      else _i18n.t("源库位无库存，无法调库", _i18n.current_lang()), "danger")
                return redirect(url_for("inout_page"))
            if src["quantity"] + 1e-9 < quantity:
                flash(_xl("该批次只有 {p1} 件，不足调出 {p2}", p1=src['quantity'], p2=quantity) if from_inv_id
                      else _xl("源库位现有 {p1} {p2}，不足调出 {p3}", p1=src['quantity'], p2=p['unit'], p3=quantity),
                      "danger")
                return redirect(url_for("inout_page"))
            if to_loc_id:
                if from_loc_id == to_loc_id:
                    flash(_i18n.t("源库位与目标位置相同，无需调库", _i18n.current_lang()), "warning")
                    return redirect(url_for("inout_page"))
            # v1.0.24 极限修复：调库同样受「库位属性 ↔ 配件状态」约束。
            # 以前不良品能从待维修区直接调进良品区（还可能被新建行默认写成良品），
            # 等于绕过了出库限制把坏件洗成好件。
            _src_cond = _row_get(src, "condition") or "good"
            if to_loc_id:
                _to_row = db.get_location(conn, to_loc_id)
                _to_attr = _row_get(_to_row, "attribute") or "good"
                if not _cond_attr_allowed(_src_cond, _to_attr):
                    flash(_xl("该库位属性 [{p1}] 不允许存放 [{p2}]",
                              p1=_i18n.t(LOC_ATTR_NAMES.get(_to_attr, "良品"), _i18n.current_lang()),
                              p2=_i18n.t(CONDITION_NAMES.get(_src_cond, "良品"), _i18n.current_lang())), "danger")
                    return redirect(url_for("inout_page"))
                # A 方案：待报废区 → 待维修区=报废复判转维修，原因必填 + 复判审批权限
                _re_err = _scrap_reassess_err(
                    conn, user, from_loc_id or src["location_id"], to_loc_id,
                    reason, _i18n.current_lang())
                if _re_err:
                    flash(_re_err, "danger")
                    return redirect(url_for("inout_page"))
            batch_done = ""
            if from_inv_id:
                # 批次行级：原子扣减 + 目标按批次/状态合并，批次/状态/维修件/入库日期全带走
                from_txt, to_txt, batch_done, terr = _transfer_move_inv_row(
                    conn, pid, src, to_loc_id, quantity, lang=_i18n.current_lang())
                if terr:
                    flash(terr, "danger")
                    return redirect(url_for("inout_page"))
            else:
                # 源库位扣减（v1.0.21：调库时维修标记跟着走，先记下这批里有几件维修件）
                carry = min(_inv_repair(conn, src["id"]), float(quantity or 0))
                if src["quantity"] - quantity < 1e-9:
                    conn.execute("DELETE FROM inventory WHERE id=?", (src["id"],))
                else:
                    conn.execute("UPDATE inventory SET quantity=quantity-? WHERE id=?", (quantity, src["id"],))
                # 目标库位增加：按 condition 分行合并，新行继承配件状态（不能默认良品）
                if to_loc_id:
                    tgt = conn.execute("SELECT id FROM inventory WHERE part_id=? AND location_id=? AND IFNULL(condition,'good')=? ORDER BY id LIMIT 1", (pid, to_loc_id, _src_cond)).fetchone()
                    to_loc_row = db.get_location(conn, to_loc_id)
                    to_txt = db.location_text(to_loc_row) if to_loc_row else ""
                else:
                    tgt = conn.execute("SELECT id FROM inventory WHERE part_id=? AND location_id IS NULL AND IFNULL(condition,'good')=? ORDER BY id LIMIT 1", (pid, _src_cond)).fetchone()
                    to_txt = "待定"
                if tgt:
                    conn.execute("UPDATE inventory SET quantity=quantity+? WHERE id=?", (quantity, tgt["id"]))
                    tgt_id = tgt["id"]
                else:
                    cur_t = conn.execute("INSERT INTO inventory(part_id, location_id, location, quantity, condition) VALUES(?,?,?,?,?)", (pid, to_loc_id, to_txt, quantity, _src_cond))
                    tgt_id = cur_t.lastrowid
                if carry > 0:
                    conn.execute("UPDATE inventory SET repair_qty=MIN(quantity, IFNULL(repair_qty,0)+?) WHERE id=?",
                                 (carry, tgt_id))
            # 记录交易流水
            from_txt = src["location"] or "待定"
            _btxt = (f"批次 {batch_done} ×{quantity}：" if batch_done else "")
            conn.execute(
                """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
                   ref_type,ref_id,operator_id,applicant_id,remark,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (db.gen_no("TX", conn, "transactions", "tx_no"), pid, "transfer", quantity,
                 p["unit_price"], 0, to_txt, "manual", 0, user["id"], user["id"],
                 (((_scrap_reassess_mark(reason) + _i18n.mk("；"))
                   if _is_scrap_to_repair(conn, from_loc_id or src["location_id"], to_loc_id) else "")
                  + (_btxt + f"直接调库 {quantity} {p['unit']}：{from_txt} → {to_txt} {reason}").strip()),
                 db.now_str()),
            )
            conn.commit()
            flash((_xl("批次 {p1} ×{p2}：", p1=batch_done, p2=quantity) if batch_done else "")
                  + _xl("调库成功：{p1} → {p2}，{p3} {p4}", p1=from_txt, p2=to_txt, p3=quantity, p4=p['unit']), "success")
            return redirect(url_for("inout_page"))
        if tx_type == "out":
            # ================= 出库：与申请出库一致的业务流程 =================
            # 申请人（默认当前仓管，可指定其他用户）
            try:
                applicant_id = _int(f.get("applicant_id") or 0)
            except (TypeError, ValueError):
                applicant_id = 0
            if not applicant_id:
                applicant_id = user["id"]
            app_user = conn.execute(
                "SELECT * FROM users WHERE id=? AND active=1", (applicant_id,)
            ).fetchone()
            if not app_user:
                flash(_i18n.t("所选申请人不存在或已停用", _i18n.current_lang()), "danger")
                return redirect(url_for("inout_page"))
            # 出库类型：一般（默认，业务不变）/ 退货 / 换货 / 内部转移
            _ot = _strict_out_type(f.get("out_type"))
            # v1.0.31：返修/维修只能走「借·换·检·修」→「修(维修)」，正常出库拒绝
            if _ot is None or not _normal_out_type_ok(_ot):
                flash(_i18n.t("出库类型不合法", _i18n.current_lang()), "danger")
                return redirect(url_for("inout_page"))
            # v1.0.27：物料级状态规则服务端同源校验（前端 cond_summary 同一真源）
            _oc_err = _out_cond_error(conn, pid, _ot,
                                      _out_loc_attr(conn, f.get("location_id")),
                                      _i18n.current_lang())
            if _oc_err:
                flash(_oc_err, "danger")
                return redirect(url_for("inout_page"))
            _out_person, _out_person_id = _track_out_person(user, _ot)
            _destination = (f.get("destination", "") or "").strip()
            _receiver = (f.get("receiver", "") or "").strip()
            _contact = (f.get("contact", "") or "").strip()
            _repair_type = (f.get("repair_type", "") or "").strip()
            # v1.0.27：报废不显示/不校验/不存目的地·接收人·联系方式（报废原因仍必填）
            if _ot == "scrap":
                _destination = _receiver = _contact = ""
            if _ot in OUT_KINDS:
                if _ot != "scrap":
                    if not _destination:
                        flash(_xl("{p1}出库请填写目的地或目的公司",
                                  p1=_i18n.t(_OUT_KIND_NAMES[_ot], _i18n.current_lang())), "danger")
                        return redirect(url_for("inout_page"))
                    if not _receiver or not _contact:
                        flash(_i18n.t("请填写接收人与联系方式", _i18n.current_lang()), "danger")
                        return redirect(url_for("inout_page"))
                if not reason and _ot not in _OUT_KIND_REASON_OPTIONAL:
                    flash(_xl("请填写{p1}", p1=_i18n.t(_OUT_KIND_REASONS[_ot], _i18n.current_lang())), "danger")
                    return redirect(url_for("inout_page"))
            _er = ""
            if _ot in BORROW_OUT_KINDS:
                _er = (f.get("expected_return", "") or "").strip()
                if not _er:
                    flash(_i18n.t("请选择预计返回日期", _i18n.current_lang()), "danger")
                    return redirect(url_for("inout_page"))
                _er = _er.replace("T", " ").strip()
                if len(_er) == 10:
                    _er += " 23:59:59"
            if _ot in OUT_KINDS:
                stock = db.total_stock(conn, pid)
                if _ot not in BORROW_OUT_KINDS and quantity > stock:
                    flash(_xl("出库数量超过当前库存（现有 {p1} {p2}）", p1=stock, p2=p['unit']), "danger")
                    return redirect(url_for("inout_page"))
                lv = _out_kind_level(conn, _ot, p)
                # v1.0.23：与批量 /inout/batch 统一口径 —— 借·换·检·修由仓管直接
                # 登记台账并实扣库存，不走审批；审批级别只对申请单出库生效。
                if lv >= 1 and _ot not in BORROW_OUT_KINDS:
                    a1, a2 = _stk_default_approvers(conn, app_user)
                    if not a1:
                        flash(_i18n.t("该申请人所属部门未配置一级审批人，请先在部门配置中设置", _i18n.current_lang()), "danger")
                        return redirect(url_for("inout_page"))
                    app_no = db.gen_no("APP", conn, "applications", "app_no")
                    conn.execute(
                        """INSERT INTO applications(app_no,doc_no,part_id,app_type,quantity,reason,
                           out_type,destination,receiver,contact,expected_return,applicant_id,
                           out_person,out_person_id,status,assigned_approver_id,approver2_id,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (app_no, app_no, pid, "out", quantity, reason,
                         _ot, _destination, _receiver, _contact, _er,
                         applicant_id, _out_person, _out_person_id,
                         "pending", a1, a2 if lv == 2 else None, db.now_str()),
                    )
                    conn.commit()
                    flash(_xl("{p1}出库已提交审批：{p2} x{p3}", p1=_i18n.t(_OUT_KIND_NAMES[_ot], _i18n.current_lang()),
                              p2=p['part_code'], p3=quantity), "success")
                    return redirect(url_for("inout_page"))
                # 不审批档：换货/维修/校验·测试登记借·换·检·修台账；退货/内部转移/返还直接扣库存
                if _ot in BORROW_OUT_KINDS:
                    now = db.now_str()
                    br_no = db.gen_no("BR", conn, "borrows", "borrow_no")
                    # v1.0.23：维修/换货/校验也支持指定出库库位。
                    # 报废区的件要「出维修」时必须能真的从报废区扣，否则自动跨库位
                    # 补足会先扣良品区，业务上等于把好件送修了。
                    # 未传 location_id 的老调用保持原行为（自动跨库位扣），不破坏兼容。
                    _bl_key = None
                    _raw_lid = str(f.get("location_id") or "").strip()
                    if _raw_lid and _raw_lid != "__NEW__":
                        _bl_key, _bl_txt, _bl_err = _parse_exec_location(conn, f, p, True, _ot)
                        if _bl_err:
                            flash(_bl_err, "danger")
                            return redirect(url_for("inout_page"))
                    # v1.0.19：办理即实扣库存；扣不动整单回滚，绝不只落台账不扣货
                    _ok, _msg, _deducted = _borrow_stock_out(
                        conn, pid, quantity, _bl_key, p["unit"], user, br_no,
                        _OUT_KIND_NAMES.get(_ot, _ot), out_type=_ot)
                    if not _ok:
                        conn.rollback()
                        flash(_msg, "danger")
                        return redirect(url_for("inout_page"))
                    if _ot in TRACK5_OUT_KINDS:
                        _borrower, _borrower_uid = _out_person, _out_person_id
                    else:
                        _borrower = (app_user["display_name"] or "") if app_user else ""
                        _borrower_uid = applicant_id
                    # v1.0.23：单笔办理也把出库库位写进台账，还回时才能默认带回原库位。
                    # 旧写法 27 列却写了 28 个占位符，这条分支以前被审批挡住从未执行，
                    # 统一「借·换·检·修免审批」后才暴露，会直接 500。
                    _bl_row = db.get_location(conn, _bl_key) if isinstance(_bl_key, int) else None
                    _bl_wh = (_bl_row["warehouse"] if _bl_row else "") or ""
                    conn.execute(
                        """INSERT INTO borrows(borrow_no,part_id,part_code,part_name,unit,qty,borrower,
                           borrower_user_id,borrowed_at,expected_return,status,confirmed_at,confirmed_by,
                           warehouse,location,location_id,note,op_id,created_at,updated_at,kind,
                           destination,receiver,contact,out_person,repair_type,
                           stock_deducted,deducted_qty,returned_qty,restocked_qty)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (br_no, pid, p["part_code"], p["name"], p["unit"], quantity,
                         _borrower, _borrower_uid, now,
                         _er, "borrowed", now, user["id"],
                         _bl_wh, _bl_txt or "", _bl_key if isinstance(_bl_key, int) else None,
                         _borrow_out_note(_ot, _destination, _receiver, _contact,
                                          reason, _out_person),
                         user["id"], now, now, _ot,
                         _destination, _receiver, _contact, _out_person, _repair_type,
                         1, _deducted, 0, 0))
                    if _ot == "exchange":
                        flash(_xl("换货出库完成：{p1} x{p2}，借·换·检·修台账单号 {p3}",
                                  p1=p['part_code'], p2=quantity, p3=br_no), "success")
                    else:
                        flash(_OUT_KIND_NAMES[_ot] + "出库完成：" + p["part_code"] + " x" +
                              str(quantity) + "，借·换·检·修台账单号 " + br_no, "success")
                    conn.commit()
                    return redirect(url_for("inout_page"))
                ok, msg, moves = _stock_out_auto(conn, pid, None, quantity, p["unit"], _ot)
                if not ok:
                    flash(msg, "danger")
                    return redirect(url_for("inout_page"))
                app_no = db.gen_no("APP", conn, "applications", "app_no")
                cur = conn.execute(
                    """INSERT INTO applications(app_no,doc_no,part_id,app_type,quantity,reason,
                       out_type,destination,receiver,contact,applicant_id,status,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (app_no, app_no, pid, "out", quantity, reason,
                     _ot, _destination, _receiver, _contact, applicant_id, "completed", db.now_str()),
                )
                aid = cur.lastrowid
                loc_text = _out_loc_text(moves)
                for m_loc, m_qty in moves:
                    conn.execute(
                        """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
                           out_type,ref_type,ref_id,operator_id,applicant_id,remark,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (db.gen_no("TX", conn, "transactions", "tx_no"), pid, "out", m_qty,
                         p["unit_price"], round(m_qty * p["unit_price"], 2), m_loc, _ot,
                         "application", aid, user["id"], applicant_id,
                         # v1.0.27：报废不写空的目的地/接收人/联系方式，流水只留「报废：原因」
                         ((_i18n.t(_OUT_KIND_NAMES[_ot], _i18n.current_lang())
                           if _ot == "scrap" else
                           _xl("{p1}：目的地 {p2}，接收人 {p3}，联系方式 {p4}",
                               p1=_i18n.t(_OUT_KIND_NAMES[_ot], _i18n.current_lang()),
                               p2=_destination, p3=_receiver, p4=_contact))
                          + _i18n.mk("；") + reason).strip(_i18n.mk("；")), db.now_str()),
                    )
                conn.execute(
                    "UPDATE applications SET operator_id=?, operator_note=?, processed_at=? WHERE id=?",
                    (user["id"], "仓管直接" + _OUT_KIND_NAMES[_ot] + "出库 @ " + loc_text,
                     db.now_str(), aid),
                )
                conn.commit()
                flash(_xl("{p1}出库成功：{p2} x{p3} @ {p4}",
                          p1=_i18n.t(_OUT_KIND_NAMES[_ot], _i18n.current_lang()),
                          p2=p['part_code'], p3=quantity, p4=loc_text), "success")
                return redirect(url_for("inout_page"))
            use_line = f.get("use_line", "").strip()
            use_device = f.get("use_device", "").strip()
            stock = db.total_stock(conn, pid)
            if quantity > stock:
                flash(_xl("出库数量超过当前库存（现有 {p1} {p2}）", p1=stock, p2=p['unit']), "danger")
                return redirect(url_for("inout_page"))
            # 贵重物料出库：转入审批流，审批人自动取申请人所在部门配置（一级/二级），无需手选
            # 审批设置选择「不审批」时直接出库（物料勾选强制二级的除外）
            assigned_approver_id = None
            approver2_id = None
            if p["is_valuable"] and _valuable_approval_level(conn, p) >= 1:
                assigned_approver_id, approver2_id = _stk_default_approvers(conn, app_user)
                if not assigned_approver_id:
                    flash(_i18n.t("该申请人所属部门未配置一级审批人，请先在部门配置中设置", _i18n.current_lang()), "danger")
                    return redirect(url_for("inout_page"))
                vlv = _valuable_approval_level(conn, p)
                if vlv != 2:
                    approver2_id = None
                app_no = db.gen_no("APP", conn, "applications", "app_no")
                conn.execute(
                    """INSERT INTO applications(app_no,doc_no,part_id,app_type,quantity,reason,
                       line_name,device_name,applicant_id,status,assigned_approver_id,approver2_id,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (app_no, app_no, pid, "out", quantity, reason, use_line, use_device,
                     applicant_id, "pending", assigned_approver_id, approver2_id, db.now_str()),
                )
                conn.commit()
                sup = conn.execute(
                    "SELECT display_name, email, enterprise_id, lang FROM users WHERE id=?", (assigned_approver_id,)
                ).fetchone()
                sup_name = sup["display_name"] if sup else ""
                try:
                    import notify as _notify
                    _notify.reload_settings()
                    with _i18n.lang_scope((sup["lang"] if sup else "") or ""):
                        _notify.notify_approver(
                            dict(sup) if sup else None,
                            _i18n.t("贵重出库待审批"),
                            _i18n.t("物料 {part} x{qty} 待您审批，请登录系统处理。",
                                    part=p["part_code"], qty=quantity))
                except Exception as _e:
                    pass
                if approver2_id:
                    flash(_xl("贵重物料出库已提交审批：{p1} x{p2}，待一级审批人 [{p3}] 审批", p1=p['part_code'], p2=quantity, p3=sup_name), "success")
                else:
                    flash(_xl("贵重物料出库已提交审批：{p1} x{p2}，待审批人 [{p3}] 审批后由仓库出库", p1=p['part_code'], p2=quantity, p3=sup_name), "success")
                return redirect(url_for("inout_page"))
            # 非贵重：直接出库 = 创建申请单并立即执行（扣库存 + 记流水，含申请人/线别/设备/原因）
            loc_key, loc_text, err = _parse_exec_location(conn, f, p, True, _ot)
            if err:
                flash(err, "danger")
                return redirect(url_for("inout_page"))
            app_no = db.gen_no("APP", conn, "applications", "app_no")
            cur = conn.execute(
                """INSERT INTO applications(app_no,doc_no,part_id,app_type,quantity,reason,
                   line_name,device_name,applicant_id,status,assigned_approver_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (app_no, app_no, pid, "out", quantity, reason, use_line, use_device,
                 applicant_id, "completed", None, db.now_str()),
            )
            aid = cur.lastrowid
            ok, msg, moves = _stock_out_auto(conn, pid, loc_key, quantity, p["unit"], _ot)
            if not ok:
                flash(msg, "danger")
                return redirect(url_for("inout_page"))
            loc_text = _out_loc_text(moves)
            for m_loc, m_qty in moves:
                conn.execute(
                    """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
                       out_type,ref_type,ref_id,operator_id,applicant_id,line_name,device_name,remark,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (db.gen_no("TX", conn, "transactions", "tx_no"), pid, "out", m_qty,
                     p["unit_price"], round(m_qty * p["unit_price"], 2), m_loc, _ot,
                     "application", aid, user["id"],
                     applicant_id, use_line, use_device, reason, db.now_str()),
                )
            conn.execute(
                "UPDATE applications SET operator_id=?, operator_note=?, processed_at=? WHERE id=?",
                (user["id"], "仓管直接出库", db.now_str(), aid),
            )
            conn.commit()
            flash(_xl("出库成功：{p1} x{p2} @ {p3}", p1=p['part_code'], p2=quantity, p3=loc_text), "success")
            return redirect(url_for("inout_page"))
        # ================= 入库：仓库直接入库 =================
        loc_key, loc_text, err = _parse_exec_location(conn, f, p, False)
        if err:
            flash(err, "danger")
            return redirect(url_for("inout_page"))
        # v1.0.22：入库选择配件好坏（良品/不良品/维修品），并校验库位属性约束
        _cond = f.get("condition") or "good"
        if _cond not in CONDITIONS:
            _cond = "good"
        _loc_attr = None
        if isinstance(loc_key, int):
            _lr = db.get_location(conn, loc_key)
            _loc_attr = _row_get(_lr, "attribute") if _lr else None
        if not _cond_attr_allowed(_cond, _loc_attr, _is_pending_loc(loc_key, loc_text)):
            if _cond == "defective":
                flash(_i18n.t("不良品可入待维修 / 待报废库位，或暂放待定", _i18n.current_lang()), "danger")
            else:
                flash(_xl("该库位属性 [{p1}] 不允许存放 [{p2}]",
                          p1=_i18n.t(LOC_ATTR_NAMES.get(_loc_attr or "good", "良品"), _i18n.current_lang()),
                          p2=_i18n.t(CONDITION_NAMES[_cond], _i18n.current_lang())), "danger")
            return redirect(url_for("inout_page"))
        delta = quantity
        batch_no = (f.get("batch_no", "") or "").strip()
        if not batch_no:
            batch_no = datetime.now().strftime("%Y%m%d")
        # 维修品整行视为维修件（保持 v1.0.21 先扣维修件机制）
        _repair_in = delta if _cond == "repair" else 0
        ok, msg = db.move_stock(conn, pid, loc_key, delta, batch_no=batch_no,
                               repair=_repair_in, condition=_cond)
        if not ok:
            flash(msg, "danger")
            return redirect(url_for("inout_page"))
        amount = round(quantity * p["unit_price"], 2)
        tx_no = db.gen_no("TX", conn, "transactions", "tx_no")
        # v1.0.13：直接入库也记子类型（默认采购），与申请执行/批量入口同口径
        _in_sub = _norm_in_type(f.get("sub_type") or f.get("in_type"))
        conn.execute(
            """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
               out_type,ref_type,operator_id,remark,batch_no,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tx_no, pid, "in", quantity, p["unit_price"], amount, loc_text,
             _in_sub, "direct", user["id"], reason, batch_no, db.now_str()),
        )
        conn.commit()
        flash(_xl("入库成功：{p1} x{p2} @ {p3}", p1=p['part_code'], p2=quantity, p3=loc_text), "success")
    finally:
        conn.close()
    return redirect(url_for("inout_page"))


@app.route("/inout/batch", methods=["POST"])
@login_required
@perm_required("stock_inout", "execute")
def inout_batch():
    """批量直接出入库/调库（出入库管理页统一批量操作）。
    JSON: {reason, items:[{tx_type, part_id, quantity, location_id, to_location_id,
            new_location_id, new_location, batch_no, applicant_id, use_line, use_device}]}
    每行可指定类型；全部行校验通过才执行（一个事务，任一行失败整批不动）；
    贵重出库行自动转审批（不直接扣库存）。
    """
    lang = _i18n.current_lang()

    def err(msg):
        return {"ok": False, "error": msg}, 400

    data = request.get_json(force=True, silent=True) or {}
    reason = (data.get("reason") or "").strip()
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        return err(_i18n.t("请先选择物料", lang))
    if len(items) > 50:
        return err(_xl("一次最多操作 50 行"))
    user = current_user()
    conn = get_db()
    try:
        plan = []
        out_need = {}
        borrow_need = {}
        part_cache = {}
        applicant_cache = {}
        for idx, it in enumerate(items):
            if not isinstance(it, dict):
                return err(_xl("第 {n} 行：明细格式错误", n=idx + 1))
            no = _xl("第 {n} 行：", n=idx + 1)
            tx_type = it.get("tx_type")
            if tx_type not in ("in", "out", "transfer", "borrow"):
                return err(no + _i18n.t("操作类型错误", lang))
            try:
                pid = int(it.get("part_id") or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid not in part_cache:
                part_cache[pid] = conn.execute(
                    "SELECT * FROM parts WHERE id=? AND deleted=0", (pid,)).fetchone()
            p = part_cache[pid]
            if not p:
                return err(no + _i18n.t("物料不存在", lang))
            quantity = _num(it.get("quantity", 0))
            ok, msg = _qty_valid(p, quantity)
            if not ok:
                return err(no + msg)
            e = {"tx_type": tx_type, "part": p, "quantity": quantity,
                 "location_id": None, "location_text": "", "to_location_id": None,
                 "to_txt": "", "applicant_id": user["id"],
                 "line_name": "", "device_name": "", "valuable": False,
                 "a1": None, "a2": None, "batch_no": "",
                 "expected_return": "",
                 "out_type": "general", "destination": "", "receiver": "", "contact": "",
                 "out_person": "", "out_person_id": None,
                 # 原因按行填写；请求级 reason 只作缺省（兼容一次填统一原因的调用）
                 "reason": (str(it.get("reason") or "").strip() or reason)}
            if tx_type == "out":
                ot = _strict_out_type(it.get("sub_type") or it.get("out_type"))
                # v1.0.31：正常出库子类型已移除返修/维修，直接 POST 一律拒绝
                if ot is None or not _normal_out_type_ok(ot):
                    return err(no + _i18n.t("出库类型不合法", lang))
                e["out_type"] = ot
                # v1.0.27：物料级状态规则服务端同源校验（前端 cond_summary 同一真源）
                _bc_err = _out_cond_error(
                    conn, pid, ot,
                    _out_loc_attr(conn, it.get("location_id") or it.get("from_key")),
                    lang)
                if _bc_err:
                    return err(no + _bc_err)
                e["destination"] = str(it.get("destination") or "").strip()
                e["receiver"] = str(it.get("receiver") or "").strip()
                e["contact"] = str(it.get("contact") or "").strip()
                e["repair_type"] = str(it.get("repair_type") or "").strip()
                if ot in OUT_KINDS:
                    e["out_person"], e["out_person_id"] = _track_out_person(user, ot)
                    # v1.0.27：报废不显示/不校验/不存三字段（报废原因仍必填）
                    if ot == "scrap":
                        e["destination"] = e["receiver"] = e["contact"] = ""
                        # v1.0.47：普通出库报废走两模式（系统审批 / 系统+纸档）
                        _mode = _scrap_approval_mode()
                        e["doc_mode"] = _mode
                        e["attachment_required"] = 1 if _mode == 3 else 0
                    else:
                        e["doc_mode"] = 0
                        e["attachment_required"] = 0
                    # 新出库类型：目的地/接收人/联系方式/原因必填；借·换·检·修台账类再加预计返回时间
                    if ot != "scrap":
                        if not e["destination"]:
                            return err(no + _xl("{p1}出库请填写目的地或目的公司",
                                                p1=_i18n.t(_OUT_KIND_NAMES[ot], lang)))
                        if not e["receiver"]:
                            return err(no + _i18n.t("请填写接收人", lang))
                        if not e["contact"]:
                            return err(no + _i18n.t("请填写联系方式", lang))
                    if not e["reason"] and ot not in _OUT_KIND_REASON_OPTIONAL:
                        return err(no + _xl("请填写{p1}", p1=_i18n.t(_OUT_KIND_REASONS[ot], lang)))
                    if ot in BORROW_OUT_KINDS:
                        er_raw = str(it.get("expected_return") or "").strip()
                        if not er_raw:
                            return err(no + _i18n.t("请选择预计返回日期", lang))
                        er_norm = er_raw.replace("T", " ").strip()
                        if len(er_norm) == 10:
                            er_norm += " 23:59:59"
                        elif len(er_norm) == 16:
                            er_norm += ":00"
                        e["expected_return"] = er_norm
                    try:
                        aid1 = int(it.get("applicant_id") or 0)
                    except (TypeError, ValueError):
                        aid1 = 0
                    if aid1:
                        if aid1 not in applicant_cache:
                            applicant_cache[aid1] = conn.execute(
                                "SELECT * FROM users WHERE id=? AND active=1", (aid1,)).fetchone()
                        au = applicant_cache[aid1]
                        if not au:
                            return err(no + _i18n.t("所选申请人不存在或已停用", lang))
                        e["applicant_id"] = aid1
                    if _part_pending(conn, pid):
                        return err(no + _i18n.t("该物料当前处于「待定」状态（所有库存均无放置位置），不允许领用；请先执行调库到具体位置", lang))
                    loc_key, loc_text, lerr = _parse_exec_location(conn, it, p, True, ot)
                    if lerr:
                        return err(no + lerr)
                    e["location_id"] = loc_key
                    e["location_text"] = loc_text
                    lv = _out_kind_level(conn, ot, p)
                    # v1.0.47：报废单强制至少一级审批（两模式都要审批通过后自动扣库）
                    if ot == "scrap":
                        lv = max(lv, 1)
                    # 借·换·检·修（换货/维修/校验·测试）直接登记台账，不走审批档（与单笔出库口径一致）
                    if lv >= 1 and ot not in BORROW_OUT_KINDS:
                        a1, a2 = _stk_default_approvers(
                            conn, applicant_cache.get(e["applicant_id"]) or user)
                        if not a1:
                            return err(no + _i18n.t("该申请人所属部门未配置一级审批人，请先在部门配置中设置", lang))
                        e["valuable"] = True  # 复用「待审批」的锁定/列表口径
                        e["a1"] = a1
                        e["a2"] = a2 if lv == 2 else None
                    if ot in BORROW_OUT_KINDS:
                        # 换货/维修/校验·测试走台账，办理时实扣库存（v1.0.19 起）；建单阶段不重复占用可用量
                        borrow_need[pid] = borrow_need.get(pid, 0.0) + quantity
                    else:
                        out_need[pid] = out_need.get(pid, 0.0) + quantity
                else:
                    if not e["reason"]:
                        return err(no + _i18n.t("请填写申请原因", lang))
                    try:
                        aid1 = int(it.get("applicant_id") or 0)
                    except (TypeError, ValueError):
                        aid1 = 0
                    if aid1:
                        if aid1 not in applicant_cache:
                            applicant_cache[aid1] = conn.execute(
                                "SELECT * FROM users WHERE id=? AND active=1", (aid1,)).fetchone()
                        au = applicant_cache[aid1]
                        if not au:
                            return err(no + _i18n.t("所选申请人不存在或已停用", lang))
                        e["applicant_id"] = aid1
                    # v1.0.39：只有「领用」才要求线别/设备；出库「其它」不挂线体，
                    # 只留领用人 + 扣减库位（用户口径：直接出库的选填框就这两项）。
                    if ot == "general":
                        use_line = str(it.get("use_line") or "").strip()
                        use_device = str(it.get("use_device") or "").strip()
                        offline = bool(it.get("offline")) or use_line in ("离线", "Offline")
                        if not use_device or (not use_line and not offline):
                            return err(no + _i18n.t("出库明细请选择使用线别与设备", lang))
                        # 离线设备：线体留空（报表里归到未分配），设备名照常记录
                        e["line_name"] = "" if offline else use_line
                        e["device_name"] = use_device
                    if _part_pending(conn, pid):
                        return err(no + _i18n.t("该物料当前处于「待定」状态（所有库存均无放置位置），不允许领用；请先执行调库到具体位置", lang))
                    out_need[pid] = out_need.get(pid, 0.0) + quantity
                    loc_key, loc_text, lerr = _parse_exec_location(conn, it, p, True, ot)
                    if lerr:
                        return err(no + lerr)
                    e["location_id"] = loc_key
                    e["location_text"] = loc_text
                    if p["is_valuable"] and _valuable_approval_level(conn, p) >= 1:
                        a1, a2 = _stk_default_approvers(
                            conn, applicant_cache.get(e["applicant_id"]) or user)
                        if not a1:
                            return err(no + _i18n.t("该申请人所属部门未配置一级审批人，请先在部门配置中设置", lang))
                        if _valuable_approval_level(conn, p) != 2:
                            a2 = None
                        e["valuable"] = True
                        e["a1"] = a1
                        e["a2"] = a2
            elif tx_type == "in":
                # v1.0.13：入库子类型（采购/返修/转入/其它），默认采购
                e["out_type"] = _norm_in_type(it.get("sub_type") or it.get("in_type"))
                loc_key, loc_text, lerr = _parse_exec_location(conn, it, p, False)
                if lerr:
                    return err(no + lerr)
                # v1.0.22：入库配件好坏 + 库位属性约束
                _cond = str(it.get("condition") or "good")
                if _cond not in CONDITIONS:
                    _cond = "good"
                _loc_attr = None
                if isinstance(loc_key, int):
                    _lr = db.get_location(conn, loc_key)
                    _loc_attr = _row_get(_lr, "attribute") if _lr else None
                if not _cond_attr_allowed(_cond, _loc_attr, _is_pending_loc(loc_key, loc_text)):
                    if _cond == "defective":
                        return err(no + _i18n.t("不良品可入待维修 / 待报废库位，或暂放待定", lang))
                    return err(no + _xl("该库位属性 [{p1}] 不允许存放 [{p2}]",
                                        p1=_i18n.t(LOC_ATTR_NAMES.get(_loc_attr or "good", "良品"), _i18n.current_lang()),
                                        p2=_i18n.t(CONDITION_NAMES[_cond], _i18n.current_lang())))
                e["condition"] = _cond
                e["location_id"] = loc_key
                e["location_text"] = loc_text
                e["batch_no"] = (str(it.get("batch_no") or "").strip()
                                 or datetime.now().strftime("%Y%m%d"))
            elif tx_type == "transfer":
                # v1.0.70：源按库存行 id 精确定位（inv_<id>），老 loc_<id> 仍兼容
                from_key = str(it.get("from_key") or it.get("location_id") or "").strip()
                _src_lines, src_mode, src = _parse_transfer_source(conn, pid, from_key)
                if src is None:
                    return err(no + (_i18n.t("所选批次不存在或已变化，请刷新重选", lang)
                                     if src_mode == "batch" else
                                     _i18n.t("请选择调库的源库位（移哪个位置的料）", lang)))
                to_raw = str(it.get("to_location_id") or "").strip()
                try:
                    to_id = int(to_raw) if to_raw and to_raw != "PENDING" else None
                except (TypeError, ValueError):
                    to_id = None
                if to_id:
                    if src["location_id"] == to_id:
                        return err(no + _i18n.t("源库位与目标位置相同，无需调库", lang))
                elif src["location_id"] is None:
                    return err(no + _i18n.t("该库位已在「待定」状态，请选择目标位置", lang))
                if src_mode == "whole":
                    # 整库位数量由前端按合计自动填入，服务端再卡一次，防绕过页面改数
                    if abs(float(src["quantity"]) - quantity) > 1e-6:
                        return err(no + _xl("整库位调库数量为该库位合计 {p1}，不可修改",
                                            p1=src["quantity"], lang=lang))
                elif src["quantity"] + 1e-9 < quantity:
                    if src_mode == "batch" and str(_row_get(src, "batch_no") or ""):
                        return err(no + _xl("该批次只有 {p1} 件，不足调出 {p2}",
                                            p1=src["quantity"], p2=quantity, lang=lang))
                    return err(no + _xl("源库位现有 {p1} {p2}，不足调出 {p3}",
                                        p1=src["quantity"], p2=p["unit"], p3=quantity, lang=lang))
                e["from_location_id"] = src["location_id"]
                e["from_txt"] = src["location"] or "待定"
                e["whole_loc"] = 1 if src_mode == "whole" else 0
                e["from_inv_id"] = int(src["inv_id"]) if src_mode == "batch" else None
                e["to_location_id"] = to_id
                to_row = db.get_location(conn, to_id) if to_id else None
                e["to_txt"] = db.location_text(to_row) if to_row else "待定"
                # A 方案：待报废 → 待维修复判调库，原因必填 + 复判审批权限
                _re_err = _scrap_reassess_err(conn, user, src["location_id"], to_id,
                                              e["reason"], lang)
                if _re_err:
                    return err(no + _re_err)
            elif tx_type == "borrow":
                # v1.0.19：直接办理借·换·检·修 —— 先实扣库存再登记台账（贵重品也不走审批）
                ot = _norm_borrow_sub(it.get("sub_type") or it.get("out_type"))
                e["out_type"] = ot
                e["destination"] = str(it.get("destination") or "").strip()
                e["receiver"] = str(it.get("receiver") or "").strip()
                e["contact"] = str(it.get("contact") or "").strip()
                e["repair_type"] = str(it.get("repair_type") or "").strip()
                e["out_person"], e["out_person_id"] = _track_out_person(user, ot)
                if ot in ("exchange", "maintenance", "verify"):
                    if not e["destination"]:
                        return err(no + _xl("{p1}出库请填写目的公司/地址",
                                            p1=_i18n.t(_OUT_KIND_NAMES.get(ot, ot), lang)))
                    if not e["receiver"]:
                        return err(no + _i18n.t("请填写接收人", lang))
                    if not e["contact"]:
                        return err(no + _i18n.t("请填写联系方式", lang))
                # v1.0.44：移除维修品类型必填校验——前端自 v1.0.21 起已不在出库时渲染该框
                # （修好退回入库时就标成维修件，出库只看库位）。留空不再拦单，字段仍照常落库。
                er_raw = str(it.get("expected_return") or "").strip()
                if not er_raw:
                    return err(no + _i18n.t("请选择预计归还日", lang))
                er_norm = er_raw.replace("T", " ").strip()
                if len(er_norm) == 10:
                    er_norm += " 23:59:59"
                elif len(er_norm) == 16:
                    er_norm += ":00"
                e["expected_return"] = er_norm
                # 借用人：默认当前登录用户，可选其他在册用户（代登记）
                try:
                    aid1 = int(it.get("applicant_id") or 0)
                except (TypeError, ValueError):
                    aid1 = 0
                if aid1:
                    if aid1 not in applicant_cache:
                        applicant_cache[aid1] = conn.execute(
                            "SELECT * FROM users WHERE id=? AND active=1", (aid1,)).fetchone()
                    au = applicant_cache[aid1]
                    if not au:
                        return err(no + _i18n.t("所选借用人不存在或已停用", lang))
                    e["applicant_id"] = aid1
                if _part_pending(conn, pid):
                    return err(no + _i18n.t("该物料当前处于「待定」状态（所有库存均无放置位置），不允许领用；请先执行调库到具体位置", lang))
                # 从哪个库位拿货：跟出库一样必须选一个该物料真有库存的位置
                loc_key, loc_text, lerr = _parse_exec_location(conn, it, p, True, ot)
                if lerr:
                    return err(no + lerr)
                at_loc = conn.execute(
                    "SELECT COALESCE(SUM(quantity),0) q FROM inventory WHERE part_id=? AND location_id=?",
                    (pid, loc_key)).fetchone()["q"]
                if at_loc + 1e-9 < quantity:
                    return err(no + _xl("借出库位 [{p1}] 现有 {p2} {p3}，不足借出 {p4} {p3}",
                                        p1=loc_text, p2=round(at_loc, 3), p3=p["unit"],
                                        p4=quantity))
                e["location_id"] = loc_key
                e["location_text"] = loc_text
                borrow_need[pid] = borrow_need.get(pid, 0.0) + quantity
            plan.append(e)
        # 出库/借出可用库存汇总校验（已被申请锁定与在借占用的数量不可再出）
        for pid, need in out_need.items():
            p = part_cache[pid]
            stock = db.total_stock(conn, pid)
            locked = conn.execute(
                """SELECT COALESCE(SUM(a.quantity),0) s FROM applications a JOIN parts pp ON pp.id=a.part_id
                   WHERE a.part_id=? AND (
                     (a.app_type='out' AND ((pp.is_valuable=1 AND a.status='approved')
                        OR ((pp.is_valuable=0 AND a.status='pending') OR (a.app_type='out' AND IFNULL(a.out_type,'') IN ('return','exchange','internal','giveback','maintenance','verify','scrap') AND a.status IN ('pending','pending2','approved')))))
                     OR (a.app_type='borrow' AND a.status IN ('pending','approved'))
                   )""",
                (pid,)).fetchone()["s"]
            in_use = _borrow_in_use(conn, pid)
            if need > stock - locked - in_use + 1e-9:
                msg = _xl("库存不足：[{code}] 可用仅 {p1} {p2}，本批共需出库 {p3} {p2}",
                           code=p["part_code"], p1=round(stock - locked - in_use, 3),
                           p2=p["unit"], p3=need)
                if in_use > 0:
                    msg += _xl("（另有 {q} {u} 被借用占用）", q=round(in_use, 3), u=p["unit"])
                return err(msg)
        for pid, need in borrow_need.items():
            p = part_cache[pid]
            stock = db.total_stock(conn, pid)
            locked = conn.execute(
                """SELECT COALESCE(SUM(a.quantity),0) s FROM applications a JOIN parts pp ON pp.id=a.part_id
                   WHERE a.part_id=? AND (
                     (a.app_type='out' AND ((pp.is_valuable=1 AND a.status='approved')
                        OR ((pp.is_valuable=0 AND a.status='pending') OR (a.app_type='out' AND IFNULL(a.out_type,'') IN ('return','exchange','internal','giveback','maintenance','verify','scrap') AND a.status IN ('pending','pending2','approved')))))
                     OR (a.app_type='borrow' AND a.status IN ('pending','approved'))
                   )""",
                (pid,)).fetchone()["s"]
            in_use = _borrow_in_use(conn, pid)
            if need > stock - locked - in_use + 1e-9:
                return err(_xl("库存不足：[{code}] 当前可用仅 {p1} {p2}，本批借·换·检·修共需 {p4} {p2}；办理后会扣减库存，还回时恢复",
                               code=p["part_code"], p1=round(stock - locked - in_use, 3), p2=p["unit"],
                               p4=need))
        # 执行（一个事务，任一行异常整批回滚）
        # v1.0.44：按业务类型统计转审批的行，前端提示据此显示真实类型（内部转移/退货/贵重领用…），
        # 不再把任何转审批都说成「贵重出库已提交审批」。
        n_exec = n_appr = 0
        submitted_kinds = {}
        try:
            for e in plan:
                p = e["part"]
                pid = p["id"]
                qty = e["quantity"]
                if e["tx_type"] == "transfer":
                    batch_done = ""
                    if e.get("whole_loc"):
                        # v1.0.78：整库位——一次把该库位所有批次搬到目标，不用逐批重复建单
                        if e["from_location_id"]:
                            all_rows = conn.execute(
                                "SELECT * FROM inventory WHERE part_id=? AND location_id=? ORDER BY id",
                                (pid, e["from_location_id"])).fetchall()
                        else:
                            all_rows = conn.execute(
                                "SELECT * FROM inventory WHERE part_id=? AND location_id IS NULL ORDER BY id",
                                (pid,)).fetchall()
                        if not all_rows:
                            raise RuntimeError(_i18n.t("源库位无库存，无法调库", lang))
                        frm, e["to_txt"], batch_done, terr = _transfer_move_whole_location(
                            conn, pid, all_rows, e["to_location_id"], qty, lang=lang)
                        if terr:
                            raise RuntimeError(terr)
                        e["from_txt"] = frm
                    elif e.get("from_inv_id"):
                        src = conn.execute("SELECT * FROM inventory WHERE id=? AND part_id=?",
                                           (e["from_inv_id"], pid)).fetchone()
                        if not src:
                            raise RuntimeError(_i18n.t("所选批次不存在或已变化，请刷新重选", lang))
                        frm, e["to_txt"], batch_done, terr = _transfer_move_inv_row(
                            conn, pid, src, e["to_location_id"], qty, lang=lang)
                        if terr:
                            raise RuntimeError(terr)
                        e["from_txt"] = frm
                    else:
                        if e["from_location_id"]:
                            src = conn.execute(
                                "SELECT * FROM inventory WHERE part_id=? AND location_id=? ORDER BY id LIMIT 1",
                                (pid, e["from_location_id"])).fetchone()
                        else:
                            src = conn.execute(
                                "SELECT * FROM inventory WHERE part_id=? AND location_id IS NULL ORDER BY id LIMIT 1",
                                (pid,)).fetchone()
                        if not src:
                            raise RuntimeError(_xl("源库位无库存：{code}", code=p["part_code"]))
                        carry = min(_inv_repair(conn, src["id"]), float(qty or 0))
                        if src["quantity"] - qty < 1e-9:
                            conn.execute("DELETE FROM inventory WHERE id=?", (src["id"],))
                        else:
                            conn.execute("UPDATE inventory SET quantity=quantity-? WHERE id=?", (qty, src["id"]))
                        if e["to_location_id"]:
                            tgt = conn.execute(
                                "SELECT id FROM inventory WHERE part_id=? AND location_id=? ORDER BY id LIMIT 1",
                                (pid, e["to_location_id"])).fetchone()
                        else:
                            tgt = conn.execute(
                                "SELECT id FROM inventory WHERE part_id=? AND location_id IS NULL ORDER BY id LIMIT 1",
                                (pid,)).fetchone()
                        if tgt:
                            conn.execute("UPDATE inventory SET quantity=quantity+? WHERE id=?", (qty, tgt["id"]))
                            tgt_id = tgt["id"]
                        else:
                            cur_t = conn.execute(
                                "INSERT INTO inventory(part_id, location_id, location, quantity) VALUES(?,?,?,?)",
                                (pid, e["to_location_id"], e["to_txt"], qty))
                            tgt_id = cur_t.lastrowid
                        if carry > 0:
                            conn.execute("UPDATE inventory SET repair_qty=MIN(quantity, IFNULL(repair_qty,0)+?) WHERE id=?",
                                         (carry, tgt_id))
                    _btxt = (f"批次 {batch_done} ×{qty}：" if batch_done else "")
                    conn.execute(
                        """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
                           ref_type,ref_id,operator_id,applicant_id,remark,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (db.gen_no("TX", conn, "transactions", "tx_no"), pid, "transfer", qty,
                         p["unit_price"], 0, e["to_txt"], "manual", 0, user["id"], user["id"],
                         (((_scrap_reassess_mark(e["reason"]) + _i18n.mk("；"))
                           if _is_scrap_to_repair(conn, e["from_location_id"], e["to_location_id"]) else "")
                          + (_btxt + f"直接调库 {qty} {p['unit']}：{e['from_txt']} → {e['to_txt']} {e['reason']}").strip()),
                         db.now_str()))
                    n_exec += 1
                elif e["tx_type"] == "in":
                    _cond = e.get("condition") or "good"
                    _repair_in = qty if _cond == "repair" else 0
                    ok, msg = db.move_stock(conn, pid, e["location_id"], qty,
                                           batch_no=e["batch_no"], repair=_repair_in, condition=_cond)
                    if not ok:
                        raise RuntimeError(msg)
                    conn.execute(
                        """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
                           out_type,ref_type,operator_id,remark,batch_no,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (db.gen_no("TX", conn, "transactions", "tx_no"), pid, "in", qty,
                         p["unit_price"], round(qty * p["unit_price"], 2), e["location_text"],
                         e["out_type"], "direct", user["id"], e["reason"], e["batch_no"], db.now_str()))
                    n_exec += 1
                elif e["tx_type"] == "out":
                    ot = e.get("out_type") or "general"
                    if ot in OUT_KINDS and e["valuable"]:
                        # 退货/换货/内部转移：按设置的审批档位转审批（不审批档已在上面直接执行）
                        app_no = db.gen_no("APP", conn, "applications", "app_no")
                        conn.execute(
                            """INSERT INTO applications(app_no,doc_no,part_id,app_type,quantity,reason,
                               out_type,destination,receiver,contact,expected_return,applicant_id,
                               out_person,out_person_id,status,assigned_approver_id,approver2_id,created_at,
                               condition,doc_mode,attachment_required,from_location_id,doc_submitted)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (app_no, app_no, pid, "out", qty, e["reason"],
                         ot, e["destination"], e["receiver"], e["contact"],
                         e["expected_return"], e["applicant_id"],
                         e["out_person"], e["out_person_id"],
                         "pending", e["a1"], e["a2"], db.now_str(),
                         "defective" if ot == "scrap" else "good",
                         e.get("doc_mode", 0), e.get("attachment_required", 0),
                         e.get("location_id") or None,
                         (0 if e.get("attachment_required") else 1)))
                        n_appr += 1
                        submitted_kinds[ot] = submitted_kinds.get(ot, 0) + 1
                    elif ot in BORROW_OUT_KINDS:
                        # v1.0.19：换货/维修/校验·测试免审批 —— 先实扣库存再登记台账
                        if ot in TRACK5_OUT_KINDS:
                            _borrower, _borrower_uid = e["out_person"], e["out_person_id"]
                        else:
                            bu = conn.execute("SELECT display_name FROM users WHERE id=?",
                                              (e["applicant_id"],)).fetchone()
                            _borrower = (bu["display_name"] if bu else "") or ""
                            _borrower_uid = e["applicant_id"]
                        now = db.now_str()
                        br_no = db.gen_no("BR", conn, "borrows", "borrow_no")
                        _ok, _msg, _deducted = _borrow_stock_out(
                            conn, pid, qty, e.get("location_id") or None, p["unit"],
                            user, br_no, _OUT_KIND_NAMES.get(ot, ot), out_type=ot)
                        if not _ok:
                            raise RuntimeError("%s：%s" % (p["part_code"], _msg))
                        loc_row = db.get_location(conn, e.get("location_id")) if e.get("location_id") else None
                        wh = (loc_row["warehouse"] if loc_row else "") or ""
                        conn.execute(
                            """INSERT INTO borrows(borrow_no,part_id,part_code,part_name,unit,qty,borrower,
                               borrower_user_id,borrowed_at,expected_return,status,confirmed_at,confirmed_by,
                               warehouse,location,location_id,note,op_id,created_at,updated_at,kind,
                               destination,receiver,contact,out_person,repair_type,
                               stock_deducted,deducted_qty,returned_qty,restocked_qty)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (br_no, pid, p["part_code"], p["name"], p["unit"], qty,
                             _borrower, _borrower_uid, now,
                             e["expected_return"] or "", "borrowed", now, user["id"],
                             wh, e.get("location_text") or "", e.get("location_id") or None,
                             _borrow_out_note(ot, e["destination"], e["receiver"], e["contact"],
                                              e["reason"], e["out_person"]),
                             user["id"], now, now, ot,
                             e["destination"], e["receiver"], e["contact"], e["out_person"], e.get("repair_type",""),
                             1, _deducted, 0, 0))
                        app_no = db.gen_no("APP", conn, "applications", "app_no")
                        cur = conn.execute(
                            """INSERT INTO applications(app_no,doc_no,part_id,app_type,quantity,reason,
                               out_type,destination,receiver,contact,expected_return,applicant_id,
                               out_person,out_person_id,status,created_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (app_no, app_no, pid, "out", qty, e["reason"],
                             ot, e["destination"], e["receiver"], e["contact"],
                             e["expected_return"], e["applicant_id"],
                             e["out_person"], e["out_person_id"], "completed", now))
                        _op_note = (_xl("仓管直接换货出库，借·换·检·修台账单号 {p1}", p1=br_no)
                                    if ot == "exchange" else
                                    "仓管直接" + _OUT_KIND_NAMES[ot] + "出库，借·换·检·修台账单号 " + br_no)
                        conn.execute(
                            "UPDATE applications SET operator_id=?, operator_note=?, processed_at=? WHERE id=?",
                            (user["id"], _op_note, now, cur.lastrowid))
                        n_exec += 1
                    elif e["valuable"]:
                        app_no = db.gen_no("APP", conn, "applications", "app_no")
                        conn.execute(
                            """INSERT INTO applications(app_no,doc_no,part_id,app_type,quantity,reason,
                               line_name,device_name,out_type,applicant_id,status,assigned_approver_id,approver2_id,created_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (app_no, app_no, pid, "out", qty, e["reason"], e["line_name"], e["device_name"],
                             ot, e["applicant_id"], "pending", e["a1"], e["a2"], db.now_str()))
                        n_appr += 1
                        submitted_kinds[ot] = submitted_kinds.get(ot, 0) + 1
                    else:
                        ok, msg, moves = _stock_out_auto(conn, pid, e["location_id"], qty, p["unit"], ot)
                        if not ok:
                            raise RuntimeError(msg)
                        out_txt = _out_loc_text(moves)
                        _ot_cols = ""
                        _ot_vals = []
                        if ot in OUT_KINDS or ot == "misc":
                            # 退货/内部转移/其它免审批：直接扣库存，业务字段随单留痕
                            _ot_cols = ",out_type,destination,receiver,contact"
                            _ot_vals = [ot, e["destination"], e["receiver"], e["contact"]]
                        app_no = db.gen_no("APP", conn, "applications", "app_no")
                        cur = conn.execute(
                            """INSERT INTO applications(app_no,doc_no,part_id,app_type,quantity,reason,
                               line_name,device_name,applicant_id,status,assigned_approver_id,created_at%s)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?%s)""" % (_ot_cols, ",?" * len(_ot_vals)),
                            (app_no, app_no, pid, "out", qty, e["reason"], e["line_name"], e["device_name"],
                             e["applicant_id"], "completed", None, db.now_str(), *_ot_vals))
                        aid1 = cur.lastrowid
                        _op_note = "仓管直接出库"
                        if ot in OUT_KINDS or ot == "misc":
                            _op_note = "仓管直接" + _OUT_KIND_NAMES[ot] + "出库"
                        for m_loc, m_qty in moves:
                            conn.execute(
                                """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
                                   out_type,ref_type,ref_id,operator_id,applicant_id,line_name,device_name,remark,created_at)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (db.gen_no("TX", conn, "transactions", "tx_no"), pid, "out", m_qty,
                                 p["unit_price"], round(m_qty * p["unit_price"], 2), m_loc, ot,
                                 "application", aid1, user["id"], e["applicant_id"],
                                 e["line_name"], e["device_name"], e["reason"], db.now_str()))
                        conn.execute(
                            "UPDATE applications SET operator_id=?, operator_note=?, processed_at=? WHERE id=?",
                            (user["id"], (_op_note + (" @ " + out_txt if len(moves) > 1 else "")).strip(),
                             db.now_str(), aid1))
                        n_exec += 1
                elif e["tx_type"] == "borrow":
                    # v1.0.19：直接办理借·换·检·修 —— 先实扣库存再登记台账 + 留痕申请单（completed）
                    _kind = e.get("out_type") or "borrow"
                    bu = conn.execute("SELECT display_name FROM users WHERE id=?",
                                      (e["applicant_id"],)).fetchone()
                    now = db.now_str()
                    br_no = db.gen_no("BR", conn, "borrows", "borrow_no")
                    _ok, _msg, _deducted = _borrow_stock_out(
                        conn, pid, qty, e.get("location_id") or None, p["unit"],
                        user, br_no, _i18n.t(_OUT_KIND_NAMES.get(_kind, "借用"),
                                             _i18n.current_lang()), out_type=_kind)
                    if not _ok:
                        raise RuntimeError("%s：%s" % (p["part_code"], _msg))
                    loc_row = db.get_location(conn, e["location_id"]) if e.get("location_id") else None
                    wh = (loc_row["warehouse"] if loc_row else "") or ""
                    if _kind == "borrow":
                        _note = e["reason"]
                    else:
                        _note = _borrow_out_note(_kind, e.get("destination", ""),
                                                 e.get("receiver", ""), e.get("contact", ""),
                                                 e["reason"], e.get("out_person", ""))
                    conn.execute(
                        """INSERT INTO borrows(borrow_no,part_id,part_code,part_name,unit,qty,borrower,
                           borrower_user_id,borrowed_at,expected_return,status,confirmed_at,confirmed_by,
                           warehouse,location,location_id,note,op_id,created_at,updated_at,
                           kind,destination,receiver,contact,out_person,repair_type,
                           stock_deducted,deducted_qty,returned_qty,restocked_qty)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (br_no, pid, p["part_code"], p["name"], p["unit"], qty,
                         (bu["display_name"] if bu else "") or "", e["applicant_id"], now,
                         e["expected_return"] or "", "borrowed", now, user["id"],
                         wh, e.get("location_text") or "", e.get("location_id") or None,
                         _note, user["id"], now, now,
                         _kind, e.get("destination", ""), e.get("receiver", ""),
                         e.get("contact", ""), e.get("out_person", ""),
                         e.get("repair_type", ""),
                         1, _deducted, 0, 0))
                    app_no = db.gen_no("APP", conn, "applications", "app_no")
                    cur = conn.execute(
                        """INSERT INTO applications(app_no,doc_no,part_id,app_type,quantity,reason,
                           applicant_id,status,expected_return,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (app_no, app_no, pid, "borrow", qty, e["reason"],
                         e["applicant_id"], "completed", e["expected_return"] or "", now))
                    conn.execute(
                        "UPDATE applications SET operator_id=?, operator_note=?, processed_at=? WHERE id=?",
                        (user["id"], _xl("仓管直接借出，台账单号 {p1}", p1=br_no), now, cur.lastrowid))
                    n_exec += 1
            if n_appr:
                try:
                    import notify as _notify
                    _notify.reload_settings()
                    seen = {}
                    for e in plan:
                        if e["tx_type"] == "out" and e["valuable"]:
                            seen[e["a1"]] = seen.get(e["a1"], 0) + 1
                    _any_kind = any(e["tx_type"] == "out" and e.get("out_type") in OUT_KINDS
                                    for e in plan)
                    for aid1, n in seen.items():
                        sup = conn.execute("SELECT * FROM users WHERE id=?", (aid1,)).fetchone()
                        with _i18n.lang_scope((sup["lang"] if sup else "") or ""):
                            _notify.notify_approver(
                                dict(sup) if sup else None,
                                _i18n.t("出库待审批") if _any_kind else _i18n.t("贵重出库待审批"),
                                _i18n.t("{n} 行物料出库待您审批，请登录系统处理。" if _any_kind
                                        else "{n} 行贵重物料出库待您审批，请登录系统处理。", n=n))
                except Exception:
                    pass
            conn.commit()
        except RuntimeError as _e:
            conn.rollback()
            return err(str(_e))
        return {"ok": True, "executed": n_exec, "submitted": n_appr,
                "submitted_kinds": submitted_kinds}
    finally:
        conn.close()
@app.route("/transfer")
@login_required
@perm_required("apply")
def transfer_page():
    preset_part = request.args.get("part_id", type=int)
    return redirect(url_for("applications_page", part_id=preset_part, app_type="transfer") if preset_part
                    else url_for("applications_page", app_type="transfer"))


@app.route("/transfer/do", methods=["POST"])
@login_required
@perm_required("apply")
def transfer_do():
    return redirect(url_for("applications_page"))


# ------------------------------------------------------------------
# 出入库调库申请（普通用户发起，仓库执行，高级审批贵重出库）
# 说明：申请单列表只显示本用户记录；支持按 日期/类别/状态 筛选与导出 Excel；
#       pending 可直接撤销；已完成/已批准的申请可提交「撤销请求」，由仓管员审批后回滚
# ------------------------------------------------------------------
@app.route("/applications")
@login_required
@perm_required("apply")
def applications_page():
    user = current_user()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    time_from = request.args.get("time_from", "").strip()
    time_to = request.args.get("time_to", "").strip()
    f_type = request.args.get("f_type", "").strip()
    f_status = request.args.get("f_status", "").strip()
    conn = get_db()
    try:
        where = ["a.applicant_id=?"]
        args = [user["id"]]
        dt_where(where, args, "a.created_at", date_from, date_to, time_from, time_to)
        if f_type in ("in", "out", "transfer", "borrow"):
            where.append("a.app_type=?")
            args.append(f_type)
        if f_status in ("pending", "approved", "rejected", "completed", "cancel_req", "canceled"):
            where.append("a.status=?")
            args.append(f_status)
        sql = """SELECT a.*, p.part_code, p.name, p.unit, p.is_valuable, u.display_name app_name,
                        COALESCE(am.display_name, ap1.display_name) approver_name,
                        CASE WHEN l.bin='' THEN l.warehouse||'-'||l.rack||'-'||l.shelf
                             ELSE l.warehouse||'-'||l.rack||'-'||l.shelf||'-'||l.bin END AS location_text,
                        CASE WHEN fl.bin='' THEN fl.warehouse||'-'||fl.rack||'-'||fl.shelf
                             ELSE fl.warehouse||'-'||fl.rack||'-'||fl.shelf||'-'||fl.bin END AS from_loc_text,
                        CASE WHEN tl.bin='' THEN tl.warehouse||'-'||tl.rack||'-'||tl.shelf
                             ELSE tl.warehouse||'-'||tl.rack||'-'||tl.shelf||'-'||tl.bin END AS to_loc_text
                 FROM applications a
                 LEFT JOIN parts p ON a.part_id=p.id
                 LEFT JOIN locations l ON p.location_id=l.id
                 LEFT JOIN locations fl ON a.from_location_id=fl.id
                 LEFT JOIN locations tl ON a.to_location_id=tl.id
                 LEFT JOIN users u ON a.applicant_id=u.id
                 LEFT JOIN users am ON a.assigned_approver_id=am.id
                 LEFT JOIN users ap1 ON a.approver_id=ap1.id
                 WHERE """ + " AND ".join(where) + """
                 ORDER BY a.id DESC LIMIT 2000"""
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        # 多库位：申请单列表的位置列显示该物料所有存放库位（便于仓管取放料）
        for r in rows:
            sls = db.stock_locations(conn, r["part_id"])
            r["all_locs_text"] = _i18n.mk("；").join(
                f"{s['location']} ×{s['quantity']}" for s in sls if s["quantity"])
        # 按申请单（doc_no）分组：一张单 = 一组明细行
        docs, order = {}, []
        for r in rows:
            key = r["doc_no"] or r["app_no"]
            if key not in docs:
                docs[key] = {"doc_no": key, "app_type": r["app_type"], "reason": r["reason"] or "",
                             "created_at": r["created_at"], "rows": []}
                order.append(key)
            docs[key]["rows"].append(r)
        doc_list = [docs[k] for k in order]
        lang = _i18n.current_lang()
        STS = {"pending": "status_pending", "pending2": "status_pending", "approved": "status_approved",
               "rejected": "status_rejected", "completed": "status_completed",
               "cancel_req": "status_cancel_req", "canceled": "status_canceled"}
        for d in doc_list:
            sts = [r["status"] for r in d["rows"]]
            # v1.0.46：报废单模式标记 + 附件计数（纸档模式审批闸门前端提示用）
            d["doc_mode"] = d["rows"][0].get("doc_mode") or 0
            d["attachment_required"] = d["rows"][0].get("attachment_required") or 0
            # v1.0.51：纸档单提交闸门（0=待提交签字档，1=已提交进审批）
            d["doc_submitted"] = all((r.get("doc_submitted") or 0) for r in d["rows"]) \
                if d["attachment_required"] else 1
            if d["attachment_required"]:
                d["attach_count"] = conn.execute(
                    "SELECT COUNT(*) c FROM scrap_attachments WHERE doc_no=?",
                    (d["doc_no"],)).fetchone()["c"]
            else:
                d["attach_count"] = 0
            d["n_rows"] = len(sts)
            d["all_pending"] = all(s == "pending" for s in sts)
            # v1.0.52：整单被驳回 → 可「重新修改提交」/「删除废单」
            d["all_rejected"] = all(s == "rejected" for s in sts)
            # v1.0.48：仓管已执行完成（completed）不允许再申请撤销；仅「已批准·待出库」可申请
            d["all_undoable"] = all(s == "approved" for s in sts)
            d["has_undoable"] = any(s == "approved" for s in sts)
            d["has_cancel_req"] = any(s == "cancel_req" for s in sts)
            seen = []
            for s in sts:
                if not any(x[0] == s for x in seen):
                    seen.append((s, sts.count(s)))
            d["status_summary"] = _i18n.mk("，", lang).join(
                f"{_i18n.t(STS.get(s, s), lang)}×{n}" for s, n in seen)
        locations = _all_locations(conn)
        lines = db.list_lines(conn)
        equipment = db.list_equipment(conn)
        _lm = {l["id"]: l["name"] for l in lines}
        for _e in equipment:
            _e["line_name"] = _lm.get(_e["line_id"], "")
    finally:
        conn.close()
    # 按申请单分页（每页条数可在 5/10/20/100/全部 间切换，记忆在当前电脑）
    per_page_raw = request.args.get("per_page", "")
    if per_page_raw not in ("5", "10", "20", "100", "all"):
        per_page_raw = str(session.get("app_per_page", 10))
    session["app_per_page"] = per_page_raw
    per_page = None if per_page_raw == "all" else int(per_page_raw)
    page = max(1, request.args.get("page", 1, type=int))
    total = len(doc_list)
    pages = 1
    docs_page = doc_list
    if per_page:
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        docs_page = doc_list[(page - 1) * per_page: page * per_page]
    return render_template("applications.html", docs=docs_page, locations=locations,
                           cur_uid=user["id"], cur_role=user["role"], cur_name=user["display_name"],
                           date_from=date_from, date_to=date_to,
                           time_from=time_from, time_to=time_to,
                           f_type=f_type, f_status=f_status,
                           lines=lines, equipment=equipment,
                           app_total=total, app_page=page, app_pages=pages,
                           condition_names={k: _i18n.t(v, _i18n.current_lang())
                                            for k, v in CONDITION_NAMES.items()},
                           cond_matrix=_LOC_ACCEPT,
                           can_reassess=can(user["role"], "stock_inout", "reassess"),
                           scrap_mode=_scrap_approval_mode(),
                           app_per_page=per_page_raw)


@app.route("/applications/export")
@login_required
@perm_required("apply")
def applications_export():
    """按当前筛选条件导出「我的申请单」为 Excel（.xlsx，纯标准库生成，无需额外组件）"""
    user = current_user()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    time_from = request.args.get("time_from", "").strip()
    time_to = request.args.get("time_to", "").strip()
    f_type = request.args.get("f_type", "").strip()
    f_status = request.args.get("f_status", "").strip()
    conn = get_db()
    try:
        where = ["a.applicant_id=?"]
        args = [user["id"]]
        dt_where(where, args, "a.created_at", date_from, date_to, time_from, time_to)
        if f_type in ("in", "out", "transfer", "borrow"):
            where.append("a.app_type=?")
            args.append(f_type)
        if f_status in ("pending", "approved", "rejected", "completed", "cancel_req", "canceled"):
            where.append("a.status=?")
            args.append(f_status)
        sql = """SELECT a.*, p.part_code, p.name, p.unit,
                        CASE WHEN fl.bin='' THEN fl.warehouse||'-'||fl.rack||'-'||fl.shelf
                             ELSE fl.warehouse||'-'||fl.rack||'-'||fl.shelf||'-'||fl.bin END AS from_loc_text,
                        CASE WHEN tl.bin='' THEN tl.warehouse||'-'||tl.rack||'-'||tl.shelf
                             ELSE tl.warehouse||'-'||tl.rack||'-'||tl.shelf||'-'||tl.bin END AS to_loc_text
                 FROM applications a
                 LEFT JOIN parts p ON a.part_id=p.id
                 LEFT JOIN locations fl ON a.from_location_id=fl.id
                 LEFT JOIN locations tl ON a.to_location_id=tl.id
                 WHERE """ + " AND ".join(where) + """
                 ORDER BY a.id DESC LIMIT 2000"""
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    type_name = {"in": _xl("入库"), "out": _xl("出库"), "transfer": _xl("调库")}
    status_name = {"pending": _xl("待处理"), "approved": _xl("已批准·待出库"),
                   "rejected": _xl("已驳回"), "completed": _xl("已完成"),
                   "cancel_req": _xl("撤销待审批"), "canceled": _xl("已撤销")}
    headers = [_xl("单号"), _xl("申请时间"), _xl("料号"), _xl("名称"), _xl("单位"),
               _xl("类型"), _xl("数量"), _xl("源位置"), _xl("目标位置"),
               _xl("使用线别"), _xl("使用设备"), _xl("申请原因"), _xl("状态"),
               _xl("处理意见")]
    data = []
    for r in rows:
        data.append([
            r["app_no"], r["created_at"] or "", r["part_code"] or "", r["name"] or "",
            r["unit"] or "", type_name.get(r["app_type"], r["app_type"]),
            r["quantity"], r["from_loc_text"] or _xl("待定"), r["to_loc_text"] or _xl("待定"),
            r["line_name"] or "", r["device_name"] or "",
            r["reason"] or "", status_name.get(r["status"], r["status"]),
            r["approval_note"] or r["operator_note"] or "",
        ])
    buf = _export_xlsx(headers, data, "我的申请单")
    return send_file(buf, as_attachment=True,
                     download_name=_i18n.t("我的申请单_{ts}.xlsx", _i18n.current_lang(),
                                           ts=datetime.now().strftime("%Y%m%d_%H%M%S")),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _new_doc_no(conn):
    """生成申请单分组号：同一张申请单的多行明细共享 doc_no"""
    while True:
        no = "DOC" + datetime.now().strftime("%Y%m%d%H%M%S") + "-" + os.urandom(2).hex().upper()
        if not conn.execute("SELECT 1 FROM applications WHERE doc_no=? LIMIT 1", (no,)).fetchone():
            return no


def _doc_rows(conn, doc_no):
    """一张申请单（doc_no）的全部明细行，附带物料信息。

    兜底：历史数据里有 doc_no 为空的行（旧代码转审批时没写 doc_no），
    这类单在列表里以 app_no 作为单号显示；按 doc_no 查不到时再按 app_no 查一次，
    否则审批/撤销会误报「申请单不存在」。
    """
    sql = """SELECT a.*, p.part_code, p.name, p.is_valuable, p.unit, p.qty_decimal
             FROM applications a JOIN parts p ON a.part_id=p.id
             WHERE %s=? ORDER BY a.id"""
    rows = conn.execute(sql % "a.doc_no", (doc_no,)).fetchall()
    if not rows:
        rows = conn.execute(sql % "a.app_no", (doc_no,)).fetchall()
    return rows


@app.route("/applications/create-doc", methods=["POST"])
@login_required
@perm_required("apply")
def application_create_doc():
    """提交一张申请单（多行明细、同一类型）。
    JSON: {app_type, reason, items:[{part_id, quantity, from_key, to_location_id, use_line, use_device}]}
    所有行校验通过才建单（一个事务）；贵重出库行按申请人部门配置自动指派审批人。
    """
    lang = _i18n.current_lang()

    def err(msg):
        return {"ok": False, "error": msg}, 400

    data = request.get_json(force=True, silent=True) or {}
    app_type = data.get("app_type", "")
    if app_type not in ("in", "out", "transfer", "borrow"):
        return err(_i18n.t("参数错误", lang))
    # 原因改为按行填写：请求级 reason 只作为缺省值（兼容老调用），每行可用 items[i].reason 覆盖
    reason = (data.get("reason") or "").strip()
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        return err(_i18n.t("请先添加物料明细", lang))
    if len(items) > 50:
        return err(_xl("一张申请单最多 50 行明细"))
    user = current_user()
    conn = get_db()
    try:
        inserts = []
        out_need = {}      # part_id -> 本单申请出库总量（按料汇总校验可用库存）
        part_cache = {}
        for idx, it in enumerate(items):
            if not isinstance(it, dict):
                return err(_xl("第 {n} 行：明细格式错误", n=idx + 1))
            no = _xl("第 {n} 行：", n=idx + 1)
            try:
                pid = int(it.get("part_id") or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid not in part_cache:
                part_cache[pid] = conn.execute(
                    "SELECT * FROM parts WHERE id=? AND deleted=0", (pid,)).fetchone()
            p = part_cache[pid]
            if not p:
                return err(no + _i18n.t("物料不存在", lang))
            quantity = _num(it.get("quantity", 0))
            ok, msg = _qty_valid(p, quantity)
            if not ok:
                return err(no + msg)
            row = {"part": p, "quantity": quantity, "from_location_id": None,
                   "from_inv_id": None, "whole_loc": 0,
                   "to_location_id": None, "line_name": "", "device_name": "",
                   "assigned_approver_id": None, "approver2_id": None,
                   "expected_return": "", "status": "pending",
                   "reason": (str(it.get("reason") or "").strip() or reason)}
            # v1.0.27：入库申请行可声明配件三态；仓管执行弹窗按该值预选，
            # 不良品才会在执行时看到「待定(不良品)」字面占位。非法值一律按良品。
            _row_cond = str(it.get("condition") or "good").strip()
            row["condition"] = _row_cond if _row_cond in CONDITIONS else "good"
            # v1.0.13：每行子类型。出库沿用五项，入库用四项（默认采购），调库/借用不细分。
            # 传输字段统一叫 sub_type，兼容老调用里的 out_type / in_type。
            _raw_sub = it.get("sub_type") or it.get("in_type") or it.get("out_type")
            if app_type == "out":
                # v1.0.27：写入路径禁止把非法 out_type 静默回落成「领用」
                _strict_sub = _strict_out_type(_raw_sub)
                if _strict_sub is None:
                    return err(no + _i18n.t("出库类型不合法", lang))
                # v1.0.31：返修/维修只能走「借·换·检·修」→「修(维修)」，
                # 正常出库子类型已移除，直接 POST maintenance 一律拒绝。
                if not _normal_out_type_ok(_strict_sub):
                    return err(no + _i18n.t("出库类型不合法", lang))
                row["sub_type"] = _strict_sub
            else:
                row["sub_type"] = _norm_sub_type(app_type, _raw_sub)
            # 借·换·检·修：换/检/修 底层走出库特殊类型（沿用审批 + 台账逻辑）；借 仍是借用
            # 每行独立判定，绝不改动外层 app_type（否则同一单据里后面的行会被串错类型）
            if app_type == "borrow" and row["sub_type"] in ("exchange", "verify", "maintenance"):
                eff_app_type = "out"
            else:
                eff_app_type = app_type
            # v1.0.49：合并单号用「有效类型 + 子类型」做分组键，借·换·检·修里的换/检/修归到 out
            row["eff_type"] = eff_app_type
            row["destination"] = str(it.get("destination") or "").strip()
            row["receiver"] = str(it.get("receiver") or "").strip()
            row["contact"] = str(it.get("contact") or "").strip()
            row["repair_type"] = str(it.get("repair_type") or "").strip()
            row["out_person"], row["out_person_id"] = _track_out_person(user, row["sub_type"])
            # 出库（领用）必须写清楚原因：返还是「备注」可空，其余按各自名称提示
            if eff_app_type == "out" and row["sub_type"] not in _OUT_KIND_REASON_OPTIONAL \
                    and not row["reason"]:
                _need = _OUT_KIND_REASONS.get(row["sub_type"], "申请原因")
                return err(no + _xl("请填写{p1}", p1=_i18n.t(_need, lang)))
            if app_type == "transfer":
                # v1.0.70：源按库存行 id 精确定位（同库位多批次不再串批），
                # 老草稿 loc_<id> 仍兼容；校验数量时只认这一行的现存量。
                from_key = str(it.get("from_key") or it.get("location_id") or "").strip()
                _src_lines, src_mode, src_line = _parse_transfer_source(conn, pid, from_key)
                if src_line is None:
                    return err(no + (_i18n.t("所选批次不存在或已变化，请刷新重选", lang)
                                     if src_mode == "batch" else
                                     _i18n.t("请选择调库的源库位（移哪个位置的料）", lang)))
                to_raw = str(it.get("to_location_id") or "").strip()
                try:
                    to_id = int(to_raw) if to_raw and to_raw != "PENDING" else None
                except (TypeError, ValueError):
                    to_id = None
                if to_id:
                    if src_line["location_id"] == to_id:
                        return err(no + _i18n.t("源库位与目标位置相同，无需调库", lang))
                elif src_line["location_id"] is None:
                    return err(no + _i18n.t("该库位已在「待定」状态，请选择目标位置或联系仓库处理", lang))
                if src_mode == "whole":
                    if abs(float(src_line["quantity"]) - quantity) > 1e-6:
                        return err(no + _xl("整库位调库数量为该库位合计 {p1}，不可修改",
                                            p1=src_line["quantity"], lang=lang))
                elif src_line["quantity"] + 1e-9 < quantity:
                    if src_mode == "batch" and str(_row_get(src_line, "batch_no") or ""):
                        return err(no + _xl("该批次只有 {p1} 件，不足调出 {p2}",
                                            p1=src_line["quantity"], p2=quantity, lang=lang))
                    return err(no + _xl("源库位现有 {p1} {p2}，调库数量超出可调数量",
                                        p1=src_line["quantity"], p2=p["unit"], lang=lang))
                row["from_location_id"] = src_line["location_id"]
                row["to_location_id"] = to_id
                row["whole_loc"] = 1 if src_mode == "whole" else 0
                row["from_inv_id"] = int(src_line["inv_id"]) if src_mode == "batch" else None
                row["batch_no"] = str(_row_get(src_line, "batch_no") or "") if src_mode == "batch" else ""
                # 调库继承状态：申请行记录源批次状态，执行时目标行同状态合并
                row["condition"] = str(_row_get(src_line, "condition") or "good")
                # A 方案：待报废 → 待维修必须写复判原因；审批权限在执行时按执行人校验
                _re_err = _scrap_reassess_err(conn, user, src_line["location_id"], to_id,
                                              row["reason"], lang, check_perm=False)
                if _re_err:
                    return err(no + _re_err)
            elif eff_app_type == "out":
                if _part_pending(conn, pid):
                    return err(no + _i18n.t("该物料当前处于「待定」状态（所有库存均无放置位置），不允许领用；请先执行调库到具体位置", lang))
                ot = row["sub_type"]
                # v1.0.27：申请页与批量页共用同一份物料级状态规则，申请行不带扣减库位，
                # 由 cond_summary 的 has_pending_scrap 兜底判定。
                _ac_err = _out_cond_error(conn, pid, ot, None, lang)
                if _ac_err:
                    return err(no + _ac_err)
                # v1.0.27：报废不显示/不校验/不存三字段
                if ot == "scrap":
                    row["destination"] = row["receiver"] = row["contact"] = ""
                    # v1.0.47：普通出库报废也走两模式审批 —— 设置 doc_mode/附件闸门/扣库库位。
                    # 库位优先用前端指定的扣减库位；没指定就取该物料第一个待报废库存行。
                    _mode = _scrap_approval_mode()
                    row["doc_mode"] = _mode
                    row["attachment_required"] = 1 if _mode == 3 else 0
                    _fl = _int(it.get("from_location_id") or it.get("location_id") or 0, 0)
                    if not _fl:
                        _cand = conn.execute(
                            """SELECT i.location_id FROM inventory i JOIN locations l ON i.location_id=l.id
                               WHERE i.part_id=? AND i.quantity>0
                                 AND COALESCE(l.attribute,'good')='pending_scrap'
                               ORDER BY i.id LIMIT 1""", (pid,)).fetchone()
                        _fl = int(_cand["location_id"]) if _cand and _cand["location_id"] else 0
                    row["from_location_id"] = _fl or None
                else:
                    row["doc_mode"] = 0
                    row["attachment_required"] = 0
                if ot in OUT_KINDS:
                    # 完整沿用「出入库管理」的特殊出库规则：目的地/接收人/联系方式必填，
                    # 换货要预计返回日并按审批设置档位流转；不审批档建单即免审通过。
                    if ot != "scrap":
                        if not row["destination"]:
                            return err(no + _xl("{p1}出库请填写目的地或目的公司",
                                                p1=_i18n.t(_OUT_KIND_NAMES[ot], lang)))
                        if not row["receiver"] or not row["contact"]:
                            return err(no + _i18n.t("请填写接收人与联系方式", lang))
                    if ot in BORROW_OUT_KINDS:
                        er = str(it.get("expected_return") or "").strip()
                        if not er:
                            return err(no + _i18n.t("请选择预计返回日期", lang))
                        er = er.replace("T", " ").strip()
                        if len(er) == 10:
                            er += " 23:59:59"
                        elif len(er) == 16:
                            er += ":00"
                        row["expected_return"] = er
                    lv = _out_kind_level(conn, ot, p)
                    # v1.0.47：报废单必须走系统审批（两模式都要审批通过后自动扣库），
                    # 即使 scrap_approval_level 配成 0 也强制至少一级。
                    if ot == "scrap":
                        lv = max(lv, 1)
                    if lv >= 1:
                        a1, a2 = _stk_default_approvers(conn, user)
                        if not a1:
                            return err(no + _i18n.t("本部门未配置一级审批人，请先在部门配置中设置", lang))
                        row["assigned_approver_id"] = a1
                        row["approver2_id"] = a2 if lv == 2 else None
                    else:
                        # 「不审批」档：等价于免审通过，仓管在待处理列表可直接执行
                        row["status"] = "approved"
                    # 换/检/修走台账并在办理时实扣库存（v1.0.19 起）；建单阶段不重复计入出库需求
                    if ot not in BORROW_OUT_KINDS:
                        out_need[pid] = out_need.get(pid, 0.0) + quantity
                else:
                    out_need[pid] = out_need.get(pid, 0.0) + quantity
                    # v1.0.39：出库「其它」免线别设备（与批量页同源），只领用人 + 库位
                    if ot == "general":
                        use_line = str(it.get("use_line") or "").strip()
                        use_device = str(it.get("use_device") or "").strip()
                        offline = bool(it.get("offline")) or use_line in ("离线", "Offline")
                        if not use_device or (not use_line and not offline):
                            return err(no + _i18n.t("出库明细请选择使用线别与设备", lang))
                        # 离线设备：线体留空（报表里归到未分配），设备名照常记录
                        row["line_name"] = "" if offline else use_line
                        row["device_name"] = use_device
                    if p["is_valuable"]:
                        a1, a2 = _stk_default_approvers(conn, user)
                        if not a1:
                            return err(no + _i18n.t("本部门未配置一级审批人，请先在部门配置中设置", lang))
                        if _valuable_approval_level(conn, p) != 2:
                            a2 = None
                        row["assigned_approver_id"] = a1
                        row["approver2_id"] = a2
            elif app_type == "borrow":
                # 借用申请：没有审批环节（贵重品也一样），提交后直达仓库办理借出。
                # 借用人默认为申请人本人；预计归还日必填（台账预警的锚点）。
                er_raw = str(it.get("expected_return") or "").strip()
                if not er_raw:
                    return err(no + _i18n.t("请选择预计归还日", lang))
                er_norm = er_raw.replace("T", " ").strip()
                if len(er_norm) == 10:
                    er_norm += " 23:59:59"      # 只填日期 = 当天整天都在期限内
                elif len(er_norm) == 16:
                    er_norm += ":00"
                row["expected_return"] = er_norm
                if _part_pending(conn, pid):
                    return err(no + _i18n.t("该物料当前处于「待定」状态（所有库存均无放置位置），不允许领用；请先执行调库到具体位置", lang))
                # 借用同样吃可用量：申请一提交就锁定，防止等待办理期间被领走
                out_need[pid] = out_need.get(pid, 0.0) + quantity
            inserts.append(row)
        # 出库：按料汇总校验可用库存（扣除其他申请已锁定数量 + 工具借用在借数量）
        for pid, need in out_need.items():
            p = part_cache[pid]
            stock = db.total_stock(conn, pid)
            locked_rows = conn.execute(
                """SELECT a.quantity, u.display_name FROM applications a
                   JOIN users u ON u.id=a.applicant_id
                   JOIN parts pp ON pp.id=a.part_id
                   WHERE a.part_id=? AND (
                     (a.app_type='out' AND ((pp.is_valuable=1 AND a.status='approved')
                        OR ((pp.is_valuable=0 AND a.status='pending') OR (a.app_type='out' AND IFNULL(a.out_type,'') IN ('return','exchange','internal','giveback','maintenance','verify','scrap') AND a.status IN ('pending','pending2','approved')))))
                     OR (a.app_type='borrow' AND a.status IN ('pending','approved'))
                   )""",
                (pid,)).fetchall()
            # 在借（含待确认）也必须扣：实物不在仓里，不能让领用再吃一遍
            in_use = _borrow_in_use(conn, pid)
            available = stock - sum(r["quantity"] for r in locked_rows) - in_use
            if need > available + 1e-9:
                detail = _i18n.mk("、", lang).join(
                    _xl("{who} 申请 {qty} 个", who=r["display_name"], qty=r["quantity"])
                    for r in locked_rows) or _xl("无")
                msg = _xl("库存不足：[{code}] 当前可用仅 {p1} {p2}，本单共需出库 {p3} {p2}（库存已被申请锁定：{p4}）",
                          code=p["part_code"], p1=available, p2=p["unit"], p3=need, p4=detail)
                if in_use > 0:
                    msg += _xl("；另有 {q} {u} 被工具借用占用", q=round(in_use, 3), u=p["unit"])
                return err(msg)
        # 全部通过 → 一个事务建单。
        # v1.0.49：同一申请人「同时批量提交」的请求按「有效类型 + 子类型 + 审批档位」分组，
        # 每组一个单号（一个单号一次批准），不同子类型不再混进同一张单；
        # 短时间窗口内同组仍待一审的单复用同一个 doc_no（纸档报废只传一份附件）。
        def _merge_key(r):
            return (r["eff_type"], r["sub_type"], r.get("doc_mode", 0),
                    r.get("attachment_required", 0),
                    r.get("assigned_approver_id") or 0, r.get("approver2_id") or 0)

        grouped = {}
        for r in inserts:
            grouped.setdefault(_merge_key(r), []).append(r)
        doc_nos, merge_since = [], (datetime.now() - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        for gkey, grows in grouped.items():
            _eff_t, _sub_t, _mode, _areq, _a1, _a2 = gkey
            doc_no = ""
            # 只复用「全部行仍待一审、审批人一致、短时间内建立」的单，避免并到已审批/已驳回的单里
            try:
                cands = conn.execute(
                    """SELECT DISTINCT a.doc_no FROM applications a
                       WHERE a.applicant_id=? AND a.app_type=? AND IFNULL(a.out_type,'')=?
                         AND IFNULL(a.doc_mode,0)=? AND IFNULL(a.attachment_required,0)=?
                         AND a.status='pending' AND a.created_at>=?""",
                    (user["id"], _eff_t, _sub_t, _mode, _areq, merge_since)).fetchall()
            except Exception:
                cands = []
            for c in cands:
                gr = conn.execute("SELECT * FROM applications WHERE doc_no=?", (c["doc_no"],)).fetchall()
                if not gr or any(x["status"] != "pending" for x in gr):
                    continue
                if any((x["assigned_approver_id"] or 0) != _a1 or (x["approver2_id"] or 0) != _a2
                       for x in gr):
                    continue
                doc_no = c["doc_no"]
                break
            if not doc_no:
                doc_no = _new_doc_no(conn)
            doc_nos.append(doc_no)
            for r in grows:
                r["doc_no"] = doc_no
                p = r["part"]
                app_no = db.gen_no("APP", conn, "applications", "app_no")
                conn.execute(
                    """INSERT INTO applications(app_no,doc_no,part_id,app_type,quantity,reason,
                       out_type,destination,receiver,contact,
                       line_name,device_name,expected_return,from_location_id,to_location_id,applicant_id,
                       out_person,out_person_id,repair_type,status,assigned_approver_id,approver2_id,created_at,condition,
                       doc_mode,attachment_required,doc_submitted,from_inv_id,whole_loc)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (app_no, doc_no, p["id"], _eff_t, r["quantity"], r["reason"],
                     r["sub_type"], r["destination"], r["receiver"], r["contact"],
                     r["line_name"], r["device_name"], r["expected_return"],
                     r["from_location_id"], r["to_location_id"],
                     user["id"], r["out_person"], r["out_person_id"], r["repair_type"],
                     r["status"], r["assigned_approver_id"], r["approver2_id"], db.now_str(),
                     r["condition"], r.get("doc_mode", 0), r.get("attachment_required", 0),
                     # v1.0.51：纸档模式建单=未提交，须上传签字档并点「提交审批」后进审批
                     (0 if r.get("attachment_required") else 1), r.get("from_inv_id"),
                     r.get("whole_loc", 0)))
        doc_no = doc_nos[0]
        # 贵重出库行：按审批人汇总发通知（同一单号只通知一次）
        val_rows = [r for r in inserts if r["assigned_approver_id"]]
        if val_rows:
            notified = {}
            for r in val_rows:
                notified.setdefault(r["assigned_approver_id"], set()).add(r["doc_no"])
            try:
                import notify as _notify
                _notify.reload_settings()
                for aid1, docs in notified.items():
                    sup = conn.execute("SELECT * FROM users WHERE id=?", (aid1,)).fetchone()
                    with _i18n.lang_scope((sup["lang"] if sup else "") or ""):
                        _notify.notify_approver(
                            dict(sup) if sup else None,
                            _i18n.t("出库申请待审批"),
                            _i18n.t("申请单 {doc} 共 {n} 行出库申请待您审批，请登录系统处理。",
                                    doc="、".join(sorted(docs)), n=len(val_rows)))
            except Exception:
                pass
        conn.commit()
        if val_rows:
            flow = _xl("含待审批出库 {n} 行，审批通过后由仓库出库", n=len(val_rows))
        else:
            flow = _i18n.t("等待仓库人员处理", lang)
        return {"ok": True, "doc_no": doc_no, "doc_nos": doc_nos, "count": len(inserts),
                "msg": _xl("申请单 {p1} 提交成功（{p2} 行，{p3}）",
                           p1="、".join(doc_nos), p2=len(inserts), p3=flow)}
    finally:
        conn.close()


@app.route("/scrap/<doc_no>/attachments")
@login_required
def scrap_attachments_list(doc_no):
    """列出某张报废单的签字附件（仅单据申请人/审批人/管理员可见）。"""
    user = current_user()
    conn = get_db()
    try:
        rows_doc = _doc_rows(conn, doc_no)
        if not rows_doc:
            return {"ok": False, "error": "单据不存在"}, 404
        # v1.0.51：审批人也要能看到申请人上传的签字档
        approver_ids = set()
        for r in rows_doc:
            for k in ("assigned_approver_id", "approver2_id", "approver_id"):
                if r[k]:
                    approver_ids.add(r[k])
        allowed = (user["role"] == "admin"
                   or any(r["applicant_id"] == user["id"] for r in rows_doc)
                   or user["id"] in approver_ids)
        if not allowed:
            return {"ok": False, "error": "没有权限查看该单据附件"}, 403
        rows = conn.execute(
            "SELECT * FROM scrap_attachments WHERE doc_no=? ORDER BY id", (doc_no,)).fetchall()
        return {"ok": True, "items": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.route("/scrap/<doc_no>/attachment", methods=["POST"])
@login_required
def scrap_attachment_upload(doc_no):
    """上传签字审批单照片/扫描件（仅模式3单据；图片存 images 表 BLOB）。"""
    lang = _i18n.current_lang()
    user = current_user()
    conn = get_db()
    try:
        rows = _doc_rows(conn, doc_no)
        if not rows or not rows[0]["attachment_required"]:
            return {"ok": False, "error": _i18n.t("该单据不需要签字附件", lang)}, 400
        if rows[0]["applicant_id"] != user["id"] and user["role"] != "admin":
            return {"ok": False, "error": _i18n.t("没有权限上传该单据附件", lang)}, 403
        # v1.0.51：点「提交审批」后锁定附件，不能再上传
        if any((r["doc_submitted"] or 0) for r in rows):
            return {"ok": False, "error": _i18n.t("已提交审批，不能再上传附件", lang)}, 400
        f = request.files.get("image")
        if f is None or not f.filename:
            return {"ok": False, "error": _i18n.t("请选择图片", lang)}, 400
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in config.ALLOWED_IMG_EXTS:
            return {"ok": False, "error": _i18n.t("不支持的图片格式（支持 jpg/png/gif/bmp/webp）", lang)}, 400
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(0)
        if size > config.MAX_IMG_SIZE:
            return {"ok": False, "error": _i18n.t("图片超过 5MB 限制", lang)}, 400
        data, _ = _compress_image(f.read(), _upload_compress_kb(conn))
        name = "scrap_%s_%s_%s" % (re.sub(r"\W", "", doc_no)[:24],
                                   datetime.now().strftime("%Y%m%d%H%M%S"),
                                   secure_filename(f.filename))
        db.image_save(conn, name, data)
        conn.execute(
            "INSERT INTO scrap_attachments(doc_no,image_name,uploader_id,uploaded_at) VALUES(?,?,?,?)",
            (doc_no, name, user["id"], db.now_str()))
        conn.commit()
        return {"ok": True, "image_name": name}
    finally:
        conn.close()


@app.route("/scrap/<doc_no>/submit", methods=["POST"])
@login_required
def scrap_doc_submit(doc_no):
    """v1.0.51：纸档单上传签字档后，申请人点「提交审批」才把单推进审批待办。

    约束：仅纸档模式单据；仅申请人本人（或管理员）；必须已上传至少一张附件；
    尚未提交；单据还在待审（pending/pending2）且未审批通过/撤销。提交后锁定附件。
    """
    lang = _i18n.current_lang()
    user = current_user()
    conn = get_db()
    try:
        rows = _doc_rows(conn, doc_no)
        if not rows or not rows[0]["attachment_required"]:
            return {"ok": False, "error": _i18n.t("该单据不需要提交签字档", lang)}, 400
        if rows[0]["applicant_id"] != user["id"] and user["role"] != "admin":
            return {"ok": False, "error": _i18n.t("没有权限提交该单据", lang)}, 403
        if any(r["status"] not in ("pending", "pending2") for r in rows):
            return {"ok": False, "error": _i18n.t("单据已审批或撤销，不能提交", lang)}, 400
        if all((r["doc_submitted"] or 0) for r in rows):
            return {"ok": False, "error": _i18n.t("该单据已提交审批", lang)}, 400
        n = conn.execute(
            "SELECT COUNT(*) c FROM scrap_attachments WHERE doc_no=?", (doc_no,)).fetchone()["c"]
        if n == 0:
            return {"ok": False, "error": _i18n.t("请先上传签字附件再提交", lang)}, 400
        conn.execute("UPDATE applications SET doc_submitted=1 WHERE doc_no=?", (doc_no,))
        conn.commit()
        return {"ok": True, "count": n}
    finally:
        conn.close()


@app.route("/scrap/<doc_no>/attachment/<int:aid>", methods=["DELETE"])
@login_required
def scrap_attachment_delete(doc_no, aid):
    """删除签字附件（仅上传者本人或管理员，单据未审批完才可删）。"""
    lang = _i18n.current_lang()
    user = current_user()
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM scrap_attachments WHERE id=? AND doc_no=?",
                           (aid, doc_no)).fetchone()
        if not row:
            return {"ok": False, "error": _i18n.t("附件不存在", lang)}, 404
        if row["uploader_id"] != user["id"] and user["role"] != "admin":
            return {"ok": False, "error": _i18n.t("没有权限删除该附件", lang)}, 403
        rows = _doc_rows(conn, doc_no)
        if any(r["status"] in ("approved", "completed") for r in rows):
            return {"ok": False, "error": _i18n.t("单据已审批通过，附件不能再删除", lang)}, 400
        # v1.0.51：点「提交审批」后锁定附件，不能再删除
        if any((r["doc_submitted"] or 0) for r in rows):
            return {"ok": False, "error": _i18n.t("已提交审批，不能再删除附件", lang)}, 400
        db.image_delete(conn, row["image_name"])
        conn.execute("DELETE FROM scrap_attachments WHERE id=?", (aid,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.route("/api/doc/<doc_no>/detail")
@login_required
def api_doc_detail(doc_no):
    """v1.0.52：单据明细（供「我的审批历史」与「驳回单」点开查看详情）。

    可见范围：申请人本人 / 参与审批的人（approver_id/approver2_id/assigned_approver_id）
    / 管理员。普通无关用户不可查看他人单据。
    """
    lang = _i18n.current_lang()
    user = current_user()
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT a.*, p.part_code, p.name, p.spec, p.unit, p.is_valuable,
                      fl.warehouse fl_wh, fl.rack fl_rack, fl.shelf fl_shelf, fl.bin fl_bin,
                      tl.warehouse tl_wh, tl.rack tl_rack, tl.shelf tl_shelf, tl.bin tl_bin,
                      ua.display_name applicant
               FROM applications a
               JOIN parts p ON p.id=a.part_id
               LEFT JOIN locations fl ON fl.id=a.from_location_id
               LEFT JOIN locations tl ON tl.id=a.to_location_id
               LEFT JOIN users ua ON ua.id=a.applicant_id
               WHERE a.doc_no=? OR a.app_no=?
               ORDER BY a.id""", (doc_no, doc_no)).fetchall()
        if not rows:
            return {"ok": False, "error": _i18n.t("单据不存在", lang)}, 404
        r0 = rows[0]
        priv = (user["role"] == "admin" or r0["applicant_id"] == user["id"]
                or r0["approver_id"] == user["id"] or r0["approver2_id"] == user["id"]
                or r0["assigned_approver_id"] == user["id"])
        if not priv:
            return {"ok": False, "error": _i18n.t("没有权限查看该单据", lang)}, 403
        ST = {"pending": "status_pending", "pending2": "status_pending",
              "approved": "status_approved", "rejected": "status_rejected",
              "completed": "status_completed", "cancel_req": "status_cancel_req",
              "canceled": "status_canceled"}
        TYPE = {"in": "type_in", "out": "type_out", "transfer": "type_transfer", "borrow": "借用"}
        def loc(w, r, s, b):
            return "-".join(x for x in (w, r, s, b) if x) or "-"
        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "part_code": r["part_code"], "name": r["name"], "spec": r["spec"] or "",
                "quantity": r["quantity"], "unit": r["unit"] or "",
                "status": _i18n.t(ST.get(r["status"], r["status"]), lang),
                "from_loc": loc(r["fl_wh"], r["fl_rack"], r["fl_shelf"], r["fl_bin"]),
                "to_loc": loc(r["tl_wh"], r["tl_rack"], r["tl_shelf"], r["tl_bin"]),
                "reason": r["reason"] or "", "out_type": r["out_type"] or "",
            })
        statuses = sorted(set(r["status"] for r in rows))
        overall = "rejected" if "rejected" in statuses else (
            "completed" if all(s == "completed" for s in statuses) else (
            "approved" if all(s in ("approved", "completed") for s in statuses) else statuses[0]))
        attach_n = conn.execute(
            "SELECT COUNT(*) c FROM scrap_attachments WHERE doc_no=?", (doc_no,)).fetchone()["c"]
        return {"ok": True, "doc_no": doc_no,
                "app_type": _i18n.t(TYPE.get(r0["app_type"], r0["app_type"]), lang),
                "applicant": r0["applicant"] or "", "created_at": r0["created_at"] or "",
                "overall": _i18n.t(ST.get(overall, overall), lang),
                "approval_note": r0["approval_note"] or "", "processed_at": r0["processed_at"] or "",
                "attach_count": attach_n,
                "need_upload": any(r["attachment_required"] for r in rows), "items": items}
    finally:
        conn.close()


@app.route("/api/doc/<doc_no>/reopen", methods=["POST"])
@login_required
def api_doc_reopen(doc_no):
    """v1.0.53：驳回单「重新修改并提交」——把 rejected 行改回 pending，
    支持：改数量（可增可减，驳回未执行、库存未动）、删除某些物料行、清空审批痕迹；
    纸档单解锁附件（doc_submitted=0），申请人可重下载/重删/重传签字档后再提交。
    仅申请人本人或管理员；必须全部行都是 rejected；不能把所有行都删光。
    """
    lang = _i18n.current_lang()
    user = current_user()
    conn = get_db()
    try:
        rows = _doc_rows(conn, doc_no)
        if not rows:
            return {"ok": False, "error": _i18n.t("单据不存在", lang)}, 404
        if any(r["applicant_id"] != user["id"] for r in rows) and user["role"] != "admin":
            return {"ok": False, "error": _i18n.t("只能修改自己发起的申请", lang)}, 403
        if any(r["status"] != "rejected" for r in rows):
            return {"ok": False, "error": _i18n.t("只有被驳回的单据可以重新提交", lang)}, 400
        payload = request.get_json(silent=True) or {}
        qtys = payload.get("qtys") or {}
        del_ids = set(str(x) for x in (payload.get("delete_ids") or []))
        valid_ids = set(str(r["id"]) for r in rows)
        # 至少保留一行，否则该单号就空了
        if del_ids and len(valid_ids - del_ids) == 0:
            return {"ok": False, "error": _i18n.t("不能删除全部物料，至少保留一行", lang)}, 400
        changed = 0
        deleted = 0
        for r in rows:
            key = str(r["id"])
            if key in del_ids:
                conn.execute("DELETE FROM applications WHERE id=?", (r["id"],))
                deleted += 1
                continue
            newq = r["quantity"]
            if key in qtys:
                try:
                    v = float(qtys[key])
                except (TypeError, ValueError):
                    return {"ok": False, "error": _i18n.t("数量格式不正确", lang)}, 400
                if v <= 0:
                    return {"ok": False, "error": _i18n.t("数量必须大于 0", lang)}, 400
                newq = int(v) if not r["qty_decimal"] else v
                if abs(float(newq) - float(r["quantity"])) > 1e-9:
                    changed += 1
            unlock = 0 if r["attachment_required"] else 1
            conn.execute(
                """UPDATE applications
                   SET status='pending', quantity=?, approval_note=NULL, approver_id=NULL,
                       approver2_id=NULL, assigned_approver_id=NULL,
                       operator_id=NULL, operator_note=NULL, processed_at=NULL, doc_submitted=?
                   WHERE id=?""", (newq, unlock, r["id"]))
        conn.commit()
        return {"ok": True, "rows": len(rows), "changed": changed, "deleted": deleted,
                "need_upload": any(r["attachment_required"] for r in rows if str(r["id"]) not in del_ids)}
    finally:
        conn.close()


@app.route("/api/doc/<doc_no>/delete-rejected", methods=["POST"])
@login_required
def api_doc_delete_rejected(doc_no):
    """v1.0.52：删除驳回废单——仅申请人本人或管理员，且全部行都是 rejected。
    同时删除该单的签字附件与关联图片。已完成的单不可删（有库存流水）。
    """
    lang = _i18n.current_lang()
    user = current_user()
    conn = get_db()
    try:
        rows = _doc_rows(conn, doc_no)
        if not rows:
            return {"ok": False, "error": _i18n.t("单据不存在", lang)}, 404
        if any(r["applicant_id"] != user["id"] for r in rows) and user["role"] != "admin":
            return {"ok": False, "error": _i18n.t("只能删除自己发起的申请", lang)}, 403
        if any(r["status"] != "rejected" for r in rows):
            return {"ok": False, "error": _i18n.t("只有被驳回的废单可以删除", lang)}, 400
        atts = conn.execute(
            "SELECT image_name FROM scrap_attachments WHERE doc_no=?", (doc_no,)).fetchall()
        for a in atts:
            try:
                db.image_delete(conn, a["image_name"])
            except Exception:
                pass
        conn.execute("DELETE FROM scrap_attachments WHERE doc_no=?", (doc_no,))
        conn.execute("DELETE FROM applications WHERE doc_no=?", (doc_no,))
        # 兜底：历史单据 doc_no 为空、用 app_no 显示的
        conn.execute("DELETE FROM applications WHERE app_no=? AND doc_no IS NULL", (doc_no,))
        conn.commit()
        return {"ok": True, "rows": len(rows)}
    finally:
        conn.close()


@app.route("/scrap/<doc_no>/pdf")
@login_required
@perm_required("apply")
def scrap_pdf(doc_no):
    """v1.0.46：纸档批量报废单的打印版审批单（Pillow 直接在内存生成 PDF，零新依赖）。"""
    from PIL import Image, ImageDraw, ImageFont
    import io as _bio
    lang = _i18n.current_lang()
    conn = get_db()
    try:
        rows = _doc_rows(conn, doc_no)
        if not rows or not any(r["attachment_required"] for r in rows):
            flash(_i18n.t("该单据没有打印版审批单", lang), "danger")
            return redirect(url_for("applications_page"))
        # 字体：Windows 自带微软雅黑；找不到就退到 PIL 默认（仅影响字形，不影响流程）
        fp = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "msyh.ttc")
        if not os.path.exists(fp):
            fp = r"C:\Windows\Fonts\simhei.ttf"
        def F(sz):
            try:
                return ImageFont.truetype(fp, sz)
            except Exception:
                return ImageFont.load_default()
        f_title, f_head, f_body, f_small = F(46), F(28), F(24), F(20)
        W, H = 1240, 1754           # A4 @150dpi
        M, RH = 70, 56              # 页边距 / 表格行高
        # 汇总：一行一个库存库位行
        items = []
        for r in rows:
            ltxt = ""
            if r["from_location_id"]:
                lr = conn.execute("SELECT * FROM locations WHERE id=?",
                                  (r["from_location_id"],)).fetchone()
                ltxt = db.location_text(lr) if lr else ""
            br = conn.execute("SELECT batch_no FROM inventory WHERE part_id=? AND location_id=? "
                              "ORDER BY id LIMIT 1",
                              (r["part_id"], r["from_location_id"])).fetchone()
            items.append({"code": r["part_code"] or "", "name": r["name"] or "",
                          "loc": ltxt, "batch": (br["batch_no"] if br else "") or "",
                          "qty": r["quantity"], "unit": r["unit"] or "",
                          "reason": r["reason"] or ""})
        header_lines = [
            _i18n.t("报废申请审批单", lang),
            _xl("单号：{p1}    申请日期：{p2}", p1=doc_no, p2=rows[0]["created_at"] or ""),
            _xl("申请人：{p1}    报废明细：{n} 行", p1=rows[0]["applicant_name"] or "", n=len(items))
            if "applicant_name" in rows[0].keys() else
            _xl("报废明细：{n} 行", n=len(items)),
        ]
        titles = [_i18n.t("料号", lang), _i18n.t("名称", lang), _i18n.t("待报废库位", lang),
                  _i18n.t("批次", lang), _i18n.t("数量", lang), _i18n.t("报废原因", lang)]
        col_w = [190, 180, 280, 130, 90, 330]
        col_x = []
        x = M
        for w in col_w:
            col_x.append(x)
            x += w
        def page_start():
            im = Image.new("RGB", (W, H), "white")
            d = ImageDraw.Draw(im)
            d.text((M, 52), header_lines[0], font=f_title, fill="#111827")
            d.text((M, 118), header_lines[1], font=f_small, fill="#374151")
            d.text((M, 154), header_lines[2], font=f_small, fill="#374151")
            y0 = 216
            d.rectangle([M, y0, W - M, y0 + RH], fill="#e5e7eb")
            for t, cx, cw in zip(titles, col_x, col_w):
                d.text((cx + 8, y0 + 14), t, font=f_head, fill="#111827")
            return im, d, y0 + RH
        pages, im, d, y = [], None, None, 0
        im, d, y = page_start()
        for it in items:
            if y + RH > H - 420:      # 给签字栏留位置
                pages.append(im)
                im, d, y = page_start()
            d.rectangle([M, y, W - M, y + RH], outline="#9ca3af", width=2)
            vals = [it["code"], it["name"], it["loc"], it["batch"],
                    _xl("{p1} {p2}", p1=it["qty"], p2=it["unit"]), it["reason"]]
            for v, cx in zip(vals, col_x):
                txt = str(v)
                while d.textlength(txt, font=f_body) > 260 and len(txt) > 4:
                    txt = txt[:-2] + "…"
                d.text((cx + 8, y + 16), txt, font=f_body, fill="#111827")
            y += RH
        # 签字栏（末页）
        sy = y + 70
        d.text((M, sy), _i18n.t("审批签署", lang), font=f_head, fill="#111827")
        sign_rows = [_i18n.t("申请人签字 / 日期", lang),
                     _i18n.t("部门主管审批 / 日期", lang),
                     _i18n.t("经理审批 / 日期", lang) if _out_kind_level(conn, "scrap") == 2
                     else _i18n.t("审批人签字 / 日期", lang)]
        for i, t in enumerate(sign_rows):
            yy = sy + 60 + i * 120
            d.text((M, yy), t, font=f_small, fill="#374151")
            d.line([M + 320, yy + 56, M + 720, yy + 56], fill="#111827", width=2)
            d.line([M + 780, yy + 56, W - M, yy + 56], fill="#111827", width=2)
        d.text((M, H - 60), _i18n.t("打印后手签归档；签字件拍照或扫描上传到本单后方可进入系统审批", lang),
               font=f_small, fill="#6b7280")
        pages.append(im)
        buf = _bio.BytesIO()
        pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:], resolution=150.0)
        return Response(buf.getvalue(), mimetype="application/pdf",
                        headers={"Content-Disposition":
                                 'attachment; filename="scrap_%s.pdf"' % re.sub(r"\W", "", doc_no)})
    finally:
        conn.close()


@app.route("/applications/doc/<doc_no>/approve", methods=["POST"])
@login_required
def application_doc_approve(doc_no):
    # 不用 perm_required：被指派的主管可能是普通角色，指派即授权；admin 例外放行
    return _doc_approval(doc_no, "approve")


@app.route("/applications/doc/<doc_no>/reject", methods=["POST"])
@login_required
def application_doc_reject(doc_no):
    return _doc_approval(doc_no, "reject")


def _scrap_auto_deduct(conn, doc_no, user, lang):
    """v1.0.46 历史函数：报废单终审自动扣库。

    v1.0.55 起不再调用：所有申请（含报废）审批通过后一律停在 approved，
    必须到「出入库调库管理」由仓管员/ADMIN 逐行执行才扣库、写流水、置 completed。
    保留函数体只为兼容旧脚本/外部调用，不影响现行流程。
    """
    rows = [r for r in _doc_rows(conn, doc_no)
            if r["app_type"] == "out" and r["out_type"] == "scrap"
            and r["doc_mode"] in (2, 3) and r["status"] == "approved"]
    if not rows:
        return 0, 0, []
    # 预检：每行指定库位的现存数量必须够（同物料同库位多行要累加）
    need = {}
    for r in rows:
        k = (r["part_id"], r["from_location_id"])
        need[k] = need.get(k, 0) + r["quantity"]
    for (pid, loc_id), q in need.items():
        cur = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) s FROM inventory WHERE part_id=? AND location_id=?",
            (pid, loc_id)).fetchone()["s"]
        if cur + 1e-9 < q:
            for r in rows:
                if (r["part_id"], r["from_location_id"]) == (pid, loc_id):
                    conn.execute(
                        "UPDATE applications SET status='rejected', approval_note=?, processed_at=? WHERE id=?",
                        (_xl("自动扣库失败：待报废库位库存不足（需要 {q}，实有 {s}）", q=q, s=cur),
                         db.now_str(), r["id"]))
            return 0, len(rows), [_xl("待报废库位库存不足，整单自动驳回：需要 {q}，实有 {s}", q=q, s=cur)]
    # 全部预检通过 → 逐行扣减 + 流水 + 置 completed
    done = 0
    for r in rows:
        loc_id = r["from_location_id"]
        loc = db.get_location(conn, loc_id) if loc_id else None
        loc_txt = db.location_text(loc) if loc else "待定"
        br = conn.execute("SELECT batch_no FROM inventory WHERE part_id=? AND location_id=? "
                          "ORDER BY id LIMIT 1", (r["part_id"], loc_id)).fetchone()
        # move_stock 的 delta：正数=入库、负数=出库（v1.0.46 踩坑：传正数会反向加库存）
        ok, msg = db.move_stock(conn, r["part_id"], loc_id, -r["quantity"],
                                batch_no=(br["batch_no"] if br else ""))
        if not ok:
            conn.execute(
                "UPDATE applications SET status='rejected', approval_note=?, processed_at=? WHERE id=?",
                (_xl("自动扣库失败：{p1}", p1=msg), db.now_str(), r["id"]))
            continue
        conn.execute(
            """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
               out_type,ref_type,ref_id,operator_id,applicant_id,line_name,device_name,remark,batch_no,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (db.gen_no("TX", conn, "transactions", "tx_no"), r["part_id"], "out", r["quantity"],
             r["unit_price"] if "unit_price" in r.keys() else 0, 0, loc_txt, "scrap",
             "application", r["id"], user["id"], r["applicant_id"], "", "",
             _i18n.t("报废（审批通过自动扣库）", lang) + "：" + (r["reason"] or ""),
             (br["batch_no"] if br else ""), db.now_str()))
        conn.execute(
            "UPDATE applications SET status='completed', operator_note=?, operator_id=?, processed_at=? WHERE id=?",
            (_i18n.t("审批通过自动扣库", lang), user["id"], db.now_str(), r["id"]))
        done += 1
    return done, len(rows) - done, []


def _doc_approval(doc_no, action):
    """整单审批：对单内全部待审出库行执行阶段逻辑（一级/二级/库存不足自动驳回）。
    覆盖两类单据：贵重出库 + 退货/换货/内部转移出库（按各自的审批档位流转）"""
    lang = _i18n.current_lang()
    user = current_user()
    conn = get_db()
    try:
        rows = _doc_rows(conn, doc_no)
        if not rows:
            flash(_i18n.t("申请单不存在", lang), "danger")
            return redirect(url_for("approval_page"))
        actionable = [r for r in rows if r["app_type"] == "out"
                      and (r["is_valuable"]
                           or (r["out_type"] or "") in OUT_KINDS)
                      and r["status"] in ("pending", "pending2")]
        if not actionable:
            # 已审批完/已撤销的单不该再出现在待办里，重复提交时给明确提示而不是报错
            flash(_xl("申请单 {p1} 已无待审批明细（可能已完成审批或已撤销）", p1=doc_no), "warning")
            return redirect(url_for("approval_page"))
        # v1.0.46：纸档批量报废单——服务端闸门，签字附件未上传不能进入系统审批
        if action == "approve" and any(r["attachment_required"] for r in actionable):
            # v1.0.51：还必须先由申请人点「提交审批」（doc_submitted=1）
            if any(not (r["doc_submitted"] or 0) for r in actionable):
                flash(_i18n.t("申请人尚未提交签字档，不能审批", lang), "danger")
                return redirect(url_for("approval_page"))
            n_attach = conn.execute(
                "SELECT COUNT(*) c FROM scrap_attachments WHERE doc_no=?", (doc_no,)).fetchone()["c"]
            if n_attach == 0:
                flash(_i18n.t("请先上传签字后的审批单照片/扫描件，再进入系统审批", lang), "danger")
                return redirect(url_for("approval_page"))
        for r in actionable:
            stage = 1 if r["status"] == "pending" else 2
            expected = r["assigned_approver_id"] if stage == 1 else r["approver2_id"]
            if user["role"] != "admin" and user["id"] != expected:
                # 没人被指派时不能「谁都能批」：回落到审批模块权限，
                # 否则二级审批人缺失/指派被清空的单子，任何登录用户都能放行贵重出库。
                if expected is None and not can(user["role"], "approve", "approve_out"):
                    flash(_i18n.t("该申请单当前环节未指派给您审批", lang), "warning")
                    return redirect(url_for("approval_page"))
                if expected is not None:
                    flash(_i18n.t("该申请单当前环节未指派给您审批", lang), "warning")
                    return redirect(url_for("approval_page"))
        note = request.form.get("note", "").strip()
        n_ok = n_p2 = n_rej = 0
        for r in actionable:
            stage = 1 if r["status"] == "pending" else 2
            _ot = (r["out_type"] or "").strip()
            _is_kind = _ot in OUT_KINDS
            if action == "approve":
                # 转二级审批：建单时只有二级档位才会写入 approver2_id，直接以它为准
                # （贵重按 valuable 档位、退货/换货/内部转移按各自档位）
                if stage == 1 and r["approver2_id"]:
                    conn.execute(
                        "UPDATE applications SET status='pending2', approval_note=?, approver_id=?, processed_at=? WHERE id=?",
                        (note, user["id"], db.now_str(), r["id"]))
                    n_p2 += 1
                    continue
                # v1.0.19：借·换·检·修办理时同样实扣库存，终审不再跳过库存校验
                stock = db.total_stock(conn, r["part_id"])
                locked = conn.execute(
                    """SELECT COALESCE(SUM(a.quantity),0) s FROM applications a JOIN parts pp ON pp.id=a.part_id
                       WHERE a.part_id=? AND a.app_type='out' AND a.id<>?
                         AND ((pp.is_valuable=1 AND a.status='approved')
                           OR ((pp.is_valuable=0 AND a.status='pending') OR (a.app_type='out' AND IFNULL(a.out_type,'') IN ('return','exchange','internal','giveback','maintenance','verify') AND a.status IN ('pending','pending2','approved'))))""",
                    (r["part_id"], r["id"])).fetchone()["s"]
                avail_now = stock - locked - _borrow_in_use(conn, r["part_id"])
                if avail_now + 1e-9 < r["quantity"]:
                    auto_note = _xl("库存不足自动驳回：当前可用仅 {avail} {unit}，不足以出库 {qty}",
                                    avail=round(avail_now, 3), unit=r["unit"], qty=r["quantity"])
                    conn.execute(
                        "UPDATE applications SET status='rejected', approval_note=?, approver_id=?, processed_at=? WHERE id=?",
                        (auto_note, user["id"], db.now_str(), r["id"]))
                    n_rej += 1
                    continue
                conn.execute(
                    "UPDATE applications SET status='approved', approval_note=?, approver_id=?, processed_at=? WHERE id=?",
                    (note, user["id"], db.now_str(), r["id"]))
                n_ok += 1
            else:
                conn.execute(
                    "UPDATE applications SET status='rejected', approval_note=?, approver_id=?, processed_at=? WHERE id=?",
                    (note or _i18n.t("审批经理驳回", lang), user["id"], db.now_str(), r["id"]))
                n_rej += 1
        # v1.0.55：审批只决定「准不准」，不动库存、不置 completed。
        # 所有类型（入库/出库/报废/借换检修修/调库）一律停在 approved，
        # 由仓管员或 ADMIN 在「出入库调库管理」逐行确认执行后才真正完成。
        conn.commit()
        if action == "approve":
            flash(_xl("审批完成：{p1} — 批准 {a} 行、转二级审批 {b} 行、库存不足自动驳回 {c} 行；待仓管在出入库调库管理执行",
                      p1=doc_no, a=n_ok, b=n_p2, c=n_rej), "success")
        else:
            flash(_xl("已整单驳回：{p1}（{c} 行）", p1=doc_no, c=n_rej), "warning")
    finally:
        conn.close()
    return redirect(url_for("approval_page"))



@app.route("/applications/<int:aid>/execute", methods=["POST"])
@login_required
@role_required("admin", "warehouse")
def application_execute(aid):
    """仓库人员执行申请单（出库扣库存 / 入库加库存 / 调库调整放置位置），也可驳回"""
    user = current_user()
    action = request.form.get("action", "complete")
    note = request.form.get("note", "").strip()
    conn = get_db()
    try:
        app_row = conn.execute(
            """SELECT a.*, p.part_code, p.name, p.unit, p.qty_decimal, p.is_valuable, p.unit_price
               FROM applications a JOIN parts p ON a.part_id=p.id WHERE a.id=?""",
            (aid,),
        ).fetchone()
        if not app_row:
            flash(_i18n.t("申请单不存在", _i18n.current_lang()), "danger")
            return redirect(url_for("inout_page"))
        # 仓库/高级管理人员可执行/驳回自己发起的申请（权限由 role_required 保证）
        if action == "reject":
            if app_row["status"] not in ("pending", "approved"):
                flash(_i18n.t("该申请单状态不可驳回", _i18n.current_lang()), "warning")
                return redirect(url_for("inout_page"))
            conn.execute(
                "UPDATE applications SET status='rejected', operator_note=?, operator_id=?, processed_at=? WHERE id=?",
                (note or "仓库驳回", user["id"], db.now_str(), aid),
            )
            conn.commit()
            flash(_xl("已驳回申请单 {p1}", p1=app_row['app_no']), "warning")
            return redirect(url_for("inout_page"))

        # v1.0.48：仓管执行时数量可修改，但只能减少（0 < 执行数量 ≤ 申请数量）。
        # 表单不带 exec_qty 时回退申请数量，兼容旧表单/回归脚本。
        _apply_qty = float(app_row["quantity"] or 0)
        _raw_exec = (request.form.get("exec_qty") or "").strip()
        try:
            qty = float(_raw_exec) if _raw_exec else _apply_qty
        except (TypeError, ValueError):
            flash(_i18n.t("执行数量格式错误", _i18n.current_lang()), "danger")
            return redirect(url_for("inout_page"))
        if qty <= 0 or qty > _apply_qty + 1e-9:
            flash(_i18n.t("执行数量不能超过申请数量（只能减少）", _i18n.current_lang()), "danger")
            return redirect(url_for("inout_page"))
        ok, msg = _qty_valid(app_row, qty)
        if not ok:
            flash(msg, "danger")
            return redirect(url_for("inout_page"))
        # 少发时留痕：流水备注/操作备注都标注「申请 X，实发 Y」
        _short_note = (_xl("（申请 {p1}，实发 {p2}）", p1=_apply_qty, p2=qty)
                       if qty + 1e-9 < _apply_qty else "")

        # 退货/换货/内部转移（审批档位≥1 时）与贵重出库一样：先审批通过，仓库再执行
        _ot = (app_row["out_type"] or "").strip()
        _kind_out = app_row["app_type"] == "out" and _ot in OUT_KINDS
        if app_row["app_type"] == "out" and app_row["is_valuable"]:
            if app_row["status"] != "approved":
                flash(_i18n.t("贵重物料出库需先由高级管理人员审批通过后才能出库", _i18n.current_lang()), "danger")
                return redirect(url_for("inout_page"))
        if _kind_out and app_row["status"] != "approved":
            flash(_xl("{p1}出库需先审批通过后才能执行",
                      p1=_i18n.t(_OUT_KIND_NAMES[_ot], _i18n.current_lang())),
                  "danger")
            return redirect(url_for("inout_page"))
        if app_row["status"] != "pending" and not (
                app_row["app_type"] == "out"
                and (app_row["is_valuable"] or _kind_out)
                and app_row["status"] == "approved"):
            flash(_i18n.t("该申请单状态不可执行", _i18n.current_lang()), "warning")
            return redirect(url_for("inout_page"))

        # ---------- 调库申请执行：v1.0.70 按申请指定的批次行搬行 ----------
        if app_row["app_type"] == "transfer":
            to_id = app_row["to_location_id"]
            src_loc_id = app_row["from_location_id"]
            # A 方案：执行待报废 → 待维修，执行人必须持复判审批权限且单上有复判原因
            _re_err = _scrap_reassess_err(conn, user, src_loc_id, to_id,
                                          app_row["reason"], _i18n.current_lang())
            if _re_err:
                flash(_re_err, "danger")
                return redirect(url_for("inout_page"))
            batch_done = ""
            from_inv_id = _row_get(app_row, "from_inv_id")
            if _row_get(app_row, "whole_loc"):
                # v1.0.78：整库位申请——一次搬走该库位全部批次（执行数量可少于申请，按 FIFO 先搬早批）
                if src_loc_id:
                    whole_rows = conn.execute(
                        """SELECT * FROM inventory WHERE part_id=? AND location_id=?
                           ORDER BY in_date, id""",
                        (app_row["part_id"], src_loc_id)).fetchall()
                else:
                    whole_rows = conn.execute(
                        """SELECT * FROM inventory WHERE part_id=? AND location_id IS NULL
                           ORDER BY in_date, id""", (app_row["part_id"],)).fetchall()
                if not whole_rows:
                    flash(_i18n.t("源库位不存在（可能已被调整），无法执行调库", _i18n.current_lang()), "danger")
                    return redirect(url_for("inout_page"))
                from_txt, to_txt, batch_done, terr = _transfer_move_whole_location(
                    conn, app_row["part_id"], whole_rows, to_id, qty,
                    lang=_i18n.current_lang())
                if terr:
                    flash(terr, "danger")
                    return redirect(url_for("inout_page"))
            elif from_inv_id:
                # 新单：申请时已锁定库存行 id，执行时只搬这一行
                src_inv = conn.execute(
                    "SELECT * FROM inventory WHERE id=? AND part_id=?",
                    (from_inv_id, app_row["part_id"]),
                ).fetchone()
                if not src_inv:
                    flash(_i18n.t("所选批次不存在或已变化，请刷新重选", _i18n.current_lang()), "danger")
                    return redirect(url_for("inout_page"))
                # 数量合法性已在函数入口统一校验（v1.0.48 执行数量只能减少）
                from_txt, to_txt, batch_done, terr = _transfer_move_inv_row(
                    conn, app_row["part_id"], src_inv, to_id, qty,
                    lang=_i18n.current_lang())
                if terr:
                    flash(terr, "danger")
                    return redirect(url_for("inout_page"))
            else:
                # 老单（v1.0.70 前）：没有行 id，仍按库位找行，行为保持不变
                if src_loc_id:
                    src_inv = conn.execute(
                        """SELECT i.* FROM inventory i WHERE i.part_id=? AND i.location_id=?
                           ORDER BY i.id LIMIT 1""",
                        (app_row["part_id"], src_loc_id),
                    ).fetchone()
                else:
                    src_inv = conn.execute(
                        """SELECT i.* FROM inventory i WHERE i.part_id=? AND i.location_id IS NULL
                           ORDER BY i.id LIMIT 1""",
                        (app_row["part_id"],),
                    ).fetchone()
                if not src_inv:
                    flash(_i18n.t("源库位不存在（可能已被调整），无法执行调库", _i18n.current_lang()), "danger")
                    return redirect(url_for("inout_page"))
                if to_id:
                    to_loc_row = db.get_location(conn, to_id)
                    to_txt = db.location_text(to_loc_row) if to_loc_row else ""
                else:
                    to_txt = "待定"
                from_txt = src_inv["location"] or "待定"
                if src_inv["quantity"] + 1e-9 < qty:
                    flash(_xl("源库位 [{p1}] 现有 {p2} {p3}，不足调出 {p4}", p1=from_txt, p2=src_inv['quantity'], p3=app_row['unit'], p4=qty), "danger")
                    return redirect(url_for("inout_page"))
                if src_inv["quantity"] - qty < 1e-9:
                    conn.execute("DELETE FROM inventory WHERE id=?", (src_inv["id"],))
                else:
                    conn.execute("UPDATE inventory SET quantity=quantity-? WHERE id=?",
                                 (qty, src_inv["id"]))
                if to_id:
                    tgt_inv = conn.execute(
                        """SELECT i.id FROM inventory i WHERE i.part_id=? AND i.location_id=?
                           ORDER BY i.id LIMIT 1""", (app_row["part_id"], to_id)).fetchone()
                else:
                    tgt_inv = conn.execute(
                        """SELECT i.id FROM inventory i WHERE i.part_id=? AND i.location_id IS NULL
                           ORDER BY i.id LIMIT 1""", (app_row["part_id"],)).fetchone()
                if tgt_inv:
                    conn.execute("UPDATE inventory SET quantity=quantity+? WHERE id=?",
                                 (qty, tgt_inv["id"]))
                else:
                    conn.execute(
                        """INSERT INTO inventory(part_id, location_id, location, quantity)
                           VALUES(?,?,?,?)""", (app_row["part_id"], to_id, to_txt, qty))
            # 流水：带上批次，仓管一眼能看出搬的是哪一批
            _btxt = (f"批次 {batch_done} ×{qty}：" if batch_done else "")
            conn.execute(
                """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
                   ref_type,ref_id,operator_id,applicant_id,remark,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (db.gen_no("TX", conn, "transactions", "tx_no"), app_row["part_id"],
                 "transfer", qty, app_row["unit_price"], 0, to_txt,
                 "application", aid, user["id"], app_row["applicant_id"],
                 (((_scrap_reassess_mark(app_row["reason"]) + _i18n.mk("；"))
                   if _is_scrap_to_repair(conn, src_loc_id, to_id) else "")
                  + (_btxt + f"调库 {qty} {app_row['unit']}：{from_txt} → {to_txt} {note}".strip()
                     + _short_note)), db.now_str()),
            )
            conn.execute(
                "UPDATE applications SET status='completed', operator_note=?, operator_id=?, processed_at=? WHERE id=?",
                (f"{from_txt} → {to_txt} {note}".strip() + _short_note, user["id"], db.now_str(), aid),
            )
            conn.commit()
            _flash_b = (_xl("批次 {p1} ×{p2}：", p1=batch_done, p2=qty) if batch_done else "")
            flash(_flash_b + _xl("调库完成：{p1}  {p2} → {p3}",
                                 p1=app_row['part_code'], p2=from_txt, p3=to_txt)
                  + _short_note, "success")
            return redirect(url_for("inout_page"))

        # ---------- v1.0.19 借用申请执行：仓库办理借出，实扣库存 + 写出库流水 ----------
        if app_row["app_type"] == "borrow":
            # 借出前再校验一次可用量：等待办理期间可能被领走
            avail, in_use, _stock = _borrow_available(conn, app_row["part_id"],
                                                      exclude_app=app_row["id"])
            if qty > avail + 1e-9:
                flash(_xl("无法借出：[{code}] 当前可用仅 {p1} {p2}，不足借出 {p4} {p2}",
                          code=app_row["part_code"], p1=round(avail, 3), p2=app_row["unit"],
                          p4=qty), "danger")
                return redirect(url_for("inout_page"))
            # 仓管要先选「从哪个库位拿货」；该库位不够时由 FIFO 自动跨库位补足
            loc_key, loc_text, lerr = _parse_exec_location(
                conn, request.form, {"id": app_row["part_id"]}, True, _ot)
            if lerr:
                flash(lerr, "danger")
                return redirect(url_for("inout_page"))
            loc_row = db.get_location(conn, loc_key)
            wh = (loc_row["warehouse"] if loc_row else "") or ""
            borrower_row = conn.execute(
                "SELECT display_name FROM users WHERE id=?", (app_row["applicant_id"],)).fetchone()
            borrower = (borrower_row["display_name"] if borrower_row else "") or ""
            now = db.now_str()
            br_no = db.gen_no("BR", conn, "borrows", "borrow_no")
            _ok, _msg, _deducted = _borrow_stock_out(
                conn, app_row["part_id"], qty, loc_key or None,
                app_row["unit"], user, br_no, _i18n.t("借用", _i18n.current_lang()), out_type="borrow")
            if not _ok:
                conn.rollback()
                flash(_xl("无法借出：[{code}] {msg}", code=app_row["part_code"], msg=_msg), "danger")
                return redirect(url_for("inout_page"))
            conn.execute(
                """INSERT INTO borrows(borrow_no,part_id,part_code,part_name,unit,qty,borrower,
                   borrower_user_id,borrowed_at,expected_return,status,confirmed_at,confirmed_by,
                   warehouse,location,location_id,note,op_id,created_at,updated_at,
                   kind,destination,receiver,contact,out_person,repair_type,
                   stock_deducted,deducted_qty,returned_qty,restocked_qty)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (br_no, app_row["part_id"], app_row["part_code"], app_row["name"], app_row["unit"],
                 qty, borrower, app_row["applicant_id"], now,
                 app_row["expected_return"] or "", "borrowed", now, user["id"],
                 wh, loc_text, loc_key or None, app_row["reason"] or "", user["id"], now, now,
                 "borrow", "", "", "", "", "",
                 1, _deducted, 0, 0),
            )
            conn.execute(
                "UPDATE applications SET status='completed', operator_note=?, operator_id=?, processed_at=? WHERE id=?",
                (_xl("借用已借出，台账单号 {p1}", p1=br_no) + " @ " + loc_text + _short_note,
                 user["id"], now, aid),
            )
            conn.commit()
            flash(_xl("借用出库完成：{p1} x{p2} 已借给 {p3}，台账单号 {p4}",
                      p1=app_row["part_code"], p2=qty, p3=borrower, p4=br_no)
                  + " @ " + loc_text + _short_note, "success")
            return redirect(url_for("inout_page"))

        # ---------- 出入库申请执行 ----------
        if app_row["app_type"] == "out":
            if _part_pending(conn, app_row["part_id"]):
                flash(_i18n.t("该物料当前处于「待定」状态（所有库存均无放置位置），不允许领用；请先执行调库到具体位置", _i18n.current_lang()), "danger")
                return redirect(url_for("inout_page"))
            if app_row["is_valuable"] and app_row["status"] != "approved":
                flash(_i18n.t("贵重物料出库需先由审批经理审批通过后才能出库", _i18n.current_lang()), "danger")
                return redirect(url_for("inout_page"))
            # v1.0.19：换货/维修/校验·测试审批通过后执行 → 实扣库存 + 台账 + 出库流水
            if _ot in BORROW_OUT_KINDS:
                avail, in_use, _stock = _borrow_available(conn, app_row["part_id"],
                                                      exclude_app=app_row["id"])
                if qty > avail + 1e-9:
                    flash(_xl("无法执行：[{code}] 当前可用仅 {p1} {p2}，不足 {p4} {p2}",
                              code=app_row["part_code"], p1=round(avail, 3), p2=app_row["unit"],
                              p4=qty), "danger")
                    return redirect(url_for("inout_page"))
                loc_key, loc_text, lerr = _parse_exec_location(
                    conn, request.form, {"id": app_row["part_id"]}, True, _ot)
                if lerr:
                    flash(lerr, "danger")
                    return redirect(url_for("inout_page"))
                loc_row = db.get_location(conn, loc_key)
                wh = (loc_row["warehouse"] if loc_row else "") or ""
                now = db.now_str()
                br_no = db.gen_no("BR", conn, "borrows", "borrow_no")
                _ok, _msg, _deducted = _borrow_stock_out(
                    conn, app_row["part_id"], qty, loc_key or None,
                    app_row["unit"], user, br_no, _OUT_KIND_NAMES.get(_ot, _ot), out_type=_ot)
                if not _ok:
                    conn.rollback()
                    flash(_xl("无法执行：[{code}] {msg}", code=app_row["part_code"], msg=_msg), "danger")
                    return redirect(url_for("inout_page"))
                dest_note = _borrow_out_note(_ot, app_row["destination"], app_row["receiver"],
                                             app_row["contact"], app_row["reason"] or "",
                                             app_row["out_person"] or "")
                if _ot in TRACK5_OUT_KINDS:
                    _borrower = app_row["out_person"] or ""
                    _borrower_uid = app_row["out_person_id"]
                else:
                    borrower_row = conn.execute(
                        "SELECT display_name FROM users WHERE id=?", (app_row["applicant_id"],)).fetchone()
                    _borrower = (borrower_row["display_name"] if borrower_row else "") or ""
                    _borrower_uid = app_row["applicant_id"]
                conn.execute(
                    """INSERT INTO borrows(borrow_no,part_id,part_code,part_name,unit,qty,borrower,
                       borrower_user_id,borrowed_at,expected_return,status,confirmed_at,confirmed_by,
                       warehouse,location,location_id,note,op_id,created_at,updated_at,kind,
                       destination,receiver,contact,out_person,repair_type,
                       stock_deducted,deducted_qty,returned_qty,restocked_qty)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (br_no, app_row["part_id"], app_row["part_code"], app_row["name"], app_row["unit"],
                     qty, _borrower, _borrower_uid, now,
                     app_row["expected_return"] or "", "borrowed", now, user["id"],
                     wh, loc_text, loc_key or None, dest_note,
                     user["id"], now, now, _ot,
                     app_row["destination"], app_row["receiver"], app_row["contact"],
                     app_row["out_person"], app_row["repair_type"] or "",
                     1, _deducted, 0, 0),
                )
                if _ot == "exchange":
                    _op_done = _xl("换货已登记借·换·检·修台账，单号 {p1}", p1=br_no) + " @ " + loc_text
                else:
                    _op_done = (_OUT_KIND_NAMES[_ot] + "已登记借·换·检·修台账，单号 " + br_no +
                                " @ " + loc_text)
                conn.execute(
                    "UPDATE applications SET status='completed', operator_note=?, operator_id=?, processed_at=? WHERE id=?",
                    (_op_done + _short_note, user["id"], now, aid),
                )
                conn.commit()
                if _ot == "exchange":
                    flash(_xl("换货出库完成：{p1} x{p2}，借·换·检·修台账单号 {p3}，预计返回 {p4}",
                              p1=app_row["part_code"], p2=qty, p3=br_no,
                              p4=(app_row["expected_return"] or "")[:10])
                          + " @ " + loc_text + _short_note, "success")
                else:
                    flash(_OUT_KIND_NAMES[_ot] + "出库完成：" + app_row["part_code"] + " x" +
                          str(qty) + "，借·换·检·修台账单号 " + br_no +
                          "，预计返回 " + (app_row["expected_return"] or "")[:16] +
                          " @ " + loc_text + _short_note, "success")
                return redirect(url_for("inout_page"))
        loc_key, loc_text, err = _parse_exec_location(
            conn, request.form, app_row, app_row["app_type"] == "out",
            app_row["out_type"] if app_row["app_type"] == "out" else None)
        if err:
            flash(err, "danger")
            return redirect(url_for("inout_page"))
        # v1.0.48：执行数量统一在函数入口解析校验（只减不增）
        ex_batch = ""
        if app_row["app_type"] == "in":
            ex_batch = (request.form.get("batch_no", "") or "").strip() or datetime.now().strftime("%Y%m%d")
            # v1.0.29：配件状态已在申请单确定——执行时以申请单存储值为准，
            # 不再采信表单里的 condition（防止前端重复选择/直接 POST 绕过）。
            _cond = str(_row_get(app_row, "condition", "") or "good")
            if _cond not in CONDITIONS:
                _cond = "good"
            _loc_attr = None
            if isinstance(loc_key, int):
                _lr = db.get_location(conn, loc_key)
                _loc_attr = _row_get(_lr, "attribute") if _lr else None
            if not _cond_attr_allowed(_cond, _loc_attr, _is_pending_loc(loc_key, loc_text)):
                if _cond == "defective":
                    # v1.0.28：旧文案与 v1.0.27 的「待定(不良品)」正面矛盾，
                    # 改用同一份已登记词条，明确不良品的可放范围。
                    flash(_i18n.t("不良品可入待维修 / 待报废库位，或暂放待定", _i18n.current_lang()), "danger")
                else:
                    flash(_xl("该库位属性 [{p1}] 不允许存放 [{p2}]",
                              p1=_i18n.t(LOC_ATTR_NAMES.get(_loc_attr or "good", "良品"), _i18n.current_lang()),
                              p2=_i18n.t(CONDITION_NAMES[_cond], _i18n.current_lang())), "danger")
                return redirect(url_for("inout_page"))
            _repair_in = qty if _cond == "repair" else 0
            ok, msg = db.move_stock(conn, app_row["part_id"], loc_key, qty,
                                   batch_no=ex_batch, repair=_repair_in, condition=_cond)
            if not ok:
                flash(msg, "danger")
                return redirect(url_for("inout_page"))
            tx_rows = [(loc_text, qty)]
        else:
            # 出库：数量可能大于单个库位，按先进先出跨库位自动补足
            ok, msg, moves = _stock_out_auto(conn, app_row["part_id"], loc_key, qty, app_row["unit"], app_row["out_type"] or "general")
            if not ok:
                flash(msg, "danger")
                return redirect(url_for("inout_page"))
            loc_text = _out_loc_text(moves)
            tx_rows = moves
        _tx_note = note + _short_note
        if _kind_out and _ot not in BORROW_OUT_KINDS:
            # v1.0.27：报废不写空的目的地/接收人/联系方式，流水只留「报废：原因」
            _tx_note = ((_i18n.t(_OUT_KIND_NAMES[_ot], _i18n.current_lang())
                         if _ot == "scrap" else
                         _xl("{p1}：目的地 {p2}，接收人 {p3}，联系方式 {p4}",
                             p1=_i18n.t(_OUT_KIND_NAMES[_ot], _i18n.current_lang()),
                             p2=app_row["destination"], p3=app_row["receiver"], p4=app_row["contact"]))
                        + _i18n.mk("；") + note + _short_note).strip(_i18n.mk("；"))
        # v1.0.13：入库申请执行时把申请单上的入库子类型带到流水（历史空值按采购）
        _tx_ot = ((_ot or "general") if app_row["app_type"] == "out"
                  else (_norm_in_type(_ot) if app_row["app_type"] == "in" else ""))
        for m_loc, m_qty in tx_rows:
            conn.execute(
                """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
                   out_type,ref_type,ref_id,operator_id,applicant_id,line_name,device_name,remark,batch_no,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (db.gen_no("TX", conn, "transactions", "tx_no"), app_row["part_id"],
                 app_row["app_type"], m_qty, app_row["unit_price"],
                 round(m_qty * app_row["unit_price"], 2), m_loc, _tx_ot,
                 "application", aid, user["id"], app_row["applicant_id"],
                 app_row["line_name"] or "", app_row["device_name"] or "", _tx_note, ex_batch, db.now_str()),
            )
        if len(tx_rows) > 1:
            note = (note + " " if note else "") + _xl("跨库位出库：{p1}", p1=loc_text)
        note = note + _short_note
        conn.execute(
            "UPDATE applications SET status='completed', operator_note=?, operator_id=?, processed_at=? WHERE id=?",
            (note, user["id"], db.now_str(), aid),
        )
        # 出库完成后：检查同物料的其他待审批出库申请，库存不足的自动驳回
        if app_row["app_type"] == "out":
            # 剩余可用同样要扣在借：借出去的东西不能算给下一张申请
            remain_stock = db.total_stock(conn, app_row["part_id"]) - _borrow_in_use(conn, app_row["part_id"])
            if remain_stock < 0:
                remain_stock = 0
            over_apps = conn.execute(
                """SELECT id, app_no, quantity, applicant_id FROM applications
                   WHERE part_id=? AND app_type='out' AND id<>? AND status IN ('pending','pending2','approved')
                   AND IFNULL(out_type,'') NOT IN ('exchange','maintenance','verify')
                   AND quantity > ?""",
                (app_row["part_id"], aid, remain_stock),
            ).fetchall()
            for oa in over_apps:
                conn.execute(
                    """UPDATE applications SET status='rejected', approval_note=?, operator_id=?, processed_at=?
                       WHERE id=?""",
                    (f"库存不足自动驳回：当前仅剩 {remain_stock} {app_row['unit']}，不足以出库 {oa['quantity']} {app_row['unit']}（库存已被其他申请领走）",
                     user["id"], db.now_str(), oa["id"]),
                )
                # 写一条流水备注？不用，只是驳回申请
        conn.commit()
        flash(_xl("申请单 {p1} 已执行完成：{p2} x{p3} {p4} @ {p5}", p1=app_row['app_no'], p2=app_row['part_code'], p3=qty, p4=app_row['unit'], p5=loc_text), "success")
    finally:
        conn.close()
    return redirect(url_for("inout_page"))


@app.route("/applications/<int:aid>/cancel", methods=["POST"])
@perm_required("apply", "cancel")
@login_required
def application_cancel_one(aid):
    """单行撤销：只撤这一条明细（整单撤销用 /applications/doc/<doc_no>/cancel）。

    一张单里常常只想撤某个物料的领用，整单撤销会把同单其他行一起作废，
    所以保留行级入口——规则与整单撤销一致：只能撤本人发起的、仍为待处理的行。
    """
    user = current_user()
    lang = _i18n.current_lang()
    conn = get_db()
    try:
        r = conn.execute(
            """SELECT a.*, p.part_code, p.unit FROM applications a JOIN parts p ON a.part_id=p.id
               WHERE a.id=?""", (aid,)).fetchone()
        if not r:
            flash(_i18n.t("申请单不存在", lang), "danger")
            return redirect(url_for("applications_page"))
        if r["applicant_id"] != user["id"]:
            flash(_i18n.t("只能撤销自己发起的申请", lang), "danger")
            return redirect(url_for("applications_page"))
        if r["status"] != "pending":
            flash(_i18n.t("该明细不是待处理状态，不能直接撤销；已完成/已批准的请使用「申请撤销」", lang), "warning")
            return redirect(url_for("applications_page"))
        conn.execute(
            "UPDATE applications SET status='canceled', operator_note=?, operator_id=?, processed_at=? WHERE id=?",
            (_i18n.t("申请人撤销", lang), user["id"], db.now_str(), aid))
        conn.commit()
        flash(_xl("已撤销明细 {p1}（{p2} {p3}）", p1=r["part_code"], p2=r["quantity"], p3=r["unit"]), "success")
    finally:
        conn.close()
    return redirect(url_for("applications_page"))


@app.route("/applications/<int:aid>/cancel-request", methods=["POST"])
@login_required
@perm_required("apply")
def application_cancel_request_one(aid):
    """单行申请撤销：仅对这一条已完成/已批准的明细发起撤销请求（仓管员审批后回滚）"""
    user = current_user()
    lang = _i18n.current_lang()
    note = request.form.get("note", "").strip()
    conn = get_db()
    try:
        r = conn.execute(
            """SELECT a.*, p.part_code, p.unit FROM applications a JOIN parts p ON a.part_id=p.id
               WHERE a.id=?""", (aid,)).fetchone()
        if not r:
            flash(_i18n.t("申请单不存在", lang), "danger")
            return redirect(url_for("applications_page"))
        if r["applicant_id"] != user["id"]:
            flash(_i18n.t("只能对本人发起的申请提交撤销请求", lang), "danger")
            return redirect(url_for("applications_page"))
        if r["status"] != "approved":
            flash(_i18n.t("仅「已批准·待出库」的申请可以申请撤销；已完成的出入库不可再申请撤销", lang), "warning")
            return redirect(url_for("applications_page"))
        # 调库行：完成后目标位置若已被再次调整，撤销会冲突
        if r["app_type"] == "transfer" and r["status"] == "completed":
            to_loc = r["to_location_id"]
            if to_loc:
                cur = conn.execute(
                    "SELECT 1 FROM inventory WHERE part_id=? AND location_id=? LIMIT 1",
                    (r["part_id"], to_loc)).fetchone()
            else:
                cur = conn.execute(
                    "SELECT 1 FROM inventory WHERE part_id=? AND location_id IS NULL LIMIT 1",
                    (r["part_id"],)).fetchone()
            if not cur:
                flash(_xl("行 [{code}] 调库完成后目标位置已被再次调整，无法撤销，请联系仓管员处理",
                          code=r["part_code"]), "warning")
                return redirect(url_for("applications_page"))
        conn.execute(
            "UPDATE applications SET status='cancel_req', operator_note=?, operator_id=?, processed_at=? WHERE id=?",
            (_i18n.t("申请人申请撤销", lang) + (": " + note if note else ""), user["id"], db.now_str(), aid))
        conn.commit()
        flash(_xl("撤销请求已提交（{p1} {p2}），等待仓管员审批", p1=r["part_code"], p2=r["quantity"]), "success")
    finally:
        conn.close()
    return redirect(url_for("applications_page"))


@app.route("/applications/doc/<doc_no>/cancel", methods=["POST"])
@perm_required("apply", "cancel")
@login_required
def application_doc_cancel(doc_no):
    """申请人整单撤销：单内所有仍为「待处理」的明细行撤销（其他状态的行保持不变）"""
    user = current_user()
    lang = _i18n.current_lang()
    conn = get_db()
    try:
        rows = _doc_rows(conn, doc_no)
        if not rows:
            flash(_i18n.t("申请单不存在", lang), "danger")
            return redirect(url_for("applications_page"))
        if any(r["applicant_id"] != user["id"] for r in rows):
            flash(_i18n.t("只能撤销自己发起的申请", lang), "danger")
            return redirect(url_for("applications_page"))
        pending = [r for r in rows if r["status"] == "pending"]
        if not pending:
            flash(_i18n.t("该申请单没有可撤销的待处理明细；已完成/已批准的请使用「申请撤销」", lang), "warning")
            return redirect(url_for("applications_page"))
        for r in pending:
            conn.execute(
                "UPDATE applications SET status='canceled', operator_note=?, operator_id=?, processed_at=? WHERE id=?",
                (_i18n.t("申请人撤销", lang), user["id"], db.now_str(), r["id"]))
        conn.commit()
        flash(_xl("申请单 {p1} 已撤销（{n} 行）", p1=doc_no, n=len(pending)), "success")
    finally:
        conn.close()
    return redirect(url_for("applications_page"))


@app.route("/applications/doc/<doc_no>/cancel-request", methods=["POST"])
@login_required
@perm_required("apply")
def application_doc_cancel_request(doc_no):
    """申请人整单请求撤销已完成/已批准的申请单（需仓管员审批，通过后逐行回滚）"""
    user = current_user()
    lang = _i18n.current_lang()
    note = request.form.get("note", "").strip()
    conn = get_db()
    try:
        rows = _doc_rows(conn, doc_no)
        if not rows:
            flash(_i18n.t("申请单不存在", lang), "danger")
            return redirect(url_for("applications_page"))
        if any(r["applicant_id"] != user["id"] for r in rows):
            flash(_i18n.t("只能对本人发起的申请提交撤销请求", lang), "danger")
            return redirect(url_for("applications_page"))
        undoable = [r for r in rows if r["status"] == "approved"]
        if not undoable:
            flash(_i18n.t("仅「已批准·待出库」的申请可以申请撤销；已完成的出入库不可再申请撤销", lang), "warning")
            return redirect(url_for("applications_page"))
        # 调库行：完成后目标行若已被再次调整，撤销会冲突 → 整单拒绝
        for r in undoable:
            if r["app_type"] == "transfer" and r["status"] == "completed":
                to_loc = r["to_location_id"]
                if to_loc:
                    cur = conn.execute(
                        "SELECT location_id FROM inventory WHERE part_id=? AND location_id=? LIMIT 1",
                        (r["part_id"], to_loc)).fetchone()
                else:
                    cur = conn.execute(
                        "SELECT location_id FROM inventory WHERE part_id=? AND location_id IS NULL LIMIT 1",
                        (r["part_id"],)).fetchone()
                if not cur:
                    flash(_xl("行 [{code}] 调库完成后目标位置已被再次调整，无法撤销，请联系仓管员处理",
                              code=r["part_code"]), "warning")
                    return redirect(url_for("applications_page"))
        for r in undoable:
            conn.execute(
                "UPDATE applications SET status='cancel_req', operator_note=?, operator_id=?, processed_at=? WHERE id=?",
                (_i18n.t("申请人申请撤销", lang) + ": " + note, user["id"], db.now_str(), r["id"]))
        conn.commit()
        flash(_xl("撤销请求已提交（{p1}，{n} 行），等待仓管员审批", p1=doc_no, n=len(undoable)), "success")
    finally:
        conn.close()
    return redirect(url_for("applications_page"))


def _rollback_app_one(conn, app_row):
    """单行撤销回滚（仓管批准撤销后调用）：入库扣回 / 出库退回 / 调库位置调回。
    返回 (ok, msg)。只做库存操作不 commit，由调用方统一提交/回滚。"""
    # v1.0.48：只允许「已批准·待出库」申请撤销，这类单尚未执行、没有库存流水，
    # 直接置已撤销即可；老数据若已执行但流水缺失，也不能凭空回滚。
    if not conn.execute(
        "SELECT 1 FROM transactions WHERE ref_type='application' AND ref_id=? LIMIT 1",
        (app_row["id"],)).fetchone():
        return True, ""
    if app_row["app_type"] == "in":
        tx = conn.execute(
            "SELECT location FROM transactions WHERE ref_type='application' AND ref_id=? AND tx_type='in' ORDER BY id DESC LIMIT 1",
            (app_row["id"],)).fetchone()
        if not tx:
            return False, _i18n.t("未找到该入库流水，无法回滚", _i18n.current_lang())
        ok, msg = db.move_stock(conn, app_row["part_id"], tx["location"], -app_row["quantity"])
        if not ok:
            return False, _i18n.t("回滚失败：{0}（该库位现有库存不足，无法扣回）", _i18n.current_lang()).format(msg)
    elif app_row["app_type"] == "out":
        tx = conn.execute(
            "SELECT location FROM transactions WHERE ref_type='application' AND ref_id=? AND tx_type='out' ORDER BY id DESC LIMIT 1",
            (app_row["id"],)).fetchone()
        if not tx:
            return False, _i18n.t("未找到该出库流水，无法回滚", _i18n.current_lang())
        ok, msg = db.move_stock(conn, app_row["part_id"], tx["location"], app_row["quantity"])
        if not ok:
            return False, msg
    else:  # transfer
        qty = app_row["quantity"]
        src_loc = app_row["from_location_id"]
        to_loc = app_row["to_location_id"]
        if to_loc:
            inv_row = conn.execute(
                """SELECT i.id, i.quantity, i.location FROM inventory i
                   WHERE i.part_id=? AND i.location_id=? ORDER BY i.id LIMIT 1""",
                (app_row["part_id"], to_loc)).fetchone()
        else:
            inv_row = conn.execute(
                """SELECT i.id, i.quantity, i.location FROM inventory i
                   WHERE i.part_id=? AND i.location_id IS NULL ORDER BY i.id LIMIT 1""",
                (app_row["part_id"],)).fetchone()
        if not inv_row:
            return False, _i18n.t("未找到调库后的目标库位库存，无法撤销（该库位可能已被再次调整），请联系仓管员处理", _i18n.current_lang())
        if inv_row["quantity"] + 1e-9 < qty:
            return False, _i18n.t("目标库位现有库存不足，无法撤销（该库位可能已被再次调整），请联系仓管员处理", _i18n.current_lang())
        carry = min(_inv_repair(conn, inv_row["id"]), float(qty or 0))
        if inv_row["quantity"] - qty < 1e-9:
            conn.execute("DELETE FROM inventory WHERE id=?", (inv_row["id"],))
        else:
            conn.execute("UPDATE inventory SET quantity=quantity-? WHERE id=?", (qty, inv_row["id"]))
        if src_loc:
            src_inv = conn.execute(
                """SELECT i.id FROM inventory i WHERE i.part_id=? AND i.location_id=?
                   ORDER BY i.id LIMIT 1""", (app_row["part_id"], src_loc)).fetchone()
            loc_row = db.get_location(conn, src_loc)
            src_txt = db.location_text(loc_row) if loc_row else ""
        else:
            src_inv = conn.execute(
                """SELECT i.id FROM inventory i WHERE i.part_id=? AND i.location_id IS NULL
                   ORDER BY i.id LIMIT 1""", (app_row["part_id"],)).fetchone()
            src_txt = "待定"
        if src_inv:
            conn.execute("UPDATE inventory SET quantity=quantity+? WHERE id=?", (qty, src_inv["id"]))
            src_id = src_inv["id"]
        else:
            cur_s = conn.execute(
                "INSERT INTO inventory(part_id, location_id, location, quantity) VALUES(?,?,?,?)",
                (app_row["part_id"], src_loc, src_txt, qty))
            src_id = cur_s.lastrowid
        if carry > 0:
            conn.execute("UPDATE inventory SET repair_qty=MIN(quantity, IFNULL(repair_qty,0)+?) WHERE id=?",
                         (carry, src_id))
    return True, ""


@app.route("/applications/doc/<doc_no>/cancel-approve", methods=["POST"])
@login_required
@perm_required("stock_inout", "undo_approval")
def application_doc_cancel_approve(doc_no):
    """仓管员整单批准撤销请求：逐行回滚库存/位置（任一行失败整单回滚），单置为已撤销"""
    user = current_user()
    lang = _i18n.current_lang()
    note = request.form.get("note", "").strip()
    conn = get_db()
    try:
        rows = _doc_rows(conn, doc_no)
        if not rows:
            flash(_i18n.t("申请单不存在", lang), "danger")
            return redirect(url_for("inout_page"))
        targets = [r for r in rows if r["status"] == "cancel_req"]
        if not targets:
            flash(_i18n.t("该申请单当前无待审批的撤销请求", lang), "warning")
            return redirect(url_for("inout_page"))
        if any(r["applicant_id"] == user["id"] for r in targets) and user["role"] not in ("admin", "warehouse"):
            flash(_i18n.t("不能审批本人发起的撤销请求，请由其他仓管员处理", lang), "danger")
            return redirect(url_for("inout_page"))
        done = 0
        for r in targets:
            ok, msg = _rollback_app_one(conn, r)
            if not ok:
                conn.rollback()
                flash(_xl("[{code}] {m}", code=r["part_code"], m=msg), "danger")
                return redirect(url_for("inout_page"))
            conn.execute(
                "UPDATE applications SET status='canceled', operator_note=?, operator_id=?, processed_at=? WHERE id=?",
                ((_i18n.t("批准撤销回滚", lang) + "：" + note).strip(), user["id"], db.now_str(), r["id"]))
            done += 1
        conn.commit()
        flash(_xl("已批准撤销，库存/位置已回滚：{p1}（{n} 行）", p1=doc_no, n=done), "success")
    finally:
        conn.close()
    return redirect(url_for("inout_page"))


@app.route("/applications/doc/<doc_no>/cancel-reject", methods=["POST"])
@login_required
@perm_required("stock_inout", "undo_approval")
def application_doc_cancel_reject(doc_no):
    """仓管员整单驳回撤销请求：单内撤销请求行恢复为原状态（已执行→已完成，未执行→已批准）"""
    user = current_user()
    lang = _i18n.current_lang()
    note = request.form.get("note", "").strip()
    conn = get_db()
    try:
        rows = _doc_rows(conn, doc_no)
        if not rows:
            flash(_i18n.t("申请单不存在", lang), "danger")
            return redirect(url_for("inout_page"))
        targets = [r for r in rows if r["status"] == "cancel_req"]
        if not targets:
            flash(_i18n.t("该申请单当前无待审批的撤销请求", lang), "warning")
            return redirect(url_for("inout_page"))
        if any(r["applicant_id"] == user["id"] for r in targets) and user["role"] not in ("admin", "warehouse"):
            flash(_i18n.t("不能审批本人发起的撤销请求，请由其他仓管员处理", lang), "danger")
            return redirect(url_for("inout_page"))
        for r in targets:
            # v1.0.48：已执行（有流水）恢复 completed；未执行（approved 申请撤销）恢复 approved
            _has_tx = conn.execute(
                "SELECT 1 FROM transactions WHERE ref_type='application' AND ref_id=? LIMIT 1",
                (r["id"],)).fetchone()
            _back = "completed" if _has_tx else "approved"
            conn.execute(
                "UPDATE applications SET status=?, operator_note=?, operator_id=?, processed_at=? WHERE id=?",
                (_back, (_i18n.t("驳回撤销", lang) + "：" + note).strip(),
                 user["id"], db.now_str(), r["id"]))
        conn.commit()
        flash(_xl("已驳回撤销请求：{p1}（{n} 行）", p1=doc_no, n=len(targets)), "warning")
    finally:
        conn.close()
    return redirect(url_for("inout_page"))


# ------------------------------------------------------------------
# 审批中心（高级管理人员）
# ------------------------------------------------------------------
def _approval_out_docs(conn, user):
    """审批待办出库单（按 doc_no 分组）。v1.0.48：纸档报废单未上传签字附件时不进
    审批列表；角标 /api/pending_counts 也走这里，两处口径完全同源。"""
    rows = _approval_out_rows(conn, user)
    for a in rows:
        sls = db.stock_locations(conn, a["part_id"])
        a["all_locs_text"] = _i18n.mk("；").join(
            f"{s['location']} ×{s['quantity']}" for s in sls if s["quantity"])
    docs, order = {}, []
    for r in rows:
        key = r["doc_no"] or r["app_no"]
        if key not in docs:
            docs[key] = {"doc_no": key, "reason": r["reason"] or "",
                         "created_at": r["created_at"], "rows": [],
                         "assignee_name": r["assignee_name"] or ""}
            order.append(key)
        docs[key]["rows"].append(r)
    doc_list = [docs[k] for k in order]
    for d in doc_list:
        d["n_rows"] = len(d["rows"])
        d["actionable"] = any(r["can_act"] and r["status"] in ("pending", "pending2")
                              for r in d["rows"])
        d["mine"] = any(r["can_act"] for r in d["rows"])
        d["has_pending2"] = any(r["status"] == "pending2" for r in d["rows"])
        d["attach_required"] = any(r["attachment_required"] for r in d["rows"])
        if d["attach_required"]:
            d["attach_count"] = conn.execute(
                "SELECT COUNT(*) c FROM scrap_attachments WHERE doc_no=?",
                (d["doc_no"],)).fetchone()["c"]
        else:
            d["attach_count"] = 0
    # v1.0.48：纸档模式未上传签字附件 → 根本不进审批待办列表
    doc_list = [d for d in doc_list if not (d["attach_required"] and d["attach_count"] == 0)]
    doc_list.sort(key=lambda d: (not d["actionable"], d["rows"][0]["id"]))
    return doc_list


@app.route("/approval")
@login_required
def approval_page():
    """审批单（v1.0.15）：出库申请 + 盘点单的统一待办入口。

    去掉 perm_required('approve')：被指派的部门主管可能没有该模块权限，
    指派即授权；可见范围由 _approval_out_rows / _approval_stk_rows 决定，
    与 /api/pending_counts 的角标完全同源。管理员看全站但只有可操作的行给按钮。
    """
    user = current_user()
    conn = get_db()
    try:
        doc_list = _approval_out_docs(conn, user)
        stk_docs = _approval_stk_rows(conn, user)
        stk_docs.sort(key=lambda s: (not s["can_act"], s["id"]))
        # v1.0.51：审批人查看「我已审批的历史」——按 approver_id（终审/驳回人）聚合，
        # 一级转二级的单 approver_id 也记了人，符合「我批过的」口径。
        hist_rows = conn.execute(
            """SELECT a.doc_no, a.status, a.approval_note, a.processed_at, a.out_type,
                      ua.display_name applicant,
                      (SELECT COUNT(*) FROM scrap_attachments sa WHERE sa.doc_no=a.doc_no) attach_count,
                      GROUP_CONCAT(p.part_code, '、') part_codes,
                      SUM(a.quantity) qty
               FROM applications a
               LEFT JOIN users ua ON a.applicant_id=ua.id
               LEFT JOIN parts p ON a.part_id=p.id
               WHERE a.approver_id=? AND a.status IN ('approved','rejected','completed')
               GROUP BY a.doc_no, a.status, a.approval_note, a.processed_at, a.out_type, ua.display_name
               ORDER BY a.processed_at DESC, a.id DESC LIMIT 200""",
            (user["id"],)).fetchall()
        my_history = [dict(h) for h in hist_rows]
    finally:
        conn.close()
    return render_template("approval.html", docs=doc_list, stk_docs=stk_docs,
                           my_history=my_history, is_admin=(user["role"] == "admin"))


# ------------------------------------------------------------------
# 我的记录（每个用户查看自己的出入库与操作流水）
# ------------------------------------------------------------------
@app.route("/my-records")
@login_required
def my_records_page():
    user = current_user()
    conn = get_db()
    try:
        txs = conn.execute(
            """SELECT t.*, p.part_code, p.name, p.unit, u.display_name op_name
               FROM transactions t
               LEFT JOIN parts p ON t.part_id=p.id
               LEFT JOIN users u ON t.operator_id=u.id
               WHERE t.operator_id=? OR t.applicant_id=?
               ORDER BY t.id DESC LIMIT 200""",
            (user["id"], user["id"]),
        ).fetchall()
        # v1.0.64：我的记录里同样还原历史回补流水的返回类型
        txs = [_borrow_return_remark_display(conn, t) for t in txs]
        apps = conn.execute(
            """SELECT a.*, p.part_code, p.name, p.is_valuable,
                      COALESCE(ua.display_name,'') assignee_name,
                      COALESCE(up.display_name,'') approver_name
               FROM applications a LEFT JOIN parts p ON a.part_id=p.id
               LEFT JOIN users ua ON a.assigned_approver_id=ua.id
               LEFT JOIN users up ON a.approver_id=up.id
               WHERE a.applicant_id=?
               ORDER BY a.id DESC LIMIT 200""",
            (user["id"],),
        ).fetchall()
    finally:
        conn.close()
    return render_template("my_records.html", txs=txs, apps=apps)


# ------------------------------------------------------------------
# 用户管理（高级管理人员）
# ------------------------------------------------------------------
@app.route("/users")
@login_required
@perm_required("user_manage")
def users_page():
    conn = get_db()
    try:
        users = conn.execute(
            "SELECT id, username, display_name, role, department, is_supervisor, active, created_at, email, enterprise_id FROM users ORDER BY id"
        ).fetchall()
        departments = conn.execute(
            "SELECT * FROM departments ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return render_template("users.html", users=users,
                           roles=[r for r in config.ROLE_NAMES if r != "guest"],   # 访客为内置角色，不手动分配
                           departments=departments,
                           cur_uid=current_user()["id"])


@app.route("/users/add", methods=["POST"])
@login_required
@perm_required("user_manage", "add")
def user_add():
    f = request.form
    username = f.get("username", "").strip()
    pwd = f.get("password", "")
    name = f.get("display_name", "").strip()
    role = f.get("role", "user")
    department = f.get("department", "").strip()
    if not re.fullmatch(r"[\w.\-]{2,30}", username or ""):
        flash(_i18n.t("用户名需为 2-30 位字母/数字/下划线/点/横线", _i18n.current_lang()), "danger")
        return redirect(url_for("users_page"))
    if len(pwd) < 6:
        flash(_i18n.t("密码至少 6 位", _i18n.current_lang()), "danger")
        return redirect(url_for("users_page"))
    if not name:
        flash(_i18n.t("请填写姓名/工号", _i18n.current_lang()), "danger")
        return redirect(url_for("users_page"))
    # 审批人由部门配置（一级/二级）指定，用户身上不再保留主管标记
    is_sup = 0
    if role not in config.ROLE_NAMES:
        role = "user"
    conn = get_db()
    try:
        if conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
            flash(_i18n.t("用户名 {0} 已存在", _i18n.current_lang()).format(username), "danger")
            return redirect(url_for("users_page"))
        if department and not conn.execute(
                "SELECT id FROM departments WHERE name=?", (department,)).fetchone():
            department = ""
        conn.execute(
            "INSERT INTO users(username,password_hash,display_name,role,department,is_supervisor,active,email,enterprise_id,created_at) VALUES(?,?,?,?,?,?,1,?,?,?)",
            (username, generate_password_hash(pwd), name, role, department, is_sup,
             f.get("email", "").strip(), f.get("enterprise_id", "").strip(), db.now_str()),
        )
        conn.commit()
        flash(_i18n.t("用户 [{0}] 添加成功", _i18n.current_lang()).format(name), "success")
    finally:
        conn.close()
    return redirect(url_for("users_page"))


@app.route("/users/<int:uid>/edit", methods=["POST"])
@login_required
@perm_required("user_manage", "edit")
def user_edit(uid):
    f = request.form
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            flash(_i18n.t("用户不存在", _i18n.current_lang()), "danger")
            return redirect(url_for("users_page"))
        name = f.get("display_name", "").strip()
        role = f.get("role", row["role"])
        department = f.get("department", "").strip()
        active = 1 if f.get("active") else 0
        new_pwd = f.get("password", "")
        is_sup = row["is_supervisor"] if "is_supervisor" in row.keys() else 0  # 保留原标记，不再通过表单修改
        if role not in config.ROLE_NAMES:
            role = row["role"]
        if not name:
            flash(_i18n.t("姓名不能为空", _i18n.current_lang()), "danger")
            return redirect(url_for("users_page"))
        # 防止把自己停用/降级后无法管理（若系统只剩一个 admin）
        admins = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE role='admin' AND active=1"
        ).fetchone()["c"]
        if uid == current_user()["id"]:
            if not active or role != "admin":
                if admins <= 1:
                    flash(_i18n.t("不能停用或降级自己（系统至少保留一名启用中的高级管理人员）", _i18n.current_lang()), "danger")
                    return redirect(url_for("users_page"))
        if department and not conn.execute(
                "SELECT id FROM departments WHERE name=?", (department,)).fetchone():
            department = ""
        if new_pwd:
            if len(new_pwd) < 6:
                flash(_i18n.t("新密码至少 6 位", _i18n.current_lang()), "danger")
                return redirect(url_for("users_page"))
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_pwd), uid))
        conn.execute(
            "UPDATE users SET display_name=?, role=?, department=?, is_supervisor=?, active=?, email=?, enterprise_id=? WHERE id=?",
            (name, role, department, is_sup, active,
             f.get("email", "").strip(), f.get("enterprise_id", "").strip(), uid),
        )
        conn.commit()
        flash(_i18n.t("用户信息已更新", _i18n.current_lang()), "success")
    finally:
        conn.close()
    return redirect(url_for("users_page"))


@app.route("/users/<int:uid>/reset", methods=["POST"])
@login_required
@perm_required("user_manage", "reset_pwd")
def user_reset(uid):
    pwd = request.form.get("password", "")
    if len(pwd) < 6:
        flash(_i18n.t("新密码至少 6 位", _i18n.current_lang()), "danger")
        return redirect(url_for("users_page"))
    conn = get_db()
    try:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pwd), uid))
        conn.commit()
        flash(_i18n.t("密码已重置", _i18n.current_lang()), "success")
    finally:
        conn.close()
    return redirect(url_for("users_page"))


@app.route("/users/<int:uid>/delete", methods=["POST"])
@login_required
@perm_required("user_manage", "delete")
def user_delete(uid):
    """删除用户 → 进入回收站（管理员可恢复）；不能删除自己，系统至少保留一名启用的 admin"""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            flash(_i18n.t("用户不存在", _i18n.current_lang()), "danger")
            return redirect(url_for("users_page"))
        if uid == current_user()["id"]:
            flash(_i18n.t("不能删除自己", _i18n.current_lang()), "danger")
            return redirect(url_for("users_page"))
        if row["role"] == "admin":
            admins = conn.execute(
                "SELECT COUNT(*) c FROM users WHERE role='admin' AND active=1 AND id<>?", (uid,)
            ).fetchone()["c"]
            if admins < 1:
                flash(_i18n.t("系统至少保留一名启用中的高级管理人员，不能删除该用户", _i18n.current_lang()), "danger")
                return redirect(url_for("users_page"))
        _recycle_add(conn, "user", uid, row)
        conn.execute("DELETE FROM users WHERE id=?", (uid,))
        conn.commit()
        flash(_xl("用户 [{p1}] 已删除并进入回收站（有权限的用户可恢复）", p1=row['display_name']), "success")
    finally:
        conn.close()
    return redirect(url_for("users_page"))


# ------------------------------------------------------------------
# 部门配置（高级管理人员）：维护部门列表，用户新增/编辑时下拉选择
# ------------------------------------------------------------------
@app.route("/departments")
@login_required
@perm_required("departments")
def departments_page():
    conn = get_db()
    try:
        deps = conn.execute(
            """SELECT d.*,
                      COALESCE(s.display_name,'') supervisor_name,
                      COALESCE(m.display_name,'') manager_name,
                      (SELECT COUNT(*) FROM users u WHERE u.department=d.name) user_count
               FROM departments d
               LEFT JOIN users s ON s.id=d.supervisor_id
               LEFT JOIN users m ON m.id=d.manager_id
               ORDER BY d.id"""
        ).fetchall()
        users = conn.execute(
            """SELECT id, display_name, username, role, department FROM users
               WHERE active=1 AND role IN ('admin','manager') ORDER BY department, display_name, username"""
        ).fetchall()
    finally:
        conn.close()
    users = [dict(u) for u in users]
    return render_template("departments.html", deps=deps, users=users)


@app.route("/departments/add", methods=["POST"])
@login_required
@perm_required("departments")
def department_add():
    name = request.form.get("name", "").strip()
    remark = request.form.get("remark", "").strip()
    if not name:
        flash(_i18n.t("部门名称不能为空", _i18n.current_lang()), "danger")
        return redirect(url_for("departments_page"))
    conn = get_db()
    try:
        if conn.execute("SELECT id FROM departments WHERE name=?", (name,)).fetchone():
            flash(_i18n.t("部门 [{0}] 已存在", _i18n.current_lang()).format(name), "danger")
            return redirect(url_for("departments_page"))
        conn.execute(
            "INSERT INTO departments(name, remark, supervisor_id, manager_id, created_at) VALUES(?,?,?,?,?)",
            (name, remark, _form_opt_int("supervisor_id"), _form_opt_int("manager_id"), db.now_str()),
        )
        conn.commit()
        flash(_i18n.t("部门 [{0}] 添加成功", _i18n.current_lang()).format(name), "success")
    finally:
        conn.close()
    return redirect(url_for("departments_page"))


@app.route("/departments/<int:did>/edit", methods=["POST"])
@login_required
@perm_required("departments")
def department_edit(did):
    name = request.form.get("name", "").strip()
    remark = request.form.get("remark", "").strip()
    if not name:
        flash(_i18n.t("部门名称不能为空", _i18n.current_lang()), "danger")
        return redirect(url_for("departments_page"))
    conn = get_db()
    try:
        old = conn.execute("SELECT * FROM departments WHERE id=?", (did,)).fetchone()
        if not old:
            flash(_i18n.t("部门不存在", _i18n.current_lang()), "danger")
            return redirect(url_for("departments_page"))
        if name != old["name"]:
            if conn.execute("SELECT id FROM departments WHERE name=?", (name,)).fetchone():
                flash(_i18n.t("部门 [{0}] 已存在", _i18n.current_lang()).format(name), "danger")
                return redirect(url_for("departments_page"))
            # 同步更新用户表中的旧部门名
            conn.execute("UPDATE users SET department=? WHERE department=?", (name, old["name"]))
        # 校验审批人角色：主管/经理必须是 部门管理(manager) 或 高级管理人员(admin)
        sup_id = _form_opt_int("supervisor_id")
        mgr_id = _form_opt_int("manager_id")
        for label, uid in [("一级审批人（主管）", sup_id), ("二级审批人（经理）", mgr_id)]:
            if uid:
                u = conn.execute("SELECT role, display_name FROM users WHERE id=? AND active=1", (uid,)).fetchone()
                if not u:
                    flash(_i18n.t("{0}用户不存在或已停用", _i18n.current_lang()).format(label), "danger")
                    return redirect(url_for("departments_page"))
                if u["role"] not in ("admin", "manager"):
                    flash(_i18n.t("{0} [{1}] 不是部门管理或高级管理人员，不能选为审批人", _i18n.current_lang()).format(label, u["display_name"]), "danger")
                    return redirect(url_for("departments_page"))
        conn.execute(
            "UPDATE departments SET name=?, remark=?, supervisor_id=?, manager_id=? WHERE id=?",
            (name, remark, sup_id, mgr_id, did),
        )
        conn.commit()
        flash(_i18n.t("部门已更新", _i18n.current_lang()), "success")
    finally:
        conn.close()
    return redirect(url_for("departments_page"))


@app.route("/departments/<int:did>/delete", methods=["POST"])
@login_required
@perm_required("departments")
def department_delete(did):
    conn = get_db()
    try:
        dep = conn.execute("SELECT * FROM departments WHERE id=?", (did,)).fetchone()
        if not dep:
            flash(_i18n.t("部门不存在", _i18n.current_lang()), "danger")
            return redirect(url_for("departments_page"))
        cnt = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE department=?", (dep["name"],)
        ).fetchone()["c"]
        if cnt:
            flash(_i18n.t("该部门下还有 {0} 名用户，请先在用户管理中调整其部门后再删除", _i18n.current_lang()).format(cnt), "danger")
            return redirect(url_for("departments_page"))
        _recycle_add(conn, "department", did, dep)
        conn.execute("DELETE FROM departments WHERE id=?", (did,))
        conn.commit()
        flash(_xl("部门 [{p1}] 已删除并进入回收站（有权限的用户可恢复）", p1=dep['name']), "success")
    finally:
        conn.close()
    return redirect(url_for("departments_page"))


# ------------------------------------------------------------------
# 产线与设备配置（产线、设备名称维护；物料建档时可绑定适用产线/设备）
# ------------------------------------------------------------------
@app.route("/lines", methods=["GET", "POST"])
@login_required
@perm_required("lines_equip")
def lines_page():
    conn = get_db()
    try:
        lines = db.list_lines(conn)
        equip = db.list_equipment(conn)
        # 设备挂产线名称、产线设备数
        line_map = {l["id"]: l["name"] for l in lines}
        eq_cnt = {}
        for e in equip:
            e["line_name"] = line_map.get(e["line_id"]) or _i18n.t("未分配")
            if e["line_id"]:
                eq_cnt[e["line_id"]] = eq_cnt.get(e["line_id"], 0) + 1
        for l in lines:
            l["equip_count"] = eq_cnt.get(l["id"], 0)
    finally:
        conn.close()
    return render_template("lines.html", lines=lines, equip=equip)


@app.route("/lines/add", methods=["POST"])
@login_required
@perm_required("lines_equip")
def line_add():
    name = request.form.get("name", "").strip()
    remark = request.form.get("remark", "").strip()
    if not name:
        flash(_i18n.t("产线名称不能为空", _i18n.current_lang()), "danger")
        return redirect(url_for("lines_page"))
    conn = get_db()
    try:
        if conn.execute("SELECT id FROM production_lines WHERE name=?", (name,)).fetchone():
            flash(_i18n.t("产线 [{0}] 已存在", _i18n.current_lang()).format(name), "danger")
            return redirect(url_for("lines_page"))
        conn.execute(
            "INSERT INTO production_lines(name, remark, created_at) VALUES(?,?,?)",
            (name, remark, db.now_str()),
        )
        conn.commit()
        flash(_i18n.t("产线 [{0}] 添加成功", _i18n.current_lang()).format(name), "success")
    finally:
        conn.close()
    return redirect(url_for("lines_page"))


@app.route("/lines/<int:lid>/delete", methods=["POST"])
@login_required
@perm_required("lines_equip")
def line_delete(lid):
    conn = get_db()
    try:
        ln = conn.execute("SELECT * FROM production_lines WHERE id=?", (lid,)).fetchone()
        if not ln:
            flash(_i18n.t("产线不存在", _i18n.current_lang()), "danger")
            return redirect(url_for("lines_page"))
        equip_cnt = conn.execute(
            "SELECT COUNT(*) c FROM equipment WHERE line_id=?", (lid,)
        ).fetchone()["c"]
        app_cnt = conn.execute(
            "SELECT COUNT(*) c FROM part_applications WHERE line_id=?", (lid,)
        ).fetchone()["c"]
        if equip_cnt or app_cnt:
            flash(_xl("产线 [{p1}] 下还有 {p2} 台设备、{p3} 条物料适用记录，请先清理", p1=ln['name'], p2=equip_cnt, p3=app_cnt), "danger")
            return redirect(url_for("lines_page"))
        conn.execute("DELETE FROM production_lines WHERE id=?", (lid,))
        conn.commit()
        flash(_xl("产线 [{p1}] 已删除", p1=ln['name']), "success")
    finally:
        conn.close()
    return redirect(url_for("lines_page"))


@app.route("/equipment/add", methods=["POST"])
@login_required
@perm_required("lines_equip")
def equipment_add():
    name = request.form.get("name", "").strip()
    model = request.form.get("model", "").strip()
    line_id = request.form.get("line_id", "").strip()
    remark = request.form.get("remark", "").strip()
    if not name:
        flash(_i18n.t("设备名称不能为空", _i18n.current_lang()), "danger")
        return redirect(url_for("lines_page"))
    try:
        line_id = int(line_id) if line_id else None
    except (TypeError, ValueError):
        line_id = None
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO equipment(line_id, name, model, remark, created_at) VALUES(?,?,?,?,?)",
            (line_id, name, model, remark, db.now_str()),
        )
        conn.commit()
        flash(_i18n.t("设备 [{0}] 添加成功", _i18n.current_lang()).format(name), "success")
    finally:
        conn.close()
    return redirect(url_for("lines_page"))


@app.route("/equipment/<int:eid>/delete", methods=["POST"])
@login_required
@perm_required("lines_equip")
def equipment_delete(eid):
    conn = get_db()
    try:
        eq = conn.execute("SELECT * FROM equipment WHERE id=?", (eid,)).fetchone()
        if not eq:
            flash(_i18n.t("设备不存在", _i18n.current_lang()), "danger")
            return redirect(url_for("lines_page"))
        app_cnt = conn.execute(
            "SELECT COUNT(*) c FROM part_applications WHERE equipment_id=?", (eid,)
        ).fetchone()["c"]
        if app_cnt:
            flash(_xl("设备 [{p1}] 正被 {p2} 条物料适用记录引用，请先在信息维护中解除", p1=eq['name'], p2=app_cnt), "danger")
            return redirect(url_for("lines_page"))
        conn.execute("DELETE FROM equipment WHERE id=?", (eid,))
        conn.commit()
        flash(_xl("设备 [{p1}] 已删除", p1=eq['name']), "success")
    finally:
        conn.close()
    return redirect(url_for("lines_page"))


@app.route("/api/lines")
@login_required
def api_lines():
    conn = get_db()
    try:
        return jsonify(db.list_lines(conn))
    finally:
        conn.close()


@app.route("/api/equipment")
@login_required
def api_equipment():
    line_id = request.args.get("line_id", "").strip()
    try:
        line_id = int(line_id) if line_id else None
    except (TypeError, ValueError):
        line_id = None
    conn = get_db()
    try:
        return jsonify(db.list_equipment(conn, line_id))
    finally:
        conn.close()


# ------------------------------------------------------------------
# 权限配置（高级管理人员）
# ------------------------------------------------------------------
@app.route("/permissions", methods=["GET", "POST"])
@login_required
@perm_required("perm_config")
def permissions_page():
    conn = get_db()
    try:
        if request.method == "POST":
            for role in config.ROLE_NAMES:
                for m in config.PERM_MODULES:
                    allow = 1 if request.form.get(f"perm_{role}_{m}") else 0
                    conn.execute(
                        "INSERT INTO permissions(role,module,action,allow) VALUES(?,?,?,?) "
                        "ON CONFLICT(role,module,action) DO UPDATE SET allow=excluded.allow",
                        (role, m, "", allow),
                    )
                for m, actions in config.PERM_ACTIONS.items():
                    for a in actions:
                        st = request.form.get(f"perm_action_state_{role}_{m}_{a}", "follow")
                        if st == "1":
                            conn.execute(
                                "INSERT INTO permissions(role,module,action,allow) VALUES(?,?,?,?) "
                                "ON CONFLICT(role,module,action) DO UPDATE SET allow=excluded.allow",
                                (role, m, a, 1),
                            )
                        elif st == "0":
                            conn.execute(
                                "INSERT INTO permissions(role,module,action,allow) VALUES(?,?,?,?) "
                                "ON CONFLICT(role,module,action) DO UPDATE SET allow=excluded.allow",
                                (role, m, a, 0),
                            )
                        else:
                            # follow：删除操作级行，恢复"跟随模块级"语义
                            conn.execute(
                                "DELETE FROM permissions WHERE role=? AND module=? AND action=?",
                                (role, m, a),
                            )
            conn.commit()
            flash(_i18n.t("权限配置已保存（立即生效）", _i18n.current_lang()), "success")
        perms = conn.execute("SELECT * FROM permissions ORDER BY role, module, action").fetchall()
        matrix = {}
        amatrix = {}
        for p in perms:
            if p["action"]:
                amatrix[(p["role"], p["module"], p["action"])] = p["allow"]
            else:
                matrix[(p["role"], p["module"])] = p["allow"]
        # 老库没配置过的模块（例如后来新增的「大屏设置」）按默认矩阵显示勾选状态。
        # 不补的话页面显示未勾选，管理员一保存就把默认开放的权限全关掉了。
        for _role in config.ROLE_NAMES:
            for _m in config.PERM_MODULES:
                if (_role, _m) not in matrix:
                    matrix[(_role, _m)] = 1 if _m in config.DEFAULT_PERMS.get(_role, []) else 0
    finally:
        conn.close()
    _lang = _i18n.current_lang()
    return render_template("permissions.html", matrix=matrix, amatrix=amatrix,
                           roles=list(config.ROLE_NAMES.keys()),
                           PERM_MODULES={k: _i18n.t(v, _lang) for k, v in config.PERM_MODULES.items()},
                           perm_actions={m: {a: _i18n.t(al, _lang) for a, al in acts.items()} for m, acts in config.PERM_ACTIONS.items()})


# ------------------------------------------------------------------
# 系统初始化向导（首次运行配置数据库位置）与系统设置
# ------------------------------------------------------------------
def db_configured():
    """数据库是否已配置：SQLite 看路径，其它数据库类型看 db_type"""
    return bool(config.DB_PATH) or config.DB_TYPE != "sqlite"


@app.before_request
def check_db_configured():
    """数据库未配置时，只允许访问初始化向导"""
    if db_configured():
        return None
    allow = ("/setup", "/login", "/static")
    if request.path.startswith(allow):
        return None
    return redirect(url_for("setup"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if db_configured():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        db_path = request.form.get("db_path", "").strip()
        demo = 1 if request.form.get("demo_data") else 0
        if not db_path:
            flash(_i18n.t("请填写数据库文件路径", _i18n.current_lang()), "danger")
            return render_template("setup.html", default_db=config.DEFAULT_DB_PATH)
        # 检查路径可用性：所在目录必须可创建
        db_dir = os.path.dirname(db_path)
        if not db_dir:
            flash(_i18n.t("数据库路径需包含目录，例如 \\\\192.168.1.100\\公共盘\\库存管理系统\\SPMS.db", _i18n.current_lang()), "danger")
            return render_template("setup.html", default_db=config.DEFAULT_DB_PATH)
        try:
            os.makedirs(db_dir, exist_ok=True)
            probe = os.path.join(db_dir, ".smt_write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
        except OSError as e:
            flash(_i18n.t("无法写入该位置（请确认目录存在且有权访问）：{0}", _i18n.current_lang()).format(e), "danger")
            return render_template("setup.html", default_db=config.DEFAULT_DB_PATH)
        # v1.0.77：在已有 settings.json 的基础上只改这三项。以前只带 port 过去，
        # settings.save() 会用 DEFAULTS 重建整份配置，把语言/审批/AD/通知等悄悄清掉。
        cfg = _settings.load()
        cfg.update({"db_path": db_path, "demo_data": demo})
        save_path = _settings.save(cfg)
        config.apply_settings(cfg)
        db.init_db(demo=bool(demo))
        flash(_i18n.t("初始化完成，数据库位置：{0}（配置已保存到 {1}）", _i18n.current_lang()).format(db_path, save_path), "success")
        return redirect(url_for("dashboard"))
    return render_template("setup.html", default_db=config.DEFAULT_DB_PATH)


@app.route("/api/browse_dir")
@login_required
@role_required("admin", "warehouse")
def browse_dir():
    """备份目录选择窗体：浏览服务端可访问的目录（本机/内网共享盘）。

    v1.0.77：恢复页的备份卡片对仓库管理人员也可见，目录浏览权限跟着对齐，
    否则管理员能点、仓库管理人员点开却 403。
    """
    import os
    path = request.args.get("path", "").strip().strip('"')
    if not path:
        path = os.path.abspath(os.sep)
    if not os.path.isdir(path):
        return jsonify({"ok": False, "msg": _xl("目录不存在或不可访问：{path}", path=path)})
    try:
        entries = sorted(os.listdir(path), key=str.lower)
    except OSError as e:
        return jsonify({"ok": False, "msg": _xl("无法读取该目录：{e}", e=e)})
    dirs = [n for n in entries if os.path.isdir(os.path.join(path, n)) and not n.startswith("$")]
    parent = os.path.dirname(path)
    if parent == path:
        parent = ""
    return jsonify({"ok": True, "cur": path, "parent": parent, "dirs": dirs[:500]})


@app.route("/settings/backups")
@login_required
@role_required("admin")
def settings_backups():
    """备份文件列表片段（AJAX 局部刷新用）"""
    return render_template("_backups.html", backups=_list_backups())


def _is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


@app.after_request
def _drop_ajax_flashes(resp):
    """AJAX 请求把提示直接回在 JSON 里，没人去渲染模板消费 flash。

    留一份在 session 里的后果不是「不显示」，而是**攒着**：等用户下一次打开
    任意页面（系统设置、物料管理…）才一次性冒出来，看着像「切页面又弹一次
    刚才保存的提示」，连点几次就是「所有弹窗又都回来了」。
    这里做最后一道兜底：JSON 响应就当提示已经送达，session 里那份丢掉。
    """
    if resp.mimetype == "application/json" and _is_ajax():
        try:
            session.pop("_flashes", None)
        except Exception:
            pass
    return resp


def _settings_reply(msg, kind="success", anchor=None, endpoint=None, **extra):
    """系统设置页各保存操作的统一返回：AJAX→JSON（页面不刷新），普通→flash+跳转锚点

    endpoint：回跳的目标页。备份卡片现在挂在「恢复」页（/restore），
    校验失败/无 JS 时必须是回 /restore 而不是回 /settings —— 否则用户
    点一次「立即备份」就被甩到系统设置页，还以为备份没做成。

    文案统一在这里过一遍取词：这个函数的 msg 走的是 JSON（AJAX 时前端直接
    贴到提示条上，不过 tr()），不在这里翻就一定是中文。已经翻过的再过一遍
    是无害的（英文串在词条表里查不到，原样返回）。
    """
    msg = _xl(msg)
    if _is_ajax():
        data = {"ok": kind == "success", "msg": msg}
        data.update(extra)
        return jsonify(data)
    flash(msg, kind)
    target = endpoint or "system_settings"
    if anchor:
        return redirect(url_for(target, _anchor=anchor))
    return redirect(url_for(target))


def _screen_reply(msg, kind="success"):
    """大屏配置页保存的统一返回：AJAX→JSON，普通→flash+跳转"""
    msg = _xl(msg)
    if _is_ajax():
        return jsonify({"ok": kind == "success", "msg": msg})
    flash(msg, kind)
    return redirect(url_for("screen_config", _anchor="card-screen"))


def _save_repair_categories():
    """v1.0.19：保存维修品类型二级字典 [{group, items}]。"""
    raw = (request.form.get("repair_categories", "[]") or "[]").strip()
    try:
        data = json.loads(raw) if raw else []
    except Exception:
        data = []
    groups = _normalize_repair_categories(data)
    _before = 0
    for _x in (data if isinstance(data, list) else []):
        if isinstance(_x, dict):
            _it = _x.get("items") or []
            _before += len(_it) if isinstance(_it, list) else 0
        elif str(_x).strip():
            _before += 1
    # 同名一级类合并，避免配置区重复建组把下拉撑乱
    merged = []
    for g in groups:
        hit = next((m for m in merged if m["group"] == g["group"]), None)
        if hit:
            for it in g["items"]:
                if it not in hit["items"]:
                    hit["items"].append(it)
        else:
            merged.append({"group": g["group"], "items": list(g["items"])})
    # 合并可能把重复词条跨组补回来，再归一化一次收口（上限 / 去重 / 长度）
    merged = _normalize_repair_categories(merged)
    n = sum(len(g["items"]) for g in merged)
    _cut = max(0, _before - n)
    conn = get_db()
    try:
        db.set_setting(conn, "repair_categories", json.dumps(merged, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    if _cut:
        return _settings_reply(
            _xl("维修品类型已保存（共 {n} 项，超出上限或重复的 {c} 项已忽略）", n=n, c=_cut),
            "success", "card-ef")
    return _settings_reply(_xl("维修品类型已保存（共 {n} 项）", n=n), "success", "card-ef")


@app.route("/settings", methods=["GET", "POST"])
@login_required
@role_required("admin")
def system_settings():
    if request.method == "POST":
        # 数据库类型配置表单（保存 / 测试连接 / 保存并初始化）
        if request.form.get("db_save") is not None:
            return _save_database()
        if request.form.get("db_test"):
            return _test_database()
        # 数据迁移：导出/导入
        if request.path == "/settings" and request.method == "POST" and request.args.get("action") == "export_data":
            return _export_all_data()
        if request.path == "/settings" and request.method == "POST" and request.args.get("action") == "import_data":
            return _import_all_data()
        # 界面主题与外观表单（theme + nav_style + 品牌 LOGO/文字 一次保存）
        if request.form.get("appearance_save") is not None or request.form.get("appearance_reset"):
            return _save_appearance()
        # 品牌信息表单（系统名称 / 副标题 / LOGO / 样式）单独提交，不切换数据库
        if request.form.get("brand_title") is not None or request.files.get("brand_logo"):
            return _save_brand()
        # 表头设置表单（自定义列名称 + 各列显示字符数，一次保存）
        if request.form.get("head_save"):
            return _save_head()
        # 物料分类字典
        if request.form.get("category_save"):
            return _save_part_categories()
        # 呆滞管理设置表单
        if request.form.get("stale_save"):
            return _save_stale()
        # 待出库超时配置表单
        if request.form.get("pending_save"):
            return _save_pending_cfg()
        # 数据库自动备份表单（保存 / 立即备份）
        if request.form.get("backup_save"):
            return _save_backup()
        if request.form.get("backup_now"):
            return _backup_now()
        # 上传图片自动压缩阈值（随数据库保存）
        if request.form.get("upload_cfg_save"):
            try:
                kb = max(0, min(_int(request.form.get("upload_compress_kb", "400") or "0", 400), 10240))
            except (TypeError, ValueError):
                kb = 400
            conn = get_db()
            try:
                db.set_setting(conn, "upload_cfg", json.dumps({"compress_kb": kb}, ensure_ascii=False))
                conn.commit()
            finally:
                conn.close()
            return _settings_reply(_xl("上传图片自动压缩已保存（大于 {kb}KB 的图片上传时自动压缩）", kb=kb), "success", "card-db")
        # 界面语言表单
        if request.form.get("language_save"):
            return _save_language()
        if request.form.get("stocktake_save"):
            return _save_stocktake_cfg()
        # 自动上下限配置表单
        if request.form.get("auto_stock_save"):
            return _save_auto_stock()
        # 消息通知配置表单
        if request.form.get("notify_save"):
            return _save_notify_cfg()
        # 对外数据接口表单（外系统查询 / 入库操作）
        if request.form.get("open_api_save"):
            return _save_open_api()
        # AD 域登录配置表单
        if request.form.get("ad_save"):
            return _save_ad()
        # 局域网部署表单（保存并重启）
        if request.form.get("deploy_save"):
            return _save_deploy()
        # 配件分类（维修品类型）表单
        if request.form.get("repair_save"):
            return _save_repair_categories()
        # 删除备份文件
        if request.form.get("backup_delete"):
            return _backup_delete()
        db_path = request.form.get("db_path", "").strip()
        demo = 1 if request.form.get("demo_data") else 0
        if not db_path:
            return _settings_reply("数据库路径不能为空", "danger", "card-db")
        db_dir = os.path.dirname(db_path)
        try:
            os.makedirs(db_dir, exist_ok=True)
            probe = os.path.join(db_dir, ".smt_write_test")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
        except OSError as e:
            return _settings_reply(_xl("无法写入该位置：{e}", e=e), "danger", "card-db")
        # v1.0.77：切换数据库同样只改这三项，保留管理员已经配好的其它设置。
        cfg = _settings.load()
        cfg.update({"db_path": db_path, "demo_data": demo})
        save_path = _settings.save(cfg)
        config.apply_settings(cfg)
        try:
            db.init_db(demo=bool(demo))
            return _settings_reply(_xl("数据库位置已切换并初始化完成：{db_path}", db_path=db_path), "success", "card-db", db_path=db_path)
        except Exception as e:
            return _settings_reply(_xl("切换失败：{e}", e=e), "danger", "card-db")
    st = _settings_cfg()
    conn2 = get_db()
    try:
        sd = _stale_days(conn2)
        sd_borrow = _stale_ignore_borrowed(conn2)
        part_categories = get_part_categories(conn2)
        head_clip = get_head_clip(conn2)
        cat_max = category_max_len(conn2)   # 与标准表头「分类」同源，页面文案与限制都用这一个数
    finally:
        conn2.close()
    return render_template("settings.html", settings=_settings_cfg(), db_path=config.DB_PATH or "",
                           default_db=config.DEFAULT_DB_PATH,
                           # 添加语言下拉：ISO 639-1 全量表（数据非文案，经 data 属性下发）
                           iso_presets=json.dumps(_i18n.iso_lang_list(), ensure_ascii=False),
                           demo_enabled=bool(st.get("demo_data")),
                           # 展示用本地化，编辑框用原值：brand 走 context_processor 之外
                           # 单独传，以前传的是 get_brand()（未本地化），把全局已翻译的
                           # brand 覆盖成中文，英文界面侧边栏就一直是「库存管理系统」。
                           brand=_brand_localized(get_brand()), brand_raw=get_brand(),
                           theme=get_theme(),
                           nav_style=get_nav_style(),
                           extra_fields=_extra_fields_with_clip(head_clip),
                           part_categories=part_categories,
                           head_clip=head_clip,
                           std_text_cols=config.STD_TEXT_COLS,
                           max_extra=config.MAX_EXTRA_FIELDS,
                           head_min=config.HEAD_CLIP_MIN, head_max=config.HEAD_CLIP_MAX,
                           head_default=config.DEFAULT_HEAD_CLIP,
                           extra_types=extra_type_options(),
                           cat_max=cat_max,
                           max_categories=config.MAX_CATEGORIES,
                           cat_hint=_xl("分类名的字符数与标准表头「分类」那一列的设置保持一致；最多 {m} 个分类",
                                        m=config.MAX_CATEGORIES),
                           auto_stock=_auto_stock_cfg(c if 'c' in dir() else get_db()),
                           stale_days=sd,
                           stale_ignore_borrowed=sd_borrow,
                           backup=get_backup(),
                           backups=_list_backups(),
                           upload_compress_kb=_upload_compress_kb(),
                           ad_cfg=_ad_cfg(),
                           login_case_sensitive=_login_case_sensitive(),
                           lan_enabled=bool(config.LAN_ENABLED),
                           lan_port=config.PORT,
                           lan_ip=_local_ip(),
                           cur_lang=_i18n.current_lang(),
                           LANGUAGES=_i18n.LANGUAGES,
                           open_api=get_open_api(),
                           db_cfg=config.get_db_cfg(),
                           DB_DIALECTS=("sqlite", "mysql", "mariadb", "postgresql", "mssql", "oracle"))


def _export_all_data():
    """导出当前数据库全部表数据为JSON文件"""
    conn = get_db()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
        data = {"version": "1.0", "tables": {}}
        for t in tables:
            rows = conn.execute(f"SELECT * FROM {t}").fetchall()
            data["tables"][t] = [dict(r) for r in rows]
        from flask import Response
        import io
        buf = io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8"))
        return Response(
            buf.getvalue(),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=spms_data_export.json"})
    finally:
        conn.close()


def _import_all_data():
    """从JSON文件导入数据到当前数据库（v1.0.77：白名单 + 逐行报错 + 全量回滚）

    历史实现把上传 JSON 里的表名/列名直接拼进 SQL，且逐行异常被 pass 吞掉，
    结果「导入完成」可能一条都没写进去，甚至能覆盖 users / permissions /
    sys_settings。现在表名必须存在于当前库结构，列名必须属于该表；任何一行
    出错都整体回滚并给出首条错误，绝不留半套数据还报成功。
    """
    f = request.files.get("file")
    if not f:
        return _settings_reply("请选择要导入的JSON文件", "danger", "card-db")
    try:
        data = json.loads(f.read().decode("utf-8"))
    except Exception as e:
        return _settings_reply(_xl("JSON文件解析失败：{e}", e=e), "danger", "card-db")
    if not isinstance(data, dict):
        return _settings_reply(_xl("JSON文件顶层必须是对象"), "danger", "card-db")
    tables = data.get("tables")
    if not isinstance(tables, dict) or not tables:
        return _settings_reply(_xl("文件中没有可导入的数据表"), "danger", "card-db")

    def q(name):
        return '"%s"' % str(name).replace('"', '""')

    conn = get_db()
    imported = 0
    errs = []
    try:
        allowed = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        for t, rows in tables.items():
            if t not in allowed:
                errs.append(_xl("表 {t} 不在当前数据库结构中，已跳过", t=str(t)[:60]))
                continue
            if not isinstance(rows, list):
                errs.append(_xl("表 {t} 的数据不是列表，已跳过", t=t))
                continue
            db_cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % q(t))}
            for idx, row in enumerate(rows, 1):
                if not isinstance(row, dict):
                    errs.append(_xl("表 {t} 第 {n} 行不是对象，已跳过", t=t, n=idx))
                    continue
                unknown = [str(c)[:40] for c in row if c not in db_cols]
                if unknown:
                    errs.append(_xl("表 {t} 第 {n} 行含未知字段 {cols}，已跳过",
                                    t=t, n=idx, cols=",".join(unknown[:5])))
                    continue
                keys = [c for c in row if c in db_cols]
                if not keys:
                    errs.append(_xl("表 {t} 第 {n} 行为空，已跳过", t=t, n=idx))
                    continue
                placeholders = ",".join(["?"] * len(keys))
                try:
                    conn.execute("INSERT OR REPLACE INTO %s (%s) VALUES (%s)" % (
                        q(t), ",".join(q(c) for c in keys), placeholders),
                        [row[c] for c in keys])
                    imported += 1
                except Exception as e:
                    errs.append(_xl("表 {t} 第 {n} 行写入失败：{e}", t=t, n=idx, e=str(e)[:120]))
            if len(errs) >= 20:
                errs.append(_xl("错误过多，已停止导入；请修正文件后重试"))
                break
        if errs:
            conn.rollback()
            return _settings_reply(
                _xl("导入失败，共 {n} 个问题，原数据库未改动。首条：{msg}",
                    n=len(errs), msg=errs[0]), "danger", "card-db")
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "msg": _xl("导入完成，共 {n} 条记录", n=imported)})


def _save_database():
    """保存数据库类型与连接参数到 settings.json（保留其他配置）。
    - SQLite：仅数据库文件路径
    - 其它类型：host/port/user/password/database/service_name
    - 保存后重启服务生效；提供「保存并初始化」在当前进程内立即建表迁移"""
    st = _settings.load()
    _db_type = (request.form.get("db_type", "sqlite") or "sqlite").strip().lower()
    if _db_type not in ("sqlite", "mysql", "mariadb", "postgresql", "mssql", "oracle"):
        return _settings_reply("不支持的数据库类型", "danger", "card-db")
    cfg = dict(st)
    cfg["db_type"] = _db_type
    cfg["db_path"] = (request.form.get("db_path", "") or "").strip()
    cfg["db_host"] = (request.form.get("db_host", "") or "").strip()
    try:
        cfg["db_port"] = _int(request.form.get("db_port", "0") or "0")
    except (TypeError, ValueError):
        cfg["db_port"] = 0
    cfg["db_user"] = (request.form.get("db_user", "") or "").strip()
    cfg["db_password"] = request.form.get("db_password", "") or ""
    cfg["db_database"] = (request.form.get("db_database", "") or "").strip()
    cfg["db_service_name"] = (request.form.get("db_service_name", "") or "").strip()
    if _db_type == "sqlite" and not cfg["db_path"]:
        return _settings_reply("SQLite 数据库文件路径不能为空", "danger", "card-db")
    # 连接校验
    import db as _db
    ok, msg = _db.test_connect(cfg)
    if not ok:
        return _settings_reply(_xl("连接测试失败：{msg}", msg=msg), "danger", "card-db")
    try:
        save_path = _settings.save(cfg)
    except OSError as e:
        return _settings_reply(_xl("配置保存失败：{e}", e=e), "danger", "card-db")
    config.apply_settings(cfg)
    note = _xl("已保存。重启软件后生效") if _db_type != "sqlite" else _xl("已保存并立即生效")
    if request.form.get("db_init"):
        try:
            db.init_db()
            note = _xl("已保存，新库已初始化（建表+默认管理员）")
        except Exception as e:
            return _settings_reply(_xl("配置已保存，但新库初始化失败：{e}（可重启软件后重试）", e=e), "danger", "card-db")
    return _settings_reply(
        _xl("数据库配置已保存（{msg}）。{note}；当前数据库类型：{db_type}",
            msg=msg, note=note, db_type=_db_type.upper()),
        "success", "card-db", db_path=cfg["db_path"] or config.DB_PATH or "")


def _test_database():
    """AJAX 测试数据库连接（不保存）"""
    import db as _db
    _db_type = (request.form.get("db_type", "sqlite") or "sqlite").strip().lower()
    try:
        _port = _int(request.form.get("db_port", "0") or "0")
    except (TypeError, ValueError):
        _port = 0
    cfg = {
        "db_type": _db_type,
        "db_path": (request.form.get("db_path", "") or "").strip(),
        "host": (request.form.get("db_host", "") or "").strip(),
        "port": _port,
        "user": (request.form.get("db_user", "") or "").strip(),
        "password": request.form.get("db_password", "") or "",
        "database": (request.form.get("db_database", "") or "").strip(),
        "service_name": (request.form.get("db_service_name", "") or "").strip(),
    }
    if _db_type == "sqlite" and not cfg["db_path"]:
        return jsonify({"ok": False, "msg": _xl("请填写数据库文件路径")})
    ok, msg = _db.test_connect(cfg)
    return jsonify({"ok": ok, "msg": _xl(msg)})


def _save_language():
    """保存界面语言：写系统默认 + 立刻切换当前会话（三处同写，下次请求才不回退）"""
    st = _settings.load()
    lang = request.form.get("language", "zh").strip()
    if lang not in _i18n.LANGUAGES:
        return _settings_reply("所选语言无效", "danger", "card-language")
    st["language"] = lang
    try:
        _settings.save(st)
    except OSError as e:
        return _settings_reply(_xl("配置保存失败：{e}", e=e), "danger", "card-language")
    # 只写 settings.json 是不够的：它是优先级最低的一层，
    # 会话/账号档案里有旧值时会把新语言盖回去（表现为「保存了没反应」）。
    _i18n.set_lang(lang)
    return _settings_reply(_xl("界面语言已切换为 {lang}", lang=_i18n.LANGUAGES.get(lang, lang)), "success", "card-language")


# ======================================================================
# 语言管理：增删语言 / 翻译引擎 / 补翻译 / 漏翻扫描
#
# 为什么要把原来那个「界面语言」下拉换掉：
#   顶栏每个页面都有切换器，设置页再放一个只是重复。设置页真正该管的是
#   「这个系统提供哪些语言」，也就是注册表本身 —— 以前这件事只能在命令行
#   跑 tools/newlang.py，现场管理员够不着。
#
# 四件事的分工：
#   语言增减  写 locale/languages.json + 生成骨架 catalog，靠 mtime 立即生效
#   翻译引擎  AI / Google / DeepL 三选一，配置存 settings.json
#   补翻译    后台线程分批跑，边翻边落盘，前端轮询进度
#   漏翻扫描  扫源码找「界面上不会翻」的地方，分三类给出修法
# ======================================================================
LOCALE_DIR = os.path.join(_settings.APP_DIR, "locale")


def _lang_mod():
    """延迟导入 i18n2 子模块。

    放在函数里而不是文件头：app.py 的导入链很长，i18n2.manage 会反查
    catalog 目录，一旦某个客户的语言包损坏，不该拖垮整个程序启动。
    """
    from i18n2 import manage, scan, translate
    return manage, scan, translate


def _api_lang(fn):
    """统一错误出口：前端只认 {ok, msg}，不需要看 traceback。"""
    @wraps(fn)
    def wrapper(*a, **kw):
        try:
            return jsonify(fn(*a, **kw))
        except ValueError as e:
            return jsonify({"ok": False, "msg": _xl(str(e))}), 400
        except Exception as e:
            app.logger.exception("lang api failed: %s", fn.__name__)
            return jsonify({"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}), 500
    return wrapper


def _body():
    d = request.get_json(silent=True)
    if isinstance(d, dict):
        return d
    return request.form.to_dict()


# ---- 翻译引擎配置 ----
def _lang_engine_cfg():
    _m, _s, tr = _lang_mod()
    return tr.merge_cfg(_settings.load().get("translate"))


def _save_engine_cfg(form):
    _m, _s, tr = _lang_mod()
    st = _settings.load()
    cfg = tr.merge_cfg(st.get("translate"))
    if form.get("provider"):
        cfg["provider"] = form["provider"]
    for p in ("ai", "google", "deepl", "mymemory"):
        for f in ("api_key", "base_url", "model", "endpoint", "email"):
            name = "%s_%s" % (p, f)
            if name not in form:
                continue
            v = (form.get(name) or "").strip()
            # Key 留空 = 不修改。管理员只调 base_url 或模型名时，
            # 不该顺手把已经填好的 Key 清掉 —— 这会让翻译突然失效且没提示。
            if f == "api_key" and not v:
                continue
            cfg[p][f] = v
        try:
            if form.get(p + "_timeout"):
                cfg[p]["timeout"] = float(form[p + "_timeout"])
        except (TypeError, ValueError):
            pass
    st["translate"] = cfg
    _settings.save(st)
    return cfg


# ---- 后台翻译任务（同时只允许一个，避免并发写同一个 catalog） ----
_LANG_JOB = {
    "seq": 0, "status": "idle", "lang": "", "total": 0, "done": 0,
    "ok": 0, "failed": 0, "errors": [], "note": "",
    "started": "", "finished": "",
}
_LANG_JOB_LOCK = threading.Lock()


def _job_snapshot():
    with _LANG_JOB_LOCK:
        d = dict(_LANG_JOB)
        d["errors"] = list(_LANG_JOB["errors"][-12:])
        return d


def _job_finish(status, note=""):
    with _LANG_JOB_LOCK:
        _LANG_JOB["status"] = status
        _LANG_JOB["note"] = note
        _LANG_JOB["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _translate_worker(lang, limit, overwrite):
    jm, _sc, tr = _lang_mod()
    try:
        cfg = _lang_engine_cfg()
        items = jm.pending_items(LOCALE_DIR, lang)
        if limit and int(limit) > 0:
            items = items[:int(limit)]
        total = len(items)
        with _LANG_JOB_LOCK:
            _LANG_JOB["total"] = total
        if not total:
            _job_finish("done", _xl("没有待翻译的条目"))
            return

        # 外层 60 条一批：translate() 内部还会按引擎上限再细分。
        # 外层这一层的作用是「边翻边落盘」—— 翻到一半断网、被关掉，
        # 已经翻好的部分不会丢，下次接着跑（pending_items 只取空位）。
        step = 60
        for i in range(0, total, step):
            chunk = items[i:i + step]
            res, errs = tr.translate([t for _id, t in chunk], lang, "zh", cfg)
            batch = {}
            for (tid, _t), dst in zip(chunk, res):
                if dst:
                    batch[tid] = dst
            wrote = jm.save_translations(LOCALE_DIR, lang, batch, overwrite=overwrite)
            with _LANG_JOB_LOCK:
                _LANG_JOB["ok"] += wrote
                _LANG_JOB["failed"] += len(chunk) - wrote
                _LANG_JOB["done"] = min(i + len(chunk), total)
                if errs:
                    _LANG_JOB["errors"].extend(errs[:4])
                    del _LANG_JOB["errors"][:-30]
        _job_finish("done")
    except Exception as e:
        app.logger.exception("translate job failed")
        _job_finish("error", "%s: %s" % (type(e).__name__, e))


@app.route("/api/lang/list")
@login_required
@role_required("admin")
@_api_lang
def api_lang_list():
    jm, _sc, _tr = _lang_mod()
    cfg = _lang_engine_cfg()
    eng = {"provider": cfg.get("provider") or "ai"}
    for p in ("ai", "google", "deepl", "mymemory"):
        eng[p] = {k: v for k, v in cfg[p].items() if k != "api_key"}
        # Key 只回「填没填」，不回明文 —— 设置页不需要看到它，
        # 而且这类页面经常被投屏或截图。MyMemory 没有 Key，恒为 False。
        eng[p]["has_key"] = bool((cfg[p].get("api_key") or "").strip())
    return {"ok": True,
            "languages": jm.list_languages(LOCALE_DIR),
            "default": jm.read_registry(LOCALE_DIR).get("_default") or "zh",
            "engine": eng,
            "job": _job_snapshot()}


@app.route("/api/lang/add", methods=["POST"])
@login_required
@role_required("admin")
@_api_lang
def api_lang_add():
    jm, _sc, _tr = _lang_mod()
    d = _body()
    res = jm.add_language(LOCALE_DIR, d.get("code"), d.get("native"),
                          d.get("en") or "", d.get("status") or "draft")
    _i18n.reload()
    if res["existed"] and not res["catalog_created"]:
        msg = _xl("语言 {lang} 已存在，未做改动", lang=res["code"])
    else:
        msg = _xl("已添加 {lang}，待翻译 {n} 条", lang=res["code"],
                  n=res["total"] - res["filled"])
    return {"ok": True, "msg": msg, "data": res}


@app.route("/api/lang/remove", methods=["POST"])
@login_required
@role_required("admin")
@_api_lang
def api_lang_remove():
    jm, _sc, _tr = _lang_mod()
    res = jm.remove_language(LOCALE_DIR, (_body().get("code") or ""))
    _i18n.reload()
    if res.get("backup"):
        msg = _xl("已删除语言 {lang}，语言包已备份到 locale/_removed/", lang=res["code"])
    else:
        msg = _xl("已删除语言 {lang}", lang=res["code"])
    return {"ok": True, "msg": msg, "data": res}


@app.route("/api/lang/status", methods=["POST"])
@login_required
@role_required("admin")
@_api_lang
def api_lang_status():
    jm, _sc, _tr = _lang_mod()
    d = _body()
    res = jm.set_status(LOCALE_DIR, d.get("code"), d.get("status"))
    _i18n.reload()
    return {"ok": True, "msg": _xl("已设为 {status}", status=res["status"]), "data": res}


@app.route("/api/lang/engine", methods=["POST"])
@login_required
@role_required("admin")
@_api_lang
def api_lang_engine():
    _jm, _sc, _tr = _lang_mod()
    cfg = _save_engine_cfg(_body())
    return {"ok": True, "msg": _xl("翻译引擎已保存（当前：{p}）", p=cfg.get("provider")),
            "data": {"provider": cfg.get("provider")}}


@app.route("/api/lang/test", methods=["POST"])
@login_required
@role_required("admin")
@_api_lang
def api_lang_test():
    """连通性测试。先保存再测，避免「测的是表单里的、跑的是存下来的」。"""
    _jm, _sc, tr = _lang_mod()
    cfg = _save_engine_cfg(_body())
    res = tr.ping(cfg)
    return {"ok": bool(res.get("ok")), "msg": res.get("detail") or "",
            "data": res}


@app.route("/api/lang/translate", methods=["POST"])
@login_required
@role_required("admin")
@_api_lang
def api_lang_translate():
    jm, _sc, _tr = _lang_mod()
    d = _body()
    lang = (d.get("lang") or "").strip()
    if not lang or lang == "zh":
        raise ValueError(_xl("请选择要补翻译的目标语言（中文是原文，不需要翻）"))
    if not jm.read_registry(LOCALE_DIR).get(lang):
        raise ValueError(_xl("语言不存在：{lang}", lang=lang))
    limit = int(d.get("limit") or 0)
    overwrite = bool(d.get("overwrite"))
    # 没配密钥就开任务，用户只会看到一个「0 条成功」的进度条，
    # 还以为系统卡住了。前置拦掉，告诉他先去填哪个。
    cfg = _lang_engine_cfg()
    prov = (cfg.get("provider") or "ai")
    if prov != "mymemory" and not ((cfg.get(prov) or {}).get("api_key") or "").strip():
        raise ValueError(_xl("「{engine}」搜索引擎还没填 API Key，请先在上面的引擎设置里填好并测试连通", engine=prov))
    with _LANG_JOB_LOCK:
        if _LANG_JOB["status"] == "running":
            raise ValueError(_xl("已有翻译任务在跑（{lang}），等它结束再开新的",
                                 lang=_LANG_JOB.get("lang")))
        _LANG_JOB.update({"seq": _LANG_JOB["seq"] + 1, "status": "running",
                          "lang": lang, "total": 0, "done": 0, "ok": 0,
                          "failed": 0, "errors": [], "note": "",
                          "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                          "finished": ""})
    threading.Thread(target=_translate_worker, args=(lang, limit, overwrite),
                     daemon=True).start()
    return {"ok": True, "msg": _xl("已开始为 {lang} 补翻译，进度见下方", lang=lang),
            "data": _job_snapshot()}


@app.route("/api/lang/job")
@login_required
@role_required("admin")
@_api_lang
def api_lang_job():
    return {"ok": True, "data": _job_snapshot()}


@app.route("/api/lang/scan", methods=["POST"])
@login_required
@role_required("admin")
@_api_lang
def api_lang_scan():
    """扫描界面漏翻。0.1 秒量级，不缓存 —— 缓存反而会让「刚改完就扫」看到旧结果。"""
    jm, sc, _tr = _lang_mod()
    d = _body()
    lang = (d.get("lang") or "").strip() or _i18n.current_lang()
    res = sc.scan(_settings.APP_DIR, LOCALE_DIR, lang,
                  jm.read_legacy(LOCALE_DIR),
                  include_py=bool(d.get("include_py")))
    # 运行期捕获：浏览器上报的漏词 + 服务端 t() 未命中的记录。
    # 静态扫描只看得到源码字面量，拼接的和数据库里的中文只有这里能看到。
    res["misses"] = _runtime_misses(lang)
    res["counts"]["runtime_miss"] = len(res["misses"])
    res["ok"] = True
    return res


# ---- 运行期漏词上报 ----
_MISSES_LOCK = threading.Lock()


def _misses_path():
    return os.path.join(_settings.APP_DIR, "data", "i18n_misses.json")


def _misses_read() -> list:
    try:
        with open(_misses_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    items = d.get("items") if isinstance(d, dict) else None
    return items if isinstance(items, list) else []


def _misses_write(items: list):
    """原子写：先写 .tmp 再改名，断电也不会留下半个 JSON。"""
    path = _misses_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _runtime_misses(lang: str) -> list:
    """合并两路运行期漏词，按出现次数倒序，界面最多展示 300 条。"""
    import i18n2
    seen, out = set(), []
    try:
        for (lg, key), n in (i18n2.missing_report() or {}).items():
            if lg != lang or not key or key in seen:
                continue
            # 老版本记进去的脏数据（日志行、带路径的整句）读的时候也滤掉
            if not _i18n.looks_chinese_authored(key):
                continue
            seen.add(key)
            out.append({"text": str(key)[:200], "count": n, "page": "server",
                        "first": "", "last": ""})
    except Exception:
        pass
    for r in _misses_read():
        if not isinstance(r, dict):
            continue
        t = str(r.get("text") or "").strip()
        if not t or t in seen:
            continue
        # 已有安装目录里的 i18n_misses.json 可能存着历史脏数据，展示前过一遍判据
        if not _i18n.looks_chinese_authored(t):
            continue
        # 只取这门语言自己报的：漏词是「这门语言的词典里没这条」，
        # 英文界面撞到的漏词拿到日文扫描结果里，等于让管理员去补一条
        # 日文根本不缺的词条。老记录没有 lang 字段，按「都算」处理。
        rec_lang = r.get("lang")
        if rec_lang and rec_lang != lang:
            continue
        seen.add(t)
        out.append({"text": t, "count": int(r.get("count") or 1),
                    "page": str(r.get("page") or "")[:200],
                    "first": str(r.get("first") or ""), "last": str(r.get("last") or "")})
    out.sort(key=lambda x: -x["count"])
    return out[:300]


@app.route("/api/lang/miss", methods=["POST"])
@login_required
@_api_lang
def api_lang_miss():
    """浏览器取词落空时自动上报（批量，前端 3 秒攒一批）。

    任何登录用户都允许上报，不限管理员 —— 漏词本来就是普通用户在页面上
    首先撞见的；管理员不可能把每个页面每种数据都点一遍。
    上报静默失败即可（fetch 不等待不重试），绝不影响页面本身。
    """
    d = request.get_json(silent=True) or {}
    keys = []
    for k in (d.get("keys") or [])[:40]:
        k = str(k or "").strip()
        # 不是界面文案就别记：日志行、带路径的整句、纯标点。前端已经挡了一层，
        # 这里是服务端兜底 —— 老客户端 / 手工构造的请求也会进这张表。
        if 1 <= len(k) <= 200 and _i18n.looks_chinese_authored(k):
            keys.append(k)
    if not keys:
        return {"ok": True, "added": 0}
    page = str(d.get("page") or "")[:200]
    lang = _i18n.current_lang()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _MISSES_LOCK:
        items = _misses_read()
        # 同一句中文在不同语言界面都可能漏（德语缺、日语缺是两件事），
        # 所以去重键是 (文案, 语言)，不能只用文案 —— 否则后报的那门语言
        # 会把先报的覆盖掉，扫描日文时看到的是英文界面的记录。
        idx = {(r.get("text"), r.get("lang") or ""): r
               for r in items if isinstance(r, dict)}
        for k in keys:
            r = idx.get((k, lang))
            if isinstance(r, dict):
                r["count"] = int(r.get("count") or 0) + 1
                r["last"] = now
            else:
                rec = {"text": k, "count": 1, "lang": lang, "page": page,
                       "first": now, "last": now}
                items.append(rec)
                idx[(k, lang)] = rec
        # 上限 500 条：这是诊断清单不是日志库，无限攒只会拖慢设置页。
        # 出现次数多的排前面（写文件前不排序，读出来再排）
        del items[:-500]
        _misses_write(items)
    return {"ok": True, "added": len(keys)}


@app.route("/api/lang/miss/clear", methods=["POST"])
@login_required
@role_required("admin")
@_api_lang
def api_lang_miss_clear():
    with _MISSES_LOCK:
        _misses_write([])
    return {"ok": True, "msg": _xl("已清空运行期捕获记录")}


def _endpoint_of_url(path: str) -> str:
    """运行期漏词上报只有 URL 路径；反查路由表拿 endpoint 当分片键。

    tr_json 按 request.endpoint 取分片，所以词条必须登记进 pages.json 的
    endpoint 键下，浏览器才能真正拿到译文 —— 否则登记了也白登记，
    下次访问照样落空照样上报。
    """
    p = (path or "").split("?")[0].split("#")[0]
    if not p.startswith("/"):
        return ""
    try:
        adapter = app.url_map.bind("localhost")
        endpoint, _vals = adapter.match(p)
    except Exception:
        return ""
    ep = (endpoint or "").split(".")[-1]
    if ep.startswith(("api_", "static")):
        return ""
    return ep


@app.route("/api/lang/register", methods=["POST"])
@login_required
@role_required("admin")
@_api_lang
def api_lang_register():
    """把扫描出的新文案登记进词条表。

    登记 = 分配词条 ID + 写 legacy_map + 补页面分片。做完这一步，
    这条文案才「可被翻译」；在此之前翻多少遍都不会显示出来。
    """
    jm, _sc, _tr = _lang_mod()
    d = _body()
    items = d.get("items") or []
    # 运行期漏词带的是 page（URL 路径），转成分片键再交给登记器
    norm = []
    for it in items:
        if isinstance(it, dict) and not it.get("file") and it.get("page"):
            ep = _endpoint_of_url(str(it["page"]))
            it = dict(it)
            if ep:
                it["file"] = ep
            it.pop("page", None)
        norm.append(it)
    items = norm
    module = (d.get("module") or "app").strip() or "app"
    res = jm.register_terms(LOCALE_DIR, items, module)
    _i18n.reload()
    # 登记过的就从运行期清单里撤掉 —— 否则「明明登记了，列表里还挂着」，
    # 管理员只能整表清空，连带把没处理的记录一起丢掉。
    done = {str(it.get("text") or "").strip() for it in items if isinstance(it, dict)}
    done.discard("")
    if done:
        with _MISSES_LOCK:
            items_old = _misses_read()
            left = [r for r in items_old
                    if str(r.get("text") or "").strip() not in done]
            if len(left) != len(items_old):
                _misses_write(left)
    if res["added"]:
        msg = _xl("已登记 {n} 条，现在可以补翻译了", n=res["added"])
    else:
        msg = _xl("这些文案已经在词条表里了，没有新增")
    return {"ok": True, "msg": msg, "data": res}


@app.route("/api/lang/miss/drop", methods=["POST"])
@login_required
@role_required("admin")
@_api_lang
def api_lang_miss_drop():
    """忽略单条运行期漏词（不是界面文案、或已确认无需处理）。"""
    d = _body()
    text = str(d.get("text") or "").strip()
    lang = str(d.get("lang") or "").strip()
    if not text:
        return {"ok": False, "msg": _xl("缺少文案")}
    with _MISSES_LOCK:
        items = _misses_read()
        left = [r for r in items
                if not (isinstance(r, dict) and str(r.get("text") or "").strip() == text
                        and (not lang or (r.get("lang") or "") == lang))]
        _misses_write(left)
    return {"ok": True, "msg": _xl("已忽略这一条"), "removed": len(items) - len(left)}



def _save_stocktake_cfg():
    """保存审批设置（盘点/贵重件/退货/换货/内部转移/返还审批档位 + FIFO开关）

    档位统一三选：0 不审批（直接执行）/ 1 一级审批 / 2 二级审批。
    贵重件与盘点原来只有 1/2 两档，现扩展 0；新出库类型默认一级。
    """
    st = _settings.load()
    for key, dft in (("stocktake_approval_level", 1), ("valuable_approval_level", 1),
                     ("return_approval_level", 1), ("exchange_approval_level", 1),
                     ("internal_approval_level", 1), ("giveback_approval_level", 1),
                     ("scrap_approval_level", 1)):
        try:
            lv = _int(request.form.get(key, dft), dft)
        except (TypeError, ValueError):
            lv = dft
        lv = lv if lv in (0, 1, 2) else dft
        # v1.0.77：报废出库固定走系统审批（纸档模式也要审批后自动扣库），
        # 不允许配成 0；以前界面给了 0 但执行时被强制抬回 1，属于前后不一致。
        if key == "scrap_approval_level":
            lv = max(lv, 1)
        st[key] = lv
    # v1.0.47：报废审批两模式（1=系统审批 / 2=系统+纸档），老表单不带该字段时保持原值
    if "scrap_approval_mode" in request.form:
        try:
            sm = _int(request.form.get("scrap_approval_mode"), 1)
        except (TypeError, ValueError):
            sm = 1
        st["scrap_approval_mode"] = sm if sm in (1, 2) else 1
    # v1.0.18：盘点周期。表单没带这个字段时保持原值，避免旧版表单/回归脚本
    # 只提交审批档位时把用户配置的周期悄悄重置回默认。
    if "stocktake_cycle_days" in request.form:
        try:
            cd = int(float(request.form.get("stocktake_cycle_days") or 30))
        except (TypeError, ValueError):
            cd = 30
        st["stocktake_cycle_days"] = cd if 1 <= cd <= 365 else 30
    # v1.0.20：节点容差（表单填百分比 0~50，存 0~0.5 的比例）与周期锚点日
    if "stocktake_cycle_tolerance_pct" in request.form:
        try:
            ct = float(request.form.get("stocktake_cycle_tolerance_pct") or 20)
        except (TypeError, ValueError):
            ct = 20.0
        if ct != ct or ct < 0:
            ct = 20.0
        st["stocktake_cycle_tolerance"] = round(min(50.0, ct) / 100.0, 3)
    if "stocktake_cycle_anchor" in request.form:
        st["stocktake_cycle_anchor"] = (request.form.get("stocktake_cycle_anchor") or "").strip()[:10]
    # v1.0.43：盘点启始日填的是每年重复的 M/D（默认 1/1），不再写死某个年份。
    # 新字段一旦提交就清掉老的完整锚点，避免两套口径互相打架；老接口/回归
    # 若仍只提交 stocktake_cycle_anchor，则保持原行为不变。
    if "stocktake_start_md" in request.form:
        raw = (request.form.get("stocktake_start_md") or "").strip()
        # v1.0.47：日历控件提交 YYYY-MM-DD，取月/日存 M/D（保持每年重复口径）
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw)
        if not m:
            m = re.match(r"^(\d{1,2})\s*[/.\-]\s*(\d{1,2})$", raw)  # 兼容老手输值
        try:
            if not m:
                valid = False
            elif len(m.groups()) >= 3:   # YYYY-MM-DD：第 2/3 组是月/日
                _mo, _dy = int(m.group(2)), int(m.group(3))
                valid = 1 <= _mo <= 12 and 1 <= _dy <= 31
            else:                        # M/D：第 1/2 组是月/日
                _mo, _dy = int(m.group(1)), int(m.group(2))
                valid = 1 <= _mo <= 12 and 1 <= _dy <= 31
            if valid:
                datetime(2024, _mo, _dy)
        except (ValueError, AttributeError, IndexError):
            valid = False
        st["stocktake_start_md"] = ("%d/%d" % (_mo, _dy)) if valid else "1/1"
        st["stocktake_cycle_anchor"] = ""
    # v1.0.23：FIFO 复选框只在这个字段确实出现在表单里时才改。
    # 表单带 hidden=0 + 勾选=1，取最后一个值；回归脚本/旧版表单只提交审批档位时
    # 不带这个字段，保持原值，不能悄悄把用户开启的 FIFO 关掉。
    if "fifo_enabled" in request.form:
        st["fifo_enabled"] = 1 if request.form.getlist("fifo_enabled")[-1] == "1" else 0
    # v1.0.78：入库摆放提示与 FIFO 开关同卡片保存；字段必须出现才改，老回归不带此字段时保持原值。
    if "fifo_in_tip" in request.form:
        st["fifo_in_tip"] = str(request.form.get("fifo_in_tip") or "").strip()
    # v1.0.77：自动上下限已经有独立保存分支（_save_auto_stock），审批表单不带这些字段。
    # 以前这里用表单缺省值再写一次 auto_stock，管理员单独调好 60/2.2/200 后，
    # 只要随手保存一次审批设置就被悄悄重置成 45/1.5/120 且开关被关掉。
    try:
        _settings.save(st)
    except OSError as e:
        return _settings_reply(_xl("配置保存失败：{e}", e=e), "danger", "card-stocktake")
    return _settings_reply("审批设置已保存", "success", "card-stocktake")


def _save_auto_stock():
    """保存自动上下限全局配置（存数据库 settings 表）"""
    try:
        ast = {
            "enabled": 1 if request.form.get("auto_stock_enabled") else 0,
            "lead_days": max(1, int(float(request.form.get("auto_stock_lead", 45) or 45))),
            "safety_factor": max(1.0, min(3.0, float(request.form.get("auto_stock_safety", 1.5) or 1.5))),
            "consume_days": max(7, int(float(request.form.get("auto_stock_consume", 120) or 120))),
        }
        conn = get_db()
        try:
            db.set_setting(conn, "auto_stock", json.dumps(ast, ensure_ascii=False))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        return _settings_reply(_xl("自动上下限保存失败：{e}", e=e), "danger", "card-auto-stock")
    return _settings_reply("自动上下限设置已保存", "success", "card-auto-stock")


def _save_notify_cfg():
    """保存消息通知配置（浏览器/邮件/企业私信）"""
    st = _settings.load()
    st["notify_browser"] = 1 if request.form.get("notify_browser") else 0
    st["notify_email_enabled"] = 1 if request.form.get("notify_email_enabled") else 0
    st["smtp_host"] = request.form.get("smtp_host", "").strip()
    try:
        st["smtp_port"] = _int(request.form.get("smtp_port", 465), 465)
    except (TypeError, ValueError):
        st["smtp_port"] = 465
    st["smtp_user"] = request.form.get("smtp_user", "").strip()
    if request.form.get("smtp_pass"):
        st["smtp_pass"] = request.form.get("smtp_pass", "")
    st["notify_imp_enabled"] = 1 if request.form.get("notify_imp_enabled") else 0
    st["imp_platform"] = request.form.get("imp_platform", "wecom")
    st["imp_corpid"] = request.form.get("imp_corpid", "").strip()
    if request.form.get("imp_secret"):
        st["imp_secret"] = request.form.get("imp_secret", "")
    st["imp_agentid"] = request.form.get("imp_agentid", "").strip()
    st["imp_webhook"] = request.form.get("imp_webhook", "").strip()
    st["imp_webhook_style"] = request.form.get("imp_webhook_style", "slack").strip().lower() or "slack"
    st["base_url"] = request.form.get("base_url", "").strip().rstrip("/")
    try:
        st["remind_first_hours"] = float(request.form.get("remind_first_hours", 4))
    except (TypeError, ValueError):
        st["remind_first_hours"] = 4
    try:
        st["remind_interval_hours"] = float(request.form.get("remind_interval_hours", 24))
    except (TypeError, ValueError):
        st["remind_interval_hours"] = 24
    try:
        _settings.save(st)
    except OSError as e:
        return _settings_reply(_xl("配置保存失败：{e}", e=e), "danger", "card-notify")
    return _settings_reply("消息通知配置已保存", "success", "card-notify")


def _save_ad():
    """保存 AD 域登录配置到 settings.json（保留其他配置）。
    - 端口：放开为 1-65535 任意合法端口，不再静默重置（非法值直接报错提示）
    - 加密方式：none 明文 / ldaps（636）/ starttls（389 升级加密）
    - 绑定方式：direct 直接绑定 / search 服务账号搜索（Base DN + 搜索属性）
    - 同时保存「用户名匹配是否区分大小写」到数据库（共享，作用于普通登录 + AD 登录）"""
    st = _settings.load()
    port_raw = request.form.get("ad_port", "389").strip() or "389"
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        return _settings_reply("AD 端口必须是 1-65535 之间的数字（如 389/636/3268/3269 或自定义端口）", "danger", "card-ad")
    enc = request.form.get("ad_enc", "none").strip()
    if enc not in ("none", "ldaps", "starttls"):
        enc = "none"
    bind_mode = request.form.get("ad_bind_mode", "direct").strip()
    if bind_mode not in ("direct", "search"):
        bind_mode = "direct"
    try:
        timeout = max(3, min(_int(request.form.get("ad_timeout", "10") or 10), 60))
    except (TypeError, ValueError):
        timeout = 10
    st["ad"] = {
        "enabled": 1 if request.form.get("ad_enabled") else 0,
        "server": request.form.get("ad_server", "").strip(),
        "port": port,
        "enc": enc,
        "domain": request.form.get("ad_domain", "").strip(),
        "name_format": request.form.get("ad_name_format", "upn").strip() or "upn",
        "bind_mode": bind_mode,
        "svc_dn": request.form.get("ad_svc_dn", "").strip(),
        "svc_pwd": request.form.get("ad_svc_pwd", ""),
        "base_dn": request.form.get("ad_base_dn", "").strip(),
        "search_attr": (request.form.get("ad_search_attr", "sAMAccountName").strip()
                        or "sAMAccountName"),
        "timeout": timeout,
        "strip_upn_suffix": 1 if request.form.get("ad_strip_upn_suffix") else 0,
    }
    try:
        _settings.save(st)
    except OSError as e:
        return _settings_reply(_xl("配置保存失败：{e}", e=e), "danger", "card-ad")
    # 用户名匹配大小写（数据库共享设置，所有电脑一致；同时作用于普通登录与 AD 登录）
    cs = 1 if request.form.get("login_case_sensitive") else 0
    conn = get_db()
    try:
        db.set_setting(conn, "login_case_sensitive", str(cs))
        conn.commit()
    finally:
        conn.close()
    # 提示安全提醒
    tip = ""
    if st["ad"].get("enabled"):
        tip = _xl("；已启用 AD 登录，登录页将显示「AD 域账号登录」入口")
    return _settings_reply(_xl("AD 域登录配置已保存") + tip, "success", "card-ad")


def _is_hex_color(v):
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", v or ""))


def _hex_is_light(v):
    """#rrggbb 是否是「浅色底」：按相对亮度判断，用于自定义风格切浅色/深色图表。

    只看页面背景一个色就够 —— 客户把背景填成白色却还画深色大屏的图表，
    线会整片糊在底上。阈值 0.55 落在 #f4f6f9(0.92) 与最深几套(≤0.01) 之间，
    不会出现「同一个值忽深忽浅」。
    """
    if not _is_hex_color(v):
        return False

    def _lin(x):
        c = int(x, 16) / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    h = v.lstrip("#")
    lum = 0.2126 * _lin(h[0:2]) + 0.7152 * _lin(h[2:4]) + 0.0722 * _lin(h[4:6])
    return lum >= 0.55


def _save_appearance():
    """保存界面主题与外观（theme + nav_style + 侧栏品牌 LOGO/文字样式，合并为一张配置卡片，一次提交）"""
    if request.form.get("appearance_reset"):
        conn = get_db()
        try:
            old_logo = get_brand(conn).get("logo_file", "")
            if old_logo:
                db.image_delete(conn, old_logo)
                fp = os.path.join(config.resolved_upload_dir(), old_logo)
                if os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
            db.set_setting(conn, "theme", json.dumps(dict(config.DEFAULT_THEME), ensure_ascii=False))
            db.set_setting(conn, "nav_style", json.dumps(dict(config.DEFAULT_NAV_STYLE), ensure_ascii=False))
            db.set_setting(conn, "brand", json.dumps(dict(DEFAULT_BRAND), ensure_ascii=False))
            db.set_setting(conn, "ui_hints", "1")
            conn.commit()
        finally:
            conn.close()
        return _settings_reply("界面主题与外观已恢复默认", "success", "card-theme")
    # 品牌（LOGO/标题/副标题 + 样式），先校验上传文件
    cur_brand = get_brand()
    logo = request.files.get("brand_logo")
    if logo and logo.filename:
        ext = os.path.splitext(logo.filename)[1].lower()
        if ext not in config.ALLOWED_IMG_EXTS:
            return _settings_reply("LOGO 图片格式不支持（jpg/png/gif/bmp/webp）", "danger", "card-theme")
        logo.seek(0, os.SEEK_END)
        size = logo.tell()
        logo.seek(0)
        if size > config.MAX_IMG_SIZE:
            return _settings_reply("LOGO 图片超过 5MB 限制", "danger", "card-theme")
    brand = _collect_brand(cur_brand)
    # 界面主题色（侧栏背景已由渐变起止色控制，不再单独配置）
    t = get_theme()
    for k in ("primary", "primary_d", "sidebar_text", "bg"):
        v = request.form.get("theme_" + k, "").strip()
        if _is_hex_color(v):
            t[k] = v.lower()
    # 左侧菜单外观（含侧栏渐变起止色）
    nav = get_nav_style()
    def _num(k, lo, hi, cur):
        try:
            v = float(request.form.get(k, "").strip())
            return lo if v < lo else (hi if v > hi else v)
        except Exception:
            return cur
    nav["menu_width"] = int(_num("nav_menu_width", 160, 360, nav["menu_width"]))
    nav["font_size"] = _num("nav_font_size", 10, 24, nav["font_size"])
    nav["icon_size"] = _num("nav_icon_size", 10, 40, nav["icon_size"])
    nav["item_padding"] = _num("nav_item_padding", 2, 24, nav["item_padding"])
    nav["item_radius"] = _num("nav_item_radius", 0, 20, nav["item_radius"])
    ff = request.form.get("nav_font_family", "").strip()
    nav["font_family"] = ff if len(ff) <= 60 else nav["font_family"]
    for k in ("sidebar_top", "sidebar_bottom"):
        v = request.form.get("nav_" + k, "").strip()
        if _is_hex_color(v):
            nav[k] = v.lower()
    conn = get_db()
    try:
        # 品牌 LOGO 二进制写入 images 表；移除/替换时清理旧记录与旧文件
        old_logo = cur_brand.get("logo_file", "")
        logo_bytes = brand.pop("_logo_bytes", None)
        if logo_bytes is not None:
            db.image_save(conn, brand["logo_file"], logo_bytes)
            if old_logo and old_logo != brand["logo_file"]:
                db.image_delete(conn, old_logo)
        elif brand["logo_file"] == "" and old_logo:
            db.image_delete(conn, old_logo)
        if old_logo and old_logo != brand["logo_file"]:
            fp = os.path.join(config.resolved_upload_dir(), old_logo)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
        db.set_setting(conn, "theme", json.dumps(t, ensure_ascii=False))
        db.set_setting(conn, "nav_style", json.dumps(nav, ensure_ascii=False))
        db.set_setting(conn, "brand", json.dumps(brand, ensure_ascii=False))
        # 说明提示开关：表单里有 ui_hints_present 才写，避免别的表单提交时把它误关掉
        if request.form.get("ui_hints_present"):
            db.set_setting(conn, "ui_hints", "1" if request.form.get("ui_hints") else "0")
        conn.commit()
    finally:
        conn.close()
    return _settings_reply("界面主题与外观已保存（立即生效）", "success", "card-theme", theme=t, nav=nav)


def _save_open_api():
    """保存对外数据接口设置（系统设置页）"""
    cfg = {
        "enabled": 1 if request.form.get("open_api_enabled") else 0,
        "token": request.form.get("open_api_token", "").strip(),
    }
    conn = get_db()
    try:
        db.set_setting(conn, "open_api", json.dumps(cfg, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return _settings_reply(_xl("对外数据接口设置已保存（{state}）",
                               state=_xl("已开放") if cfg["enabled"] else _xl("已关闭")),
                           "success", "card-api")


def get_open_api(conn=None):
    """读取对外数据接口配置（sys_settings key=open_api）"""
    cfg = dict(config.DEFAULT_OPEN_API)
    if not db_configured():
        return cfg
    own = conn is None
    if own:
        conn = get_db()
    try:
        raw = db.get_setting(conn, "open_api")
        if raw:
            try:
                d = json.loads(raw)
                if isinstance(d, dict):
                    for k in cfg:
                        if d.get(k) is not None:
                            cfg[k] = d[k]
            except Exception:
                pass
    finally:
        if own:
            conn.close()
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["token"] = str(cfg.get("token") or "").strip()
    return cfg


def _api_token_ok(cfg):
    """校验请求 X-API-Token 是否有效 → (ok, err, http_status)

    状态码必须由这里给出，不能事后拿「err 里有没有『无效』两个字」去猜：
    提示语是要翻译的（英文下是 "Invalid API token"），一翻译状态码就跟着变了
    （曾经表现为英文界面下 401 全退化成 400）。
    """
    if not cfg["enabled"]:
        return False, _xl("接口未启用（请先在系统设置中开放）"), 400
    if not cfg["token"]:
        return False, _xl("系统未配置接口密钥"), 400
    if request.headers.get("X-API-Token", "") != cfg["token"]:
        return False, _xl("API Token 无效"), 401
    return True, "", 200



def _system_robot(conn):
    """获取/创建「系统机器人」用户（外部系统提交的申请记录，申请人显示为系统机器人）"""
    row = conn.execute("SELECT id FROM users WHERE username='system_robot'").fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        """INSERT INTO users(username, display_name, role, department, password_hash, active, created_at)
           VALUES(?,?,?,?,?,1,?)""",
        ("system_robot", "系统机器人", "user", "", "*", db.now_str()),
    )
    return cur.lastrowid


@app.route("/api/inventory/list")
def api_inventory_list():
    """外系统查询物料库存列表（GET）
    请求头：X-API-Token: <接口密钥>
    返回：{ok, total, items:[{part_code,name,category,spec,unit,qty_decimal,stock,min_stock,max_stock,
                              is_valuable, locations:[{location,quantity}], apps}]}"""
    if not db_configured():
        return jsonify({"ok": False, "error": _xl("系统尚未初始化")}), 400
    conn = get_db()
    try:
        cfg = get_open_api(conn)
    finally:
        conn.close()
    ok, err, code = _api_token_ok(cfg)
    if not ok:
        return jsonify({"ok": False, "error": err}), code
    kw = (request.args.get("kw") or "").strip()
    conn = get_db()
    try:
        sql = """SELECT p.*, COALESCE(SUM(i.quantity),0) stock
                 FROM parts p LEFT JOIN inventory i ON p.id=i.part_id WHERE 1=1"""
        params = []
        if kw:
            sql += " AND (p.part_code LIKE ? OR p.name LIKE ? OR p.spec LIKE ?)"
            params += [f"%{kw}%"] * 3
        sql += " GROUP BY p.id ORDER BY p.part_code"
        rows = conn.execute(sql, params).fetchall()
        items = []
        for r in rows:
            p = dict(r)
            locs = db.stock_locations(conn, p["id"])
            apps = db.part_apps(conn, p["id"])
            items.append({
                "part_code": p["part_code"], "name": p["name"], "category": p["category"] or "",
                "spec": p["spec"] or "", "unit": p["unit"] or "",
                "qty_decimal": 1 if p["qty_decimal"] else 0,
                "stock": p["stock"] or 0, "min_stock": p["min_stock"] or 0,
                "max_stock": p["max_stock"] or 0,
                "is_valuable": 1 if p["is_valuable"] else 0,
                "locations": [{"location": l["location"], "quantity": l["quantity"]} for l in locs],
                "apps": [_apps_label(a, plain=True) for a in apps],
            })
        return jsonify({"ok": True, "total": len(items), "items": items})
    finally:
        conn.close()


@app.route("/api/inbound", methods=["POST"])
def api_inbound():
    """外系统提交物料入库申请（POST）
    请求头：X-API-Token: <接口密钥>；Content-Type: application/json
    请求体字段：
      part_code  料号（必填，若系统中不存在则自动建档，存放库位=待定）
      quantity   入库数量（必填，>0；已存在物料按数量属性校验整数/小数）
      name/unit/category/spec/unit_price/qty_decimal  自动建档时的可选属性
      remark/note 备注（可选）
    流程：与网页申请一致，生成入库申请单（申请人=系统机器人），需仓库人员执行后入库
    返回：{ok, app_no, part_created}"""
    if not db_configured():
        return jsonify({"ok": False, "error": "系统尚未初始化"}), 400
    conn = get_db()
    try:
        cfg = get_open_api(conn)
    finally:
        conn.close()
    ok, err, code = _api_token_ok(cfg)
    if not ok:
        return jsonify({"ok": False, "error": err}), code
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": _xl("请求体不是合法 JSON")}), 400
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": _xl("请求体需为 JSON 对象")}), 400
    part_code = str(data.get("part_code") or "").strip()
    if not part_code:
        return jsonify({"ok": False, "error": _xl("缺少必填字段 part_code（料号）")}), 400
    try:
        qty = float(data.get("quantity"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": _xl("quantity（数量）必须是数字")}), 400
    if qty <= 0:
        return jsonify({"ok": False, "error": _xl("quantity（数量）必须大于 0")}), 400
    remark = str(data.get("remark") or data.get("note") or "").strip()

    conn = get_db()
    try:
        part = conn.execute("SELECT * FROM parts WHERE part_code=?", (part_code,)).fetchone()
        part_created = False
        if not part:
            # 自动建档：库位=待定（建档本身不建库存，由入库执行时产生库存）
            name = str(data.get("name") or part_code).strip()
            unit = str(data.get("unit") or "个").strip() or "个"
            qdec = data.get("qty_decimal")
            if qdec is None or str(qdec).strip() == "":
                qdec = 0 if unit.lower() in ("个", "pcs", "pcs.", "件", "套", "片", "张", "支", "台", "包", "只", "根", "条", "块") else 1
            else:
                try:
                    qdec = _qdec(qdec) if isinstance(qdec, str) else (1 if float(qdec) else 0)
                except (TypeError, ValueError):
                    qdec = 0
            price = 0.0
            try:
                price = float(data.get("unit_price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            cur = conn.execute(
                """INSERT INTO parts(part_code,name,category,spec,unit,qty_decimal,unit_price,min_stock,max_stock,
                   is_valuable,supplier,remark,location_id,extra,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,0,'',?,NULL,'{}',?,?)""",
                (part_code, name, str(data.get("category") or "无").strip() or "无",
                 str(data.get("spec") or "无").strip() or "无", unit, qdec, price,
                 _num(data.get("min_stock")) or 0, _num(data.get("max_stock")) or 0,
                 remark or "无", db.now_str(), db.now_str()),
            )
            pid = cur.lastrowid
            part_created = True
            part = conn.execute("SELECT * FROM parts WHERE id=?", (pid,)).fetchone()
        # 数量属性校验
        qok, qmsg = _qty_valid(part, qty)
        if not qok:
            return jsonify({"ok": False, "error": qmsg}), 400
        robot_id = _system_robot(conn)
        app_no = db.gen_no("APP", conn, "applications", "app_no")
        conn.execute(
            """INSERT INTO applications(app_no,doc_no,part_id,app_type,quantity,reason,applicant_id,
               status,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            (app_no, app_no, part["id"], "in", qty, remark, robot_id, "pending", db.now_str()),
        )
        conn.commit()
        return jsonify({"ok": True, "app_no": app_no, "part_created": part_created})
    finally:
        conn.close()


def _save_stale():
    """保存呆滞配置：判定天数 + 借用是否记入呆滞判断（默认勾选）"""
    try:
        v = _int(request.form.get("stale_days", "").strip() or 90, 90)
    except (TypeError, ValueError):
        v = 90
    v = max(1, min(3650, v))
    ignore_borrowed = 1 if request.form.get("stale_ignore_borrowed") else 0
    conn = get_db()
    try:
        db.set_setting(conn, "stale_days", str(v))
        db.set_setting(conn, "stale_ignore_borrowed", str(ignore_borrowed))
        conn.commit()
    finally:
        conn.close()
    rule = _i18n.t("stale.borrow_on") if ignore_borrowed else _i18n.t("stale.borrow_off")
    msg = _xl("呆滞物料天数已保存：超过 {days} 天未领用即判定为呆滞", days=v) + _i18n.mk("；") + rule
    return _settings_reply(msg, "success", "card-stale")


def _save_pending_cfg():
    """保存待出库超时配置：提醒时长（小时）、自动取消时长（小时）"""
    try:
        remind_h = _int(request.form.get("pending_remind_hours", "").strip() or 24, 24)
    except (TypeError, ValueError):
        remind_h = 24
    try:
        release_h = _int(request.form.get("pending_release_hours", "").strip() or 36, 36)
    except (TypeError, ValueError):
        release_h = 36
    remind_h = max(1, min(720, remind_h))
    release_h = max(1, min(720, release_h))
    # v1.0.77：以前这里悄悄把 release_h 改成 remind_h+12，界面显示的却是用户填的值
    # 之外的另一个数，属于「保存成功但参数不是我填的」。改成明确报错让用户改。
    if release_h <= remind_h:
        return _settings_reply(_xl("自动取消时长必须大于提醒时长，请重新填写"), "danger", "card-pending")
    # 保存到 settings.json
    s = _settings.load()
    s["pending_remind_hours"] = remind_h
    s["pending_release_hours"] = release_h
    _settings.save(s)
    return _settings_reply(_xl("待出库超时配置已保存：{remind} 小时后提醒领货，{release} 小时后自动取消",
                               remind=remind_h, release=release_h), "success", "card-pending")


def _stale_days(conn):
    """呆滞判定天数（sys_settings，默认 90）"""
    try:
        return int(db.get_setting(conn, "stale_days") or 90)
    except (TypeError, ValueError):
        return 90


def _stale_ignore_borrowed(conn):
    """借用过的物料是否不计入呆滞（sys_settings，默认开启=1）。

    关闭时只按出库日期判定；开启时最后借用日期也计入活跃参考。
    """
    try:
        v = db.get_setting(conn, "stale_ignore_borrowed")
    except Exception:
        return True
    if v is None or str(v).strip() == "":
        return True  # 老库没有这项配置时按默认（勾选）
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def _save_head():
    """保存表头设置：自定义列名称 + 各文本列显示字符数（标准列/自定义列一次存）。

    extra_fields 仍按原结构存（key/label），字数单独存 head_clip，两把钥匙
    一次提交 —— 老版本升级后 extra_fields 数据原样可用。
    """
    def _clip(raw):
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return config.DEFAULT_HEAD_CLIP
        return max(config.HEAD_CLIP_MIN, min(config.HEAD_CLIP_MAX, v))

    # 自定义列：名称 / 字数 / 类型成组收集；名称留空的列不保存
    fields = []
    extra_clip = {}
    for i in range(config.MAX_EXTRA_FIELDS):
        label = request.form.get(f"ef_label_{i}", "").strip()
        clip_v = _clip(request.form.get(f"ef_clip_{i}"))
        ftype = (request.form.get(f"ef_type_{i}") or "").strip()
        if ftype not in config.EXTRA_FIELD_TYPES:
            ftype = config.DEFAULT_EXTRA_TYPE
        if not label:
            continue
        key = f"f{i + 1}"
        extra_clip[key] = clip_v
        if not any(f["key"] == key for f in fields):
            fields.append({"key": key, "label": label[:30], "type": ftype})
    # 标准文本列字数
    std_clip = {}
    for k, _label in config.STD_TEXT_COLS:
        std_clip[k] = _clip(request.form.get(f"clip_std_{k}"))
    conn = get_db()
    try:
        db.set_setting(conn, "extra_fields", json.dumps(fields, ensure_ascii=False))
        db.set_setting(conn, "head_clip", json.dumps(
            {"std": std_clip, "extra": extra_clip}, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return _settings_reply(_xl("表头设置已保存（自定义列 {n} 列）", n=len(fields)),
                           "success", "card-ef")


def _save_part_categories():
    """保存物料分类字典（每行一个，分号也当分隔符）"""
    raw = request.form.get("part_categories", "")
    lines = re.split(r"[\r\n;；]+", raw)
    rejects = []
    conn = get_db()
    try:
        out = save_part_categories(conn, lines, rejects=rejects)
        conn.commit()
    finally:
        conn.close()
    if rejects:
        names = _i18n.mk("、", _i18n.current_lang()).join(x[0] for x in rejects[:3])
        msg = _xl("物料分类已保存（共 {n} 个）；以下 {k} 个超过 {m} 个字符已拒绝：{names}",
                  n=len(out), k=len(rejects), m=rejects[0][1], names=names)
        return _settings_reply(msg, "warning", "card-ef")
    return _settings_reply(_xl("物料分类已保存（共 {n} 个）", n=len(out)), "success", "card-ef")


def _collect_brand(cur):
    """从表单收集品牌字段（标题/副标题/LOGO 上传与移除/图片大小/文字大小/字体/字色）"""
    title = request.form.get("brand_title", "").strip() or DEFAULT_BRAND["title"]
    subtitle = request.form.get("brand_subtitle", "").strip()
    logo_file = cur.get("logo_file", "")
    logo_bytes = None
    logo = request.files.get("brand_logo")
    if logo and logo.filename:
        ext = os.path.splitext(logo.filename)[1].lower()
        if ext in config.ALLOWED_IMG_EXTS and logo.tell() <= config.MAX_IMG_SIZE:
            logo.seek(0)
            logo_bytes, _ = _compress_image(logo.read(), _upload_compress_kb())
            save_name = f"brand_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secure_filename(logo.filename)}"
            logo_file = save_name
    if request.form.get("brand_remove_logo"):
        logo_file = ""
        logo_bytes = None

    def _num(k, lo, hi, cur_v):
        try:
            v = _int(request.form.get(k, "").strip())
            return lo if v < lo else (hi if v > hi else v)
        except Exception:
            return cur_v

    def _color(k, cur_v):
        v = request.form.get(k, "").strip()
        return v.lower() if _is_hex_color(v) else cur_v

    ff = request.form.get("brand_font_family", "").strip()
    return {
        "logo_file": logo_file,
        "_logo_bytes": logo_bytes,   # 新上传 LOGO 的二进制（由调用方写入 images 表）
        "title": title[:30],
        "subtitle": subtitle[:50],
        "logo_height": _num("brand_logo_height", 12, 160, cur["logo_height"]),
        "title_size": _num("brand_title_size", 10, 36, cur["title_size"]),
        "subtitle_size": _num("brand_subtitle_size", 8, 22, cur["subtitle_size"]),
        "title_color": _color("brand_title_color", cur.get("title_color", "")),
        "subtitle_color": _color("brand_subtitle_color", cur.get("subtitle_color", "")),
        "font_family": ff[:80],
        # 默认关：品牌名当配置内容原样显示。勾选后才随界面语言翻译。
        "i18n": bool(request.form.get("brand_i18n")),
    }


def _save_brand():
    """保存品牌信息（侧栏 LOGO / 标题 / 副标题 + 图片大小、文字大小、字体、字色）"""
    cur = get_brand()
    logo = request.files.get("brand_logo")
    if logo and logo.filename:
        ext = os.path.splitext(logo.filename)[1].lower()
        if ext not in config.ALLOWED_IMG_EXTS:
            return _settings_reply("LOGO 图片格式不支持（jpg/png/gif/bmp/webp）", "danger", "card-theme")
        logo.seek(0, os.SEEK_END)
        size = logo.tell()
        logo.seek(0)
        if size > config.MAX_IMG_SIZE:
            return _settings_reply("LOGO 图片超过 5MB 限制", "danger", "card-theme")
    brand = _collect_brand(cur)
    logo_bytes = brand.pop("_logo_bytes", None)
    conn = get_db()
    try:
        # 新 LOGO 二进制写入 images 表；移除/替换时清理旧记录与旧文件
        old_logo = cur.get("logo_file", "")
        if logo_bytes is not None:
            db.image_save(conn, brand["logo_file"], logo_bytes)
            if old_logo and old_logo != brand["logo_file"]:
                db.image_delete(conn, old_logo)
        elif brand["logo_file"] == "" and old_logo:
            db.image_delete(conn, old_logo)
        if old_logo and old_logo != brand["logo_file"]:
            fp = os.path.join(config.resolved_upload_dir(), old_logo)
            if os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
        db.set_setting(conn, "brand", json.dumps(brand, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return _settings_reply("品牌信息已保存（侧边栏立即生效）", "success", "card-theme")


def _settings_cfg():
    cfg = _settings.load()
    # v1.0.47：设置页「盘点启始日」用日历控件选完整日期，字段值算出真实日期给 input[type=date]
    # v1.0.54：老库若只留了「周期锚点日」，也照它显示，保证「所见即所算」——
    # 保存时只把看到的日期原样写回，不再出现「什么都没改、周期却整体漂移」。
    _raw = cfg.get("stocktake_start_md") or cfg.get("stocktake_cycle_anchor") or ""
    cfg["stk_start_date"] = (_stk_dt(_raw) or _stk_start_md_date(_raw)).isoformat()
    return cfg


# ------------------------------------------------------------------
# 数据库自动备份（后台线程按间隔检查，任意一台运行软件的电脑都会自动备份到共享盘）
# ------------------------------------------------------------------
def get_backup(conn=None):
    """读取数据库自动备份配置（sys_settings key=backup）"""
    cfg = dict(config.DEFAULT_BACKUP)
    if not db_configured():
        return cfg
    own = conn is None
    if own:
        conn = get_db()
    try:
        raw = db.get_setting(conn, "backup")
        if raw:
            try:
                d = json.loads(raw)
                if isinstance(d, dict):
                    for k in cfg:
                        if d.get(k) is not None:
                            cfg[k] = d[k]
            except Exception:
                pass
    finally:
        if own:
            conn.close()
    return cfg


def _backup_cfg(conn):
    """读取并做边界校验的备份配置"""
    cfg = get_backup(conn)
    try:
        cfg["enabled"] = 1 if int(cfg.get("enabled")) else 0
    except (TypeError, ValueError):
        cfg["enabled"] = 0
    try:
        cfg["interval_hours"] = max(1, min(int(cfg.get("interval_hours") or 24), 24 * 30))
    except (TypeError, ValueError):
        cfg["interval_hours"] = 24
    try:
        cfg["keep"] = max(1, min(int(cfg.get("keep") or 10), 99))
    except (TypeError, ValueError):
        cfg["keep"] = 10
    cfg["dir"] = (str(cfg.get("dir") or "")).strip()
    return cfg


def _backup_target_dir(cfg):
    return cfg["dir"] or os.path.join(os.path.dirname(config.DB_PATH), "backups")


def _do_backup(cfg=None):
    """执行一次备份：返回备份文件绝对路径"""
    if cfg is None:
        conn = get_db()
        try:
            cfg = _backup_cfg(conn)
        finally:
            conn.close()
    bdir = _backup_target_dir(cfg)
    if not os.path.exists(bdir):
        os.makedirs(bdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(config.DB_PATH))[0]
    target = os.path.join(bdir, f"{base}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
    db.backup_database(target)
    # v1.0.26：备份产出必须自检（SQLite 文件头 + quick_check）。
    # 坏文件不能留在 backups/ 里冒充可用备份，更不能在恢复时盖到生产库上。
    try:
        import share_guard
        _ok_b, _why = share_guard.verify_backup_file(target)
        if not _ok_b:
            logging.getLogger("restore").error("备份文件校验失败，已删除：%s（%s）", target, _why)
            try:
                os.remove(target)
            except OSError:
                pass
            raise OSError("备份文件校验失败：%s" % _why)
    except ImportError:
        pass
    # 清理：只保留最近 keep 份
    try:
        files = [f for f in os.listdir(bdir) if f.startswith(base + "_backup_") and f.endswith(".db")]
        full = [(os.path.join(bdir, f), os.path.getmtime(os.path.join(bdir, f))) for f in files]
        full.sort(key=lambda x: x[1], reverse=True)
        for fp, _ in full[cfg["keep"]:]:
            try:
                os.remove(fp)
            except OSError:
                pass
    except OSError:
        pass
    return target


def _maybe_auto_backup():
    """后台线程每次调用：检查是否到期，到期则备份（多台电脑运行时，任何一台到期都会备份）"""
    if not config.DB_PATH or not os.path.exists(config.DB_PATH):
        return
    conn = get_db()
    try:
        cfg = _backup_cfg(conn)
    except Exception:
        return
    finally:
        conn.close()
    if not cfg["enabled"]:
        return
    bdir = _backup_target_dir(cfg)
    if not os.path.exists(bdir):
        try:
            os.makedirs(bdir, exist_ok=True)
        except OSError:
            return
    base = os.path.splitext(os.path.basename(config.DB_PATH))[0]
    newest = 0.0
    try:
        for f in os.listdir(bdir):
            if f.startswith(base + "_backup_") and f.endswith(".db"):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(bdir, f)))
                except OSError:
                    pass
    except OSError:
        return
    if time.time() - newest < cfg["interval_hours"] * 3600:
        return
    # v1.0.26：100 台各自跑 60s 轮询线程，到期时会同时往同一个 backups/
    # 写、互相踩 keep 保留份数。用 backup 资源锁选主，只有抢到的那一台
    # 执行本轮备份（与业务 write 锁分开，不会阻塞出入库）。
    try:
        import share_guard
        if share_guard.enabled():
            try:
                _h = share_guard.acquire("backup", wait=0.5)
            except share_guard.LockBusy:
                return
            try:
                _do_backup(cfg)
            finally:
                share_guard.release("backup", _h)
        else:
            _do_backup(cfg)
    except Exception as e:
        log.warning("自动备份失败：%s", e)


def _backup_worker():
    """后台守护线程：每 60 秒检查一次是否需要自动备份"""
    while True:
        try:
            _maybe_auto_backup()
        except Exception:
            pass
        time.sleep(60)


def _persist_backup_cfg():
    """把备份表单落库，返回规范化后的配置。

    单独拆出来是因为「立即备份一次」也要先存设置：用户刚把目录改成
    D:\\bak 就点立即备份，按老写法备份还落在旧目录里，等于白改。
    """
    try:
        interval = max(1, min(_int(request.form.get("backup_interval_hours", 24) or 24, 24), 24 * 30))
    except (TypeError, ValueError):
        interval = 24
    try:
        keep = max(1, min(_int(request.form.get("backup_keep", 10) or 10, 10), 99))
    except (TypeError, ValueError):
        keep = 10
    cfg = {
        "enabled": 1 if request.form.get("backup_enabled") else 0,
        "interval_hours": interval,
        "keep": keep,
        "dir": request.form.get("backup_dir", "").strip(),
    }
    conn = get_db()
    try:
        db.set_setting(conn, "backup", json.dumps(cfg, ensure_ascii=False))
        conn.commit()
    finally:
        conn.close()
    return cfg


def _save_backup(endpoint=None):
    """保存自动备份设置"""
    _persist_backup_cfg()
    return _settings_reply("自动备份设置已保存", "success", "card-backup", endpoint=endpoint)


def _backup_now(endpoint=None, save_form=False):
    """立即备份一次（仅 SQLite 支持文件级备份）"""
    if config.DB_TYPE != "sqlite":
        return _settings_reply(_xl("备份/恢复仅支持 SQLite 数据库；{type} 请使用数据库自带备份工具",
                                   type=config.DB_TYPE.upper()), "danger", "card-backup", endpoint=endpoint)
    if not config.DB_PATH or not os.path.exists(config.DB_PATH):
        return _settings_reply("数据库未配置或文件不存在，无法备份", "danger", "card-backup", endpoint=endpoint)
    if save_form and request.form.get("backup_interval_hours") is not None:
        cfg = _persist_backup_cfg()      # 先存设置，再按新目录备份
    else:
        conn = get_db()
        try:
            cfg = _backup_cfg(conn)
        finally:
            conn.close()
    try:
        target = _do_backup(cfg)
    except Exception as e:
        return _settings_reply(_xl("备份失败：{err}", err=e), "danger", "card-backup", endpoint=endpoint)
    backups = _list_backups()
    return _settings_reply(_xl("备份完成：{name}", name=os.path.basename(target)), "success", "card-backup",
                           endpoint=endpoint,
                           backups_html=render_template("_backups.html", backups=backups),
                           backups=backups)


def _list_backups():
    """列出当前备份文件（名称/大小/时间，最新的在前）；仅 SQLite"""
    if config.DB_TYPE != "sqlite" or not config.DB_PATH:
        return []
    conn = get_db()
    try:
        cfg = _backup_cfg(conn)
    finally:
        conn.close()
    bdir = _backup_target_dir(cfg)
    if not os.path.isdir(bdir):
        return []
    base = os.path.splitext(os.path.basename(config.DB_PATH))[0]
    files = [f for f in os.listdir(bdir) if f.startswith(base + "_backup_") and f.endswith(".db")]
    rows = []
    for f in sorted(files, reverse=True):
        fp = os.path.join(bdir, f)
        try:
            st = os.stat(fp)
            rows.append({
                "name": f,
                "dir": bdir,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
        except OSError:
            pass
    return rows


def _backup_delete(endpoint=None):
    """删除指定的备份文件"""
    name = os.path.basename(request.form.get("backup_file", "").strip() or "")
    if not name:
        return _settings_reply("未指定备份文件", "danger", "card-backup", endpoint=endpoint)
    conn = get_db()
    try:
        cfg = _backup_cfg(conn)
    finally:
        conn.close()
    bdir = _backup_target_dir(cfg)
    base = os.path.splitext(os.path.basename(config.DB_PATH))[0]
    if not name.startswith(base + "_backup_") or not name.endswith(".db"):
        return _settings_reply("非法的备份文件名", "danger", "card-backup", endpoint=endpoint)
    fp = os.path.join(bdir, name)
    try:
        if os.path.exists(fp):
            os.remove(fp)
            msg, kind = _xl("备份已删除：{name}", name=name), "success"
        else:
            msg, kind = _xl("备份文件不存在"), "warning"
    except OSError as e:
        return _settings_reply(_xl("删除失败：{err}", err=e), "danger", "card-backup", endpoint=endpoint)
    # 这里原来没有 return —— 删完备份直接掉进「返回 None」，
    # Flask 抛 TypeError，页面上就是 Internal Server Error。
    return _settings_reply(msg, kind, "card-backup", endpoint=endpoint,
                           backups_html=render_template("_backups.html", backups=_list_backups()))


def _local_ip():
    """获取本机局域网 IP（用于部署提示）"""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _save_deploy():
    """保存局域网部署设置并自动重启服务（保存到 settings.json 后拉起新进程）"""
    lan = 1 if request.form.get("lan_enabled") else 0
    try:
        port = _int(request.form.get("lan_port", 5000) or 5000, 5000)
    except (TypeError, ValueError):
        port = 5000
    if not 1024 <= port <= 65535:
        port = 5000
    cfg = _settings_cfg()
    cfg["lan_enabled"] = bool(lan)
    cfg["port"] = port
    _settings.save(cfg)
    config.apply_settings(cfg)
    # 开机自启：Windows下写入注册表
    try:
        import sys
        if sys.platform == "win32":
            import winreg
            start_bat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Start.bat")
            run_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            if lan:
                winreg.SetValueEx(run_key, "SPMS", 0, winreg.REG_SZ, f'cmd /c "{start_bat}"')
                # 自动加防火墙放行规则
                import subprocess
                subprocess.run(f'netsh advfirewall firewall add rule name="SPMS" dir=in action=allow protocol=TCP localport={port}', shell=True, capture_output=True)
            else:
                try:
                    winreg.DeleteValue(run_key, "SPMS")
                except FileNotFoundError:
                    pass
            winreg.CloseKey(run_key)
    except Exception as e:
        log.warning("设置开机自启失败：%s", e)
    # 自动重启：交给启动器的循环来做（见 _restart_now 的说明）。
    # 以前这里是「Popen 子进程 + os._exit(0)」：父进程还占着端口时子进程就起来了，
    # 子进程一看端口被占，直接开个浏览器就退出 —— 于是「保存部署设置」的实际效果
    # 常常是服务停了。改成先退出、由启动器再拉起，端口天然释放，顺序不会错。
    open_url = "http://%s:%s" % (_local_ip() if lan else "127.0.0.1", port)
    _set_open_on_restart(open_url)
    _restart_self()
    _msg = _i18n.t("部署设置已保存：其他电脑可通过 http://{0}:{1} 访问（正在重启服务…）",
                   _i18n.current_lang()).format(_local_ip() if lan else "127.0.0.1", port)
    return _settings_reply(_msg, "success", "card-lan", restart=True, url=open_url)


# 辅助
# ------------------------------------------------------------------
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0


def _int(v, default=0):
    """安全整数解析：表单/JSON/查询串里拿到的值一个都不信。

    直接用 int() 解析用户输入会把 "1 OR 1=1"、"" 这类值炸成 500（异常路径还会把
    源码位置写进日志），所以所有请求参数一律走这里，解析不出来就给默认值。
    """
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v) if v == v and abs(v) != float("inf") else default
        s = str(v).strip()
        if not s:
            return default
        return int(float(s)) if ("." in s or "e" in s.lower()) else int(s)
    except (TypeError, ValueError):
        return default


def _num_strict(v):
    """严格数字解析：失败返回 None（用于导入覆盖模式的数据验证）"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _round_int(v, default=0):
    """四舍五入取整（半数进位），与 SQLite 的 ROUND() 同向。

    不能用内置 round()：它是「银行家舍入」（round-half-even），5000.5→5000、
    5001.5→5002，和 Excel / 用户直觉不一致；导入时按 Excel 填的数取整会莫名少 1。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f == float("inf") or f == float("-inf"):
        return default
    return int(math.floor(f + 0.5)) if f >= 0 else -int(math.floor(-f + 0.5))


def _fmt_qty(v):
    """数量提示用：整数不带小数点，小数保留有效位（不用 %g，5000.332 会被截成 5000.33）"""
    f = _num_finite(v)
    if float(f).is_integer():
        return "%d" % f
    return ("%.6f" % f).rstrip("0").rstrip(".")


def _fmt_qty_s(v):
    """列表里的库存数量：最多两位小数，整数不带 .0（5000.0 显示成 5000）"""
    f = round(_num_finite(v), 2)
    if float(f).is_integer():
        return "%d" % f
    return ("%.2f" % f).rstrip("0").rstrip(".")


def _qdec(v):
    """数量类型解析：填 float=小数；不填或填 int=整数（兼容旧写法 小数/是/1… 与 整数/否/0…）"""
    s = str(v or "").strip().lower()
    if s in ("float", "小数", "是", "1", "y", "yes", "true", "真"):
        return 1
    return 0  # int / integer / 整数 / 否 / 0 / false / 空 → 整数


def _finite(v):
    """数值有限性：挡住 NaN / ±Infinity。
    float('nan')、float('inf')、float('1e309') 都是合法 float，
    但它们既不满足 `<= 0` 也不满足 `> 0`，会把所有数量校验悄悄放过去：
    nan 落到 SQLite 变 NULL（触发 NOT NULL 报错），inf 直接把库存写成 ∞，
    该库位之后再出库就是 inf-inf=nan，整行永久报废。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and f != float("inf") and f != float("-inf")


def _num_finite(v, default=0.0):
    """解析数字并挡掉非有限值（NaN/Inf），用于单价、安全库存等所有数值字段"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f == float("inf") or f == float("-inf"):
        return default
    return f


def _qty_valid(p, q):
    """数量合法性：q 必须是有限数且 >0；整数类物料（个/PCS/卷…）必须为正整数，小数类（米/克/KG…）允许小数"""
    if not _finite(q):
        # 复用已登记词条，避免为一句报错新增 11 语言译文
        return False, _xl("参数错误")
    if float(q) <= 0:
        return False, _xl("数量必须大于 0")
    if not p["qty_decimal"] and not float(q).is_integer():
        return False, _xl("该物料按『{unit}』计数，数量必须为整数（如 10），不能填小数", unit=p['unit'])
    return True, ""


def _dept_manager(conn, applicant):
    """申请人所在部门的经理（role='manager' 且同部门且启用）；无则返回 None"""
    if not applicant["department"]:
        return None
    return conn.execute(
        """SELECT id, display_name FROM users
           WHERE role='manager' AND department=? AND active=1
           ORDER BY id LIMIT 1""",
        (applicant["department"],),
    ).fetchone()




def _stock_out_auto(conn, part_id, loc_key, qty, unit="", out_type=None, batch_out=None):
    """出库扣减：从「首取库位」开始扣，该库位不够时按先进先出跨库位自动补足。

    单据上只填数量，仓管选一个库位只是「先从这里拿」。一个库位装不下整单数量时，
    继续从同物料其他库位（按入库先后）扣减，不再出现「库存够、但单库位不够就出不了库」。
    v1.0.23：跨库位补足时优先消耗「维修品」所在库位（repair_qty>0 或 condition=repair），
    避免修好的件长期积压在库里；用户明确选了首取库位时仍尊重该选择。
    v1.0.24：out_type 传入后，跨库位补足只允许从「该出库类型允许的库位属性」扣。
      以前不区分属性，良品区不够就继续从待维修/待报废区扣不良品，等于把坏件
      当良品领走；现在总库存口径、扣减候选行口径都用同一份属性白名单。
      out_type=None 表示不限制（兼容老调用），但系统内所有调用点均已传值。
    v1.0.28：再叠一层配件状态白名单 —— 只有良品与维修品可走正常出库，不良品
      只能经报废/维修出库。待定(不良品)行 location_id=NULL 会被属性表达式折算成
      good，单纯靠库位属性挡不住，必须同时按 inventory.condition 过滤。
      总库存与候选行两处 SQL 用同一个 _out_cond_where，绝不出现「够数却扣不动」。
    v1.0.30：报废 / 维修若已显式选定首取库位，跨库位补足收窄到「同属性库位」。
      以前从待报废库位起扣、补足阶段却跨回良品区，把良品当报废品 / 送修品扣走；
      _out_cond_error 的「含不良品必须待报废库位」也被这条补足路径绕过。现在选了
      哪类库位就只在同类库位补，不够就明确报库存不足。普通出库（general/return/
      internal/giveback/换/检/借）保持原跨库位行为，状态白名单仍挡住不良品。
    返回 (ok, err, moves)；moves=[(location_text, take)]，按实际扣减顺序。
    """
    qty = float(qty or 0)
    # 未知/空属性按良品处理，避免老库位脏值把库存行整行排除掉
    _attr_expr = ("CASE WHEN l.attribute IN ('pending_repair','pending_scrap') "
                  "THEN l.attribute ELSE 'good' END")
    _attrs = _loc_attrs_allowed(out_type) if out_type is not None else list(_ALL_LOC_ATTRS)
    # v1.0.30：报废 / 维修显式选中首取库位时，补足范围锁定到该库位属性，
    # 防止跨回良品区吃掉良品。loc_key 为 None（老调用/未选库位）时保持原白名单。
    if isinstance(loc_key, int) and out_type in ("scrap", "maintenance"):
        try:
            _first_loc = db.get_location(conn, loc_key)
            _first_attr = (_row_get(_first_loc, "attribute") or "good") if _first_loc else None
            if _first_attr in _attrs:
                _attrs = [_first_attr]
        except Exception:
            pass
    _ph = ",".join("?" for _ in _attrs)
    _conds = list(_out_cond_allow(out_type) or ())
    _cw = _out_cond_where(_conds)
    _join = " FROM inventory i LEFT JOIN locations l ON l.id=i.location_id "
    total = conn.execute(
        "SELECT COALESCE(SUM(i.quantity),0) s" + _join +
        "WHERE i.part_id=? AND " + _attr_expr + " IN (" + _ph + ")" + _cw,
        tuple([part_id] + _attrs + _conds)).fetchone()["s"]
    if float(total) + 1e-9 < qty:
        return False, _xl("库存不足：当前总库存仅 {p1} {p2}，需出库 {p3} {p2}",
                          p1=round(float(total), 3), p2=unit, p3=round(qty, 3)), []
    # v1.0.23：FIFO 开关以前只影响前端批次提示，后端永远按入库日期扣，
    # 设置页写「默认开启」却关不掉；这里让两端口径一致。
    # 关闭时不按批次先后，改按库位文本顺序扣；维修品优先与首取库位不受影响。
    _fifo_on = int(_settings.load().get("fifo_enabled", 1)) == 1
    _order_tail = "i.in_date, i.id" if _fifo_on else "i.location, i.id"
    # v1.0.28「返还」子模式：优先扣借入批次（入库子类型=借入的批次号），
    # 其次维修品，最后先进先出；用户显式选中的首取库位依旧最优先。
    # 只对 giveback 生效，不影响 check_v1023 的「维修品优先」既有护栏。
    _borrow_batches = _borrow_in_batches(conn, part_id) if out_type == "giveback" else []
    _batch_case = ""
    if _borrow_batches:
        _batch_case = ("CASE WHEN i.batch_no IN (" +
                       ",".join("?" for _ in _borrow_batches) + ") THEN 0 ELSE 1 END, ")
    rows = conn.execute(
        "SELECT i.id, i.location_id, i.location, i.quantity, i.batch_no" + _join +
        "WHERE i.part_id=? AND i.quantity>0 AND " + _attr_expr +
        " IN (" + _ph + ")" + _cw +
        " ORDER BY CASE WHEN i.location_id=? THEN 0 ELSE 1 END, " +
        _batch_case +
        " CASE WHEN IFNULL(i.repair_qty,0)>0 OR IFNULL(i.condition,'')='repair'"
        " THEN 0 ELSE 1 END, " + _order_tail,
        tuple([part_id] + _attrs + _conds + [loc_key] + _borrow_batches)).fetchall()
    rem, moves = qty, []
    for r in rows:
        if rem <= 1e-9:
            break
        take = min(float(r["quantity"]), rem)
        if take <= 1e-9:
            continue
        cur = conn.execute(
            "UPDATE inventory SET quantity=quantity-? WHERE id=? AND quantity+1e-9>=?",
            (take, r["id"], take))
        if cur.rowcount != 1:
            return False, _xl("库存已被他人改动，请刷新后重试"), []
        moves.append(((r["location"] or _i18n.t("待定", _i18n.current_lang())), take))
        # v1.0.79：借·换·检·修借出时记下实际扣减批次，供还回时「借保留原批次」追溯。
        # 默认 None，其他调用点零影响；列表与 moves 一一对应。
        if batch_out is not None:
            batch_out.append(str(r["batch_no"] or ""))
        rem -= take
    if rem > 1e-9:
        return False, _xl("库存不足：仍缺 {p1} {p2}", p1=round(rem, 3), p2=unit), []
    return True, "", moves


def _out_loc_text(moves, unit=""):
    """把跨库位扣减结果拼成一句人话：A×33、B×7"""
    def _n(v):
        return str(int(v)) if abs(v - int(v)) < 1e-9 else f"{v:g}"
    if not moves:
        return ""
    if len(moves) == 1:
        return moves[0][0]
    return "、".join(f"{t}×{_n(n)}" for t, n in moves)


def _parse_exec_location(conn, f, part, is_out, out_type=None):
    """解析执行/直接出入库的库位参数
    返回 (loc_key, loc_text, err)：
      loc_key: int=locations.id 传给 move_stock；str=旧文本库位
    出库：必须选择现有库存库位（不允许新库位）
    入库：可现有库位或新库位（位置 id 或文本）
    out_type：出库类型（v1.0.22）；出库时按库位属性限制可选库位——
      待维修库位仅「维修」可出，待报废库位「报废 / 维修」可出（v1.0.23）。
    """
    loc_id = str(f.get("location_id", "") or "").strip()
    # v1.0.27：入库「待定(不良品)」——字面占位，不是真实库位，落库为
    # location_id NULL + location='待定'，由同行 condition=defective 区分。
    # 出库不允许选待定（没有可扣减的库存行）。
    if loc_id in ("PENDING", "__PENDING_DEF__", "待定"):
        if is_out:
            return None, "", _xl("出库请选择该物料有库存的库位")
        # move_stock 的「旧文本库位」路径要求 loc 是字符串；返回 None 会走
        # INSERT inventory(location=NULL)，违反 NOT NULL（v1.0.27 探针实测 500）。
        # 这里直接返回字面占位文本，落库即 location_id NULL + location='待定'。
        return "待定", "待定", ""
    if loc_id and loc_id != "__NEW__":
        try:
            lid = int(loc_id)
        except (TypeError, ValueError):
            return None, "", _xl("库位参数错误")
        loc = db.get_location(conn, lid)
        if not loc:
            return None, "", _xl("所选库位不存在")
        # v1.0.23：出库时按库位属性限制（待维修→仅维修；待报废→报废/维修）
        if is_out and not _loc_out_allowed(_row_get(loc, "attribute"), out_type):
            _at = _row_get(loc, "attribute") or "good"
            if _at == "pending_repair":
                return None, "", _xl("该「待维修」库位库存仅能通过「维修」出库")
            if _at == "pending_scrap":
                return None, "", _xl("该「待报废」库位库存仅能通过「报废」或「维修」出库")
            return None, "", _xl("该库位属性不允许此出库类型")
        # 库位允许多物料共用（inventory 本来就是 part×location 多对多），
        # 不再做「一个位置只放一种物料」的互斥判断——真实货位经常混放。
        return lid, db.location_text(loc), ""
    if is_out:
        return None, "", _xl("出库请选择该物料有库存的库位")
    # 入库新库位：位置 id 优先，其次文本
    new_lid = f.get("new_location_id", "").strip()
    new_txt = f.get("new_location", "").strip()
    if new_lid:
        try:
            nid = int(new_lid)
        except (TypeError, ValueError):
            return None, "", _xl("新库位参数错误")
        loc = db.get_location(conn, nid)
        if not loc:
            return None, "", _xl("新库位不存在，请先在货架管理中建立")
        return nid, db.location_text(loc), ""
    if new_txt:
        return None, new_txt, ""
    return None, "", _xl("请选择库位（入库可选新库位）")


def _xl(text, lang=None, **params):
    """界面固定文案取词：表头、Sheet 名、枚举值、文件名、提示语。

    为什么单独一个函数：导出内容是拼出来的字符串，不走模板也不走 TR()，
    以前从头到尾写死中文，切英文后整张表还是中文。
    只包界面文案，**不要包业务数据**（仓库名/物料名/线体名是用户数据，
    翻了就成了乱码）；查不到词条时原样返回，所以中文环境下零影响。

    **params 是给「带数据的整句」用的**：flash(f"已驳回申请 {no}") 这种
    f-string 拼出来每单都不一样，词条表永远匹配不到，英文界面必漏中文。
    改成 _xl("已驳回申请 {no}", no=...)，整句作为稳定词条登记后才翻得动。
    """
    if not isinstance(text, str) or not text:
        return text
    try:
        return _i18n.t(text, lang or _i18n.current_lang(), **params)
    except Exception:
        return text


# 模板里要拼「带数量」的整句（例如分类字数上限跟着表头设置走），
# 用 {{ xl('最多 {n} 个字符', n=cat_max) }} 而不是 {{ tr('最多') }} {{ n }} {{ tr('个字符') }}
# —— 后者在英文/日文里语序必然拼错。
app.jinja_env.globals["xl"] = _xl


def _hc(text, lang=None):
    """表头单位兜底：把写死的 (元)/(CNY)/(¥) 换成当前币种标注。

    走词条的表头已经被 {cur} 注入过了，这个函数只补救那些没登记词条的老硬编码串
    （导出带图片 Excel 里还留着一批按语言写死的 "Unit Price (CNY)"）。
    只认带括号的形式，「单元」「元件」「復元」这类不会被误伤。
    """
    if not isinstance(text, str) or not text:
        return text
    lab = _i18n.cur_label(lang)
    for a, b in (("(元)", "({c})"), ("（元）", "（{c}）"), ("(CNY)", "({c})"),
                 ("(¥)", "({c})"), ("(￥)", "({c})")):
        if a in text:
            text = text.replace(a, b.format(c=lab))
    return text


def _sheet_name(text):
    """工作表名净化：Excel 禁用字符 `\\ / ? * [ ] :` 一律换成 `-`，最长 31 字。

    踩坑：英文界面「出入库流水」译成 `Stock In/Out Transactions`，斜杠直接
    写进 workbook.xml → Excel/WPS 打开弹修复、openpyxl 读出
    `Invalid character / found in sheet title`。中文词条没斜杠，所以只有
    切英文才炸，属于「看着能用、一导出就坏」的隐蔽问题。
    """
    if not isinstance(text, str):
        text = str(text or "")
    for ch in "\\/?*[]:":
        text = text.replace(ch, "-")
    # 首尾单引号 Excel 也不接受
    text = text.strip().strip("'")[:31].strip()
    return text or "Sheet1"


def _export_xlsx(headers, rows, sheet_name="导出数据", unit_head=True):
    """纯标准库生成 .xlsx（zip+xml，零第三方依赖），返回 BytesIO

    unit_head=False 供导入模板使用：模板表头必须稳定，否则用户填好传回来解析不到列。
    """
    import zipfile
    from io import BytesIO

    _lang = _i18n.current_lang()
    if unit_head:
        headers = [_hc(_xl(h, _lang), _lang) for h in headers]
    else:
        headers = [_xl(h, _lang) for h in headers]
    sheet_name = _sheet_name(_xl(sheet_name, _lang))

    def col_name(i):
        s = ""
        i += 1
        while i:
            i, m = divmod(i - 1, 26)
            s = chr(65 + m) + s
        return s

    xml_esc = lambda v: (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                         .replace('"', "&quot;"))
    # sheet1.xml：表头 + 数据行（标准结构：<row r="N"> 包裹单元格）
    def make_row(r_num, values):
        cells = []
        for c_i, v in enumerate(values):
            r = col_name(c_i) + str(r_num)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cells.append(f'<c r="{r}"><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{r}" t="inlineStr"><is><t>{xml_esc(v)}</t></is></c>')
        return f'<row r="{r_num}">{"".join(cells)}</row>'

    data_rows = [make_row(1, headers)]
    for r_i, row in enumerate(rows, start=2):
        data_rows.append(make_row(r_i, row))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(data_rows)}</sheetData></worksheet>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{xml_esc(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    buf.seek(0)
    return buf


def _export_xlsx_multi(sheets):
    """多 Sheet 导出（纯标准库生成 .xlsx）
    sheets: [(sheet_name, headers, rows), ...]；零第三方依赖"""
    import zipfile
    from io import BytesIO

    _lang = _i18n.current_lang()
    sheets = [(_sheet_name(_xl(sname, _lang)), [_hc(_xl(h, _lang), _lang) for h in headers], rows)
              for sname, headers, rows in sheets]

    def col_name(i):
        s = ""
        i += 1
        while i:
            i, m = divmod(i - 1, 26)
            s = chr(65 + m) + s
        return s

    xml_esc = lambda v: (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                         .replace('"', "&quot;"))

    def make_row(r_num, values):
        cells = []
        for c_i, v in enumerate(values):
            r = col_name(c_i) + str(r_num)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                cells.append(f'<c r="{r}"><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{r}" t="inlineStr"><is><t>{xml_esc(v)}</t></is></c>')
        return f'<row r="{r_num}">{"".join(cells)}</row>'

    sheet_parts, sheet_names = [], []
    for idx, (sname, headers, rows) in enumerate(sheets, start=1):
        data_rows = [make_row(1, headers)]
        for r_i, row in enumerate(rows, start=2):
            data_rows.append(make_row(r_i, row))
        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(data_rows)}</sheetData></worksheet>'
        )
        sheet_parts.append(sheet_xml)
        sheet_names.append((idx, xml_esc(sname)))
    n = len(sheet_parts)

    def _wps():
        parts = []
        for i in range(1, n + 1):
            parts.append(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        return ''.join(parts)

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + _wps() +
        '</Types>'
    )
    wb_sheets = ''.join(
        f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i, name in sheet_names)
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{wb_sheets}</sheets></workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + ''.join(
            f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, n + 1)) +
        '</Relationships>'
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        ))
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        for i in range(1, n + 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", sheet_parts[i - 1])
    buf.seek(0)
    return buf


# ------------------------------------------------------------------
# 带样式的 xlsx（盘点报告导出用）
# ------------------------------------------------------------------
# _export_xlsx / _export_xlsx_multi 只写裸数据：没有抬头、没有分区标题、
# 没有边框、数字不带千分位。导出的「盘点报告」拿给主管看，跟系统里那页纸
# 完全不像，还得自己重新排版。这里补一套最小可用的样式层：
#   fonts/fills/borders/cellXfs + 列宽 + 合并单元格 + 行高 + 横向打印
# 样式索引写死在 _XLSX_XF 里，单元格只需按名字取，改配色只改这一段。
_XLSX_XF = {
    # name        numFmt  font  fill  border  align     wrap
    "def":    {"nf": 0,   "f": 0, "fill": 0, "b": 0, "al": "left",   "wrap": 0},
    "title":  {"nf": 0,   "f": 2, "fill": 0, "b": 0, "al": "left",   "wrap": 0},
    "sub":    {"nf": 0,   "f": 3, "fill": 0, "b": 0, "al": "right",  "wrap": 0},
    "sec":    {"nf": 0,   "f": 4, "fill": 2, "b": 1, "al": "left",   "wrap": 0},
    "mlab":   {"nf": 0,   "f": 1, "fill": 3, "b": 1, "al": "left",   "wrap": 0},
    "mval":   {"nf": 0,   "f": 0, "fill": 0, "b": 1, "al": "left",   "wrap": 0},
    "th":     {"nf": 0,   "f": 4, "fill": 2, "b": 1, "al": "center", "wrap": 1},
    "td":     {"nf": 0,   "f": 0, "fill": 0, "b": 1, "al": "left",   "wrap": 1},
    "tdc":    {"nf": 0,   "f": 0, "fill": 0, "b": 1, "al": "center", "wrap": 0},
    "num2":   {"nf": 176, "f": 0, "fill": 0, "b": 1, "al": "right",  "wrap": 0},
    "numg":   {"nf": 177, "f": 0, "fill": 0, "b": 1, "al": "right",  "wrap": 0},
    "pos":    {"nf": 177, "f": 5, "fill": 0, "b": 1, "al": "right",  "wrap": 0},
    "neg":    {"nf": 177, "f": 6, "fill": 0, "b": 1, "al": "right",  "wrap": 0},
    # 金额列要两位小数（数量列用上面两个：11 而不是 11.00）
    "pos2":   {"nf": 176, "f": 5, "fill": 0, "b": 1, "al": "right",  "wrap": 0},
    "neg2":   {"nf": 176, "f": 6, "fill": 0, "b": 1, "al": "right",  "wrap": 0},
    "totl":   {"nf": 0,   "f": 1, "fill": 3, "b": 1, "al": "right",  "wrap": 0},
    "totg":   {"nf": 177, "f": 1, "fill": 3, "b": 1, "al": "right",  "wrap": 0},
    "tot2":   {"nf": 176, "f": 1, "fill": 3, "b": 1, "al": "right",  "wrap": 0},
    "statl":  {"nf": 0,   "f": 1, "fill": 4, "b": 1, "al": "center", "wrap": 0},
    "statn":  {"nf": 177, "f": 7, "fill": 4, "b": 1, "al": "center", "wrap": 0},
    "stat2":  {"nf": 176, "f": 7, "fill": 4, "b": 1, "al": "center", "wrap": 0},
    "stats":  {"nf": 0,   "f": 8, "fill": 4, "b": 1, "al": "center", "wrap": 0},
    "box":    {"nf": 0,   "f": 0, "fill": 0, "b": 1, "al": "left",   "wrap": 1},
    "sign":   {"nf": 0,   "f": 1, "fill": 3, "b": 1, "al": "center", "wrap": 0},
    "blank":  {"nf": 0,   "f": 0, "fill": 0, "b": 1, "al": "left",   "wrap": 0},
    "val":    {"nf": 0,   "f": 9, "fill": 0, "b": 1, "al": "center", "wrap": 0},
}
_XLSX_XF_ORDER = list(_XLSX_XF)
_XLSX_XF_IDX = {name: i for i, name in enumerate(_XLSX_XF_ORDER)}


def _xlsx_styles_xml():
    """styles.xml：9 个字体 / 5 个填充 / 2 种边框 / 24 个单元格样式。

    数字格式用自定义号段（176 金额两位小数、177 数量不补零），
    避免复用内置号段时不同 Excel 版本解释不一致。
    """
    fonts = (
        '<font><sz val="10"/><color theme="1"/><name val="Microsoft YaHei"/></font>'
        '<font><b/><sz val="10"/><color rgb="FF1F2B3D"/><name val="Microsoft YaHei"/></font>'
        '<font><b/><sz val="18"/><color rgb="FF0F2C4C"/><name val="Microsoft YaHei"/></font>'
        '<font><sz val="9"/><color rgb="FF64748B"/><name val="Microsoft YaHei"/></font>'
        '<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Microsoft YaHei"/></font>'
        '<font><sz val="10"/><color rgb="FF15803D"/><name val="Microsoft YaHei"/></font>'
        '<font><sz val="10"/><color rgb="FFB91C1C"/><name val="Microsoft YaHei"/></font>'
        '<font><b/><sz val="14"/><color rgb="FF143A6B"/><name val="Microsoft YaHei"/></font>'
        '<font><sz val="9"/><color rgb="FF334155"/><name val="Microsoft YaHei"/></font>'
        '<font><b/><sz val="10"/><color rgb="FFB45309"/><name val="Microsoft YaHei"/></font>'
    )
    fills = (
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1F3B57"/>'
        '<bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFEEF2F7"/>'
        '<bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFE8F0F8"/>'
        '<bgColor indexed="64"/></patternFill></fill>'
    )
    thin = ('<left style="thin"><color rgb="FFCBD5E1"/></left>'
            '<right style="thin"><color rgb="FFCBD5E1"/></right>'
            '<top style="thin"><color rgb="FFCBD5E1"/></top>'
            '<bottom style="thin"><color rgb="FFCBD5E1"/></bottom><diagonal/>')
    borders = ('<border><left/><right/><top/><bottom/><diagonal/></border>'
               '<border>' + thin + '</border>')

    xfs = []
    for name in _XLSX_XF_ORDER:
        c = _XLSX_XF[name]
        attrs = ('numFmtId="%d" fontId="%d" fillId="%d" borderId="%d" xfId="0"'
                 % (c["nf"], c["f"], c["fill"], c["b"]))
        if c["nf"]:
            attrs += ' applyNumberFormat="1"'
        if c["f"]:
            attrs += ' applyFont="1"'
        if c["fill"]:
            attrs += ' applyFill="1"'
        if c["b"]:
            attrs += ' applyBorder="1"'
        if c["al"] != "left" or c["wrap"] or c["b"]:
            al = '<alignment horizontal="%s" vertical="%s"%s/>' % (
                c["al"], "top" if c["wrap"] and c["al"] == "left" else "center",
                ' wrapText="1"' if c["wrap"] else "")
            attrs += ' applyAlignment="1">' + al + '</xf>'
        else:
            attrs += '/>'
        xfs.append('<xf ' + attrs)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="2"><numFmt numFmtId="176" formatCode="#,##0.00"/>'
        '<numFmt numFmtId="177" formatCode="#,##0.###"/></numFmts>'
        '<fonts count="10">' + fonts + '</fonts>'
        '<fills count="5">' + fills + '</fills>'
        '<borders count="2">' + borders + '</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="%d">%s</cellXfs>' % (len(xfs), "".join(xfs)) +
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )


def _xlsx_styled(sheets):
    """带样式的多 Sheet 导出（纯标准库，零第三方依赖）。

    sheets: [{
        "name":   工作表名,
        "cols":   [列宽, ...],
        "rows":   [{"h": 行高(可选), "cells": [(值, 样式名), ...]}, ...],
        "merges": ["A1:L1", ...]（可选）,
        "print":  True → 横向 + 缩放到一页宽（打印友好，可选）,
        "grid":   False → 隐藏网格线（报告页像纸，不想看见格子）,
    }]
    样式名见 _XLSX_XF；数字单元格自动按样式里的 numFmt 显示。
    """
    import zipfile
    from io import BytesIO

    def col_name(i):
        s = ""
        i += 1
        while i:
            i, m = divmod(i - 1, 26)
            s = chr(65 + m) + s
        return s

    def esc(v):
        return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    def numtxt(v):
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return repr(v) if isinstance(v, float) else str(v)

    def cell_xml(ref, val, sname):
        sidx = _XLSX_XF_IDX.get(sname, 0)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, sidx, numtxt(val))
        txt = "" if val is None else str(val)
        if not txt:
            return '<c r="%s" s="%d"/>' % (ref, sidx)
        return ('<c r="%s" s="%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                % (ref, sidx, esc(txt)))

    parts, names = [], []
    for idx, sh in enumerate(sheets, start=1):
        rows = []
        for r_i, row in enumerate(sh.get("rows") or [], start=1):
            # 行可以是 {"cells":..,"h":..}，也可以直接给 cells 列表（不需要自定义行高时）
            if isinstance(row, dict):
                cells_src, ht = row.get("cells") or [], row.get("h")
            else:
                cells_src, ht = row or [], None
            cells = []
            for c_i, item in enumerate(cells_src):
                sname = "def"
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    val, sname = item
                else:
                    val = item
                cells.append(cell_xml(col_name(c_i) + str(r_i), val, sname))
            attr = (' ht="%s" customHeight="1"' % ht) if ht else ""
            rows.append('<row r="%d"%s>%s</row>' % (r_i, attr, "".join(cells)))
        cols_xml = ""
        widths = sh.get("cols") or []
        if widths:
            cols_xml = "<cols>" + "".join(
                '<col min="%d" max="%d" width="%s" customWidth="1"/>' % (i + 1, i + 1, w)
                for i, w in enumerate(widths)) + "</cols>"
        merges = sh.get("merges") or []
        merge_xml = ""
        if merges:
            merge_xml = ('<mergeCells count="%d">%s</mergeCells>'
                         % (len(merges), "".join('<mergeCell ref="%s"/>' % m for m in merges)))
        view = ('<sheetViews><sheetView workbookViewId="0"%s/></sheetViews>'
                % ('' if sh.get("grid", True) else ' showGridLines="0"'))
        setup = ""
        if sh.get("print"):
            setup = ('<printOptions horizontalCentered="0"/>'
                     '<pageMargins left="0.4" right="0.4" top="0.5" bottom="0.5" '
                     'header="0.3" footer="0.3"/>'
                     '<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" '
                     'fitToHeight="0"/>')
        parts.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            + ('<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>' if sh.get("print") else "")
            + view + '<sheetFormatPr defaultRowHeight="15"/>' + cols_xml
            + '<sheetData>' + "".join(rows) + '</sheetData>' + merge_xml + setup
            + '</worksheet>')
        names.append((idx, esc(_sheet_name(sh.get("name") or ("Sheet%d" % idx)))))

    n = len(parts)
    overrides = "".join(
        '<Override PartName="/xl/worksheets/sheet%d.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        % i for i in range(1, n + 1))
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + overrides + '</Types>')
    wb_sheets = "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (nm, i, i)
                        for i, nm in names)
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>' + wb_sheets + '</sheets></workbook>')
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                  'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>'
                  % (i, i) for i in range(1, n + 1))
        + '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
          'officeDocument/2006/relationships/styles" Target="styles.xml"/>' % (n + 1)
        + '</Relationships>')
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'))
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", _xlsx_styles_xml())
        for i in range(1, n + 1):
            z.writestr("xl/worksheets/sheet%d.xml" % i, parts[i - 1])
    buf.seek(0)
    return buf


# ------------------------------------------------------------------
# 启动入口（桌面直连版）
# ------------------------------------------------------------------
def setup_logging():
    """日志写入软件目录 logs/app.log（打包成无窗口 exe 后仍可排查问题）"""
    log_dir = os.path.join(_settings.APP_DIR, "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        log_dir = os.path.join(os.path.expanduser("~"), "库存管理系统", "logs")
        os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        filename=os.path.join(log_dir, "app.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    # 控制台（有窗口时）同步输出
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    logging.getLogger().addHandler(console)


def find_free_port(prefer):
    """端口被占用时自动向后找空闲端口"""
    import socket as _sock
    for p in range(prefer, prefer + 10):
        with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return prefer


def is_port_in_use(port):
    import socket as _sock
    with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    # v1.0.26：原先这里的 db_bad 是局部变量（缺 global），模块级 db_bad 永远为
    # False，导致 _db_guard 的「启动失败跳配置页」守卫从不生效、所有碰库请求 500。
    global db_bad
    setup_logging()
    log = logging.getLogger("main")
    # 若本机已有实例在运行，直接打开浏览器并退出（避免重复启动）
    if is_port_in_use(config.PORT):
        log.info("检测到本机端口 %s 已有实例在运行，直接打开浏览器", config.PORT)
        webbrowser.open(f"http://127.0.0.1:{config.PORT}")
        return
    port = find_free_port(config.PORT)
    db_bad = False
    if db_configured():
        try:
            db.init_db()
            log.info("数据库就绪：%s", config.DB_PATH)
        except Exception as e:
            log.error("数据库初始化失败：%s，将自动跳转到配置页修复", e)
            db_bad = True
    else:
        log.info("数据库未配置，进入初始化向导模式")
    # 延迟 0.8s 等服务起来再打开浏览器（测试环境可设 SMT_NO_BROWSER=1 关闭自动打开）
    # 若是「重启」而非新开（恢复库/改端口），地址可能已经不是用户手上那个页签了，
    # 这种情况以标记文件里的地址为准，且不受 SMT_NO_BROWSER 影响。
    _open_url = _take_open_on_restart()
    if _open_url:
        import threading as _th
        _th.Timer(0.8, lambda: webbrowser.open(_open_url)).start()
    elif not os.environ.get("SMT_NO_BROWSER"):
        import threading as _th
        _th.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    # v1.0.26：共享盘可达性探针。本地单机 / 服务器库（MySQL 等）下
    # start_probe() 直接返回 False，不启动任何线程、零开销。
    try:
        import share_guard
        share_guard.start_probe()
    except Exception as _e:  # noqa: BLE001
        log.warning("共享盘探针未启动（不影响本地部署）：%s", _e)
    # 数据库自动备份后台线程（按系统设置中的间隔自动备份到共享盘/指定目录）
    threading.Thread(target=_backup_worker, daemon=True).start()
    # 回收站自动清理后台线程（超保留期物料自动彻底清除）
    threading.Thread(target=_recycle_worker, daemon=True).start()
    # 审批超时催办后台线程
    threading.Thread(target=_remind_worker, daemon=True).start()
    # 待出库超时自动取消后台线程
    threading.Thread(target=_pending_release_worker, daemon=True).start()
    if config.LAN_ENABLED:
        # 局域网部署：监听所有网卡，其他电脑通过 http://本机IP:端口 访问
        host = "0.0.0.0"
        log.info("库存管理系统启动（局域网部署）：http://0.0.0.0:%s", port)
    else:
        host = "127.0.0.1"
        log.info("库存管理系统启动：http://127.0.0.1:%s", port)
    app.run(host=host, port=port, debug=False, threaded=True)


# ===================== 工具借用（borrows） =====================
def _parse_dt(s):
    """解析借用时间字段（兼容 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD'）。"""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _borrow_in_use(conn, part_id, exclude_bid=None):
    """v1.0.19：已废弃的「在借占用可用量」口径，恒返回 0。

    旧版本借·换·检·修不扣库存，靠这里把在借量从可用量里减掉防超领。现在办理时
    已实扣 inventory，若再减一次就是双重扣减。保留函数与签名是为了兼容 7 个历史
    调用点（都写成 stock - locked - in_use），让改动集中在一处、不漏改任何一条链。
    台账页的「在借数量」展示请直接查 borrows，不要走这里。
    """
    return 0.0


def _borrow_available(conn, part_id, exclude_bid=None, exclude_app=None):
    """v1.0.19 借用/领用共用口径：可用 = 真实库存 − 已提交未执行的申请锁定量。

    exclude_app：执行某张申请单时要把它自己排除，否则「库存 3、申请 3」会因为
    自己锁自己算成可用 0，永远办不出去。
    返回 (可用量, 兼容占位 0, 总库存)；第二项保留给旧报错文案，恒为 0。
    """
    stock = float(db.total_stock(conn, part_id) or 0)
    in_use = _borrow_in_use(conn, part_id, exclude_bid)
    locked = 0.0
    try:
        # 锁定口径：出库申请照旧（贵重=批准后锁定，普通=提交即锁定）；
        # 借用申请没有审批环节，提交即锁定（等仓库办理借出），否则等待期间会被领走
        sql = """SELECT COALESCE(SUM(a.quantity),0) s FROM applications a
               JOIN parts pp ON pp.id=a.part_id
               WHERE a.part_id=? AND (
                 (a.app_type='out' AND ((pp.is_valuable=1 AND a.status='approved')
                    OR ((pp.is_valuable=0 AND a.status='pending') OR (a.app_type='out' AND IFNULL(a.out_type,'') IN ('return','exchange','internal','giveback','maintenance','verify') AND a.status IN ('pending','pending2','approved')))))
                 OR (a.app_type='borrow' AND a.status IN ('pending','approved'))
               )"""
        args = [part_id]
        if exclude_app:
            sql += " AND a.id<>?"
            args.append(exclude_app)
        row = conn.execute(sql, args).fetchone()
        locked = float(row["s"] or 0) if row else 0.0
    except Exception:
        pass
    return stock - in_use - locked, in_use, stock


def _borrow_stock_out(conn, part_id, qty, loc_key, unit, user, br_no, kind_name="", out_type=None):
    """v1.0.19：借·换·检·修办理出库 —— 实扣库存 + 写出库流水。

    走 _stock_out_auto（首取库位 + 跨库位 FIFO 补足），保证与普通出库同一套
    维修品优先批次与并发安全口径。返回 (ok, err_msg, deducted_qty)。
    失败时调用方必须回滚整单/整批，不能只落台账不扣库存。
    """
    qty = float(qty or 0)
    if qty <= 0:
        return False, _i18n.t("出库数量必须大于 0", _i18n.current_lang()), 0.0
    # v1.0.31：显式选了真实库位时，先按属性白名单校验（与 _parse_exec_location 同源）。
    # 维修只能从待维修/待报废库位出，良品不送修；挡住 batch borrow 链绕过前端的 POST。
    if loc_key and not _is_pending_loc(loc_key, None):
        try:
            _lid = int(loc_key)
        except (TypeError, ValueError):
            _lid = 0
        if _lid:
            _lrow = db.get_location(conn, _lid)
            if _lrow and not _loc_out_allowed(_row_get(_lrow, "attribute"), out_type):
                return False, _i18n.t("不良品只能入「待维修 / 待报废」库位",
                                      _i18n.current_lang()), 0.0
    # v1.0.79：收集实际扣减批次，写进借出流水；还回时据此保留原批次（仅「借」）。
    _out_batches = []
    ok, msg, moves = _stock_out_auto(conn, part_id, loc_key, qty, unit, out_type,
                                     batch_out=_out_batches)
    if not ok:
        return False, msg, 0.0
    loc_txt = _out_loc_text(moves, unit) or (moves[0][0] if moves else "")
    # 跨库位借出可能扣多个批次；台账只记一条，取按扣减顺序的第一个非空批次。
    _out_batch = next((b for b in _out_batches if b), "")
    p = conn.execute("SELECT unit_price FROM parts WHERE id=?", (part_id,)).fetchone()
    price = float(p["unit_price"] or 0) if p else 0.0
    conn.execute(
        """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
           ref_type,operator_id,remark,batch_no,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (db.gen_no("TX", conn, "transactions", "tx_no"), part_id, "out", qty,
         price, round(qty * price, 2), loc_txt, "borrow",
         user["id"] if user else None,
         _xl("借·换·检·修办理出库 {no}（{kind}）", no=br_no,
             kind=kind_name or _i18n.t("借用", _i18n.current_lang())),
         _out_batch, db.now_str()))
    return True, "", qty


def _inv_repair(conn, inv_id):
    """v1.0.21：某条库存行里现有多少件维修品（读不到按 0，绝不抛错打断出库）。"""
    try:
        r = conn.execute("SELECT repair_qty FROM inventory WHERE id=?",
                         (inv_id,)).fetchone()
        return float(r["repair_qty"] or 0) if r else 0.0
    except Exception:
        return 0.0


# v1.0.64：流水备注里的返回类型按当时借·换·检·修子类型显示，
# 不再笼统写「回补」。写库时用 _borrow_return_kind_label()，展示时给历史行补同样的口径。
_BORROW_RETURN_KIND_ZH = {"borrow": "借用", "exchange": "换货",
                          "maintenance": "维修", "verify": "校验/测试"}


def _borrow_return_kind_label(kind, lang=None):
    key = (kind or "").strip()
    zh = _BORROW_RETURN_KIND_ZH.get(key, "借·换·检·修")
    if lang and lang != "zh" and key in _BORROW_RETURN_KIND_ZH:
        return _i18n.t(zh, lang)
    return zh


def _borrow_return_remark_display(conn, row):
    """历史流水展示兼容：把「（回补 N 个）」改为「（借用/换货/维修/校验·测试 返回 N 个）」。

    只改展示副本，不改库里的 remark：单号从备注里取，再回查 borrows.kind。
    同一单号分次还回时类型不变，因此历史行也能正确还原。
    """
    d = dict(row)
    remark = d.get("remark") or ""
    if d.get("ref_type") == "borrow" and "（回补 " in remark:
        # 单号不只有 BRyyyymmdd-xxx（老单据/回归数据还有 _REG 之类），
        # 从固定文案里取「还回入库」与「（回补」之间的整段即可。
        m = re.search(r"还回入库\s+([^\s（]+)\s*（回补 ", remark)
        if m:
            br = conn.execute("SELECT kind FROM borrows WHERE borrow_no=?",
                              (m.group(1),)).fetchone()
            # 台账行被清理的老流水查不到类型：至少把「回补」换成通用返回口径
            label = _borrow_return_kind_label(br["kind"] if br else "")
            d["remark"] = remark.replace("（回补 ", "（%s返回 " % label, 1)
    return d


def _borrow_out_batch(conn, br_no):
    """v1.0.79：按借用单号回查借出流水里实际扣减的批次号。

    borrows 表按 v1.0.28 口径不存批次（真源在 transactions / inventory），
    这里从借出流水 remark 精确匹配单号，取最近一次借出的 batch_no。
    老单据借出时没记批次 → 返回空串，由还回侧生成 RT 批次兜底。
    """
    if not br_no:
        return ""
    try:
        rows = conn.execute(
            "SELECT batch_no, remark FROM transactions "
            "WHERE tx_type='out' AND ref_type='borrow' AND remark LIKE ? "
            "ORDER BY id DESC", ('%' + str(br_no) + '%',)).fetchall()
    except Exception:
        return ""
    for r in rows:
        # 单号后不能紧跟数字，避免 BR...001 命中 BR...0011
        if not re.search(re.escape(str(br_no)) + r'(?!\d)', str(r["remark"] or "")):
            continue
        batch = str(r["batch_no"] or "").strip()
        if batch:
            return batch
    return ""


def _borrow_restock(conn, part_id, qty, location_id, location_text,
                    user, br_no, unit="", kind="", condition="good", repair_ok=0,
                    src_batch=""):
    """v1.0.19：还回时把库存回补到指定库位 + 写入库流水。

    只由 api_borrow_return 调用，回补量已由调用方按 deducted_qty 封顶。
    v1.0.21：维修还回的这批累加 repair_qty，出库时优先从这里扣。
    v1.0.23：只有「已修好」的维修件才打维修状态（condition='repair' + repair_qty），
    未修好的按不良品（condition='defective'）回待维修/待报废区，不参与维修品优先出库。
    v1.0.79：回补必须带批次与入库日期 —— 「借」沿用借出时的原批次，
    「换/检/修」生成 RT+日期+单号 的可追溯新批次；老单据无原批次时同样走 RT 兜底。
    返回 (ok, err_msg)。
    """
    qty = float(qty or 0)
    if qty <= 1e-9:
        return True, ""
    # v1.0.30：还回也是一次入库，必须遵守「库位属性 ↔ 配件状态」矩阵。
    # db.move_stock 只管原子增减、不校验属性，以前借/换/检的良品（condition=good）
    # 能被还进待报废/待维修库位，形成good件落在pending_scrap的脏数据。
    # 服务端在这里补上唯一真源校验；维修件沿用 api_borrow_return 的目标属性前置校验，
    # 这里再用同一函数兜底，双保险。location_id 为空（老单据文本库位）时不拦。
    if location_id:
        try:
            _lr = db.get_location(conn, location_id)
        except Exception:
            _lr = None
        if _lr:
            _attr = _row_get(_lr, "attribute") or "good"
            if not _cond_attr_allowed(condition, _attr, is_pending=False):
                return False, _xl("库位属性 [{p1}] 不允许存放 [{p2}]",
                                  p1=_i18n.t(LOC_ATTR_NAMES.get(_attr, "良品"), _i18n.current_lang()),
                                  p2=_i18n.t(CONDITION_NAMES.get(condition, "良品"), _i18n.current_lang()))
    # v1.0.23：仅「已修好」的维修件才记成维修品（数量跟回补量一致）
    mark_repair = qty if ((kind or "") == "maintenance" and repair_ok) else 0
    # v1.0.79：借还回保留原批次；换/检/修还回是「新入库」，生成 RT 批次。
    # 老单据借出没记批次时也用 RT 兜底，绝不再落 B-LEGACY + 空 in_date。
    _today = datetime.now().strftime("%Y%m%d")
    _src_batch = str(src_batch or "").strip()[:50]
    if (kind or "") == "borrow" and _src_batch:
        ret_batch = _src_batch
    else:
        ret_batch = ("RT" + _today + "-" + str(br_no or "")).strip()[:50]
    ok, msg = db.move_stock(conn, part_id, location_id, qty, batch_no=ret_batch,
                            repair=mark_repair, condition=condition)
    if not ok:
        return False, msg or _i18n.t("库存回补失败，请刷新后重试", _i18n.current_lang())
    p = conn.execute("SELECT unit_price FROM parts WHERE id=?", (part_id,)).fetchone()
    price = float(p["unit_price"] or 0) if p else 0.0
    # v1.0.61：借·换·检·修回补也是一次入库，类型不能再空着落到默认的「采购」。
    # 修(维修)还回且已修好 → repair（返修回库）；借/换/检及未修好的维修件 → misc（其它）。
    _in_kind = "repair" if ((kind or "") == "maintenance" and repair_ok) else "misc"
    conn.execute(
        """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
           ref_type,operator_id,remark,created_at,out_type,batch_no) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (db.gen_no("TX", conn, "transactions", "tx_no"), part_id, "in", qty,
         price, round(qty * price, 2), location_text or "", "borrow",
         user["id"] if user else None,
         _xl("借·换·检·修还回入库 {no}（{kind}返回 {q} {u}）", no=br_no,
             kind=_borrow_return_kind_label(kind, _i18n.current_lang()),
             q=(int(qty) if qty == int(qty) else round(qty, 3)), u=unit or ""),
         db.now_str(), _in_kind, ret_batch))
    return True, ""


def _borrow_color(status, borrowed_at, expected_return, now=None, confirmed_at=None):
    """借用颜色预警：计算不入库，仅展示时算。
    returned → 'done'（灰）；cancelled → 'cancel'（灰）；
    无期限 → 'green'；已超预计归还 → 'red'；达到 80% 期限 → 'yellow'；否则 'green'。
    预警起点优先用仓管确认时间，老数据回退到借出时间。
    （已无 pending「待确认」：借出在出入库页办理时即完成，台账只办还回。）"""
    if status == "returned":
        return "done"
    if status == "cancelled":
        return "cancel"
    ba = _parse_dt(confirmed_at) or _parse_dt(borrowed_at)
    er = _parse_dt(expected_return)
    if not ba or not er:
        return "green"
    now = now or datetime.now()
    if now >= er:
        return "red"
    warn = ba + timedelta(seconds=0.8 * (er - ba).total_seconds())
    if now >= warn:
        return "yellow"
    return "green"


@app.route("/borrows")
@login_required
@perm_required("borrow")
def borrows_page():
    """工具借用管理：台账列表 + 统计 + 发起借用 + 办理还回。"""
    # v1.0.45：维修还回时选择维修品类型（一级类目+二级词条，出厂字典兜底）
    return render_template("borrows.html", repair_cats=get_repair_categories())


@app.route("/api/borrows")
@login_required
@perm_required("borrow")
def api_borrows():
    kw = (request.args.get("kw") or "").strip()
    status = (request.args.get("status") or "").strip()
    kind = (request.args.get("kind") or "").strip()
    if kind not in ("borrow", "exchange", "maintenance", "verify"):
        kind = ""
    borrower = (request.args.get("borrower") or "").strip()
    user = current_user()
    # 「借用管理」才看全量台账；只有「借用权」的角色只能看到本人记录，
    # 否则普通用户能在借用台账里看到所有人借了什么、借到哪个库位。
    can_manage = can(user["role"], "borrow", "manage")
    mine_only = not can_manage
    conn = get_db()
    try:
        sql = (
            "SELECT b.*, COALESCE(u.display_name,'') borrower_name, "
            "COALESCE(o.display_name,'') op_name, COALESCE(ro.display_name,'') return_op_name "
            "FROM borrows b "
            "LEFT JOIN users u ON u.id=b.borrower_user_id "
            "LEFT JOIN users o ON o.id=b.op_id "
            "LEFT JOIN users ro ON ro.id=b.return_op_id WHERE 1=1"
        )
        args = []
        if mine_only:
            sql += " AND b.borrower_user_id=?"
            args.append(user["id"])
        if kind:
            sql += " AND IFNULL(b.kind,'borrow')=?"
            args.append(kind)
        if kw:
            sql += " AND (b.borrow_no LIKE ? OR b.part_code LIKE ? OR b.part_name LIKE ?)"
            args += ["%" + kw + "%"] * 3
        # 统计卡不受「状态」下拉影响：默认只看进行中的单据时，
        # 「已还回」不该跟着变成 0 —— 否则顶部四个数字会自相矛盾。
        _stat_sql = ("SELECT b.status s, b.borrowed_at, b.expected_return, b.confirmed_at "
                     "FROM borrows b WHERE 1=1")
        _stat_args = []
        if mine_only:
            _stat_sql += " AND b.borrower_user_id=?"
            _stat_args.append(user["id"])
        if kind:
            _stat_sql += " AND IFNULL(b.kind,'borrow')=?"
            _stat_args.append(kind)
        if kw:
            _stat_sql += " AND (b.borrow_no LIKE ? OR b.part_code LIKE ? OR b.part_name LIKE ?)"
            _stat_args += ["%" + kw + "%"] * 3
        if borrower and can_manage:
            _stat_sql += " AND (b.borrower LIKE ? OR EXISTS (SELECT 1 FROM users bu "
            _stat_sql += "WHERE bu.id=b.borrower_user_id AND bu.display_name LIKE ?))"
            _stat_args += ["%" + borrower + "%"] * 2
        _stat_rows = conn.execute(_stat_sql, _stat_args).fetchall()
        if status in ("borrowed", "returned", "cancelled"):
            sql += " AND b.status=?"
            args.append(status)
        if borrower and can_manage:
            sql += " AND (b.borrower LIKE ? OR u.display_name LIKE ?)"
            args += ["%" + borrower + "%"] * 2
        # 排序按"待处理优先"：逾期 → 有期限未逾期 → 无固定期限 → 已还回/已驳回。
        sql += (
            " ORDER BY CASE"
            "   WHEN b.status IN ('returned','cancelled') THEN 3"
            "   WHEN b.expected_return<>'' AND b.expected_return<=datetime('now','localtime') THEN 0"
            "   WHEN b.expected_return='' THEN 2"
            "   ELSE 1 END, b.id DESC"
        )
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    now = datetime.now()
    data, stats = [], {"borrowed": 0, "overdue": 0, "warning": 0, "returned": 0}
    for r in rows:
        d = dict(r)
        d["color"] = _borrow_color(d.get("status"), d.get("borrowed_at"), d.get("expected_return"),
                                   now, d.get("confirmed_at"))
        data.append(d)
    # 统计口径独立于列表筛选（见上面的 _stat_sql）
    for r in _stat_rows:
        st0 = r["s"]
        if st0 == "returned":
            stats["returned"] += 1
        elif st0 == "borrowed":
            stats["borrowed"] += 1
            c0 = _borrow_color(st0, r["borrowed_at"], r["expected_return"], now, r["confirmed_at"])
            if c0 == "red":
                stats["overdue"] += 1
            elif c0 == "yellow":
                stats["warning"] += 1
    return jsonify(ok=True, rows=data, stats=stats, mine_only=mine_only,
                   now=now.strftime("%Y-%m-%d %H:%M:%S"))


@app.route("/api/borrows/<int:bid>/return_locations")
@login_required
@perm_required("borrow", "return")
def api_borrow_return_locations(bid):
    """办理还回时可选的目标库位：原库位排第一并默认选中。

    候选顺序：原库位 → 该物料现有的库位（含数量）→ 其他库位（物料没有库位时兜底，
    否则会出现「借出去的物料一个库位都选不了」）。"""
    conn = get_db()
    try:
        b = conn.execute(
            "SELECT id, part_id, qty, unit, location, location_id, kind, "
            "COALESCE(returned_qty,0) returned_qty FROM borrows WHERE id=?",
            (bid,)).fetchone()
        if not b:
            return jsonify(ok=False, msg=_i18n.t("借用记录不存在", _i18n.current_lang())), 404
        rows = conn.execute(
            """SELECT i.location_id, COALESCE(SUM(i.quantity),0) qty
               FROM inventory i WHERE i.part_id=? AND i.location_id IS NOT NULL
               GROUP BY i.location_id""", (b["part_id"],)).fetchall()
        opts, seen = [], set()
        for r in rows:
            loc = db.get_location(conn, r["location_id"])
            if not loc:
                continue
            opts.append({"id": r["location_id"], "text": db.location_text(loc),
                         "qty": float(r["qty"] or 0)})
            seen.add(int(r["location_id"]))
        # 原库位（老单据只存了文本，没存 id）
        origin_id = _int(b["location_id"], 0)
        if origin_id and origin_id not in seen:
            loc = db.get_location(conn, origin_id)
            if loc:
                opts.insert(0, {"id": origin_id, "text": db.location_text(loc), "qty": 0.0})
                seen.add(origin_id)
        elif not origin_id and (b["location"] or "").strip():
            hit = conn.execute(
                "SELECT * FROM locations WHERE warehouse||'-'||rack||'-'||shelf=? "
                "OR warehouse||'-'||rack||'-'||shelf||'-'||bin=? LIMIT 1",
                (b["location"].strip(), b["location"].strip())).fetchone()
            if hit:
                origin_id = hit["id"]
                if origin_id not in seen:
                    opts.insert(0, {"id": origin_id, "text": db.location_text(hit), "qty": 0.0})
                    seen.add(origin_id)
        # v1.0.47：维修单「未修好」可以回到待报废库位——即使该物料在待报废区当前没有库存
        # （借出去之前在别的库位），也必须让待维修/待报废属性的库位出现在候选里。
        if (b["kind"] or "") == "maintenance":
            for loc in conn.execute(
                    "SELECT * FROM locations WHERE COALESCE(attribute,'good') IN "
                    "('pending_repair','pending_scrap') ORDER BY warehouse, rack, shelf, bin"):
                if int(loc["id"]) not in seen:
                    opts.append({"id": loc["id"], "text": db.location_text(loc), "qty": 0.0})
                    seen.add(int(loc["id"]))
        if not opts:
            # 物料一个库位都没登记（多为历史数据）：列出全部库位，别把还回卡死
            for loc in conn.execute("SELECT * FROM locations ORDER BY warehouse, rack, shelf, bin"):
                opts.append({"id": loc["id"], "text": db.location_text(loc), "qty": 0.0})
        for o in opts:
            o["is_origin"] = bool(origin_id) and o["id"] == origin_id
            # v1.0.23：把库位属性带给前端，维修单还回时按「是否修好」过滤候选库位
            _ol = db.get_location(conn, o["id"])
            o["attribute"] = _row_get(_ol, "attribute") or "good"
            o["attr_name"] = LOC_ATTR_NAMES.get(o["attribute"], "良品")
        _remain = max(0.0, _num(b["qty"]) - _num(b["returned_qty"]))
        # v1.0.19：分次还回明细，弹窗里能看到这一单已经还过几笔
        try:
            hist = conn.execute(
                "SELECT r.quantity, r.restocked_qty, r.location, r.return_note, r.created_at, "
                "COALESCE(u.display_name,'') op_name FROM borrow_returns r "
                "LEFT JOIN users u ON u.id=r.op_id WHERE r.borrow_id=? ORDER BY r.id",
                (bid,)).fetchall()
            returns = [dict(x) for x in hist]
        except Exception:
            returns = []
        return jsonify(ok=True, default=origin_id or (opts[0]["id"] if opts else 0),
                       unit=b["unit"] or "", qty=b["qty"],
                       kind=b["kind"] or "", is_maintenance=(b["kind"] or "") == "maintenance",
                       returned_qty=_num(b["returned_qty"]),
                       remaining_qty=_remain, locations=opts, returns=returns)
    finally:
        conn.close()


@app.route("/api/borrow/return", methods=["POST"])
@login_required
@perm_required("borrow", "return")
def api_borrow_return():
    """v1.0.19：办理还回，支持分次/部分还回。

    办理时已实扣库存，这里按本次还回数量把库存回补到选定库位并写入库流水。
    回补量以「已扣未回补量」封顶：历史单升级补扣时可能扣不足，不能凭空造库存。
    qty 留空 = 一次还清剩余数量；全额还回后状态才转 returned。
    """
    data = request.get_json(silent=True) or request.form
    bid = _int(data.get("id"), 0)
    lid = _int(data.get("location_id"), 0)
    lang = _i18n.current_lang()
    raw_qty = data.get("qty", "")
    note = (data.get("return_note") or "").strip()
    # v1.0.23：维修单还回时必须声明「是否修好」；未声明按「未修好」保守处理
    repair_ok = 1 if str(data.get("repair_ok", "0")).strip().lower() in ("1", "true", "on", "yes") else 0
    # v1.0.45：维修单还回时选择维修品类型（还回环节登记，回写 borrows 供柏拉图统计）
    repair_type = str(data.get("repair_type") or "").strip()
    if not bid:
        return jsonify(ok=False, msg=_i18n.t("参数错误", lang))
    if not lid:
        return jsonify(ok=False, msg=_i18n.t("请选择返还库位", lang))
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, borrow_no, status, part_id, part_code, unit, qty, kind, "
            "COALESCE(returned_qty,0) returned_qty, COALESCE(deducted_qty,0) deducted_qty, "
            "COALESCE(restocked_qty,0) restocked_qty, COALESCE(stock_deducted,0) stock_deducted "
            "FROM borrows WHERE id=?", (bid,)).fetchone()
        if not row:
            return jsonify(ok=False, msg=_i18n.t("借用记录不存在", lang))
        if row["status"] == "returned":
            return jsonify(ok=False, msg=_i18n.t("该记录已还回", lang))
        if row["status"] == "cancelled":
            return jsonify(ok=False, msg=_i18n.t("该记录已驳回，无需还回", lang))
        total = _num(row["qty"])
        returned = _num(row["returned_qty"])
        remain = max(0.0, total - returned)
        if remain <= 1e-9:
            return jsonify(ok=False, msg=_i18n.t("该记录已全部还回，无需重复办理", lang))
        raw_qty = str(raw_qty).strip()
        try:
            back = float(raw_qty) if raw_qty else remain
        except (TypeError, ValueError):
            return jsonify(ok=False, msg=_i18n.t("还回数量格式不正确", lang))
        if back <= 1e-9:
            return jsonify(ok=False, msg=_i18n.t("还回数量必须大于 0", lang))
        if back > remain + 1e-9:
            return jsonify(ok=False, msg=_xl(
                "还回数量超过未还数量：本次最多可还 {p1} {p2}",
                p1=round(remain, 3), p2=row["unit"] or ""))
        loc_row = db.get_location(conn, lid)
        if not loc_row:
            return jsonify(ok=False, msg=_i18n.t("返还库位不存在，请重新选择", lang))
        ret_txt = db.location_text(loc_row)
        # v1.0.23：维修件还回 —— 维修结果决定状态与目标库位属性，服务端强校验
        #   已修好 → condition=repair（维修品，出库优先），只能进良品区
        #   未修好 → condition=defective（不良品），只能回待维修 / 待报废区
        is_maint = (row["kind"] or "") == "maintenance"
        ret_attr = _row_get(loc_row, "attribute") or "good"
        if is_maint:
            if not repair_type:
                return jsonify(ok=False, msg=_i18n.t("请选择维修品类型", lang))
            if repair_ok and ret_attr != "good":
                return jsonify(ok=False, msg=_i18n.t(
                    "已修好的维修品只能回良品区库位，请重新选择", lang))
            if (not repair_ok) and ret_attr not in ("pending_repair", "pending_scrap"):
                return jsonify(ok=False, msg=_i18n.t(
                    "未修好的维修件只能回待维修或待报废库位，请重新选择", lang))
            ret_condition = "repair" if repair_ok else "defective"
        else:
            ret_condition = "good"
        user = current_user()
        # 历史单升级时可能没扣足；按「已扣 − 已回补」封顶，绝不凭空造库存
        can_restock = max(0.0, _num(row["deducted_qty"]) - _num(row["restocked_qty"]))
        restock = min(back, can_restock)
        # v1.0.79：借出时记在流水里的原批次，供「借」还回沿用；换/检/修不用。
        _src_batch = _borrow_out_batch(conn, row["borrow_no"])
        ok2, msg2 = _borrow_restock(conn, row["part_id"], restock, lid, ret_txt,
                                    user, row["borrow_no"], row["unit"] or "",
                                    kind=row["kind"] or "",
                                    condition=ret_condition, repair_ok=repair_ok,
                                    src_batch=_src_batch)
        if not ok2:
            conn.rollback()
            return jsonify(ok=False, msg=_xl("库存回补失败：{p1}", p1=msg2))
        now = db.now_str()
        conn.execute(
            """INSERT INTO borrow_returns(borrow_id,part_id,quantity,restocked_qty,
               location_id,location,return_note,op_id,created_at,repair_ok,condition,repair_type)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (bid, row["part_id"], back, restock, lid, ret_txt, note,
             user["id"] if user else None, now,
             repair_ok if is_maint else 0, ret_condition,
             repair_type if is_maint else ""))
        new_returned = returned + back
        new_restocked = _num(row["restocked_qty"]) + restock
        done = new_returned + 1e-9 >= total
        if is_maint:
            # v1.0.45：维修还回把「维修品类型 + 是否修好」回写借用行（柏拉图/台账统计口径）。
            # borrows.repair_ok：1=已修好，0=未修好；分次还回以最后一次选择为准。
            conn.execute(
                """UPDATE borrows SET returned_qty=?, restocked_qty=?, status=?, returned_at=?,
                   return_note=?, return_op_id=?, return_location=?, return_location_id=?,
                   repair_type=?, repair_ok=?, updated_at=? WHERE id=?""",
                (new_returned, new_restocked, "returned" if done else "borrowed",
                 now if done else "", note or "Returned",
                 user["id"] if user else None, ret_txt, lid,
                 repair_type, repair_ok, now, bid))
        else:
            conn.execute(
                """UPDATE borrows SET returned_qty=?, restocked_qty=?, status=?, returned_at=?,
                   return_note=?, return_op_id=?, return_location=?, return_location_id=?,
                   updated_at=? WHERE id=?""",
                (new_returned, new_restocked, "returned" if done else "borrowed",
                 now if done else "", note or "Returned",
                 user["id"] if user else None, ret_txt, lid, now, bid))
        conn.commit()
    finally:
        conn.close()
    fmt_q = lambda v: int(v) if abs(v - int(v)) < 1e-9 else round(v, 3)
    msg = _xl("已还回 {p1} {p2}，库存回补 {p3} {p2} 至 {p4}",
              p1=fmt_q(back), p2=row["unit"] or "", p3=fmt_q(restock), p4=ret_txt)
    if is_maint:
        msg += (_i18n.t("；维修结果：已修好（维修品，出库优先）", lang) if repair_ok
                else _i18n.t("；维修结果：未修好（不良品）", lang))
        if repair_type:
            msg += _xl("；维修品类型：{p1}", p1=repair_type)
    if not done:
        msg += _xl("；剩余未还 {p1} {p2}",
                   p1=fmt_q(max(0.0, total - new_returned)), p2=row["unit"] or "")
    return jsonify(ok=True, msg=msg, return_location=ret_txt,
                   returned_qty=new_returned,
                   remaining_qty=max(0.0, total - new_returned),
                   restocked_qty=restock,
                   status=("returned" if done else "borrowed"))
@app.route("/api/borrow/export")
@login_required
@perm_required("borrow", "export")
def api_borrow_export():
    lang = _i18n.current_lang()
    user = current_user()
    conn = get_db()
    try:
        if can(user["role"], "borrow", "manage"):
            rows = conn.execute(
                "SELECT b.*, COALESCE(u.display_name,'') borrower_name FROM borrows b "
                "LEFT JOIN users u ON u.id=b.borrower_user_id ORDER BY b.id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT b.*, COALESCE(u.display_name,'') borrower_name FROM borrows b "
                "LEFT JOIN users u ON u.id=b.borrower_user_id "
                "WHERE b.borrower_user_id=? ORDER BY b.id DESC",
                (user["id"],),
            ).fetchall()
    finally:
        conn.close()
    now = datetime.now()
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    buf.write("\ufeff")
    w = _csv.writer(buf)
    w.writerow([
        _i18n.t("borrow.borrow_no", lang), _i18n.t("借用物料", lang), _i18n.t("物料编码", lang),
        _i18n.t("单位", lang), _i18n.t("借用数量", lang), _i18n.t("借用人", lang),
        _i18n.t("借出库位", lang),
        _i18n.t("借出时间", lang), _i18n.t("预计归还时间", lang), _i18n.t("状态", lang),
        _i18n.t("返还库位", lang),
        _i18n.t("归还时间", lang), _i18n.t("归还说明", lang), _i18n.t("borrow.note", lang),
    ])
    for r in rows:
        d = dict(r)
        color = _borrow_color(d.get("status"), d.get("borrowed_at"), d.get("expected_return"),
                              now, d.get("confirmed_at"))
        if d.get("status") == "returned":
            st = _i18n.t("已还回", lang)
        elif d.get("status") == "cancelled":
            st = _i18n.t("已驳回", lang)
        elif d.get("status") == "pending":
            st = _i18n.t("待确认", lang)
        elif color == "red":
            st = _i18n.t("已逾期", lang)
        elif color == "yellow":
            st = _i18n.t("即将到期", lang)
        else:
            st = _i18n.t("borrow.status_borrowed", lang)
        w.writerow([
            d.get("borrow_no"), d.get("part_name"), d.get("part_code"), d.get("unit"),
            d.get("qty"), d.get("borrower"), d.get("location"), d.get("borrowed_at"),
            d.get("expected_return"),
            st, d.get("return_location"), d.get("returned_at"), d.get("return_note"), d.get("note"),
        ])
    resp = Response(buf.getvalue(), mimetype="text/csv; charset=utf-8-sig")
    resp.headers["Content-Disposition"] = "attachment; filename=borrows_%s.csv" % now.strftime("%Y%m%d_%H%M%S")
    return resp


@app.route("/api/users/list")
@login_required
def api_users_list():
    """系统用户列表（用于借用「关联系统用户」下拉；返回启用中的用户）。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, display_name, username, role, department FROM users "
            "WHERE active=1 AND role<>'guest' ORDER BY display_name"
        ).fetchall()
    finally:
        conn.close()
    return jsonify(ok=True, users=[dict(r) for r in rows])


# v1.0.26：共享盘状态轮询端点。前端据此显示「只读降级 / 维护中 / 大导入中」横幅；
# 非 sqlite_smb 模式返回 enabled=False，前端不显示任何横幅、也不轮询写状态。
@app.route("/api/share_status")
def api_share_status():
    import share_guard
    return jsonify(share_guard.status())


# 降级 / 维护期间仍允许的请求：登录、语言、配置、状态查询、静态资源。
# 「查询 / 导出 / 报表 / 图表」类 POST 也视为只读，避免只读降级时连查询都用不了。
_GUARD_SAFE_POST = (
    "/login", "/logout", "/lang", "/set_lang", "/dbconfig",
    "/api/share_status", "/static", "/health", "/i18n",
)
_GUARD_READONLY_RE = re.compile(
    r"export|query|search|list|report|stat|chart|preview|print|filter|lookup|analy|dashboard",
    re.I)


def _guard_readonly_post(path):
    if any(path == p or path.startswith(p + "/") for p in _GUARD_SAFE_POST):
        return True
    return bool(_GUARD_READONLY_RE.search(path))


db_bad = False

@app.before_request
def _db_guard():
    # 数据库初始化失败时，自动跳配置页修复，不会直接进不去
    global db_bad
    if db_bad and not request.path.startswith('/dbconfig') and not request.path.startswith('/static'):
        return redirect('/dbconfig')
    # v1.0.26：共享盘断连 / 系统维护时冻结写入。读请求一律放行；
    # 写事务入口（db_lock）另有 require_writable() 二次兜底，防止白名单误放。
    try:
        import share_guard
        g.share_readonly = bool(share_guard.degraded() or share_guard.maintenance())
        g._sg_holder = None
        g._sg_keepalive = None
        if request.method in ("POST", "PUT", "PATCH", "DELETE") \
                and not _guard_readonly_post(request.path):
            share_guard.require_writable()
            # v1.0.26：所有写请求统一在入口拿跨进程 write 锁。
            # 放在这里而不是逐个 handler 套 db_lock，是因为写入口有几十处，
            # 漏改任意一处就等于给并发写留了后门。
            #   · 100 人同时写 → 串行排队，抢不到 30s 后给友好提示而非 500
            #   · 1 人 400M 导入 → 整段持锁，其他人写被挡，读不受影响
            #   · 长请求由 KeepAlive 每 5s 续锁，避免被 15s 看门狗误判崩溃
            if share_guard.enabled():
                _h = share_guard.acquire("write", wait=share_guard.LOCK_WAIT_SEC)
                g._sg_holder = _h
                g._sg_keepalive = share_guard.start_keepalive(_h)
    except share_guard.ShareUnavailable as e:
        _ajax = (request.path.startswith("/api/") or request.is_json or
                 request.headers.get("X-Requested-With") == "XMLHttpRequest")
        if _ajax:
            return jsonify(ok=False, readonly=True, kind=e.kind, msg=e.message), 409
        flash(e.message, "warning")
        return redirect(request.referrer or url_for("dashboard"))

@app.teardown_request
def _sg_release(exc):
    """请求结束（含异常 / 重定向）一律释放写锁，避免僵死到 15s 看门狗超时。"""
    try:
        import share_guard
        _h = getattr(g, "_sg_holder", None)
        if not _h:
            return
        _ka = getattr(g, "_sg_keepalive", None)
        if _ka is not None:
            _ka.stop()
            if _ka.lost.is_set():
                # 锁已被别的进程接管，此时 release 会把对方的锁删掉
                return
        share_guard.release("write", _h)
    except Exception as _e:  # noqa: BLE001
        logging.getLogger("main").warning("释放请求写锁失败：%s", _e)


@app.after_request
def _no_cache_html(resp):
    # 页面 HTML 不缓存：避免部署/升级后浏览器仍显示旧界面（自定义表头等修改即时生效）
    ct = (resp.headers.get("Content-Type") or "")
    if ct.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp




if __name__ == "__main__":
    import logging
    import webbrowser
    main()
