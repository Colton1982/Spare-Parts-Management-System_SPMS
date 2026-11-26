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
库存管理系统 - 全局配置
================================
架构说明（桌面直连版）：
1. 每个用户电脑上双击本软件即可运行（软件包已内置 Python 运行时与全部依赖，无需预装 Python）。
2. 数据库文件放在内网公共盘：首次启动软件会弹出「初始化向导」，
   填写公共盘上的数据库路径（如 \\\\192.168.1.100\\公共盘\\库存管理系统\\SPMS.db），
   之后所有用户都直连这一个数据库文件，数据实时共享。
3. 配置保存在软件目录 settings.json 中（可随时在「系统设置」页面修改数据库位置）。
"""
import os

import settings as _settings

# 作者信息：软件页脚、关于弹窗与 README 共用同一份口径。
# 这里用中文原文保存，页面展示时由模板直接引用，避免被界面语言切换改写。
AUTHOR = {
    "name": "菜鸟大叔",
    "handles": "@rookie-uncle / @Colton1982",
    "gitee": "https://gitee.com/rookie-uncle",
    "github": "https://github.com/Colton1982",
    "emails": ["510658927@qq.com", "colton.zhou@outlook.com", "colton.zhou@email.cn"],
    "qq": "510658927",
    "footer": "Copyright © 2026 菜鸟大叔 . All rights reserved.",
    "copyright": "Copyright © 2026 菜鸟大叔 (rookie-uncle / Colton1982). All rights reserved.",
    "about_title": "关于作者 / About the Author",
    "contact_title": "联系方式 / Contacts",
    "license_title": "开源协议 / License",
    "copyright_title": "版权声明 / Copyright",
    "license": "Apache-2.0",
}

# ------------------------------------------------------------------
# 由 settings.json 加载的运行配置
# ------------------------------------------------------------------
_CFG = _settings.load()

# 数据库类型：sqlite / mysql / mariadb / postgresql / mssql / oracle
DB_TYPE = (_CFG.get("db_type") or "sqlite").strip().lower()
if DB_TYPE not in ("sqlite", "mysql", "mariadb", "postgresql", "mssql", "oracle"):
    DB_TYPE = "sqlite"

# 共享盘保护模式（v1.0.26，仅对 SQLite 有意义）：
#   auto       按 db_path 自动判定：UNC 路径或 Windows 映射网络盘 → sqlite_smb
#   sqlite_smb 强制启用（自动判定不到时手工指定）
#   off        强制关闭（本地单机盘）
# db_type != sqlite 时 share_guard 全部 no-op —— 后期换 MySQL/PostgreSQL 只需改
# db_type，无需删除或重构本版新增的任何代码。
DB_MODE = (_CFG.get("db_mode") or "auto").strip().lower()
if DB_MODE not in ("auto", "off", "sqlite_smb"):
    DB_MODE = "auto"

# 数据库连接参数（sqlite 仅用 db_path；其余使用 host/port/user/password/database/service_name）
DB_HOST = (_CFG.get("db_host") or "").strip()
DB_PORT = _CFG.get("db_port") or 0
DB_USER = (_CFG.get("db_user") or "").strip()
DB_PASSWORD = _CFG.get("db_password") or ""
DB_NAME = (_CFG.get("db_database") or "").strip()
DB_SERVICE = (_CFG.get("db_service_name") or "").strip()

# 数据库文件位置（未配置前为 None，首次启动进入初始化向导）
DB_PATH = (_CFG.get("db_path") or "").strip() or None

# 物料图片目录（空 = 数据库同级 parts_img）
UPLOAD_DIR = (_CFG.get("upload_dir") or "").strip() or None

# 本机服务端口（每个用户只在本机监听，互不冲突）
PORT = _CFG.get("port", 5000)

# 局域网部署：True=监听 0.0.0.0，其他电脑可通过 http://本机IP:端口 访问
LAN_ENABLED = bool(_CFG.get("lan_enabled"))

# 会话加密密钥（本机会话，仅用于登录 Cookie 签名）
SECRET_KEY = "smt-parts-mgmt-2026-desktop"

# 允许上传的图片格式与大小上限
ALLOWED_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
MAX_IMG_SIZE = 5 * 1024 * 1024  # 5MB

# 数据库目录不可访问时的备用默认位置（软件目录下 data）
BASE_DIR = _settings.APP_DIR
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "SPMS.db")


def apply_settings(cfg):
    """应用新的运行配置（系统设置页保存后调用）"""
    global DB_PATH, UPLOAD_DIR, PORT, LAN_ENABLED, DB_TYPE, DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME, DB_SERVICE, DB_MODE
    _db_type = (cfg.get("db_type") or "sqlite").strip().lower()
    if _db_type not in ("sqlite", "mysql", "mariadb", "postgresql", "mssql", "oracle"):
        _db_type = "sqlite"
    DB_TYPE = _db_type
    DB_MODE = (cfg.get("db_mode") or "auto").strip().lower()
    if DB_MODE not in ("auto", "off", "sqlite_smb"):
        DB_MODE = "auto"
    DB_HOST = (cfg.get("db_host") or "").strip()
    try:
        DB_PORT = int(cfg.get("db_port") or 0)
    except Exception:
        DB_PORT = 0
    DB_USER = (cfg.get("db_user") or "").strip()
    DB_PASSWORD = cfg.get("db_password") or ""
    DB_NAME = (cfg.get("db_database") or "").strip()
    DB_SERVICE = (cfg.get("db_service_name") or "").strip()
    DB_PATH = (cfg.get("db_path") or "").strip() or None
    UPLOAD_DIR = (cfg.get("upload_dir") or "").strip() or None
    LAN_ENABLED = bool(cfg.get("lan_enabled"))
    try:
        PORT = int(cfg.get("port") or 5000)
    except Exception:
        PORT = 5000
    if not 1024 <= PORT <= 65535:
        PORT = 5000


def get_db_cfg():
    """返回数据库连接配置 dict（供 db.connect / 测试连接使用）"""
    return {
        "db_type": DB_TYPE, "db_path": DB_PATH or "",
        "host": DB_HOST, "port": DB_PORT, "user": DB_USER,
        "password": DB_PASSWORD, "database": DB_NAME,
        "service_name": DB_SERVICE,
    }


def resolved_upload_dir():
    """图片目录：未单独配置时与数据库同级 parts_img"""
    if UPLOAD_DIR:
        return UPLOAD_DIR
    if DB_PATH:
        return os.path.join(os.path.dirname(DB_PATH), "parts_img")
    return os.path.join(BASE_DIR, "uploads", "parts")


# ------------------------------------------------------------------
# 权限模块定义（可配置权限矩阵的基础）
# ------------------------------------------------------------------
PERM_MODULES = {
    "dashboard":       "数据图表分析",
    "query":           "信息查询",
    "apply":           "出入库调库申请",
    "parts_manage":    "信息维护(建档)",
    "stock_inout":     "出入库管理",
    "transfer":        "调库(已并入申请流程)",
    "approve":         "贵重物料出库审批",
    "departments":     "部门配置",
    "lines_equip":     "产线与设备配置",
    "user_manage":     "用户管理",
    "perm_config":     "权限配置",
    "stale":           "呆滞物料管理",
    "stocktake":       "盘点管理",
    "borrow":          "工具借用管理",
    "screen_config":   "大屏设置",
    "recycle":         "回收站",
    "price":           "物料金额查看（单价/金额，敏感内容）",
}

# 操作级权限（模块内细分操作；模块级权限=拥有全部操作，操作级用于更细粒度控制）
PERM_ACTIONS = {
    "parts_manage": {
        "add": "新增",
        "edit": "编辑",
        "delete": "删除",
        "import": "导入",
        "export": "导出",
    },
    "stock_inout": {
        "execute": "执行出入库",
        "reject": "驳回申请",
        "undo_approval": "撤销审批",
        "reassess": "待报废转待维修复判",
        "export": "导出出入库报表",
    },
    "user_manage": {
        "add": "新增用户",
        "edit": "编辑用户",
        "reset_pwd": "重置密码",
        "delete": "删除用户",
    },
    "perm_config": {
        "edit": "修改权限",
    },
    "apply": {
        "cancel": "撤销申请",
    },
    "stale": {
        "export": "导出呆滞清单",
    },
    "stocktake": {
        "create": "创建/录入盘点",
        "approve1": "盘点差异一级审批",
        "approve2": "盘点差异二级审批(终审)",
        "export": "导出盘点报表",
    },
    "approve": {
        "approve_out": "审批贵重出库",
    },
    "borrow": {
        "create": "发起借用",
        "return": "办理还回（仓管）",
        "manage": "借用管理（查看全部借用记录）",
        "export": "导出借用台账",
    },
}

# 各角色默认权限（首次建库时写入；之后可在「权限配置」页面调整）
DEFAULT_PERMS = {
    "admin":     ["dashboard", "query", "apply", "parts_manage", "stock_inout",
                  "transfer", "approve", "departments", "lines_equip", "user_manage", "perm_config",
                  "stale", "stocktake", "borrow", "screen_config", "recycle", "price"],
    "manager":   ["dashboard", "query", "apply", "approve", "stale", "stocktake", "borrow", "price"],
    "warehouse": ["dashboard", "query", "apply", "parts_manage", "stock_inout", "transfer", "stale",
                  "stocktake", "borrow", "screen_config", "recycle", "price"],
    "user":      ["dashboard", "query", "apply", "borrow"],
    "guest":     ["query", "stale"],   # 访客（AD 自动进入）：仅信息查询 + 呆滞物料查阅
}

# 各角色默认操作级权限（按职责级别配置，仅列与模块级默认不同的细化项）
# 说明：操作级行存在即生效（优先于模块级）。模块级已勾选但某项操作级=0 时，该操作被禁止。
DEFAULT_PERM_ACTIONS = {
    # 高级管理人员：全部模块的细分操作都默认开放
    "admin": {
        "parts_manage": {"add": 1, "edit": 1, "delete": 1, "import": 1, "export": 1},
        "stock_inout": {"execute": 1, "reject": 1, "undo_approval": 1, "reassess": 1, "export": 1},
        "user_manage": {"add": 1, "edit": 1, "reset_pwd": 1, "delete": 1},
        "perm_config": {"edit": 1},
        "apply": {"cancel": 1},
        "stale": {"export": 1},
        "approve": {"approve_out": 1},
        "stocktake": {"create": 1, "approve1": 1, "approve2": 1, "export": 1},
        "borrow": {"create": 1, "return": 1, "manage": 1, "export": 1},
    },
    # 部门管理：可申请/审批贵重出库/审批盘点差异/看盘点报告/发起借用。
    # 不参与仓库作业：不能执行出入库、不能办理借用还回、不能看全量借用台账、
    # 不能新建盘点（盘点只由仓管与管理员创建，部门管理只看报告并审差异）。
    "manager": {
        "apply": {"cancel": 1},
        "stale": {"export": 1},
        "approve": {"approve_out": 1},
        "stocktake": {"create": 0, "approve1": 1, "approve2": 0, "export": 1},
        "borrow": {"create": 1, "return": 0, "manage": 0, "export": 0},
    },
    # 仓库管理人员：建档/出入库执行/调库/撤销申请/导出（各项全开；导入物料默认关闭——默认仅最高用户可导入，可按需勾选）
    "warehouse": {
        "parts_manage": {"add": 1, "edit": 1, "delete": 1, "import": 0, "export": 1},
        "stock_inout": {"execute": 1, "reject": 1, "undo_approval": 1, "reassess": 1, "export": 1},
        "apply": {"cancel": 1},
        "stale": {"export": 1},
        "stocktake": {"create": 1, "approve1": 1, "approve2": 0, "export": 1},
        "borrow": {"create": 1, "return": 1, "manage": 1, "export": 1},
    },
    # 普通用户：只能申请与撤销自己的申请（其余操作一律未配置=跟随模块级，模块级本身未开放）
    "user": {
        "apply": {"cancel": 1},
        "borrow": {"create": 1, "return": 0, "manage": 0, "export": 0},
    },
    # 访客：仅查阅，禁止一切导出
    "guest": {"stale": {"export": 0}, "query": {"export": 0}},
}

# 角色中文名
ROLE_NAMES = {
    "admin":     "高级管理人员",
    "manager":   "部门管理",
    "warehouse": "仓库管理人员",
    "user":      "普通用户",
    "guest":     "访客（AD 未在白名单自动进入）",
}

# ------------------------------------------------------------------
# 数据库自动备份（管理员在系统设置页配置；备份文件与数据库同级 backups 目录）
# ------------------------------------------------------------------
DEFAULT_BACKUP = {
    "enabled": 0,           # 0=关闭 1=开启（软件运行期间后台线程自动检查并按间隔备份）
    "interval_hours": 24,   # 备份间隔（小时）：1/6/12/24/72/168(每周)/720(每月)
    "keep": 10,             # 保留最近 N 份备份，自动删除更早的
    "dir": "",              # 备份目录（空=数据库同级 backups 文件夹）
}


# ------------------------------------------------------------------
# 大屏展示配置
# ------------------------------------------------------------------
SCREEN_MODULES = {
    "stat_cards": "出入库统计卡片",
    "stock_overview": "库存总览",
    "qty_trend":  "出入库数量趋势",
    "amt_trend":  "出入库金额趋势",
    "category":   "分类库存占比",
    "alerts":     "库存上下限预警",
    "recent_tx":  "最新出入库流水",
    "pending":    "待处理申请",
    "stale":      "呆滞物料监控",
    "usage_line_qty":   "线体领用数量柱状图",
    "usage_line_amt":   "线体领用金额柱状图",
    "usage_device_qty": "设备领用数量柱状图",
    "usage_device_amt": "设备领用金额柱状图",
}

# 大屏可排版卡片单元（key -> 名称 + 所属模块）。模块未勾选时整卡隐藏。
SCREEN_CARDS = {
    "stat_in_qty":  {"label": "日度入库数量", "module": "stat_cards"},
    "stat_in_amt":  {"label": "日度入库金额", "module": "stat_cards"},
    "stat_out_qty": {"label": "日度出库数量", "module": "stat_cards"},
    "stat_out_amt": {"label": "日度出库金额", "module": "stat_cards"},
    "stock_total":  {"label": "库存总览", "module": "stock_overview"},
    "pending":      {"label": "待处理申请", "module": "pending"},
    "qty_trend":    {"label": "出入库数量趋势", "module": "qty_trend"},
    "amt_trend":    {"label": "出入库金额趋势", "module": "amt_trend"},
    # 分类占比卡：有金额版并排画「金额 / 数量」两个饼；无金额版只画数量饼。
    # 所以它不属于「纯金额卡」，无金额布局里也会出现。
    "category":     {"label": "分类库存占比", "module": "category"},
    "alerts":       {"label": "库存上下限预警", "module": "alerts"},
    "stale":        {"label": "呆滞物料监控", "module": "stale"},
    "recent_tx":    {"label": "最新出入库流水", "module": "recent_tx"},
    "line_qty":     {"label": "线体领用数量", "module": "usage_line_qty"},
    "line_amt":     {"label": "线体领用金额", "module": "usage_line_amt"},
    "dev_qty":      {"label": "设备领用数量", "module": "usage_device_qty"},
    "dev_amt":      {"label": "设备领用金额", "module": "usage_device_amt"},
}

# 大屏默认排列组合（key -> 名称 + 每张卡的位置）；网格尺寸由前端布局决定。

SCREEN_LAYOUTS = {
    "classic": {
        "name": "经典布局",
        "cards": {
            # 6 个 KPI 卡并成一排（入库数量/出库数量/待处理/库存总览/入库金额/出库金额），
            # 各 2.0 格等分（12/6），高度压到 1 格，把纵向空间让给下面的图表
            "stat_in_qty":  (0, 0, 2, 1), "stat_out_qty": (2, 0, 2, 1), "pending": (4, 0, 2, 1),
            "stock_total":  (6, 0, 2, 1),
            "stat_in_amt":  (8, 0, 2, 1), "stat_out_amt": (10, 0, 2, 1),
            # 以下 5 行图表卡统一 2.2 格高：数量榜 / 金额榜 / 占比 / 预警 / 呆滞 / 流水
            # 一眼看过去高度齐平，哪一行都不会被挤成一条线
            "qty_trend":    (0, 1, 6, 2.2), "amt_trend":    (6, 1, 6, 2.2),
            # 线体/设备 四张柱图做成 2×2 等宽方块（各 4 格），横看线体↔设备、竖看数量↔金额，
            # 眼睛不用来回找；右侧 4 格留给分类占比，一张饼跨满这两行（4.4 格高），
            # 否则左 3 张 4 格、下面 2 张 6 格，宽度参差看着就乱。
            "line_qty":     (0, 3.2, 4, 2.2), "dev_qty":       (4, 3.2, 4, 2.2),
            "line_amt":     (0, 5.4, 4, 2.2), "dev_amt":       (4, 5.4, 4, 2.2),
            "category":     (8, 3.2, 4, 4.4),
            "alerts":       (0, 7.6, 6, 2.2), "stale":       (6, 7.6, 6, 2.2),
            "recent_tx":    (0, 9.8, 12, 2.2),
        },
        # 无金额版
        "noprice": {
            "stat_in_qty":  (0, 0, 3, 1), "stat_out_qty": (3, 0, 3, 1), "pending": (6, 0, 3, 1),
            "stock_total":  (9, 0, 3, 1),
            # 4 行图表卡同样等高：2.75 = (12 - 1) / 4
            "qty_trend":    (0, 1, 8, 2.75), "category":    (8, 1, 4, 2.75),
            "line_qty":     (0, 3.75, 6, 2.75), "dev_qty":     (6, 3.75, 6, 2.75),
            "alerts":       (0, 6.5, 6, 2.75), "stale":       (6, 6.5, 6, 2.75),
            "recent_tx":    (0, 9.25, 12, 2.75),
        },
    },
    "split": {
        "name": "左图右表",
        "cards": {
            "stat_in_qty":  (0, 0, 3, 1.5), "stat_out_qty": (3, 0, 3, 1.5), "pending":      (6, 0, 3, 1.5),
            "stock_total":  (9, 0, 3, 1.5),
            "stat_in_amt":  (0, 1.5, 6, 1.5), "stat_out_amt": (6, 1.5, 6, 1.5),
            "qty_trend":    (0, 3, 6, 4), "recent_tx":    (6, 3, 6, 6),
            "amt_trend":    (0, 7, 6, 2),
            # 分类占比卡要容下「金额 / 数量」两个饼，给 6 格；预警/呆滞各 3 格
            "category":     (0, 9, 6, 1.5), "alerts":       (6, 9, 3, 1.5),
            "stale":        (9, 9, 3, 1.5),
            "line_qty":     (0, 10.5, 3, 1.5), "line_amt":    (3, 10.5, 3, 1.5),
            "dev_qty":      (6, 10.5, 3, 1.5), "dev_amt":     (9, 10.5, 3, 1.5),
        },
        "noprice": {
            "stat_in_qty":  (0, 0, 3, 1.5), "stat_out_qty": (3, 0, 3, 1.5), "pending": (6, 0, 3, 1.5),
            "stock_total":  (9, 0, 3, 1.5),
            # 趋势图让出 3 格给分类占比饼（4+3+5=12）
            "qty_trend":    (0, 1.5, 4, 7), "category":     (4, 1.5, 3, 7),
            "recent_tx":    (7, 1.5, 5, 7),
            "line_qty":     (0, 8.5, 6, 3.5), "dev_qty":     (6, 8.5, 6, 3.5),
        },
    },
    "grid": {
        "name": "均布网格",
        "cards": {
            "stat_in_qty":  (0, 0, 3, 1.5), "stat_out_qty": (3, 0, 3, 1.5), "pending":      (6, 0, 3, 1.5),
            "stock_total":  (9, 0, 3, 1.5),
            "stat_in_amt":  (0, 1.5, 6, 1.5), "stat_out_amt": (6, 1.5, 6, 1.5),
            "qty_trend":    (0, 3, 4, 4), "amt_trend":    (4, 3, 4, 4),
            "recent_tx":    (8, 3, 4, 4),
            "category":     (0, 7, 6, 2), "alerts":       (6, 7, 3, 2),
            "stale":        (9, 7, 3, 2),
            "line_qty":     (0, 9, 3, 3), "line_amt":     (3, 9, 3, 3),
            "dev_qty":      (6, 9, 3, 3), "dev_amt":      (9, 9, 3, 3),
        },
        "noprice": {
            # 旧版 stat 与 pending 都写在 y=0 满宽/半宽互相叠（历史遗留），这次按 4 卡等分理顺
            "stat_in_qty":  (0, 0, 3, 1.5), "stat_out_qty": (3, 0, 3, 1.5),
            "pending":      (6, 0, 3, 1.5), "stock_total":  (9, 0, 3, 1.5),
            # 右侧 4 格拆成「线体柱图 2 格 + 分类占比饼 2 格」，正好接住左列趋势图的 4 格高
            "qty_trend":    (0, 1.5, 8, 4), "line_qty":     (8, 1.5, 4, 2),
            "category":     (8, 3.5, 4, 2),
            "dev_qty":      (0, 5.5, 4, 3), "alerts":       (4, 5.5, 4, 3),
            "stale":        (8, 5.5, 4, 3),
            "recent_tx":    (0, 8.5, 12, 3.5),
        },
    },
    "flow": {
        "name": "流水优先",
        "cards": {
            "recent_tx":    (0, 0, 12, 4),
            "stat_in_qty":  (0, 4, 3, 1.5), "stat_out_qty": (3, 4, 3, 1.5), "pending": (6, 4, 3, 1.5),
            "stock_total":  (9, 4, 3, 1.5),
            "stat_in_amt":  (0, 5.5, 6, 1.5), "stat_out_amt": (6, 5.5, 6, 1.5),
            "qty_trend":    (0, 7, 6, 2.5), "amt_trend":    (6, 7, 6, 2.5),
            "category":     (0, 9.5, 6, 2.5), "alerts":       (6, 9.5, 3, 2.5),
            "stale":        (9, 9.5, 3, 2.5),
        },
        "noprice": {
            "recent_tx":    (0, 0, 12, 4),
            "stat_in_qty":  (0, 4, 3, 1.5), "stat_out_qty": (3, 4, 3, 1.5), "pending": (6, 4, 3, 1.5),
            "stock_total":  (9, 4, 3, 1.5),
            "qty_trend":    (0, 5.5, 8, 3), "category":     (8, 5.5, 4, 3),
            "line_qty":     (0, 8.5, 6, 3.5), "dev_qty":     (6, 8.5, 6, 3.5),
        },
    },
    "wide": {
        "name": "宽屏矩阵",
        "cards": {
            # 6 张 KPI 卡并成一排等分（12/6=2.0）：入库数量 / 出库数量 / 待处理 / 库存总览 / 入库金额 / 出库金额
            "stat_in_qty":  (0, 0, 2, 1.2), "stat_out_qty": (2, 0, 2, 1.2),
            "pending":      (4, 0, 2, 1.2), "stock_total":  (6, 0, 2, 1.2),
            "stat_in_amt":  (8, 0, 2, 1.2), "stat_out_amt": (10, 0, 2, 1.2),
            # 趋势图（2.7 格，与下一行 3.9 起的中排正好接上，老版 2.8 会压住中排 0.1 格）
            "qty_trend":    (0, 1.2, 6, 2.7), "amt_trend":    (6, 1.2, 6, 2.7),
            # 中间一排三张：占比（6 格，容两个饼）/ 预警 / 呆滞
            "category":     (0, 3.9, 6, 2.2), "alerts":       (6, 3.9, 3, 2.2),
            "stale":        (9, 3.9, 3, 2.2),
            # 线体 / 设备 四张等高等宽一排：2.6 格（旧版 2 格太扁，
            # 四张的坐标轴刻度会挤在一起，看着高矮不齐）
            "line_qty":     (0, 6.1, 3, 2.6), "line_amt":     (3, 6.1, 3, 2.6),
            "dev_qty":      (6, 6.1, 3, 2.6), "dev_amt":      (9, 6.1, 3, 2.6),
            "recent_tx":    (0, 8.7, 12, 3.3),
        },
        "noprice": {
            # 无金额版：四张 KPI 等分一排（旧版 pending 与上面两张同在 y=0 且满宽，
            # 会叠在一起，被挤成 1/3 + 1/3 + 1/2 的怪比例）
            "stat_in_qty":  (0, 0, 3, 1.1), "stat_out_qty": (3, 0, 3, 1.1),
            "pending":      (6, 0, 3, 1.1), "stock_total":  (9, 0, 3, 1.1),
            "qty_trend":    (0, 1.1, 8, 2.9), "category":    (8, 1.1, 4, 2.9),
            "line_qty":     (0, 4, 6, 2.9), "dev_qty":     (6, 4, 6, 2.9),
            "alerts":       (0, 6.9, 6, 2.6), "stale":      (6, 6.9, 6, 2.6),
            "recent_tx":    (0, 9.5, 12, 2.5),
        },
    },
    # 无金额版独立布局（与上方带金额版一一对应；不含金额类卡片，避免留空，已重新优化排列）
    "classic_np": {
        "name": "经典布局（无金额）",
        "cards": {
            "stat_in_qty":  (0, 0, 3, 1), "stat_out_qty": (3, 0, 3, 1),
            "pending":      (6, 0, 3, 1), "stock_total":  (9, 0, 3, 1),
            "qty_trend":    (0, 1, 12, 3),
            "line_qty":     (0, 4, 6, 2.5), "dev_qty":      (6, 4, 6, 2.5),
            "alerts":       (0, 6.5, 6, 2.5), "stale":        (6, 6.5, 6, 2.5),
            "recent_tx":    (0, 9, 12, 3),
        },
    },
    "split_np": {
        "name": "左图右表（无金额）",
        "cards": {
            "stat_in_qty":  (0, 0, 3, 1), "stat_out_qty": (3, 0, 3, 1),
            "pending":      (6, 0, 3, 1), "stock_total":  (9, 0, 3, 1),
            "qty_trend":    (0, 1, 7, 7.5), "recent_tx":    (7, 1, 5, 7.5),
            "line_qty":     (0, 8.5, 6, 3.5), "dev_qty":     (6, 8.5, 6, 3.5),
        },
    },
    "grid_np": {
        "name": "均布网格（无金额）",
        "cards": {
            "stat_in_qty":  (0, 0, 3, 1), "stat_out_qty": (3, 0, 3, 1),
            "pending":      (6, 0, 3, 1), "stock_total":  (9, 0, 3, 1),
            "qty_trend":    (0, 1, 12, 4),
            "line_qty":     (0, 5, 6, 2), "dev_qty":     (6, 5, 6, 2),
            "alerts":       (0, 7, 6, 2), "stale":       (6, 7, 6, 2),
            "recent_tx":    (0, 9, 12, 3),
        },
    },
    "flow_np": {
        "name": "流水优先（无金额）",
        "cards": {
            "recent_tx":    (0, 0, 12, 4),
            "stat_in_qty":  (0, 4, 3, 1), "stat_out_qty": (3, 4, 3, 1),
            "pending":      (6, 4, 3, 1), "stock_total":  (9, 4, 3, 1),
            "qty_trend":    (0, 5, 12, 3.5),
            "line_qty":     (0, 8.5, 6, 3.5), "dev_qty":     (6, 8.5, 6, 3.5),
        },
    },
    "wide_np": {
        "name": "宽屏矩阵（无金额）",
        "cards": {
            "stat_in_qty":  (0, 0, 3, 1), "stat_out_qty": (3, 0, 3, 1),
            "pending":      (6, 0, 3, 1), "stock_total":  (9, 0, 3, 1),
            "qty_trend":    (0, 1, 12, 3),
            "line_qty":     (0, 4, 4, 2), "dev_qty":     (4, 4, 4, 2),
            "alerts":       (8, 4, 4, 2),
            "stale":        (0, 6, 4, 6), "recent_tx":    (4, 6, 8, 6),
        },
    },
    # ---------------- 高端模板（布局 + app.SCREEN_STYLES 配色 + screen.html 皮肤三件套） ----------------
    # 与经典系列的差别：KPI 卡加高（1.5 格，数字更有存在感）、行距统一、
    # 每行等高对齐，配合独立皮肤（玻璃拟态/鎏金/HUD 四角括号）整体换脸。
    "aurora": {
        "name": "深空极光",
        "hidden": True,   # 排版已归并到五套，仅保留坐标供老库解析

        "cards": {
            "stat_in_qty":  (0, 0, 2, 1.5), "stat_out_qty": (2, 0, 2, 1.5),
            "pending":      (4, 0, 2, 1.5), "stock_total":  (6, 0, 2, 1.5),
            "stat_in_amt":  (8, 0, 2, 1.5), "stat_out_amt": (10, 0, 2, 1.5),
            "qty_trend":    (0, 1.5, 6, 2.7), "amt_trend":    (6, 1.5, 6, 2.7),
            # 中排：分类占比 6 格（两个饼并排）/ 线体 3 格 / 设备 3 格
            "category":     (0, 4.2, 6, 2.9), "line_qty":     (6, 4.2, 3, 2.9),
            "dev_qty":      (9, 4.2, 3, 2.9),
            "alerts":       (0, 7.1, 6, 2.9), "stale":        (6, 7.1, 6, 2.9),
            "line_amt":     (0, 10, 3, 2), "dev_amt":       (3, 10, 3, 2),
            "recent_tx":    (6, 10, 6, 2),
        },
        # 无金额版：总额高恰好 12 格（1.5+3.5+3+4）
        "noprice": {
            "stat_in_qty":  (0, 0, 3, 1.5), "stat_out_qty": (3, 0, 3, 1.5), "pending": (6, 0, 3, 1.5),
            "stock_total":  (9, 0, 3, 1.5),
            # 趋势 / 分类饼 / 流水三等分这一行（4+4+4=12）
            "qty_trend":    (0, 1.5, 4, 3.5), "category":    (4, 1.5, 4, 3.5),
            "recent_tx":   (8, 1.5, 4, 3.5),
            "line_qty":     (0, 5, 6, 3), "dev_qty":        (6, 5, 6, 3),
            "alerts":       (0, 8, 6, 4), "stale":          (6, 8, 6, 4),
        },
    },
    "obsidian": {
        "name": "曜黑鎏金",
        "hidden": True,

        "cards": {
            "stat_in_qty":  (0, 0, 2, 1.4), "stat_out_qty": (2, 0, 2, 1.4),
            "pending":      (4, 0, 2, 1.4), "stock_total":  (6, 0, 2, 1.4),
            "stat_in_amt":  (8, 0, 2, 1.4), "stat_out_amt": (10, 0, 2, 1.4),
            # 主趋势给 7 格宽当主角，金额趋势 5 格做副位
            "qty_trend":    (0, 1.4, 7, 3.1), "amt_trend":    (7, 1.4, 5, 3.1),
            "category":     (0, 4.5, 4, 3.6), "alerts":       (4, 4.5, 4, 3.6),
            "stale":        (8, 4.5, 4, 3.6),
            "line_qty":     (0, 8.1, 3, 2), "line_amt":      (3, 8.1, 3, 2),
            "dev_qty":      (6, 8.1, 3, 2), "dev_amt":       (9, 8.1, 3, 2),
            "recent_tx":    (0, 10.1, 12, 1.9),
        },
        "noprice": {
            "stat_in_qty":  (0, 0, 3, 1.4), "stat_out_qty": (3, 0, 3, 1.4), "pending": (6, 0, 3, 1.4),
            "stock_total":  (9, 0, 3, 1.4),
            "qty_trend":    (0, 1.4, 8, 3.4), "recent_tx":   (8, 1.4, 4, 3.4),
            "line_qty":     (0, 4.8, 4, 3.6), "dev_qty":     (4, 4.8, 4, 3.6),
            "alerts":       (8, 4.8, 4, 3.6),
            "stale":        (0, 8.4, 8, 3.6), "category":    (8, 8.4, 4, 3.6),
        },
    },
    "command": {
        "name": "指挥舱蓝",
        "hidden": True,

        "cards": {
            "stat_in_qty":  (0, 0, 2, 1.5), "stat_out_qty": (2, 0, 2, 1.5),
            "pending":      (4, 0, 2, 1.5), "stock_total":  (6, 0, 2, 1.5),
            "stat_in_amt":  (8, 0, 2, 1.5), "stat_out_amt": (10, 0, 2, 1.5),
            "qty_trend":    (0, 1.5, 3, 3), "amt_trend":     (3, 1.5, 3, 3),
            "category":     (6, 1.5, 6, 3),
            "line_qty":     (0, 4.5, 3, 2.7), "line_amt":    (3, 4.5, 3, 2.7),
            "dev_qty":      (6, 4.5, 3, 2.7), "dev_amt":     (9, 4.5, 3, 2.7),
            "alerts":       (0, 7.2, 6, 2.4), "stale":       (6, 7.2, 6, 2.4),
            "recent_tx":    (0, 9.6, 12, 2.4),
        },
        "noprice": {
            "stat_in_qty":  (0, 0, 3, 1.5), "stat_out_qty": (3, 0, 3, 1.5), "pending": (6, 0, 3, 1.5),
            "stock_total":  (9, 0, 3, 1.5),
            "qty_trend":    (0, 1.5, 4, 3.3), "category":    (4, 1.5, 3, 3.3),
            "recent_tx":   (7, 1.5, 5, 3.3),
            "line_qty":     (0, 4.8, 4, 3.4), "dev_qty":     (4, 4.8, 4, 3.4),
            "alerts":       (8, 4.8, 4, 3.4),
            "stale":        (0, 8.2, 12, 3.8),
        },
    },
}

DEFAULT_SCREEN = {
    "modules": ["stat_cards", "stock_overview", "qty_trend", "amt_trend", "category", "alerts", "recent_tx",
                "pending", "stale",
                "usage_line_qty", "usage_line_amt",
                "usage_device_qty", "usage_device_amt"],
    "refresh": 30,       # 刷新间隔（秒），默认 30
    "show_price": 0,     # 大屏是否显示金额类卡片（单价/金额为敏感内容，默认不展示；管理员可在大屏配置中开启）
    "period": "day",     # 统计周期 day/week/month
    "theme": "dark",     # dark 深色 / light 浅色
    # 各列表模块显示条数
    "limits": {"recent_tx": 10, "alerts": 10, "stale": 10},
    # 趋势图回溯长度：按日=近N天，按周=近N周，按月=近N月
    "trend": {"day": 14, "week": 8, "month": 6},
    # 大屏呆滞判定天数；0 = 跟随系统设置（默认90天）
    "stale_days": 0,
    # 大屏标题与 LOGO（空/0 = 跟随系统设置的侧栏品牌；sb_show_logo=0 则不显示 LOGO）
    "sb_title": "",
    "sb_subtitle": "",
    "sb_show_logo": 1,
    "sb_logo_height": 48,     # 大屏 LOGO 高度默认值；填 0 = 跟随系统品牌高度
    "sb_title_size": 28,      # 大屏标题字号默认值；填 0 = 跟随系统品牌字号
    "sb_subtitle_size": 14,   # 大屏副标题字号默认值；填 0 = 跟随系统品牌字号
    "sb_title_color": "",
    "sb_subtitle_color": "",
    "sb_font_family": "",
    # 大屏风格：预设名（tech-blue/command/aurora/obsidian/green-data/light-clean）或 custom（手动）
    # 老的 tech-cyan / midnight-gold / nebula-purple 由 app.SCREEN_STYLE_ALIASES 收敛到现存那套
    "style": "command",
    "palette": "deepblue",
    # 手动自定义风格色值（custom 时生效，空项跟随科技蓝默认）
    "style_custom": {},
    # 卡片布局：mode = 预设名(classic/split/grid/flow/wide) 或 custom（手动排版）
    # cards = {卡片key: {"x":..,"y":..,"w":..,"h":..}}，网格 12 列 x 10 行
    "layout": {
        "mode": "classic",
        # version：price = 有金额版排布，np = 无金额版排布。
        # 勾选「大屏显示金额」时两版都能选；不勾选时强制 np（配置页左列置灰）。
        "version": "price",
        "cards": {k: {"x": v[0], "y": v[1], "w": v[2], "h": v[3]}
                  for k, v in SCREEN_LAYOUTS["classic"]["cards"].items()},
        "noprice": {k: {"x": v[0], "y": v[1], "w": v[2], "h": v[3]}
                    for k, v in SCREEN_LAYOUTS["classic"]["noprice"].items()},
    },
}

# ------------------------------------------------------------------
# 界面主题（管理员可在系统设置页自由配置色系）
# 默认：侧边栏深蓝→墨蓝渐变 + 明亮蓝主色
# ------------------------------------------------------------------
DEFAULT_THEME = {
    "primary": "#3b82c4",    # 主色（按钮/菜单高亮，明亮蓝）
    "primary_d": "#2f6ea8",  # 主色深（hover）
    "sidebar": "#16314f",    # 侧边栏底色（渐变的起点兜底，深蓝）
    "sidebar_text": "#eaf3fb",  # 侧边栏文字
    "bg": "#f0f5fa",         # 页面背景（淡蓝灰）
}

# 自定义表头（物料列表额外列），存 sys_settings key=extra_fields
# 示例：[{"key": "f1", "label": "用途"}, {"key": "f2", "label": "责任人"}]
MAX_EXTRA_FIELDS = 8  # 最多可配置的自定义表头数量

# 物料分类字典（系统设置页可维护），存 sys_settings key=part_categories
# 建档/编辑/筛选的分类下拉都从这里取；未配置时用这份出厂值
DEFAULT_PART_CATEGORIES = ["备件", "辅料", "耗材", "工具", "夹具",
                           "电阻", "电容", "IC", "包材", "其他"]
MAX_CATEGORIES = 60        # 字典最多多少个分类
# 单个分类名的字符上限：**跟随表头设置里「分类」那一列**（默认 60），不再单独定死。
# 这里只是表头设置读不到时的兜底值（见 app.category_max_len）。
MAX_CATEGORY_LEN = 60

# 表头设置：物料列表各文本列最多显示多少个字符（超出显示省略号，悬停看全文）。
# 标准文本列与每个自定义列分别配置，存 sys_settings key=head_clip
# 示例：{"std": {"code": 60, "name": 60, ...}, "extra": {"f1": 60, ...}}
DEFAULT_HEAD_CLIP = 60     # 每列默认字符数
HEAD_CLIP_MIN, HEAD_CLIP_MAX = 4, 200
# 标准文本列（key, 设置页显示用中文）。数值/状态列不截断，不在这里。
STD_TEXT_COLS = (("code", "料号"), ("name", "名称"), ("category", "分类"),
                 ("spec", "规格"), ("locs", "存放库位"), ("apps", "适用产线/设备"))

# 自定义表头每列的字段类型：文本 / 整数 / 小数 / 日期。
# 只影响「建档、编辑弹窗用什么录入控件」和保存时的校验，列表照原样显示。
EXTRA_FIELD_TYPES = ("text", "int", "decimal", "date")
DEFAULT_EXTRA_TYPE = "text"

# ------------------------------------------------------------------
# 左侧菜单外观（管理员可在系统设置页自行配置：字号/图标大小/侧栏宽度/字体等）
# 存 sys_settings key=nav_style
# ------------------------------------------------------------------
DEFAULT_NAV_STYLE = {
    "menu_width": 232,     # 侧栏宽度 px
    "font_size": 15,       # 菜单文字字号 px（更大更清晰）
    "icon_size": 19,       # 菜单图标尺寸 px（SVG 图标）
    "font_family": "",     # 菜单字体（空=跟随系统默认）
    "item_padding": 11,    # 菜单项上下内边距 px
    "item_radius": 10,     # 菜单项圆角 px
    "sidebar_top": "#14304f",     # 侧边栏渐变起点（深蓝）
    "sidebar_bottom": "#0e1e33",  # 侧边栏渐变终点（墨蓝）
}

# ------------------------------------------------------------------
# 对外数据接口（WEB API：物料库存查询 / 入库申请）
# 存 sys_settings key=open_api；启用后其他系统带 X-API-Token 头即可调用
# ------------------------------------------------------------------
DEFAULT_OPEN_API = {
    "enabled": False,   # 是否开放 WEB API
    "token": "",        # 接口密钥（调用方请求头 X-API-Token）
}
