# -*- coding: utf-8 -*-
# SPMS 备件辅料仓管系统 (Spare Parts Management System)
# 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
# 邮箱：510658927@qq.com
# QQ：510658927
# Gitee：https://gitee.com/rookie-uncle
# GitHub：https://github.com/Colton1982
# Copyright (c) 2026 rookie-uncle. All rights reserved.
# 开源协议：Apache-2.0
"""i18n2.translate —— 三个翻译引擎的统一适配层（零第三方依赖）

为什么自己做适配层而不用某个 SDK
--------------------------------
1. 交付包不能装依赖（客户机器离线，双击 Start.bat 就要能跑）。
   Google / DeepL / OpenAI 兼容接口全是普通 HTTPS + JSON，urllib 就够。
2. 三个引擎的**错误语义**差别太大，上层不该关心。这里统一成：
   要么返回等长的译文数组，要么把失败原因塞进 errors 列表 —— 单条失败
   不拖垮整批。

三个引擎的分工（不是互相替代）
------------------------------
  ai        OpenAI 兼容接口。可指向公网（OpenAI / DeepSeek / 通义），
            也可以指向内网自建（Ollama / vLLM / LM Studio 的 /v1）。
            **这是唯一能在完全离线环境里工作的选项**，也是唯一能读懂上下文的
            （同一批词条一起给它，术语风格会自洽）。
  google    Google Cloud Translation v2。按量计费，快，术语一致性一般。
  deepl     DeepL。欧洲语言质量最好，中文→日韩明显弱于 AI。
  mymemory  MyMemory 公共翻译记忆库。**免 Key**，匿名每天约 1000 词
            （填邮箱可提高额度），质量一般 —— 定位是「试翻 / 小批量补翻」，
            不是大批量生产。逐条请求，天然限速。

占位符是最容易出事的地方
------------------------
词条是整句带 {name} / {d} 的（"各{name}库存占比"）。机器翻译看到 {name}
有三种反应：原样保留（对）、翻译成 {名称}（灾难：运行期 .format 找不到
这个键，整句崩或原样吐出花括号）、直接删掉（更隐蔽：句子还通，但数据没了）。

所以调用前不替换（替换成哨兵再还原，反而更容易被 MT 改写），
调用后**逐条校验占位符集合是否完全一致**，不一致就丢弃这条并记错。
宁可留空回退中文，也不能写进一条会渲染出 {名称} 的译文。
"""
from __future__ import annotations

import html as _html
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import core

CJK = re.compile(r"[\u4e00-\u9fff]")

PROVIDERS = ("ai", "google", "deepl", "mymemory")

# 每批条数。太小浪费往返，太大容易被截断或超 token：
#   AI 的瓶颈是输出 token（一条译文平均 20 token，18 条一批比较稳）
#   Google 单请求上限 128 段，DeepL 50 段，MyMemory 一条一请求，这里都留了余量
BATCH = {"ai": 18, "google": 100, "deepl": 40, "mymemory": 10}

DEFAULT_TIMEOUT = 40


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def default_cfg() -> dict:
    """引擎配置的形状。前端表单、settings.json 存的就是这个结构。"""
    return {
        "provider": "ai",
        "ai": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "model": "gpt-4o-mini",
            "timeout": DEFAULT_TIMEOUT,
        },
        "google": {
            "api_key": "",
            "endpoint": "https://translation.googleapis.com/language/translate/v2",
            "timeout": DEFAULT_TIMEOUT,
        },
        "deepl": {
            "api_key": "",
            # key 以 :fx 结尾的是免费版，域名不同，这里按 key 自动切换
            "endpoint": "",
            "timeout": DEFAULT_TIMEOUT,
        },
        "mymemory": {
            # 免 Key。邮箱是可选的：MyMemory 对匿名调用按 IP 限每日约 1000 词，
            # 带上邮箱（任意真实邮箱即可，不是注册）额度会放宽
            "email": "",
            "endpoint": "",
            "timeout": DEFAULT_TIMEOUT,
        },
    }


def merge_cfg(saved) -> dict:
    """把存下来的配置合并进默认结构。

    逐层合并而不是整体替换：以后加一个新字段（比如 deepL 的 formality），
    老配置文件里没有也能拿到默认值，不会 KeyError。
    """
    cfg = default_cfg()
    if not isinstance(saved, dict):
        return cfg
    for k, v in saved.items():
        if k in ("ai", "google", "deepl", "mymemory") and isinstance(v, dict):
            cfg[k].update(v)
        elif v not in (None, ""):
            cfg[k] = v
    return cfg


def _timeout(conf: dict) -> float:
    try:
        t = float(conf.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        t = DEFAULT_TIMEOUT
    return max(5.0, min(t, 180.0))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
# 默认校验证书。但实测过有的交付环境（杀软 / 网关改写证书链、嵌入版 python
# 缺 CA 库）会报 CERTIFICATE_VERIFY_FAILED —— 真打不通时用户只看到「连不上」，
# 没法自查。这里首次校验失败后记住，本次进程内降级为不校验重试。
# 翻译接口只发界面文案、不带凭据，这个取舍是可接受的。
_SSL_UNSAFE = False


def _ssl_ctx():
    if _SSL_UNSAFE:
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def _mark_ssl(e) -> bool:
    """证书校验失败 → 打降级标记并要求重试；其它网络错误照旧抛出。"""
    global _SSL_UNSAFE
    if "CERTIFICATE_VERIFY_FAILED" in str(getattr(e, "reason", e)):
        _SSL_UNSAFE = True
        return True
    return False


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError("HTTP %s %s" % (e.code, detail or e.reason))
    except urllib.error.URLError as e:
        if _mark_ssl(e):
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl._create_unverified_context()) as r:
                raw = r.read().decode("utf-8", "replace")
        else:
            raise RuntimeError("连不上：%s" % (e.reason,))
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError("返回的不是 JSON：" + raw[:200])


def _post_form(url: str, fields, headers: dict, timeout: float) -> dict:
    """DeepL 用 form-encoded（同一字段重复出现表示数组）。"""
    body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise RuntimeError("HTTP %s %s" % (e.code, detail or e.reason))
    except urllib.error.URLError as e:
        if _mark_ssl(e):
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl._create_unverified_context()) as r:
                raw = r.read().decode("utf-8", "replace")
        else:
            raise RuntimeError("连不上：%s" % (e.reason,))
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError("返回的不是 JSON：" + raw[:200])


def _get_json(url: str, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, method="GET")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s %s" % (e.code, e.reason))
    except urllib.error.URLError as e:
        if _mark_ssl(e):
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl._create_unverified_context()) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        raise RuntimeError("连不上：%s" % (e.reason,))
    except ValueError:
        raise RuntimeError("返回的不是 JSON")


# --------------------------------------------------------------------------
# 占位符与结果校验
# --------------------------------------------------------------------------
_RE_FMT = re.compile(r"%[sdif]|%\([a-zA-Z_]+\)s")


def _ph_ok(src: str, dst: str) -> bool:
    """占位符**出现次数**必须完全一致。

    多出来的（{名称}）运行期替换不掉，会原样露在界面上；
    少掉的（被 MT 当语气词吃掉）会让句子读不通，且丢数据。
    两种都不接受。只比集合会漏掉「两次变一次」——名字还在，数字少一个，
    界面上看不出来少了什么，所以这里按次数比。
    """
    if core.placeholder_counts(src) != core.placeholder_counts(dst):
        return False
    return sorted(_RE_FMT.findall(src)) == sorted(_RE_FMT.findall(dst))


def _looks_untranslated(src: str, dst: str, target: str) -> bool:
    """目标不是中文，译文却和中文原文一字不差 → 判为没翻。

    这是对「机器翻译偷懒原样返回」的兜底。两条边界：
      · 源码本身没有汉字（API / Excel / OK / 123 / SPMS）时，原样返回往往
        就是正确译法，不能丢 —— 否则这些词条永远存不进去，覆盖率卡住。
      · 只比整串相等，不做「含汉字」判断：日文译文里出现汉字是正常的
        （日语大量共用汉字），按含汉字判会把整门日语判成没翻。
    """
    if target == "zh":
        return False
    d = (dst or "").strip()
    if not d:
        return True
    s = (src or "").strip()
    if d != s:
        return False
    return bool(CJK.search(s))


# --------------------------------------------------------------------------
# 引擎实现：AI（OpenAI 兼容）
# --------------------------------------------------------------------------
_SYS_AI = (
    "You are a professional software localizer. Translate UI strings.\n"
    "Rules:\n"
    "1. Output ONLY a JSON array of strings, same length and order as the input. "
    "No markdown fences, no explanations.\n"
    "2. Keep every {placeholder} exactly as-is: do not translate, rename, reorder "
    "or drop them. Same for %s / %d.\n"
    "3. Keep UI style: short, imperative, no trailing period unless the source has one.\n"
    "4. Never translate product names, codes, or file extensions "
    "(SPMS, SMT, Excel, CSV, API, URL).\n"
    "5. If a string is already in the target language, return it unchanged."
)


def _ai(texts, target, source, conf):
    base = (conf.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    key = (conf.get("api_key") or "").strip()
    model = (conf.get("model") or "gpt-4o-mini").strip()
    if not key:
        raise RuntimeError("还没填 AI 的 API Key")
    url = base + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _SYS_AI},
            {"role": "user", "content":
                "Target language: %s (source: %s)\nInput:\n%s"
                % (target, source, json.dumps(texts, ensure_ascii=False))},
        ],
    }
    data = _post_json(url, payload, {"Authorization": "Bearer " + key}, _timeout(conf))
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("返回结构不认识：" + json.dumps(data, ensure_ascii=False)[:200])

    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        raise RuntimeError("模型没按 JSON 数组返回：" + content[:160])
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        raise RuntimeError("JSON 解析失败：" + content[:160])
    if not isinstance(arr, list):
        raise RuntimeError("返回的不是数组")
    out = []
    for i in range(len(texts)):
        v = arr[i] if i < len(arr) else None
        out.append(v if isinstance(v, str) else None)
    return out


# --------------------------------------------------------------------------
# 引擎实现：Google Cloud Translation v2
# --------------------------------------------------------------------------
# Google 的 source/target 用 ISO-639-1 小写；带地区的（pt-BR）保留
def _g_lang(code: str) -> str:
    return (code or "en").replace("_", "-")


def _google(texts, target, source, conf):
    key = (conf.get("api_key") or "").strip()
    if not key:
        raise RuntimeError("还没填 Google 的 API Key")
    url = (conf.get("endpoint") or
           "https://translation.googleapis.com/language/translate/v2")
    url += "?key=" + urllib.parse.quote(key)
    payload = {"q": texts, "source": _g_lang(source), "target": _g_lang(target),
               "format": "text"}
    data = _post_json(url, payload, {}, _timeout(conf))
    try:
        rows = data["data"]["translations"]
    except (KeyError, TypeError):
        err = (data.get("error") or {}).get("message") if isinstance(data, dict) else ""
        raise RuntimeError("Google 返回异常：%s" % (err or json.dumps(data, ensure_ascii=False)[:200]))
    out = []
    for i in range(len(texts)):
        v = rows[i].get("translatedText") if i < len(rows) else None
        # v2 即使 format=text 也会把 & ' < > 转成实体，必须还原
        out.append(_html.unescape(v) if isinstance(v, str) else None)
    return out


# --------------------------------------------------------------------------
# 引擎实现：DeepL
# --------------------------------------------------------------------------
_DEEPL_LANG = {
    "zh": "ZH", "en": "EN", "ja": "JA", "ko": "KO", "de": "DE", "fr": "FR",
    "es": "ES", "it": "IT", "pt": "PT-PT", "pt-BR": "PT-BR", "ru": "RU",
    "nl": "NL", "pl": "PL", "tr": "TR", "uk": "UK", "cs": "CS", "id": "ID",
    "vi": "VI", "th": "TH", "ar": "AR", "sv": "SV", "da": "DA", "fi": "FI",
    "nb": "NB", "el": "EL", "hu": "HU", "ro": "RO", "sk": "SK", "sl": "SL",
    "bg": "BG", "et": "ET", "lt": "LT", "lv": "LV",
}


def _deepl_lang(code: str) -> str:
    c = code or "en"
    if c in _DEEPL_LANG:
        return _DEEPL_LANG[c]
    base = core.base_lang(c)
    if base in _DEEPL_LANG:
        return _DEEPL_LANG[base]
    raise RuntimeError("DeepL 不支持这个语言：" + c)


def _deepl_endpoint(conf) -> str:
    ep = (conf.get("endpoint") or "").strip().rstrip("/")
    if ep:
        return ep
    key = (conf.get("api_key") or "").strip()
    # 免费版 key 以 :fx 结尾，必须打 api-free，打错域名会 403 且报错很含糊
    return "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"


def _deepl(texts, target, source, conf):
    key = (conf.get("api_key") or "").strip()
    if not key:
        raise RuntimeError("还没填 DeepL 的 API Key")
    url = _deepl_endpoint(conf) + "/v2/translate"
    fields = [("text", t) for t in texts]
    fields.append(("target_lang", _deepl_lang(target)))
    try:
        fields.append(("source_lang", _deepl_lang(source)))
    except RuntimeError:
        pass   # 源语言不支持就交给 DeepL 自己检测
    data = _post_form(url, fields, {"Authorization": "DeepL-Auth-Key " + key},
                      _timeout(conf))
    rows = data.get("translations") or []
    if not rows:
        raise RuntimeError("DeepL 返回空结果：" + json.dumps(data, ensure_ascii=False)[:200])
    out = []
    for i in range(len(texts)):
        v = rows[i].get("text") if i < len(rows) else None
        out.append(v if isinstance(v, str) else None)
    return out


# --------------------------------------------------------------------------
# 引擎实现：MyMemory（公共翻译记忆库，免 Key）
# --------------------------------------------------------------------------
# MyMemory 的 langpair 用 RFC3066 标签：中文必须写 zh-CN，写 zh 会 422
def _mm_lang(code: str) -> str:
    c = core.base_lang(code or "en")
    return {"zh": "zh-CN", "en": "en-GB", "pt": "pt-BR"}.get(c, c)


def _mymemory(texts, target, source, conf):
    """一条文本一次 GET。公共接口就是这么设计的，批量化反而容易被限流。

    任一条失败就抛错 —— 上层 translate() 会把整批记为失败但不拖垮其它批，
    和其它引擎的错误语义一致。
    """
    ep = (conf.get("endpoint") or
          "https://api.mymemory.translated.net/get").strip().rstrip("/")
    email = (conf.get("email") or "").strip()
    out = []
    for t in texts:
        params = {"q": t, "langpair": "%s|%s" % (_mm_lang(source), _mm_lang(target))}
        if email:
            params["de"] = email
        data = _get_json(ep + "?" + urllib.parse.urlencode(params), {}, _timeout(conf))
        st = data.get("responseStatus")
        if isinstance(st, str) and st.isdigit():
            st = int(st)
        txt = (data.get("responseData") or {}).get("translatedText")
        if st != 200 or not isinstance(txt, str) or not txt.strip():
            raise RuntimeError("MyMemory 返回异常：%s"
                               % json.dumps(data, ensure_ascii=False)[:160])
        # 记忆库匹配返回值偶尔带 HTML 实体，和 Google 分支一样还原
        out.append(_html.unescape(txt))
    return out


_FN = {"ai": _ai, "google": _google, "deepl": _deepl, "mymemory": _mymemory}


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def translate(texts, target: str, source: str = "zh", cfg=None, progress=None):
    """把一批中文串翻成 target。

    返回 (results, errors)：
      results 与 texts 等长，失败位是 None（绝不掺入原文冒充成功）
      errors  人类可读的失败原因，直接显示在界面上

    progress(done, total) 可选回调，给后台任务报进度用。
    """
    texts = list(texts or [])
    cfg = merge_cfg(cfg)
    provider = (cfg.get("provider") or "ai").lower()
    if provider not in _FN:
        raise ValueError("未知的翻译引擎：" + provider)

    conf = cfg.get(provider) or {}
    fn = _FN[provider]
    size = BATCH.get(provider, 20)
    out = [None] * len(texts)
    errors = []
    total = len(texts)
    done = 0

    for start in range(0, total, size):
        batch = texts[start:start + size]
        try:
            res = fn(batch, target, source, conf)
        except Exception as e:            # 单批失败不影响其它批
            errors.append("[%d-%d] %s" % (start + 1, start + len(batch), e))
            done += len(batch)
            if progress:
                progress(done, total)
            continue

        if len(res) != len(batch):
            errors.append("[%d-%d] 返回条数不符（%d/%d）"
                          % (start + 1, start + len(batch), len(res), len(batch)))
            done += len(batch)
            if progress:
                progress(done, total)
            continue

        for i, (src, dst) in enumerate(zip(batch, res)):
            idx = start + i
            if not isinstance(dst, str) or not dst.strip():
                errors.append("空译文：%s" % (src or "")[:40])
                continue
            if not _ph_ok(src, dst):
                errors.append("占位符被改动，已丢弃：%s" % (src or "")[:40])
                continue
            if _looks_untranslated(src, dst, target):
                errors.append("疑似未翻译，已丢弃：%s" % (src or "")[:40])
                continue
            out[idx] = dst.strip()
        done += len(batch)
        if progress:
            progress(done, total)

    return out, errors


def ping(cfg=None) -> dict:
    """连通性测试。返回 {ok, provider, detail}。

    用一条真实请求验证，不看配置填没填 —— 填了 key 但没网、key 错、
    模型名不存在，这三种「看起来配好了其实用不了」的情况只有真打一次才暴露。
    """
    cfg = merge_cfg(cfg)
    provider = (cfg.get("provider") or "ai").lower()
    conf = cfg.get(provider) or {}
    try:
        if provider == "ai":
            base = (conf.get("base_url") or "https://api.openai.com/v1").rstrip("/")
            key = (conf.get("api_key") or "").strip()
            if not key:
                return {"ok": False, "provider": provider, "detail": "还没填 API Key"}
            data = _get_json(base + "/models", {"Authorization": "Bearer " + key},
                             _timeout(conf))
            models = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            want = (conf.get("model") or "").strip()
            detail = "可用模型 %d 个" % len(models)
            if want and models and want not in models:
                detail += "；注意：当前模型 %s 不在列表里，可能不可用" % want
            return {"ok": True, "provider": provider, "detail": detail}
        if provider == "google":
            res, errs = translate(["测试"], "en", "zh", cfg)
            if res and res[0]:
                return {"ok": True, "provider": provider, "detail": "连通，样例：%s" % res[0]}
            return {"ok": False, "provider": provider, "detail": "；".join(errs[:2]) or "无返回"}
        if provider == "deepl":
            key = (conf.get("api_key") or "").strip()
            if not key:
                return {"ok": False, "provider": provider, "detail": "还没填 API Key"}
            data = _get_json(_deepl_endpoint(conf) + "/v2/usage",
                             {"Authorization": "DeepL-Auth-Key " + key}, _timeout(conf))
            used = data.get("character_count")
            limit = data.get("character_limit")
            return {"ok": True, "provider": provider,
                    "detail": "连通，本月已用 %s / %s 字符" % (used, limit)}
        if provider == "mymemory":
            res, errs = translate(["库存分析"], "en", "zh", cfg)
            if res and res[0]:
                return {"ok": True, "provider": provider, "detail": "连通，样例：%s" % res[0]}
            return {"ok": False, "provider": provider, "detail": "；".join(errs[:2]) or "无返回"}
        return {"ok": False, "provider": provider, "detail": "未知引擎"}
    except Exception as e:
        return {"ok": False, "provider": provider, "detail": str(e)}
