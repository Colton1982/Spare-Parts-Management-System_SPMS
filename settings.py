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
配置持久化：数据库路径等设置保存在软件目录 settings.json 中
（软件放公共盘时，所有用户共享同一份配置；也可各自复制到本机后各自配置）
"""
import json
import os
import sys
from datetime import datetime

# 软件运行目录：打包成 exe 后为 exe 所在目录
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

APP_DIR = app_dir()
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
_FALLBACK_FILE = None  # 软件目录不可写时回退到用户目录

DEFAULTS = {
    "db_path": "",        # SQLite 数据库文件位置（默认空=未配置，启动后进入初始化向导）
    "db_type": "sqlite",  # 数据库类型：sqlite/mysql/mariadb/postgresql/mssql/oracle
    "db_host": "",        # 服务器地址（非 SQLite）
    "db_port": 0,         # 端口（非 SQLite）
    "db_user": "",        # 用户名（非 SQLite）
    "db_password": "",    # 密码（非 SQLite）
    "db_database": "",    # 数据库/库名（非 SQLite；Oracle 用 service_name 时可为空）
    "db_service_name": "",# Oracle 服务名（仅 Oracle）
    # v1.0.26：共享盘保护模式 auto/off/sqlite_smb。仅对 SQLite 生效；
    # 切到 MySQL 等服务器库时全部保护自动失效（no-op），不用删代码。
    "db_mode": "auto",
    "upload_dir": "",     # 已停用（图片统一存入数据库 images 表）；保留空值以兼容旧配置清理
    "port": 5000,         # 本机服务端口
    "demo_data": False,   # 首次建库是否写入演示数据
    "lan_enabled": False, # 局域网部署：True=监听 0.0.0.0，其他电脑可用 http://本机IP:端口 访问
    "ad": {},             # AD 域登录配置（enabled/server/port/ssl/domain/name_format）
    "language": "zh",     # 界面语言（zh/en/ja/ko/th/vi/ru/fr/de）
    "pending_remind_hours": 24,   # 待出库提醒时长（小时）：审批通过后超过该时间未出库，发提醒给申请人
    "pending_release_hours": 36,  # 待出库释放时长（小时）：审批通过后超过该时间未出库，自动取消申请单，释放锁定库存
    "notify_browser": 1,          # 浏览器桌面通知总开关（v1.0.77 起真正生效；老用户默认保持开启）
    "stocktake_approval_level": 1,  # 盘点差异审批级别：0=不审批，1=仅一级（主管），2=两级（主管+经理）。默认一级
    "valuable_approval_level": 1,   # 贵重物料出库审批级别：0=不审批，1=仅一级，2=两级。默认一级
    "return_approval_level": 1,     # 退货出库审批级别：0=不审批，1=仅一级（默认），2=两级
    "exchange_approval_level": 1,   # 换货出库审批级别：0=不审批，1=仅一级（默认），2=两级
    "internal_approval_level": 1,   # 内部转移审批级别：0=不审批，1=仅一级（默认），2=两级
    "giveback_approval_level": 1,   # 返还出库审批级别：0=不审批，1=仅一级（默认），2=两级
    "scrap_approval_level": 1,      # 报废出库审批级别：0=不审批，1=仅一级（默认），2=两级（v1.0.23 补）
    # v1.0.47：报废审批模式：1=系统审批（审批通过自动扣库），
    # 2=系统+纸档审批（打印签字单上传附件后进入系统审批，通过自动扣库）
    "scrap_approval_mode": 1,
    "fifo_enabled": 1,              # 出库先进先出（FIFO）：1=开启（默认），0=关闭（v1.0.23 补）
    # v1.0.78：入库摆放提示（FIFO 放置原则）。可留空=不显示；四路 i18n 负责默认文案翻译。
    "fifo_in_tip": "先进先出物料放置原则：请在物料上写上批次，新件放在最里面，维修件放在最外面，不混放物料！",
    "repair_categories": [],        # 配件分类（维修品类型下拉选项源）；管理员在系统设置中增删
    # v1.0.18：盘点周期（天）。盘点管理页按此间隔生成盘点节点，做应盘/实盘/漏盘对比。
    "stocktake_cycle_days": 30,
    # v1.0.20：周期容差（比例）。应盘日前后各 20% 内完成都算按期；0=严格按日，上限 0.5
    "stocktake_cycle_tolerance": 0.2,
    # v1.0.20：周期锚点日（YYYY-MM-DD，老配置兼容）。
    # v1.0.54：留空=按「盘点启始日」默认 1/1（与设置页提示一致），不再回退首张盘点单日期。
    "stocktake_cycle_anchor": "",
}


def _fallback_file():
    global _FALLBACK_FILE
    if _FALLBACK_FILE:
        return _FALLBACK_FILE
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "备件管理系统")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.dirname(SETTINGS_FILE)
    _FALLBACK_FILE = os.path.join(d, "settings.json")
    return _FALLBACK_FILE


def load():
    """读取配置（不存在返回默认值）"""
    data = dict(DEFAULTS)
    for path in (SETTINGS_FILE, _fallback_file()):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data.update(json.load(f))
                break
            except Exception:
                continue
    return _normalize(data)


def _normalize(data):
    """统一归一化：load() 读盘后、save() 落盘前都走这一份，口径不会分叉。

    v1.0.23：以前只有 load() 归一化，写入时非法值会原样落进 settings.json，
    别的工具直接读文件（备份/迁移/运维排障）就会拿到脏值，跟界面显示不一致。
    """
    # 类型与边界清理
    try:
        data["port"] = int(data.get("port") or 5000)
    except Exception:
        data["port"] = 5000
    if not 1024 <= data["port"] <= 65535:
        data["port"] = 5000
    # 审批级别归一化：None / 非法值一律按各自默认处理；合法档位 0（不审批）/1/2
    for _k in ("stocktake_approval_level", "valuable_approval_level",
               "return_approval_level", "exchange_approval_level",
               "internal_approval_level", "giveback_approval_level",
               "scrap_approval_level"):
        try:
            _v = int(data.get(_k))
        except (TypeError, ValueError):
            _v = 1
        data[_k] = _v if _v in (0, 1, 2) else 1
    # v1.0.47：报废审批两模式；老配置里的 3（纸档）映射到 2（系统+纸档）
    try:
        _sm = int(data.get("scrap_approval_mode", 1))
    except (TypeError, ValueError):
        _sm = 1
    if _sm == 3:
        _sm = 2
    data["scrap_approval_mode"] = _sm if _sm in (1, 2) else 1
    # v1.0.26：共享盘保护模式只允许三个取值，脏值回落 auto
    _dm = str(data.get("db_mode") or "auto").strip().lower()
    data["db_mode"] = _dm if _dm in ("auto", "off", "sqlite_smb") else "auto"
    # v1.0.23：FIFO 是布尔开关，老配置里可能存成字符串或缺失；合法 0 不能被吞成 1
    try:
        _fifo = int(data.get("fifo_enabled", 1))
    except (TypeError, ValueError):
        _fifo = 1
    data["fifo_enabled"] = 1 if _fifo not in (0, 1) else _fifo
    # v1.0.78：入库提示允许留空（=不显示），但必须是字符串；缺键时沿用默认文案
    data["fifo_in_tip"] = str(data.get("fifo_in_tip", DEFAULTS["fifo_in_tip"]) or "").strip()
    # v1.0.18：盘点周期天数必须是 1..365 的整数，非法值回落到 30 天
    try:
        _cd = int(data.get("stocktake_cycle_days") or 30)
    except (TypeError, ValueError):
        _cd = 30
    data["stocktake_cycle_days"] = _cd if 1 <= _cd <= 365 else 30
    # v1.0.20：容差比例 0~0.5（非法值回落 0.2）；锚点必须是合法日期，否则留空交给运行时兜底
    try:
        _ct = float(data.get("stocktake_cycle_tolerance", 0.2))
    except (TypeError, ValueError):
        _ct = 0.2
    if _ct != _ct or _ct < 0:
        _ct = 0.2
    data["stocktake_cycle_tolerance"] = round(min(0.5, _ct), 3)
    _ca = str(data.get("stocktake_cycle_anchor") or "").strip()[:10]
    try:
        if _ca:
            _dt = datetime.strptime(_ca, "%Y-%m-%d")
            _ca = _dt.strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        _ca = ""
    data["stocktake_cycle_anchor"] = _ca
    return data


def save(settings):
    """保存配置：优先写软件目录，写失败回退用户目录；返回实际保存位置"""
    data = dict(DEFAULTS)
    data.update(settings)
    data = _normalize(data)
    # 先尝试软件目录
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return SETTINGS_FILE
    except OSError:
        path = _fallback_file()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path
