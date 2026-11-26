# SPMS 备件辅料仓管系统 (Spare Parts Management System)
# 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
# 邮箱：510658927@qq.com
# QQ：510658927
# Gitee：https://gitee.com/rookie-uncle
# GitHub：https://github.com/Colton1982
# Copyright (c) 2026 rookie-uncle. All rights reserved.
# 开源协议：Apache-2.0
"""消息通知：邮件 + 企业私信（企业微信/钉钉/飞书自建应用）。
配置存 settings.json，新待审批单时由业务代码调用 notify_approver()。"""
import json, smtplib, ssl, urllib.request, urllib.parse
from email.mime.text import MIMEText
from email.header import Header

_SETTINGS_CACHE = None

def _settings():
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is None:
        try:
            _SETTINGS_CACHE = json.load(open("settings.json", encoding="utf-8"))
        except Exception:
            _SETTINGS_CACHE = {}
    return _SETTINGS_CACHE

def reload_settings():
    global _SETTINGS_CACHE
    _SETTINGS_CACHE = None


def send_email(to_addr, subject, body, link=None):
    s = _settings()
    if not s.get("notify_email_enabled") or not to_addr:
        return False, "邮件通知未开启或无收件人"
    host = s.get("smtp_host", "")
    port = int(s.get("smtp_port", 465))
    user = s.get("smtp_user", "")
    pwd = s.get("smtp_pass", "")
    if not (host and user and pwd):
        return False, "SMTP 未配置完整"
    try:
        base = s.get("base_url", "").rstrip("/")
        url = (link or "/applications") if not base else (base + (link or "/applications"))
        html = f"""<div style="font-family:Arial,sans-serif;font-size:14px">
          <p>{body}</p>
          <p><a href="{url}" style="display:inline-block;padding:8px 16px;background:#2f6fed;color:#fff;text-decoration:none;border-radius:4px">前往审批</a></p>
          <p style="color:#888;font-size:12px">如按钮无法点击，请复制链接：{url}</p>
        </div>"""
        msg = MIMEText(html, "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = user
        msg["To"] = to_addr
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=10) as smtp:
            smtp.login(user, pwd)
            smtp.sendmail(user, [to_addr], msg.as_string())
        return True, "ok"
    except Exception as e:
        return False, str(e)


def _http_post_json(url, data, headers=None, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# Webhook 去重的记忆表：同一张单同时通知多个审批人时，频道里只发一条
_HOOK_SEEN = {}


def _hook_once(key, ttl=120):
    import time as _t
    now = _t.time()
    for k, ts in list(_HOOK_SEEN.items()):
        if now - ts > ttl:
            _HOOK_SEEN.pop(k, None)
    if key in _HOOK_SEEN:
        return False
    _HOOK_SEEN[key] = now
    return True


def _send_webhook(url, style, title, text):
    """通用 Webhook：Slack / Microsoft Teams / Discord / 自建系统。
    这几种平台吃同一个 URL 但字段名不同，所以按用户选的格式组装 payload。"""
    if style == "teams":
        # Teams（Workflows / Incoming Webhook）：MessageCard
        payload = {"@type": "MessageCard", "@context": "https://schema.org/extensions",
                   "summary": title, "title": title, "text": text.replace("\n", "  \n")}
    elif style == "discord":
        payload = {"content": text}
    elif style == "plain":
        payload = {"title": title, "text": text}
    else:  # slack 及兼容平台
        payload = {"text": f"*{title}*\n{text}"}
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
    return True, "ok"


def send_imp_msg(enterprise_id, title, body, link=None):
    """企业私信：按平台配置发到指定 userid。
    wecom/dingtalk/feishu = 国内三家自建应用；webhook = 通用 Webhook（国外企业用 Slack/Teams/Discord）。"""
    s = _settings()
    if not s.get("notify_imp_enabled"):
        return False, "企业私信未开启"
    plat = s.get("imp_platform", "wecom")
    base = s.get("base_url", "").rstrip("/")
    url = (link or "/applications") if not base else (base + (link or "/applications"))
    brand = s.get("brand_title") or "SPMS"
    text = f"[{brand}] {title}\n{body}\n{url}"

    if plat == "webhook":
        hook = (s.get("imp_webhook") or "").strip()
        if not hook:
            return False, "未配置 Webhook 地址"
        # 同一张单（标题+正文相同）在 2 分钟内只发一次，避免多审批人重复刷屏
        if not _hook_once(hook + "|" + title + "|" + body):
            return True, "duplicate-skipped"
        try:
            return _send_webhook(hook, (s.get("imp_webhook_style") or "slack").lower(), title, text)
        except Exception as e:
            return False, str(e)

    if not enterprise_id:
        return False, "企业私信未开启或无企业账号"
    corpid = s.get("imp_corpid", "")
    secret = s.get("imp_secret", "")
    agentid = s.get("imp_agentid", "")
    if not (corpid and secret):
        return False, "企业私信未配置完整"
    try:
        if plat == "wecom":
            token = _http_get(f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={secret}")["access_token"]
            _http_post_json(f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}", {
                "touser": enterprise_id, "msgtype": "text",
                "agentid": int(agentid or 1),
                "text": {"content": text},
            })
        elif plat == "dingtalk":
            token = _http_get(f"https://oapi.dingtalk.com/gettoken?appkey={corpid}&appsecret={secret}")["access_token"]
            _http_post_json(f"https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2?access_token={token}", {
                "userid_list": enterprise_id,
                "msg": {"msgtype": "text", "text": {"content": text}},
            })
        elif plat == "feishu":
            token = _http_post_json("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                                   {"app_id": corpid, "app_secret": secret})["tenant_access_token"]
            _http_post_json("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id", {
                "receive_id": enterprise_id, "msg_type": "text",
                "content": json.dumps({"text": text}),
            }, headers={"Authorization": f"Bearer {token}"})
        else:
            return False, "未知平台"
        return True, "ok"
    except Exception as e:
        return False, str(e)


def notify_approver(approver_row, title, body):
    """给审批人发通知。approver_row: dict 含 email, enterprise_id, display_name。"""
    if not approver_row:
        return
    email = approver_row.get("email") or ""
    eid = approver_row.get("enterprise_id") or ""
    if email:
        send_email(email, title, body)
    # Webhook 发的是频道消息，不依赖审批人的企业账号，所以没填也要发
    if eid or (_settings().get("imp_platform") == "webhook"):
        send_imp_msg(eid, title, body)
