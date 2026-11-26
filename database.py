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
数据库层：建表、初始化、通用查询辅助
使用 SQLite（单服务端进程访问，放在公共盘也安全）。
"""
import os
import sqlite3
import threading
import json
from datetime import datetime, timedelta

import config
import db as _db

# 全局写锁：SQLite 单进程下串行化写操作，避免并发写入冲突
_write_lock = threading.Lock()


def _migration_error(e):
    """启动迁移里 OperationalError 的统一分流（v1.0.26）。

    以前迁移代码一律 `except _db.OperationalError: pass`，于是
    "database is locked" / "disk I/O error" / "database disk image is malformed"
    全被静默吞掉：启动照常继续，留下半迁移的 schema，用户几天后才发现数据错。
    现在只放行「列/表已存在」这类预期错误；其余记录日志并中止 init_db，
    由 main() 捕获后跳 /dbconfig 让管理员修复，绝不带着坏库继续跑。

    【边界】严格检查只在 sqlite_smb（共享盘）模式生效；本地单机 SQLite
    与 MySQL/PostgreSQL 等服务器库保持 v1.0.25 原行为（直接忽略），
    确保本版改动不会影响后期换库部署。
    """
    try:
        import share_guard
        if not share_guard.enabled():
            return
    except Exception:  # noqa: BLE001
        return
    _s = str(e).lower()
    if any(k in _s for k in ("duplicate column", "already exists", "duplicate",
                             "no such table", "no such column")):
        return
    import logging as _lg
    _lg.getLogger("db").error("启动迁移遇到数据库错误（不忽略，中止启动）：%s", e)
    raise e


def _xt(text, **params):
    """库存校验类报错的取词（库位不存在 / 库存不足 …）。

    这些消息会一路 flash 到界面上，以前是 f-string 直接拼的，英文界面必漏中文。
    放在这里延迟导入 i18n2：database 层不依赖 Flask，也不该在导入期就
    拉起整个语言包；取不到词条时原样返回中文，中文环境零影响。
    """
    try:
        from i18n2 import t, current_lang
        return t(text, current_lang(), **params)
    except Exception:
        return text


def get_conn():
    """获取一个新的数据库连接（每请求一个连接，用完即关）
    支持多数据库类型（系统设置中配置）：sqlite/mysql/mariadb/postgresql/mssql/oracle
    - SQLite 自动开启 WAL / busy_timeout / 外键
    - 其它数据库按配置连接，SQL 方言在 db 抽象层自动转换
    """
    # v1.0.26：原先这里每次请求都执行 CREATE TABLE restore_apps（写 DDL）。
    # 共享盘 100 人同时只读时，这条 DDL 会让每个「读请求」都去碰共享库、
    # 抢写锁，既是并发瓶颈也是断连时的隐患。restore_apps 已由 init_db 的
    # 建表脚本（本文件约 811 行）保证存在，此处直接返回连接即可。
    return _db.connect(config.get_db_cfg())


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _migrate_transactions(conn):
    """兼容迁移：transactions 增加 line_name/device_name 列并回填历史申请数据"""
    cols = _db.list_columns(conn, "transactions")
    if "line_name" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN line_name TEXT DEFAULT ''")
    if "device_name" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN device_name TEXT DEFAULT ''")
    if "batch_no" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN batch_no TEXT DEFAULT ''")
    # 出库子类型（general/return/exchange/internal/giveback）：空 = 一般出库，
    # 与 applications.out_type 同口径。流水页要按子类型筛选，必须落在流水上，
    # 否则只能靠 ref_id 反查申请单，直接出库（ref_type=manual）就查不到。
    if "out_type" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN out_type TEXT DEFAULT ''")
    icols = _db.list_columns(conn, "inventory")
    if "batch_no" not in icols:
        conn.execute("ALTER TABLE inventory ADD COLUMN batch_no TEXT DEFAULT 'B-LEGACY'")
    if "in_date" not in icols:
        conn.execute("ALTER TABLE inventory ADD COLUMN in_date TEXT DEFAULT ''")
        conn.execute("UPDATE inventory SET in_date=? WHERE in_date=''", (today_str(),))
    # 历史回填的前提：applications.out_type 必须存在。全新库的建表 DDL 里没有这一列
    # （它由后面的迁移补上），以前这里直接 SELECT a.out_type 会抛
    # no such column: a.out_type，把整个 init_db 打断在「建完表、还没写 admin」的半路 ——
    # 表现就是新装客户能进初始化向导，建完库却一个账号都没有，怎么都登不进去。
    acols = _db.list_columns(conn, "applications")
    if "out_type" not in acols:
        conn.execute("ALTER TABLE applications ADD COLUMN out_type TEXT DEFAULT ''")
    # 历史回填：申请单出库流水的线体/设备
    conn.execute(
        """UPDATE transactions SET line_name=(
               SELECT a.line_name FROM applications a WHERE a.id=transactions.ref_id
           ) WHERE ref_type='application' AND tx_type='out'
             AND (line_name IS NULL OR line_name='')"""
    )
    # 历史回填：申请单出库流水的出库子类型（空值一律视为「一般」）
    conn.execute(
        """UPDATE transactions SET out_type=(
               SELECT CASE WHEN IFNULL(a.out_type,'')='' THEN 'general' ELSE a.out_type END
               FROM applications a WHERE a.id=transactions.ref_id
           ) WHERE ref_type='application' AND tx_type='out'
             AND (out_type IS NULL OR out_type='')"""
    )
    conn.execute(
        """UPDATE transactions SET device_name=(
               SELECT a.device_name FROM applications a WHERE a.id=transactions.ref_id
           ) WHERE ref_type='application' AND tx_type='out'
             AND (device_name IS NULL OR device_name='')"""
    )
    conn.commit()


def _migrate_users_lang(conn):
    """给老库补 users.lang 列。

    没有这一列时，通知邮件只能按「当前请求语言」渲染，而催办线程根本没有请求，
    结果就是俄语同事永远收到中文邮件。列加上后，偏好存在用户身上，谁的语言发给谁。
    """
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    except Exception:
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        except Exception:
            return
    if cols and "lang" not in cols:
        try:
            conn.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT ''")
            conn.commit()
        except Exception:
            pass


def _migrate_stocktake_report(conn):
    """盘点报告：原因分析 / 改善行动 / 明细差异原因 / 逐行改善行动任务。

    报告是要签字归档的纸质凭证，这些字段必须跟着单据长期保存，所以走列迁移
    而不是另建表 —— 老库直接升级，历史盘点单的这几列留空即可（报告里显示未填写）。
    v1.0.17 追加逐行原因分析与改善处理，老行 cause/action 为空、action_status
    默认 open，任务监控只认「有改善处理文本」的行，历史单不会凭空冒出任务。
    """
    plan = (
        ("stocktakes", (("cause", "TEXT DEFAULT ''"),
                        ("action", "TEXT DEFAULT ''"),
                        ("action_owner", "TEXT DEFAULT ''"),
                        ("action_due", "TEXT DEFAULT ''"),
                        ("analyzed_at", "TEXT DEFAULT ''"),
                        ("analyzed_by", "TEXT DEFAULT ''"),
                        ("cause_auto", "TEXT DEFAULT ''"),
                        ("action_auto", "TEXT DEFAULT ''"),
                        ("summary_manual", "INTEGER NOT NULL DEFAULT 0"),
                        # v1.0.41：延期原因说明。老库升级后留空，不影响历史单据。
                        ("delay_reason", "TEXT DEFAULT ''"))),
        ("stocktake_items", (("reason", "TEXT DEFAULT ''"),
                             ("cause", "TEXT DEFAULT ''"),
                             ("action", "TEXT DEFAULT ''"),
                             ("action_owner", "TEXT DEFAULT ''"),
                             ("action_due", "TEXT DEFAULT ''"),
                             ("action_status", "TEXT DEFAULT 'open'"),
                             ("action_done_at", "TEXT DEFAULT ''"),
                             ("action_done_by", "TEXT DEFAULT ''"))),
    )
    for table, cols in plan:
        try:
            have = _db.list_columns(conn, table)
        except Exception:
            continue
        for name, decl in cols:
            if name in have:
                continue
            try:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
            except Exception:
                pass
    conn.commit()


def _migrate_borrows(conn):
    """工具借用台账：CREATE TABLE IF NOT EXISTS 已覆盖新库，这里只做兼容兜底。

    若老库因某种原因没有 borrows 表，建之；已存在则不动。借用是新增模块，
    老库里不可能有数据，直接建表即可，不会丢单。
    """
    try:
        conn.execute(
            _db.ddl(_db.dialect(conn), """
            CREATE TABLE IF NOT EXISTS borrows(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                borrow_no TEXT NOT NULL,
                part_id INTEGER NOT NULL,
                part_code TEXT DEFAULT '',
                part_name TEXT DEFAULT '',
                unit TEXT DEFAULT '',
                qty REAL NOT NULL DEFAULT 1,
                borrower TEXT DEFAULT '',
                borrower_user_id INTEGER,
                borrowed_at TEXT DEFAULT '',
                expected_return TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'borrowed',
                confirmed_at TEXT DEFAULT '',
                confirmed_by INTEGER,
                cancel_note TEXT DEFAULT '',
                returned_at TEXT DEFAULT '',
                return_note TEXT DEFAULT '',
                warehouse TEXT DEFAULT '',
                location TEXT DEFAULT '',
                location_id INTEGER,
                return_location TEXT DEFAULT '',
                return_location_id INTEGER,
                note TEXT DEFAULT '',
                op_id INTEGER,
                return_op_id INTEGER,
                kind TEXT DEFAULT 'borrow',
                created_at TEXT,
                updated_at TEXT
            );
            """
            )
        )
        # 兼容老库：逐列补齐后加的字段
        #   note         —— 早期建表漏了「借用说明」，表单填了不落库
        #   confirmed_at/by —— 借用确认流程（普通用户提交=待确认，仓管确认后才算出借）
        #   cancel_note  —— 待确认单被驳回时的理由
        #   location_id  —— 借出库位的 id（以前只存文本，还回时没法默认回原库位）
        #   return_location/id —— 还回时实际放置的库位（默认原库位，可在弹窗里改选）
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(borrows)")]
            for _name, _ddl in (
                ("note", "TEXT DEFAULT ''"),
                ("confirmed_at", "TEXT DEFAULT ''"),
                ("confirmed_by", "INTEGER"),
                ("cancel_note", "TEXT DEFAULT ''"),
                ("location_id", "INTEGER"),
                ("return_location", "TEXT DEFAULT ''"),
                ("return_location_id", "INTEGER"),
                ("kind", "TEXT DEFAULT 'borrow'"),   # 台账类型：借用/换货/校验·测试/维修
                # v1.0.16：借·换·检·修 台账结构化字段（原先只塞进 note 文本）
                ("destination", "TEXT DEFAULT ''"),
                ("receiver", "TEXT DEFAULT ''"),
                ("contact", "TEXT DEFAULT ''"),
                ("out_person", "TEXT DEFAULT ''"),
                ("repair_type", "TEXT DEFAULT ''"),
                # v1.0.19：实扣库存 + 分次还回
                ("stock_deducted", "INTEGER DEFAULT 0"),
                ("deducted_qty", "REAL DEFAULT 0"),
                ("returned_qty", "REAL DEFAULT 0"),
                ("restocked_qty", "REAL DEFAULT 0"),
            ):
                if _name not in cols:
                    conn.execute("ALTER TABLE borrows ADD COLUMN %s %s" % (_name, _ddl))
        except Exception:
            pass
        # 老数据兜底：确认流程上线前登记的单据一律视为「已确认」，
        # 否则它们会掉进 pending 永远等确认，预警计时也会失效
        try:
            conn.execute(
                "UPDATE borrows SET confirmed_at=COALESCE(NULLIF(borrowed_at,''),created_at) "
                "WHERE status='borrowed' AND (confirmed_at IS NULL OR confirmed_at='')"
            )
        except Exception:
            pass
        # v2.9.46：取消「待确认」状态。
        # 借出现在只在出入库/调库页办理（办理即出借，台账直接落 borrowed），
        # 借用台账只负责办理还回；历史遗留的 pending 单据按「已借出」接管，避免长期占着库存。
        try:
            conn.execute(
                "UPDATE borrows SET status='borrowed', "
                "confirmed_at=COALESCE(NULLIF(confirmed_at,''),NULLIF(borrowed_at,''),created_at), "
                "borrowed_at=COALESCE(NULLIF(borrowed_at,''),created_at) "
                "WHERE status='pending'"
            )
        except Exception:
            pass
        conn.commit()
    except Exception:
        pass


def _migrate_borrow_stock_v1019(conn):
    """v1.0.19：借·换·检·修改为「办理即实扣库存」的历史数据补扣。

    老版本办理时只登记 borrows、不动 inventory（靠「在借占用可用量」）。升级后
    inventory 必须是真实余量，所以把 status='borrowed' 且尚未实扣的历史单按 id
    顺序逐单补扣。库存不足时扣到多少记多少（deducted_qty），绝不扣成负数；
    还回时按 deducted_qty 封顶回补，也不会凭空造出库存。

    幂等靠 stock_deducted 字段本身：新版本办理链一律写入 1，不会被重复扫描。
    故意挂在 init_db 末尾（演示数据播种之后），保证演示库里的在借单也被补齐。
    """
    try:
        rows = conn.execute(
            "SELECT id, part_id, qty FROM borrows "
            "WHERE status='borrowed' AND COALESCE(stock_deducted,0)=0 "
            "ORDER BY id").fetchall()
        migrated, got_all, short_all = 0, 0.0, 0.0
        for r in rows:
            qty = float(r["qty"] or 0)
            if qty <= 0:
                conn.execute("UPDATE borrows SET stock_deducted=1 WHERE id=?", (r["id"],))
                continue
            left = qty
            for inv in conn.execute(
                    "SELECT id, quantity FROM inventory WHERE part_id=? AND quantity>0 ORDER BY id",
                    (r["part_id"],)).fetchall():
                if left <= 1e-9:
                    break
                take = min(float(inv["quantity"] or 0), left)
                if take <= 1e-9:
                    continue
                cur = conn.execute(
                    "UPDATE inventory SET quantity=quantity-? WHERE id=? AND quantity+1e-9>=?",
                    (take, inv["id"], take))
                if cur.rowcount != 1:
                    continue
                left -= take
            got = qty - left
            conn.execute(
                "UPDATE borrows SET stock_deducted=1, deducted_qty=?, "
                "returned_qty=COALESCE(returned_qty,0), restocked_qty=COALESCE(restocked_qty,0) "
                "WHERE id=?", (got, r["id"]))
            got_all += got
            short_all += left
            migrated += 1
        if rows:
            set_setting(conn, "borrow_stock_migrate_stat", json.dumps({
                "migrated": migrated, "deducted": round(got_all, 3),
                "shortage": round(short_all, 3)}, ensure_ascii=False))
        conn.commit()
    except Exception:
        pass


def _migrate_stocktake_cycle_v1020(conn):
    """v1.0.20：盘点周期与在库覆盖率所需的字段/表。

    - stocktakes.cycle_key：本期应盘日（周期统计归属，不能再用 created_at 直接分桶，
      否则「上期漏盘、本期补盘」会算到本期头上）
    - stocktakes.done_at：终审完成日（算提前/按期/超期，只能用完成日，不能用创建日）
    - stocktake_items.inv_id：建单时对应的 inventory.id，用于「本期已盘行」精确去重
      与审批时的按行校准（库位改名后按文本匹配会失配，见 P2-8）
    - stocktake_cycles：每期一行的分母快照与 ±容差窗口

    历史期没有分母快照，total_rows 留 0，统计侧按「无快照」处理（不伪造覆盖率）。
    """
    def _cols(table):
        try:
            rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
        except Exception:
            return []
        out = []
        for r in rows:
            try:
                out.append(r["name"])
            except (IndexError, KeyError, TypeError):
                out.append(r[1])
        return out

    try:
        sc = _cols("stocktakes")
        if sc and "cycle_key" not in sc:
            conn.execute("ALTER TABLE stocktakes ADD COLUMN cycle_key TEXT DEFAULT ''")
        if sc and "done_at" not in sc:
            conn.execute("ALTER TABLE stocktakes ADD COLUMN done_at TEXT DEFAULT ''")
        si = _cols("stocktake_items")
        if si and "inv_id" not in si:
            conn.execute("ALTER TABLE stocktake_items ADD COLUMN inv_id INTEGER")

        conn.execute("""CREATE TABLE IF NOT EXISTS stocktake_cycles(
            cycle_key TEXT PRIMARY KEY,
            due_from TEXT DEFAULT '',
            due_to TEXT DEFAULT '',
            total_rows INTEGER DEFAULT 0,
            total_qty REAL DEFAULT 0,
            scope_note TEXT DEFAULT '',
            missed_reason TEXT DEFAULT '',
            created_at TEXT,
            updated_at TEXT
        )""")
        # v1.0.67：未盘点期没有报告，也要能直接备注原因（与盘点的 delay_reason 分开存）
        _cc = _cols("stocktake_cycles")
        if _cc and "missed_reason" not in _cc:
            conn.execute("ALTER TABLE stocktake_cycles ADD COLUMN missed_reason TEXT DEFAULT ''")

        # 回填：应盘日按创建日（历史无更准的依据），完成日按 updated_at
        conn.execute(
            "UPDATE stocktakes SET cycle_key=substr(created_at,1,10) "
            "WHERE COALESCE(cycle_key,'')='' AND created_at IS NOT NULL")
        conn.execute(
            "UPDATE stocktakes SET done_at=COALESCE(updated_at,created_at) "
            "WHERE status='done' AND COALESCE(done_at,'')=''")
        # 回填 inv_id：同物料同库位多行时取最小 id（老数据无更精依据，回退链兜底）
        conn.execute(
            """UPDATE stocktake_items SET inv_id=(
                   SELECT i.id FROM inventory i
                   WHERE i.part_id=stocktake_items.part_id
                     AND IFNULL(i.location,'')=IFNULL(stocktake_items.location,'')
                   ORDER BY i.id LIMIT 1)
               WHERE inv_id IS NULL""")
        # 进程崩溃/重启后残留的「处理中」状态复位：启动瞬间不可能有请求正在校准，
        # 否则这张单会永远卡在 applying，列表里既不显示草稿也不显示待审。
        conn.execute(
            "UPDATE stocktakes SET status=CASE WHEN COALESCE(review_level,2)>=2 "
            "THEN 'pending2' ELSE 'pending1' END WHERE status='applying'")
        # v1.0.20：历史盘点流水补 out_type 标记。老版本正常盘盈/盘亏行没写 out_type，
        # 被「历史消耗 / 出库报表」当成业务出库统计（盘亏一次，下月安全库存就虚高）。
        conn.execute(
            "UPDATE transactions SET out_type='stocktake' "
            "WHERE ref_type='stocktake' AND IFNULL(out_type,'')=''")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _migrate_repair_stock_v1021(conn):
    """v1.0.21：维修品按「物料×库位」计数 + 盘点明细的「本期已盘」快照。

    - inventory.repair_qty：该行库存里有多少件是维修件（与 quantity 是包含关系）。
      维修件不单独拆库存行，否则同物料同库位会出现多行，盘点与占用口径全乱。
    - stocktake_items.prior_st/prior_qty/prior_at：建单时把「本期已被别的单据盘过」
      的结果快照落到行上。必须快照，不能运行时现算 —— 别人中途盘了另一张单，
      这张单的行会突然从可编辑变灰，现场没法解释。
    - 触发器 trg_inv_repair_out：任何路径扣减库存时，先扣维修件再扣新件。
      出库有 8 处调用 move_stock、还有调库/盘点直接 UPDATE inventory，
      逐个改容易漏，用触发器兜住全部路径。
    """
    def _cols(table):
        try:
            rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
        except Exception:
            return []
        out = []
        for r in rows:
            try:
                out.append(r["name"])
            except (IndexError, KeyError, TypeError):
                out.append(r[1])
        return out

    try:
        ic = _cols("inventory")
        if ic and "repair_qty" not in ic:
            conn.execute("ALTER TABLE inventory ADD COLUMN repair_qty REAL NOT NULL DEFAULT 0")
        si = _cols("stocktake_items")
        if si and "prior_st" not in si:
            conn.execute("ALTER TABLE stocktake_items ADD COLUMN prior_st TEXT DEFAULT ''")
        if si and "prior_qty" not in si:
            conn.execute("ALTER TABLE stocktake_items ADD COLUMN prior_qty REAL")
        if si and "prior_at" not in si:
            conn.execute("ALTER TABLE stocktake_items ADD COLUMN prior_at TEXT DEFAULT ''")

        # 老库兜底：维修件数必须落在 [0, 库存] 区间内
        conn.execute("UPDATE inventory SET repair_qty=0 WHERE IFNULL(repair_qty,0)<0")
        conn.execute("UPDATE inventory SET repair_qty=quantity "
                     "WHERE IFNULL(repair_qty,0)>quantity")

        conn.execute("DROP TRIGGER IF EXISTS trg_inv_repair_out")
        conn.execute("""CREATE TRIGGER trg_inv_repair_out
            AFTER UPDATE OF quantity ON inventory
            FOR EACH ROW WHEN NEW.quantity < OLD.quantity - 0.000000001
            BEGIN
              UPDATE inventory
                 SET repair_qty = MAX(0, MIN(NEW.quantity,
                         IFNULL(OLD.repair_qty,0) - (OLD.quantity - NEW.quantity)))
               WHERE id = NEW.id AND IFNULL(repair_qty,0) > 0;
            END""")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _migrate_condition_v1022(conn):
    """v1.0.22：配件好坏状态 + 货架属性。

    - inventory.condition：good(良品)/defective(不良品)/repair(维修品)，默认 good。
      与 repair_qty 协调：落库时 condition='repair' 的行令 repair_qty=quantity，
      保持 v1.0.21「任何路径扣库存先扣维修件」触发器不变。
    - locations.attribute：good(良品)/pending_repair(待维修)/pending_scrap(待报废)，
      默认 good（历史库位统一视为良品区，可放良品与维修品）。
    """
    def _cols(table):
        try:
            rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
        except Exception:
            return []
        out = []
        for r in rows:
            try:
                out.append(r["name"])
            except (IndexError, KeyError, TypeError):
                out.append(r[1])
        return out

    try:
        ic = _cols("inventory")
        if ic and "condition" not in ic:
            conn.execute(
                "ALTER TABLE inventory ADD COLUMN condition TEXT NOT NULL DEFAULT 'good'")
        lc = _cols("locations")
        if lc and "attribute" not in lc:
            conn.execute(
                "ALTER TABLE locations ADD COLUMN attribute TEXT NOT NULL DEFAULT 'good'")
        # 兜底：枚举值必须在三态内（错写/历史脏值归位）
        conn.execute(
            "UPDATE inventory SET condition='good' "
            "WHERE condition NOT IN ('good','defective','repair')")
        conn.execute(
            "UPDATE locations SET attribute='good' "
            "WHERE attribute NOT IN ('good','pending_repair','pending_scrap')")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _migrate_repair_return_v1023(conn):
    """v1.0.23：维修还回记录「是否修好」与回库状态。

    borrow_returns.repair_ok：0=未修好（不良品），1=已修好（维修品）
    borrow_returns.condition：回库落库状态 good/defective/repair
    历史记录默认 0 / good，不追溯改写库存，只保证新链路有列可写。
    v1.0.45：borrow_returns.repair_type 还回时选择的维修品类型；
    borrows.repair_ok 可空（NULL=未还回，0=未修好，1=已修好），供维修柏拉图区分。
    """
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(borrow_returns)").fetchall()]
    except Exception:
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(borrow_returns)").fetchall()]
        except Exception:
            return
    if not cols:
        return
    try:
        if "repair_ok" not in cols:
            conn.execute("ALTER TABLE borrow_returns ADD COLUMN repair_ok INTEGER NOT NULL DEFAULT 0")
        if "condition" not in cols:
            conn.execute("ALTER TABLE borrow_returns ADD COLUMN condition TEXT NOT NULL DEFAULT 'good'")
        if "repair_type" not in cols:
            conn.execute("ALTER TABLE borrow_returns ADD COLUMN repair_type TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE borrow_returns SET condition='good' "
                     "WHERE condition NOT IN ('good','defective','repair')")
        conn.execute("UPDATE borrow_returns SET repair_ok=0 WHERE repair_ok NOT IN (0,1)")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    # v1.0.45：borrows.repair_ok 可空列（柏拉图区分已修好/未修好；NULL=未还回）
    try:
        bcols = [r["name"] for r in conn.execute("PRAGMA table_info(borrows)").fetchall()]
        if bcols and "repair_ok" not in bcols:
            conn.execute("ALTER TABLE borrows ADD COLUMN repair_ok INTEGER")
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _migrate_scrap_batch_v1046(conn):
    """v1.0.46：待报废批量报废单模式标记 + 纸档签字附件关联。

    applications.doc_mode：0/空=普通单据；1=当前方式；2=免纸档批量；3=纸档批量。
    applications.attachment_required：1=模式3单据，审批前必须上传签字附件。
    scrap_attachments：一单多附件（图片本体存 images 表 BLOB，这里只存关联）。
    """
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(applications)").fetchall()]
    except Exception:
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(applications)").fetchall()]
        except Exception:
            cols = []
    if cols:
        try:
            if "doc_mode" not in cols:
                conn.execute("ALTER TABLE applications ADD COLUMN doc_mode INTEGER NOT NULL DEFAULT 0")
            if "attachment_required" not in cols:
                conn.execute("ALTER TABLE applications ADD COLUMN attachment_required INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS scrap_attachments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_no TEXT NOT NULL,
            image_name TEXT NOT NULL,
            uploader_id INTEGER,
            uploaded_at TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_scrap_attach_doc ON scrap_attachments(doc_no)")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _migrate_doc_submit_v1051(conn):
    """v1.0.51：纸档报废单「上传附件后需点提交才进审批」的提交闸门标记。

    applications.doc_submitted：0=待提交签字档（不进审批），1=已提交（进审批）。
    默认 1 —— 历史单据/普通单据一律视为已提交，行为不变；只有新建的纸档模式
    （attachment_required=1）单据建单时显式写 0，等申请人上传签字档并点「提交审批」
    后才置 1。这样老库升级无需数据迁移，也不影响非纸档单据。
    """
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(applications)").fetchall()]
    except Exception:
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(applications)").fetchall()]
        except Exception:
            cols = []
    if cols and "doc_submitted" not in cols:
        try:
            conn.execute("ALTER TABLE applications ADD COLUMN doc_submitted INTEGER NOT NULL DEFAULT 1")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass


def _migrate_borrow_app(conn):
    """借用申请：applications 表补 expected_return 列（预计归还日）。

    借用走申请单流程后，归还期限跟着申请单走；老库直接补列，历史申请留空无影响。
    """
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(applications)").fetchall()]
    except Exception:
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(applications)").fetchall()]
        except Exception:
            return
    if cols and "expected_return" not in cols:
        try:
            conn.execute("ALTER TABLE applications ADD COLUMN expected_return TEXT DEFAULT ''")
            conn.commit()
        except Exception:
            pass


def _migrate_role_perms(conn):
    """一次性纠正「部门管理」被误放开的三项仓库作业权限。

    历史默认矩阵把并入了模块级权限：borrow 模块一开，manage/return 就跟着开，
    于是部门管理能看全公司借用台账、还能替仓管点「办理还回」；stocktake 模块
    未配置操作级，create 回落到模块级，部门管理也能新建盘点。

    这里只修一次（sys_settings 里留标记），之后管理员在权限配置页面手动调整
    不会再被覆盖。仓管与管理员不受影响。
    """
    try:
        row = conn.execute(
            "SELECT value FROM sys_settings WHERE key='perm_fix_manager_101'").fetchone()
        if row:
            return
        fixes = [
            ("borrow", "manage", 0),
            ("borrow", "return", 0),
            ("borrow", "export", 0),
            ("stocktake", "create", 0),
        ]
        for module, action, allow in fixes:
            cur = conn.execute(
                "SELECT id FROM permissions WHERE role='manager' AND module=? AND action=?",
                (module, action),
            ).fetchone()
            if cur:
                conn.execute(
                    "UPDATE permissions SET allow=? WHERE role='manager' AND module=? AND action=?",
                    (allow, module, action))
            else:
                conn.execute(
                    "INSERT INTO permissions(role,module,action,allow) VALUES(?,?,?,?)",
                    ("manager", module, action, allow))
        conn.execute("INSERT INTO sys_settings(key,value) VALUES(?,?)",
                     ("perm_fix_manager_101", "1"))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        import logging as _lg
        _lg.getLogger("db").warning("部门管理权限纠正失败：%s", e)


def _migrate_qty_int(conn):
    """把「数量类型=整数」的物料里已经存成小数的数量归整（四舍五入取整）。

    以前导入不做数量类型转换：Excel 里填 5000.332，即使该物料是 int 计数，
    也会原样写进 inventory.quantity —— 列表显示 5000.33，出库时这 0.33 的零头
    既扣不掉也调不走，永久卡在库位上。这里一次性把库存与上下限对齐到整数。
    幂等：没有小数时不产生任何更新，每次启动都可以安全跑。
    """
    targets = (
        ("inventory", "quantity", "库存数量"),
        ("parts", "min_stock", "最低库存"),
        ("parts", "max_stock", "最高库存"),
    )
    fixed = {}
    for table, col, label in targets:
        if table == "inventory":
            sql = ("UPDATE inventory SET quantity=ROUND(quantity) "
                   "WHERE quantity<>ROUND(quantity) AND part_id IN "
                   "(SELECT id FROM parts WHERE IFNULL(qty_decimal,0)=0)")
        else:
            sql = ("UPDATE parts SET %s=ROUND(%s) "
                   "WHERE IFNULL(qty_decimal,0)=0 AND %s<>ROUND(%s)" % (col, col, col, col))
        try:
            cur = conn.execute(sql)
            n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            if n:
                fixed[label] = n
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            import logging as _lg
            _lg.getLogger("db").warning("数量类型归整失败（%s.%s）：%s", table, col, e)
            return fixed
    if fixed:
        try:
            conn.commit()
        except Exception:
            pass
        import logging as _lg
        _lg.getLogger("db").info("已按数量类型「整数」归整历史数据：%s", fixed)
    return fixed


def init_db(demo=False):
    """初始化数据库：建目录、建表、写入默认权限与管理员
    demo=True 时在空库中写入演示物料与流水（供试用/演示）
    """
    db_dir = os.path.dirname(config.DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    upd = config.resolved_upload_dir()
    if not os.path.exists(upd):
        os.makedirs(upd, exist_ok=True)

    conn = get_conn()
    try:
        # v1.0.26：共享盘多机部署把库级日志模式切到 DELETE（只在这里做一次）。
        # WAL 依赖 -shm 共享内存，SQLite 官方明确不适用于网络文件系统；
        # 本地单机与服务器库保持 WAL / 直接跳过，行为零变化。
        try:
            import logging as _lg
            _jm = _db.apply_journal_mode(conn)
            if str(_jm).startswith("unchanged"):
                _lg.getLogger("db").warning("日志模式切换未完成：%s（下次启动重试）", _jm)
        except Exception as _e:  # noqa: BLE001
            import logging as _lg
            _lg.getLogger("db").warning("日志模式切换跳过：%s", _e)
        ddl = _db.ddl(_db.dialect(conn), """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                department TEXT DEFAULT '',          -- 所属部门（部门配置中维护）
                is_supervisor INTEGER NOT NULL DEFAULT 0,  -- 是否为部门主管（盘点一级默认审批人）
                active INTEGER NOT NULL DEFAULT 1,
                lang TEXT DEFAULT '',                -- 界面语言偏好（空=跟随系统；通知邮件按它渲染）
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS departments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                remark TEXT DEFAULT '',
                supervisor_id INTEGER,             -- 主管（盘点一级默认审批人）
                manager_id INTEGER,               -- 经理（盘点二级默认审批人）
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS permissions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                module TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT '',   -- ''=模块级权限；具体操作名=操作级权限
                allow INTEGER NOT NULL DEFAULT 1,
                UNIQUE(role, module, action)
            );

            CREATE TABLE IF NOT EXISTS parts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                part_code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                category TEXT DEFAULT '',
                spec TEXT DEFAULT '',
                unit TEXT DEFAULT '个',
                qty_decimal INTEGER NOT NULL DEFAULT 0,   -- 0=整数（个/PCS/卷…） 1=小数（米/克/KG…）
                unit_price REAL NOT NULL DEFAULT 0,
                min_stock REAL NOT NULL DEFAULT 0,
                max_stock REAL NOT NULL DEFAULT 0,
                is_valuable INTEGER NOT NULL DEFAULT 0,
                image_file TEXT DEFAULT '',
                supplier TEXT DEFAULT '',
                remark TEXT DEFAULT '',
                location_id INTEGER,               -- 固定放置位置（格位）
                extra TEXT DEFAULT '',             -- 自定义字段 JSON
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS locations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                warehouse TEXT NOT NULL,           -- 仓库号
                rack TEXT NOT NULL,                -- 货架号
                shelf TEXT NOT NULL,               -- 货位号
                bin TEXT NOT NULL DEFAULT '',      -- 格位号（可空，空=整货位）
                remark TEXT DEFAULT '',
                created_at TEXT,
                UNIQUE(warehouse, rack, shelf, bin)
            );

            CREATE TABLE IF NOT EXISTS audit_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                obj_type TEXT NOT NULL,            -- part=物料 rack=货架位置
                obj_id INTEGER,
                obj_code TEXT DEFAULT '',          -- 物料编号 / 位置文本
                action TEXT NOT NULL,              -- add/edit/delete/restore/purge
                detail TEXT DEFAULT '',            -- JSON：{f:{字段:[旧,新]}, note:'...'}
                operator_id INTEGER,
                operator_name TEXT DEFAULT '',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS images(
                name TEXT PRIMARY KEY,             -- 图片标识名（物料图片/品牌LOGO统一入库）
                data BLOB NOT NULL,                -- 图片二进制
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS inventory(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                part_id INTEGER NOT NULL,
                location_id INTEGER,               -- 关联 locations.id（NULL=旧文本库位）
                location TEXT NOT NULL,            -- 库位显示文本（位置文本或旧库位名）
                quantity REAL NOT NULL DEFAULT 0,
                repair_qty REAL NOT NULL DEFAULT 0, -- v1.0.21 其中维修件件数（包含于 quantity）
                FOREIGN KEY(part_id) REFERENCES parts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS applications(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_no TEXT UNIQUE NOT NULL,
                part_id INTEGER NOT NULL,
                app_type TEXT NOT NULL,            -- in 入库申请 / out 出库申请 / transfer 调库申请
                quantity REAL NOT NULL DEFAULT 1,
                reason TEXT DEFAULT '',
                line_name TEXT DEFAULT '',          -- 出库申请：使用线别（来自产线与设备配置）
                device_name TEXT DEFAULT '',        -- 出库申请：使用设备
                expected_return TEXT DEFAULT '',    -- 借用申请：预计归还日（借用出库后进台账预警）
                from_location_id INTEGER,          -- 调库：源放置位置（locations.id）
                to_location_id INTEGER,            -- 调库：目标放置位置（locations.id，NULL=待定）
                applicant_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                -- pending 待处理 / approved 已批准(待出库) / rejected 已驳回 / completed 已完成
                -- cancel_req 申请人请求撤销(待仓管审批) / canceled 已撤销
                approval_note TEXT DEFAULT '',
                approver_id INTEGER,               -- 实际审批人（贵重出库）
                assigned_approver_id INTEGER,      -- 指定审批人（贵重出库：普通用户=部门经理；仓管/高级=手动选择）
                operator_id INTEGER,
                operator_note TEXT DEFAULT '',
                created_at TEXT,
                processed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS transactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_no TEXT UNIQUE NOT NULL,
                part_id INTEGER NOT NULL,
                tx_type TEXT NOT NULL,             -- in 入库 / out 出库 / transfer_in 调拨入 / transfer_out 调拨出
                quantity REAL NOT NULL,
                unit_price REAL NOT NULL DEFAULT 0,
                amount REAL NOT NULL DEFAULT 0,
                location TEXT DEFAULT '',
                ref_type TEXT DEFAULT '',          -- direct 直接操作 / application 申请单
                ref_id INTEGER,
                operator_id INTEGER NOT NULL,
                applicant_id INTEGER,
                line_name TEXT DEFAULT '',         -- 出库领用线别（来自申请）
                device_name TEXT DEFAULT '',       -- 出库领用设备（来自申请）
                remark TEXT DEFAULT '',
                created_at TEXT
            );


            CREATE TABLE IF NOT EXISTS restore_apps(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_no TEXT UNIQUE NOT NULL,
                backup_name TEXT NOT NULL,           -- 备份文件名
                backup_dir TEXT DEFAULT '',          -- 备份所在目录
                backup_size INTEGER DEFAULT 0,
                applicant_id INTEGER NOT NULL,       -- 申请恢复人（仓管/最高）
                status TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/completed/canceled
                assigned_approver_id INTEGER,        -- 指定审批人（仓管=部门经理或最高；最高自选自己或经理以上）
                approver_id INTEGER,
                approval_note TEXT DEFAULT '',
                created_at TEXT,
                processed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sys_settings(
                key TEXT PRIMARY KEY,
                value TEXT
            );

            -- 币种调整单：一次「换记账本位币」= 一张单，可撤销
            --   from_cur × rate = to_cur（例：CNY 0.1373 → USD，即 1 元 = 0.1373 美元）
            --   status: active 已生效 / reverted 已被撤销
            CREATE TABLE IF NOT EXISTS currency_ops(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                op_no TEXT UNIQUE NOT NULL,
                from_cur TEXT NOT NULL,
                to_cur TEXT NOT NULL,
                rate REAL NOT NULL,
                mode TEXT NOT NULL DEFAULT 'exact',      -- exact 快照还原 / approx 反向汇率
                part_count INTEGER DEFAULT 0,
                tx_count INTEGER DEFAULT 0,
                total_before REAL DEFAULT 0,             -- 换算前库存总额
                total_after REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                revert_of INTEGER,                       -- 撤销单指向的原单 id
                reverted_by INTEGER,                     -- 原单被哪张撤销单撤掉
                operator_id INTEGER,
                operator_name TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_curops_status ON currency_ops(status, created_at);

            -- 逐条快照：撤销时按此精确写回（不能靠乘回去，浮点会漂）
            CREATE TABLE IF NOT EXISTS currency_op_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                op_id INTEGER NOT NULL,
                kind TEXT NOT NULL,                      -- part / tx
                row_id INTEGER NOT NULL,                 -- parts.id / transactions.id
                old_num REAL NOT NULL DEFAULT 0,         -- 单价（part 与 tx 都有）
                new_num REAL NOT NULL DEFAULT 0,
                old_amt REAL DEFAULT 0,                  -- 金额（仅 tx 有意义）
                new_amt REAL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_curops_items ON currency_op_items(op_id, kind);

            CREATE TABLE IF NOT EXISTS recycle_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_type TEXT NOT NULL,       -- 'location' / 'department' / 'user'
                item_id INTEGER NOT NULL,      -- 原记录 id（恢复时保留原 id，库存/流水引用自动重连）
                data TEXT NOT NULL,            -- 原记录 JSON 快照
                deleted_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_recycle_type ON recycle_items(item_type, deleted_at);

            CREATE TABLE IF NOT EXISTS production_lines(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                remark TEXT DEFAULT '',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS equipment(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                line_id INTEGER,               -- 所属产线（可空=未分配）
                name TEXT NOT NULL,
                model TEXT DEFAULT '',
                remark TEXT DEFAULT '',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS part_applications(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                part_id INTEGER NOT NULL,
                line_id INTEGER,                -- 可空：未分配产线的设备仅按设备适用
                equipment_id INTEGER,          -- 可空=整条产线适用
                created_at TEXT,
                UNIQUE(part_id, line_id, equipment_id),
                FOREIGN KEY(part_id) REFERENCES parts(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_tx_part ON transactions(part_id);
            CREATE INDEX IF NOT EXISTS idx_tx_time ON transactions(created_at);
            CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status);
            CREATE TABLE IF NOT EXISTS stocktakes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                no TEXT UNIQUE NOT NULL,
                scope TEXT DEFAULT '{}',
                scope_text TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                review_level INTEGER NOT NULL DEFAULT 2,
                approver1_id INTEGER,             -- 指派一级审批人（本部门主管默认，可改选；NULL=任何有权限者）
                approver2_id INTEGER,             -- 指派二级审批人（本部门经理默认，可改选；NULL=任何有权限者）
                created_by_id INTEGER,
                created_by_name TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT,
                summary TEXT DEFAULT '',
                cause TEXT DEFAULT '',            -- 原因分析（报告级：v1.0.17 起默认由逐行分析自动汇总，可人工改写）
                action TEXT DEFAULT '',           -- 改善行动（报告级：v1.0.17 起默认由逐行分析自动汇总，可人工改写）
                action_owner TEXT DEFAULT '',     -- 改善行动责任人（整单兼容字段，逐行责任人见 item.action_owner）
                action_due TEXT DEFAULT '',       -- 改善行动计划完成日期（整单兼容字段）
                analyzed_at TEXT DEFAULT '',      -- 分析最后保存时间
                analyzed_by TEXT DEFAULT '',      -- 分析最后保存人
                cause_auto TEXT DEFAULT '',       -- 系统自动汇总的原因分析快照（人工改写后仍保留，供「重新汇总」还原）
                action_auto TEXT DEFAULT '',      -- 系统自动汇总的改善行动快照
                summary_manual INTEGER NOT NULL DEFAULT 0, -- 1=报告级分析被人工改写，逐行分析变更时不再自动覆盖
                cycle_key TEXT DEFAULT '',        -- v1.0.20 本期应盘日 YYYY-MM-DD（周期统计归属）
                delay_reason TEXT DEFAULT '',     -- v1.0.41 当期延期说明（报告页填写，首页展示）
                done_at TEXT DEFAULT ''           -- v1.0.20 终审完成时间（状态置 done 时写）
            );
            CREATE TABLE IF NOT EXISTS stocktake_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stocktake_id INTEGER NOT NULL,
                part_id INTEGER NOT NULL,
                part_code TEXT DEFAULT '',
                part_name TEXT DEFAULT '',
                location_id INTEGER,
                location TEXT DEFAULT '',
                inv_id INTEGER,                  -- v1.0.20 建单时对应的 inventory.id（本期已盘行去重 / 精确校准）
                prior_st TEXT DEFAULT '',        -- v1.0.21 本期已由哪张单盘过（空=未盘）
                prior_qty REAL,                  -- v1.0.21 上次盘成的数量
                prior_at TEXT DEFAULT '',        -- v1.0.21 上次完成时间
                book_qty REAL NOT NULL DEFAULT 0,
                actual_qty REAL,
                diff REAL NOT NULL DEFAULT 0,
                actual_part_id INTEGER,          -- 错料：实盘发现的实物料号 id（NULL=无错料）
                actual_part_code TEXT DEFAULT '',-- 错料：实物料号编码
                note TEXT DEFAULT '',
                reason TEXT DEFAULT '',          -- 差异原因码（见 app._STK_REASONS）
                cause TEXT DEFAULT '',           -- v1.0.17 逐行原因分析（自由文本）
                action TEXT DEFAULT '',          -- v1.0.17 逐行改善处理（自由文本，同时是一条改善行动任务）
                action_owner TEXT DEFAULT '',    -- v1.0.17 逐行改善行动责任人
                action_due TEXT DEFAULT '',      -- v1.0.17 逐行改善行动计划完成日期 YYYY-MM-DD
                action_status TEXT DEFAULT 'open',  -- v1.0.17 open=未完成 / done=已完成（读侧用 _rowv 兜底 'open'）
                action_done_at TEXT DEFAULT '',  -- v1.0.17 改善行动完成时间
                action_done_by TEXT DEFAULT '',  -- v1.0.17 改善行动完成人
                FOREIGN KEY(stocktake_id) REFERENCES stocktakes(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_stocktake_items_sid ON stocktake_items(stocktake_id);

            -- v1.0.20 盘点周期：每期一行，只存该期的容差窗口与「在库行数」分母快照。
            -- 分母必须快照（不能实时 COUNT），否则历史柱高会随日常出入库漂移，没法复盘。
            CREATE TABLE IF NOT EXISTS stocktake_cycles(
                cycle_key TEXT PRIMARY KEY,        -- 本期应盘日 YYYY-MM-DD
                due_from TEXT DEFAULT '',          -- 容差窗口起（含）
                due_to TEXT DEFAULT '',            -- 容差窗口止（含）
                total_rows INTEGER DEFAULT 0,      -- 首次建单时全库在库行数（0=无快照，历史期）
                total_qty REAL DEFAULT 0,          -- 同期账面数量合计（仅参考，跨单位不可加）
                scope_note TEXT DEFAULT '',        -- 本期已覆盖范围，如「A仓、B仓」
                missed_reason TEXT DEFAULT '',     -- v1.0.67 未盘点期原因备注（无报告时也能登记）
                created_at TEXT,
                updated_at TEXT
            );

            -- 借·换·检·修台账（v1.0.19 起办理即实扣库存并写出库流水，还回时回补 + 写入库流水）
            CREATE TABLE IF NOT EXISTS borrows(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                borrow_no TEXT NOT NULL,           -- 借用单号 BR+yyyymmdd+序号
                part_id INTEGER NOT NULL,
                part_code TEXT DEFAULT '',
                part_name TEXT DEFAULT '',
                unit TEXT DEFAULT '',
                qty REAL NOT NULL DEFAULT 1,
                borrower TEXT DEFAULT '',          -- 借用人（自由文本，兼容外来人员）
                borrower_user_id INTEGER,          -- 可选关联系统用户
                borrowed_at TEXT DEFAULT '',       -- 借出时间
                expected_return TEXT DEFAULT '',   -- 预计归还时间（预警锚点）
                status TEXT NOT NULL DEFAULT 'borrowed',  -- pending 待确认 / borrowed 已借出 / returned 已还回 / cancelled 已驳回
                confirmed_at TEXT DEFAULT '',      -- 仓管确认时间（预警起点，空则回退借出时间）
                confirmed_by INTEGER,              -- 确认人
                cancel_note TEXT DEFAULT '',       -- 驳回理由
                returned_at TEXT DEFAULT '',
                return_note TEXT DEFAULT '',       -- 归还说明（防误点：非空才允许还回）
                warehouse TEXT DEFAULT '',         -- 借出仓库（可选）
                location TEXT DEFAULT '',          -- 借出库位文本（可选）
                location_id INTEGER,               -- 借出库位 id（还回时默认回这里）
                return_location TEXT DEFAULT '',   -- 还回时的实际放置库位文本
                return_location_id INTEGER,        -- 还回时的实际放置库位 id
                note TEXT DEFAULT '',              -- 借用说明（备注）
                op_id INTEGER,                     -- 经办仓管（借出）
                return_op_id INTEGER,              -- 还回经办仓管
                kind TEXT DEFAULT 'borrow',        -- 台账类型：borrow 借用 / exchange 换货 / verify 校验·测试 / maintenance 维修（借·换·检·修台账共用一张表）
                destination TEXT DEFAULT '',        -- 目的公司/地址
                receiver TEXT DEFAULT '',           -- 接收人
                contact TEXT DEFAULT '',            -- 联系方式
                out_person TEXT DEFAULT '',         -- 出库人（维修/校验·测试只读当前登录人）
                repair_type TEXT DEFAULT '',        -- 维修品类型（来自系统设置维修品类型分类，仅维修）
                stock_deducted INTEGER DEFAULT 0,  -- v1.0.19 是否已实扣库存（老库历史单由迁移补扣）
                deducted_qty REAL DEFAULT 0,       -- v1.0.19 实际扣减量（库存不足时可能小于 qty）
                returned_qty REAL DEFAULT 0,       -- v1.0.19 累计已还回数量（支持分次还回）
                restocked_qty REAL DEFAULT 0,      -- v1.0.19 累计已回补库存量（不超过 deducted_qty）
                created_at TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_borrows_status ON borrows(status);
            CREATE INDEX IF NOT EXISTS idx_borrows_part ON borrows(part_id);
            -- v1.0.19 分次还回明细：每次还回一行，回补量按实际扣减量封顶
            CREATE TABLE IF NOT EXISTS borrow_returns(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                borrow_id INTEGER NOT NULL,
                part_id INTEGER,
                quantity REAL NOT NULL DEFAULT 0,      -- 本次还回数量
                restocked_qty REAL NOT NULL DEFAULT 0, -- 本次实际回补库存量
                location_id INTEGER,
                location TEXT DEFAULT '',
                return_note TEXT DEFAULT '',
                op_id INTEGER,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_borrow_returns_bid ON borrow_returns(borrow_id);
            """
        )
        # 多数据库：DDL 逐条方言化后执行（sqlite 兼容原脚本语义）
        for stmt in _db.split_statements(ddl):
            if stmt.strip():
                conn.execute(stmt)
        # 兼容迁移逐条兜底：任何一条失败都不许中断建库流程。
        # 历史教训：applications.out_type 的回填在全新库上抛 no such column，
        # 把 init_db 打断在「表建好了、admin 还没写」的半路，客户看到的是
        # 「初始化完成，但一个账号都登不进去」。坏一条也先把库跑起来，
        # 迁移自己能修的在下次启动继续修。
        for _fn, _desc in ((_migrate_transactions, "transactions 兼容列/回填"),
                           (_migrate_users_lang, "users.lang"),
                           (_migrate_stocktake_report, "盘点报告分析字段"),
                           (_migrate_borrows, "借用台账"),
                           (_migrate_borrow_app, "借用申请 expected_return")):
            try:
                _fn(conn)
            except Exception as _e:  # noqa: BLE001
                print("[migrate] %s 迁移跳过：%s" % (_desc, _e))
        conn.commit()

        # v1.0.26：共享盘保护用的 sys_lock / sys_flag 两张表 + 维护位初始值。
        # 非 sqlite_smb 模式 ensure_tables() 直接返回，服务器库完全不碰这两张表，
        # 后期切 MySQL 时本段自动成为 no-op。
        try:
            import share_guard
            share_guard.ensure_tables(conn)
        except Exception as _e:  # noqa: BLE001
            import logging as _lg
            _lg.getLogger("db").warning("共享盘保护表初始化跳过：%s", _e)

        # 兼容旧库：permissions 表加 action 列（旧表无此列时重建，保留原有权限数据）
        try:
            cols = set(_db.list_columns(conn, "permissions"))
            if "action" not in cols:
                for stmt in _db.split_statements(_db.ddl(_db_dialect(conn), """
                    ALTER TABLE permissions RENAME TO permissions_old;
                    CREATE TABLE permissions(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        role TEXT NOT NULL,
                        module TEXT NOT NULL,
                        action TEXT NOT NULL DEFAULT '',
                        allow INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(role, module, action)
                    );
                    INSERT INTO permissions(role, module, action, allow)
                        SELECT role, module, '', allow FROM permissions_old;
                    DROP TABLE permissions_old;
                    """)):
                    if stmt.strip():
                        conn.execute(stmt)
                conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)

        # 默认权限矩阵（action='' 表示模块级权限）
        for role, modules in config.DEFAULT_PERMS.items():
            for m in modules:
                conn.execute(
                    "INSERT OR IGNORE INTO permissions(role, module, action, allow) VALUES(?,?,?,1)",
                    (role, m, ""),
                )
        # 迁移：本轮新增模块（呆滞物料管理）为默认授权角色开启（旧库中该模块可能已按禁止写入）
        for role, modules in config.DEFAULT_PERMS.items():
            if "stale" in modules:
                conn.execute(
                    "UPDATE permissions SET allow=1 WHERE role=? AND module='stale' AND action=''", (role,)
                )
        # 默认操作级权限（按角色级别预置细化项；已配置的行不覆盖）
        for role, mods in config.DEFAULT_PERM_ACTIONS.items():
            for m, acts in mods.items():
                for a, allow in acts.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO permissions(role, module, action, allow) VALUES(?,?,?,?)",
                        (role, m, a, allow),
                    )
        # 未配置的模块默认禁止
        all_mods = list(config.PERM_MODULES.keys())
        for role in config.DEFAULT_PERMS.keys():
            existing = {
                r["module"]
                for r in conn.execute(
                    "SELECT module FROM permissions WHERE role=? AND action=''", (role,)
                ).fetchall()
            }
            for m in all_mods:
                if m not in existing:
                    conn.execute(
                        "INSERT OR IGNORE INTO permissions(role, module, action, allow) VALUES(?,?,?,0)",
                        (role, m, ""),
                    )
        conn.commit()

        # 兼容旧库：为 parts 表补充新列（已存在则跳过）
        for col, ddl in (("location_id", "INTEGER"), ("extra", "TEXT"), ("qty_decimal", "INTEGER DEFAULT 0")):
            try:
                conn.execute(f"ALTER TABLE parts ADD COLUMN {col} {ddl}")
                conn.commit()
            except _db.OperationalError as e:
                _migration_error(e)
        # 回收站（软删除）：deleted=1 进入回收站（管理员可见/可恢复），deleted_at 删除时间（超期自动彻底清除）
        for col, ddl in (("deleted", "INTEGER NOT NULL DEFAULT 0"), ("deleted_at", "TEXT")):
            try:
                conn.execute(f"ALTER TABLE parts ADD COLUMN {col} {ddl}")
                conn.commit()
            except _db.OperationalError as e:
                _migration_error(e)
        # 物料级二级审批覆盖（0=跟随系统设置，1=该贵重件强制走二级审批）
        try:
            conn.execute("ALTER TABLE parts ADD COLUMN valuable_lvl INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 自动上下限开关（0=手动上下限生效，1=按历史消耗自动算）
        try:
            conn.execute("ALTER TABLE parts ADD COLUMN auto_min INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 停用/淘汰标记（0=在用，1=已停用，不预警、不参与领用选择）
        try:
            conn.execute("ALTER TABLE parts ADD COLUMN deprecated INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        try:
            conn.execute("ALTER TABLE parts ADD COLUMN deprecated_at TEXT DEFAULT ''")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # v1.0.16：是否维修品开关（出库时维修品批次优先扣减）
        try:
            conn.execute("ALTER TABLE parts ADD COLUMN is_repair INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 币种标记：记录这条记录的金额是哪个币种。老库一律回填 CNY（历史数据本来就是人民币）。
        # 换算时不读它做判断，只用来看「这笔是谁家的钱」；全局当前币种在 sys_settings.currency。
        for tbl in ("parts", "transactions"):
            try:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN currency TEXT DEFAULT 'CNY'")
                conn.commit()
            except _db.OperationalError as e:
                _migration_error(e)
        # 用户企业私信账号（企业微信userid/钉钉userid/飞书open_id）
        try:
            conn.execute("ALTER TABLE users ADD COLUMN enterprise_id TEXT NOT NULL DEFAULT ''")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 用户邮箱（邮件通知用）
        try:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 审批单催办记录（最后一次催办时间）
        try:
            conn.execute("ALTER TABLE applications ADD COLUMN last_remind_at TEXT NOT NULL DEFAULT ''")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 兼容旧库：为 applications 表补充调库位置列
        for col, ddl in (("from_location_id", "INTEGER"), ("to_location_id", "INTEGER"),
                         ("quantity", "REAL DEFAULT 1"), ("batch_no", "TEXT DEFAULT ''")):
            try:
                conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {ddl}")
                conn.commit()
            except _db.OperationalError as e:
                _migration_error(e)
        # v1.0.70：调库源精确到库存行 id（库位+批次+状态唯一）。老单该列留空，
        # 执行时自动回退到按库位找行，历史数据照旧可用。
        try:
            conn.execute("ALTER TABLE applications ADD COLUMN from_inv_id INTEGER")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # v1.0.78：整库位调库标记。老单该列为 0，执行时仍走原按库位/批次逻辑。
        try:
            conn.execute("ALTER TABLE applications ADD COLUMN whole_loc INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 兼容旧库：出库类型扩展（一般/退货/换货/内部转移）与对应业务字段。
        # out_type 为空 = 一般出库（历史数据全部视为一般，业务不变）；
        # expected_return 复用借用列，存换货的预计返回日期。
        for col, ddl in (("out_type", "TEXT DEFAULT ''"),
                         ("destination", "TEXT DEFAULT ''"),
                         ("receiver", "TEXT DEFAULT ''"),
                         ("contact", "TEXT DEFAULT ''"),
                         # v1.0.16：维修 / 校验·测试的出库人（只读当前登录人）
                         ("out_person", "TEXT DEFAULT ''"),
                         ("out_person_id", "INTEGER"),
                         # v1.0.16：维修子类型的「维修品类型」（来自系统设置配件分类）
                         ("repair_type", "TEXT DEFAULT ''"),
                         # v1.0.27：入库申请行声明的配件三态；仓管执行弹窗据此预选，历史单按良品
                         ("condition", "TEXT DEFAULT 'good'")):
            try:
                conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {ddl}")
                conn.commit()
            except _db.OperationalError as e:
                _migration_error(e)
        # 兼容旧库：为 applications 表补充申请单分组号（一张申请单多行明细）
        # doc_no 相同的记录同属一张申请单；旧数据每行自成一张单（doc_no=app_no）
        try:
            conn.execute("ALTER TABLE applications ADD COLUMN doc_no TEXT DEFAULT ''")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        try:
            conn.execute(
                "UPDATE applications SET doc_no=app_no WHERE doc_no IS NULL OR doc_no=''")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 兼容旧库：v2.9.3 之前「离线」是按当前界面语言写进 line_name 的字面值，
        # 现在离线 = 线体留空（报表归到「未分配」）。清掉历史字面值，否则报表里
        # 会同时冒出「未分配」和「离线」两个同类分组，而且换个界面语言原值还不变。
        for _tbl in ("applications", "transactions"):
            try:
                conn.execute(
                    "UPDATE %s SET line_name='' WHERE line_name IN "
                    "('离线','Offline','offline','OFFLINE','Hors ligne','Fuera de línea')" % _tbl)
                conn.commit()
            except _db.OperationalError as e:
                _migration_error(e)
        # 兼容旧库：为 users 表补充部门列
        try:
            conn.execute("ALTER TABLE users ADD COLUMN department TEXT DEFAULT ''")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 兼容旧库：users 表补充部门主管标记（盘点一级默认审批人）
        try:
            conn.execute("ALTER TABLE users ADD COLUMN is_supervisor INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 兼容旧库：部门配置补充主管/经理审批人（盘点一级/二级默认审批人）
        for col, ddl in (("supervisor_id", "INTEGER"), ("manager_id", "INTEGER")):
            try:
                conn.execute(f"ALTER TABLE departments ADD COLUMN {col} {ddl}")
                conn.commit()
            except _db.OperationalError as e:
                _migration_error(e)
        # 兼容旧库：盘点单补充指派审批人列
        for col, ddl in (("approver1_id", "INTEGER"), ("approver2_id", "INTEGER")):
            try:
                conn.execute(f"ALTER TABLE stocktakes ADD COLUMN {col} {ddl}")
                conn.commit()
            except _db.OperationalError as e:
                _migration_error(e)
        # 兼容旧库：盘点明细补充错料字段（actual_part_id/actual_part_code = 实盘发现的实物料号）
        for col, ddl in (("actual_part_id", "INTEGER"), ("actual_part_code", "TEXT DEFAULT ''")):
            try:
                conn.execute(f"ALTER TABLE stocktake_items ADD COLUMN {col} {ddl}")
                conn.commit()
            except _db.OperationalError as e:
                _migration_error(e)
        # 兼容旧库：part_applications.line_id 若为 NOT NULL（旧结构），重建为可空
        # （支持"只选未分配产线的设备"：line_id 为空、仅按设备适用）
        pa_cols = {c: {} for c in _db.list_columns(conn, "part_applications")}
        if pa_cols and pa_cols.get("line_id") and pa_cols["line_id"]["notnull"]:
            for stmt in _db.split_statements(_db.ddl(_db_dialect(conn), """
                ALTER TABLE part_applications RENAME TO part_applications_old;
                CREATE TABLE part_applications(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    part_id INTEGER NOT NULL,
                    line_id INTEGER,
                    equipment_id INTEGER,
                    created_at TEXT,
                    UNIQUE(part_id, line_id, equipment_id),
                    FOREIGN KEY(part_id) REFERENCES parts(id) ON DELETE CASCADE
                );
                INSERT INTO part_applications(id, part_id, line_id, equipment_id, created_at)
                    SELECT id, part_id, line_id, equipment_id, created_at FROM part_applications_old;
                DROP TABLE part_applications_old;
                """)):
                if stmt.strip():
                    conn.execute(stmt)
            conn.commit()
        # 兼容旧库：为 inventory 表补充 location_id 列（NULL=旧文本库位）
        try:
            conn.execute("ALTER TABLE inventory ADD COLUMN location_id INTEGER")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 兼容旧库：为 applications 表补充指定审批人列
        try:
            conn.execute("ALTER TABLE applications ADD COLUMN assigned_approver_id INTEGER")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 兼容旧库：为 applications 表补充二级审批人列
        try:
            conn.execute("ALTER TABLE applications ADD COLUMN approver2_id INTEGER")
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 兼容旧库：为 applications 表补充出库领用线别/设备列
        for col, ddl in (("line_name", "TEXT DEFAULT ''"), ("device_name", "TEXT DEFAULT ''")):
            try:
                conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {ddl}")
                conn.commit()
            except _db.OperationalError as e:
                _migration_error(e)
        # 兼容旧库：尝试把旧文本库位映射到 locations（location 文本=位置文本时关联）
        try:
            conn.execute(
                """UPDATE inventory SET location_id=(
                     SELECT l.id FROM locations l
                     WHERE l.warehouse||'-'||l.rack||'-'||l.shelf
                           ||COALESCE('-'||NULLIF(l.bin,''),'') = inventory.location)
                   WHERE location_id IS NULL"""
            )
            conn.commit()
        except _db.OperationalError as e:
            _migration_error(e)
        # 默认部门（首次建库时写入，可在「部门配置」中调整）
        for dname in ("生产部", "品质部", "设备部", "仓库", "办公室"):
            conn.execute(
                "INSERT OR IGNORE INTO departments(name, created_at) VALUES(?,?)",
                (dname, now_str()),
            )
        conn.commit()

        # 默认管理员（库中没有任何用户时自动创建）
        cur = conn.execute("SELECT COUNT(*) c FROM users")
        if cur.fetchone()["c"] == 0:
            from werkzeug.security import generate_password_hash
            conn.execute(
                "INSERT INTO users(username,password_hash,display_name,role,active,created_at) VALUES(?,?,?,?,1,?)",
                (
                    "admin",
                    generate_password_hash("admin123"),
                    "系统管理员",
                    "admin",
                    now_str(),
                ),
            )
            conn.commit()

        # 内置访客账号（AD 验证通过但不在白名单时以访客身份进入；无密码，不可直接登录）
        cur = conn.execute("SELECT COUNT(*) c FROM users WHERE username='guest'")
        if cur.fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO users(username,password_hash,display_name,role,active,created_at) VALUES(?,?,?,?,1,?)",
                ("guest", "!", "访客", "guest", now_str()),
            )
            conn.commit()

        if demo:
            _seed_demo_if_empty(conn)
            _seed_demo_locations_if_missing(conn)
        # 数量类型=整数的物料不允许出现小数数量：老库/旧导入留下的零头在建库流程里归整
        _migrate_qty_int(conn)
        # 一次性纠正部门管理被误放开的仓库作业权限（借用还回/台账、盘点创建）
        _migrate_role_perms(conn)
        # v1.0.19：历史在借单补扣库存（放在演示数据播种之后，保证演示库口径一致）
        _migrate_borrow_stock_v1019(conn)
        # v1.0.20：盘点周期字段（cycle_key/done_at）、明细 inv_id、周期快照表
        _migrate_stocktake_cycle_v1020(conn)
        # v1.0.21：维修品按库位计数（repair_qty + 先扣维修件触发器）、盘点明细本期已盘快照
        _migrate_repair_stock_v1021(conn)
        # v1.0.22：配件好坏状态（inventory.condition）+ 货架属性（locations.attribute）
        _migrate_condition_v1022(conn)
        # v1.0.23：维修还回记录「是否修好」+ 回库状态（borrow_returns.repair_ok/condition）
        _migrate_repair_return_v1023(conn)
        # v1.0.46：待报废批量报废单模式标记 + 签字附件关联表
        _migrate_scrap_batch_v1046(conn)
        # v1.0.51：纸档单提交闸门（doc_submitted），老单默认已提交
        _migrate_doc_submit_v1051(conn)
        # v1.0.11 兜底：产品口径要求「申请人所在部门必须配一级审批人」，空部门一律拒绝
        # 提交。admin 建号时没带部门，_seed_demo_if_empty 里虽补过一次，但只要演示种子
        # 因 parts 非空提前 return 就会漏掉 —— 冷启动新库上 admin.department 仍是空串，
        # 客户第一笔退货/返还/盘点提交就被挡住（冒烟里 check_v339 连红 6 项）。
        # 这里在建库末尾统一兜底，挂到第一个配了主管的部门；已有部门的老库不动。
        try:
            d = conn.execute(
                "SELECT name FROM departments WHERE COALESCE(supervisor_id,0)>0 "
                "ORDER BY id LIMIT 1").fetchone()
            if d:
                conn.execute(
                    "UPDATE users SET department=? WHERE username='admin' "
                    "AND (department IS NULL OR TRIM(department)='')", (d["name"],))
                conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


# ------------------------------------------------------------------
# 图片入库：图片二进制统一存入主数据库 images 表（单文件随库带走）
# 物料图片与品牌 LOGO 都走这里；旧版已存在 upload_dir 目录中的图片
# 仍可被 /media 路由回退读取，新图片一律入库存取。
# ------------------------------------------------------------------
def image_save(conn, name, data):
    """保存图片二进制到 images 表（同名覆盖）。conn 须为已打开的主库连接"""
    conn.execute(
        "INSERT INTO images(name, data, created_at) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET data=excluded.data, created_at=excluded.created_at",
        (name, data, now_str()),
    )


def image_load(conn, name):
    """按图片名读取二进制；不存在返回 None"""
    row = conn.execute("SELECT data FROM images WHERE name=?", (name,)).fetchone()
    return bytes(row["data"]) if row else None


def image_delete(conn, name):
    """删除图片记录"""
    conn.execute("DELETE FROM images WHERE name=?", (name,))


def backup_database(target_path):
    """在线备份主数据库到指定文件（仅 SQLite 支持：文件级备份）。
    其它数据库类型请使用数据库自身的备份机制（如 MySQL mysqldump、PostgreSQL pg_dump、SQL Server/Oracle 管理工具）"""
    if config.DB_TYPE != "sqlite":
        raise OSError("备份/恢复仅支持 SQLite 数据库；%s 请使用数据库自带备份工具" % config.DB_TYPE.upper())
    if not config.DB_PATH or not os.path.exists(config.DB_PATH):
        raise OSError("数据库未配置或文件不存在")
    src = sqlite3.connect(config.DB_PATH, timeout=30)
    dst = None
    try:
        dst = sqlite3.connect(target_path, timeout=30)
        src.backup(dst)
    finally:
        if dst is not None:
            dst.close()
        src.close()
    return target_path


def _seed_demo_if_empty(conn):
    """空库时生成演示数据，便于直接查看图表效果（初始化向导中勾选「演示数据」触发）"""
    if conn.execute("SELECT COUNT(*) c FROM parts").fetchone()["c"] > 0:
        return
    from werkzeug.security import generate_password_hash
    # 演示账号
    demo_users = [
        ("warehouse01", "123456", "王仓管", "warehouse", "仓库"),
        ("manager01", "123456", "李经理", "manager", "生产部"),
        ("user01", "123456", "张三", "user", "生产部"),
    ]
    uid_map = {}
    for u, p, n, r, dep in demo_users:
        cur = conn.execute(
            "INSERT INTO users(username,password_hash,display_name,role,department,active,created_at) VALUES(?,?,?,?,?,1,?)",
            (u, generate_password_hash(p), n, r, dep, now_str()),
        )
        uid_map[u] = cur.lastrowid

    # 部门审批人（v1.0.9 补）：贵重出库、退货/换货转审批、盘点两级审批都要按
    # 「部门配置」里的一级/二级审批人自动指派。以前这里留空，新装演示库一提交就报
    # 「该申请人所属部门未配置一级审批人」，审批链路等于走不通。
    # 演示口径：生产部/品质部/设备部/办公室 一级=李经理、二级=李经理；
    # 仓库一级=王仓管（自家领用自己批，演示够用，客户可在部门配置里改）。
    _approvers = {
        "生产部": (uid_map.get("manager01"), uid_map.get("manager01")),
        "品质部": (uid_map.get("manager01"), uid_map.get("manager01")),
        "设备部": (uid_map.get("manager01"), uid_map.get("manager01")),
        "办公室": (uid_map.get("manager01"), uid_map.get("manager01")),
        "仓库": (uid_map.get("warehouse01"), uid_map.get("manager01")),
    }
    for dname, (sup1, sup2) in _approvers.items():
        if not sup1:
            continue
        conn.execute(
            "UPDATE departments SET supervisor_id=?, manager_id=? WHERE name=?",
            (sup1, sup2, dname),
        )
    # 管理员账号也挂到办公室：否则客户首启用 admin 提交退货/贵重出库/盘点，
    # 一样被「申请人所属部门未配置一级审批人」挡住，第一反应就是"这功能是坏的"。
    conn.execute("UPDATE users SET department='办公室' WHERE username='admin' "
                 "AND (department IS NULL OR department='')")
    conn.commit()

    # 演示物料：30 种，覆盖 工具/耗材/辅料/备件/夹具 五大类（原电子料不在本系统管理范围内）
    demo_parts = [
        # code, name, category, spec, unit, price, min, max, valuable, supplier
        # --- 工具（7）---
        ("T-ESD-5000", "电动螺丝刀", "工具", "BLG-5000X 0.1~1.0N·m", "把", 380.0, 4, 20, 0, "宝工"),
        ("T-IRON-936", "恒温电烙铁", "工具", "936 60W 350~450℃", "把", 165.0, 5, 25, 0, "白光"),
        ("T-TWS-8250", "扭力螺丝刀", "工具", "8250 预置扭力型", "把", 95.0, 3, 15, 0, "宝工"),
        ("T-PLI-170", "防静电尖嘴钳", "工具", "170mm ESD", "把", 32.0, 6, 30, 0, "马牌"),
        ("T-HOT-8018", "热风枪", "工具", "8018 700W 数显", "把", 260.0, 2, 10, 0, "快克"),
        ("T-DMM-17B", "数字万用表", "工具", "17B 真有效值 600V", "台", 320.0, 3, 12, 1, "福禄克"),
        ("T-PUMP-30B", "真空吸笔", "工具", "30B 防静电", "把", 46.0, 5, 25, 0, "宝工"),
        # --- 耗材（7）---
        ("C-NOZ-503", "贴片机吸嘴", "耗材", "503型 0402", "个", 25.0, 30, 300, 0, "雅马哈"),
        ("C-NOZ-505", "贴片机吸嘴", "耗材", "505型 0603", "个", 25.0, 30, 300, 0, "雅马哈"),
        ("C-MILL-10", "分板机铣刀", "耗材", "φ1.0 双刃", "支", 60.0, 10, 60, 0, "川井"),
        ("C-FLT-A120", "过滤棉", "耗材", "A120 10片/包", "包", 12.0, 8, 80, 0, "三和"),
        ("C-STL-3728", "SMT钢网", "耗材", "37×28cm 激光切割", "张", 850.0, 2, 15, 1, "精工"),
        ("C-GLU-706", "贴片红胶", "耗材", "706 500g", "瓶", 75.0, 5, 40, 0, "乐泰"),
        ("C-BRU-3", "防静电毛刷", "耗材", "3号 宽12mm", "把", 22.0, 6, 40, 0, "宝工"),
        # --- 辅料（5）---
        ("M-SLD-08", "焊锡丝", "辅料", "0.8mm 63/37 500g", "卷", 120.0, 5, 40, 0, "云锡"),
        ("M-FLUX-881", "助焊剂", "辅料", "FLUX-881 1L", "瓶", 85.0, 6, 60, 0, "同方"),
        ("M-IPA-500", "无水酒精", "辅料", "500ml 分析纯", "瓶", 15.0, 10, 100, 0, "国药"),
        ("M-WIP-060", "无尘布", "辅料", "0609 100片/包", "包", 45.0, 8, 60, 0, "航天"),
        ("M-TPG-01", "导热硅脂", "辅料", "100g 罐装", "罐", 38.0, 4, 30, 0, "信越"),
        # --- 备件（7）---
        ("S-SRV-400W", "伺服电机", "备件", "400W 带刹车", "台", 2600.0, 2, 8, 1, "安川"),
        ("S-BLT-315", "同步带", "备件", "5M-315 宽15mm", "条", 65.0, 4, 20, 0, "盖茨"),
        ("S-CYL-CDG", "气缸", "备件", "CDG1BN20-75", "个", 210.0, 2, 12, 0, "SMC"),
        ("S-SEN-FS", "光纤传感器", "备件", "FS-N11N 含光纤", "套", 480.0, 2, 10, 1, "基恩士"),
        ("S-THC-K", "热电偶", "备件", "K型 φ3.2 300mm", "支", 55.0, 6, 30, 0, "欧姆龙"),
        ("S-BRG-6204", "轴承", "备件", "6204ZZ", "个", 18.0, 10, 50, 0, "NSK"),
        ("S-PSU-24V", "开关电源", "备件", "24V 5A", "个", 180.0, 2, 12, 0, "欧姆龙"),
        # --- 夹具（4）---
        ("F-TST-A02", "ICT测试治具", "夹具", "A02机型 双面", "套", 1500.0, 1, 6, 1, "自制成型"),
        ("F-TRAY-360", "载具托盘", "夹具", "360×260 防静电", "个", 130.0, 10, 60, 0, "亚克力"),
        ("F-PRS-M1", "压合治具", "夹具", "M1机型 20模位", "套", 620.0, 1, 8, 0, "自制成型"),
        ("F-BRD-220", "烧录治具", "夹具", "220pin 转接", "套", 280.0, 1, 10, 0, "自制成型"),
    ]
    part_id_map = {}
    for (code, name, cat, spec, unit, price, mn, mx, val, sup) in demo_parts:
        cur = conn.execute(
            """INSERT INTO parts(part_code,name,category,spec,unit,unit_price,min_stock,max_stock,is_valuable,supplier,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, name, cat, spec, unit, price, mn, mx, val, sup, now_str(), now_str()),
        )
        part_id_map[code] = cur.lastrowid
        # 初始库存：两个库位
        conn.execute(
            "INSERT INTO inventory(part_id,location,quantity) VALUES(?,?,?)",
            (cur.lastrowid, "A线物料库", round(mx * 0.55)),
        )
        conn.execute(
            "INSERT INTO inventory(part_id,location,quantity) VALUES(?,?,?)",
            (cur.lastrowid, "B线物料库", round(mx * 0.2)),
        )

    # 演示产线 / 设备 / 物料适用对象
    line_map = {}
    for ln in ("SMT-1线", "SMT-2线", "DIP线"):
        cur = conn.execute(
            "INSERT INTO production_lines(name, remark, created_at) VALUES(?,?,?)",
            (ln, "演示产线", now_str()),
        )
        line_map[ln] = cur.lastrowid
    equip_rows = [
        (line_map["SMT-1线"], "贴片机1#", "YSM20"),
        (line_map["SMT-1线"], "回流焊1#", "RF-10"),
        (line_map["SMT-2线"], "印刷机2#", "SP-8"),
        (line_map["SMT-2线"], "分板机1#", "VCM-3"),
        (line_map["DIP线"], "波峰焊1#", "WS-450"),
        (None, "离线AOI", "AOI-200"),
    ]
    equip_map = {}
    for lid, en, m in equip_rows:
        cur = conn.execute(
            "INSERT INTO equipment(line_id, name, model, created_at) VALUES(?,?,?,?)",
            (lid, en, m, now_str()),
        )
        equip_map[en] = cur.lastrowid
    # 每种物料的适用产线/设备（出库流水的线别/设备从这里取）
    APP = {
        "T-ESD-5000": [("SMT-1线", "贴片机1#"), ("SMT-2线", "印刷机2#"), ("DIP线", "波峰焊1#")],
        "T-IRON-936": [("DIP线", "波峰焊1#"), ("SMT-1线", None)],
        "T-TWS-8250": [("SMT-1线", "贴片机1#"), ("SMT-2线", None)],
        "T-PLI-170": [("SMT-1线", None), ("DIP线", None)],
        "T-HOT-8018": [("DIP线", "波峰焊1#"), ("SMT-2线", None)],
        "T-DMM-17B": [(None, "离线AOI"), ("SMT-1线", None)],
        "T-PUMP-30B": [("SMT-1线", "贴片机1#"), ("SMT-2线", "印刷机2#")],
        "C-NOZ-503": [("SMT-1线", "贴片机1#"), ("SMT-2线", None)],
        "C-NOZ-505": [("SMT-1线", "贴片机1#")],
        "C-MILL-10": [("SMT-2线", "分板机1#")],
        "C-FLT-A120": [("SMT-1线", "贴片机1#"), ("SMT-1线", "回流焊1#")],
        "C-STL-3728": [("SMT-2线", "印刷机2#"), ("SMT-1线", None)],
        "C-GLU-706": [("SMT-2线", "印刷机2#")],
        "C-BRU-3": [("SMT-1线", None), ("SMT-2线", None), ("DIP线", None)],
        "M-SLD-08": [("DIP线", "波峰焊1#"), ("SMT-1线", None)],
        "M-FLUX-881": [("DIP线", "波峰焊1#"), ("SMT-2线", None)],
        "M-IPA-500": [("SMT-1线", None), ("SMT-2线", None), ("DIP线", None)],
        "M-WIP-060": [("SMT-1线", "贴片机1#"), ("SMT-2线", "印刷机2#"), ("DIP线", "波峰焊1#")],
        "M-TPG-01": [(None, "离线AOI")],
        "S-SRV-400W": [("SMT-1线", "贴片机1#"), ("SMT-2线", "印刷机2#")],
        "S-BLT-315": [("SMT-1线", "贴片机1#"), ("SMT-2线", "印刷机2#")],
        "S-CYL-CDG": [("SMT-2线", "印刷机2#"), ("DIP线", "波峰焊1#")],
        "S-SEN-FS": [("SMT-1线", "回流焊1#"), (None, "离线AOI")],
        "S-THC-K": [("SMT-1线", "回流焊1#"), ("DIP线", "波峰焊1#")],
        "S-BRG-6204": [("SMT-1线", "贴片机1#"), ("SMT-2线", "印刷机2#")],
        "S-PSU-24V": [(None, "离线AOI"), ("DIP线", "波峰焊1#")],
        "F-TST-A02": [("SMT-1线", None), ("SMT-2线", None)],
        "F-TRAY-360": [("SMT-1线", "贴片机1#"), ("SMT-2线", "印刷机2#")],
        "F-PRS-M1": [("DIP线", "波峰焊1#"), ("SMT-2线", None)],
        "F-BRD-220": [("SMT-1线", "贴片机1#"), (None, "离线AOI")],
    }
    for code, pairs in APP.items():
        if code in part_id_map:
            set_part_apps(conn, part_id_map[code],
                          [(line_map.get(ln), equip_map.get(dv)) for ln, dv in pairs])

    # 故意制造三个演示状态：
    #   低于下限（伺服电机）、超过上限（无水酒精）、呆滞（万用表/烧录治具长期无领用）
    low = conn.execute("SELECT id FROM parts WHERE part_code='S-SRV-400W'").fetchone()
    if low:
        conn.execute("UPDATE inventory SET quantity=1 WHERE part_id=? AND location='A线物料库'", (low["id"],))
        conn.execute("UPDATE inventory SET quantity=0 WHERE part_id=? AND location='B线物料库'", (low["id"],))
    high = conn.execute("SELECT id FROM parts WHERE part_code='M-IPA-500'").fetchone()
    if high:
        conn.execute("UPDATE inventory SET quantity=88 WHERE part_id=? AND location='A线物料库'", (high["id"],))
        conn.execute("UPDATE inventory SET quantity=22 WHERE part_id=? AND location='B线物料库'", (high["id"],))

    # 生成今年 1 月 1 日至今的出入库流水（确定性随机，便于图表演示）。
    # 月度权重：9/10/11 月明显偏多，其余月份日常水平；2 月（春节）最少。
    import random
    rnd = random.Random(20261102)
    codes = list(part_id_map.keys())
    today = datetime.now()
    now_dt = today
    y0 = today.year
    start = datetime(y0, 1, 1)
    # 各类物料的单次收发数量（工具/夹具/备件按台套走小量，耗材辅料走批量）
    QTY = {
        "工具": ((1, 4), (1, 2)),
        "耗材": ((10, 60), (5, 40)),
        "辅料": ((5, 40), (5, 30)),
        "备件": ((1, 5), (1, 3)),
        "夹具": ((1, 4), (1, 3)),
    }
    cat_of = {r["part_code"]: r["category"] for r in
              conn.execute("SELECT part_code, category FROM parts").fetchall()}
    # 呆滞演示：这两种料今年夏末之后就没有出库了（台账里「未领用(天)」会大于 90）
    STALE_CUTOFF = {"T-DMM-17B": datetime(y0, 4, 30), "F-BRD-220": datetime(y0, 6, 15)}
    price_of = {r["part_code"]: r["unit_price"] for r in
                conn.execute("SELECT part_code, unit_price FROM parts").fetchall()}
    WMON = {1: 1.0, 2: 0.6, 3: 0.9, 4: 0.8, 5: 0.9, 6: 0.8, 7: 0.7, 8: 0.8, 9: 1.8, 10: 2.0, 11: 1.7}
    tx_idx = 1
    d = start
    while d <= today:
        w = WMON.get(d.month, 1.0)
        for _ in range(int(rnd.random() * 2.5 * w) + (1 if rnd.random() < w else 0)):
            code = rnd.choice(codes)
            pid = part_id_map[code]
            price = price_of[code]
            typ = "in" if rnd.random() < 0.42 else "out"
            if typ == "out" and code in STALE_CUTOFF and d > STALE_CUTOFF[code]:
                typ = "in"   # 呆滞料夏末后只进不出
            qi, qo = QTY[cat_of[code]]
            qty = rnd.randint(*qi) if typ == "in" else rnd.randint(*qo)
            loc = rnd.choice(["A线物料库", "B线物料库"])
            # 出库随机分配到产线/设备，便于线体分析、设备分析出图
            out_line = ""
            out_device = ""
            if typ == "out":
                pairs = APP.get(code) or [("SMT-1线", None)]
                ln, dv = rnd.choice(pairs)
                out_line = ln or ""
                out_device = dv or ""
            ts = d.replace(hour=rnd.randint(8, 18), minute=rnd.randint(0, 59),
                           second=rnd.randint(0, 59))
            # 今天这一天的流水不能落在「还没到的时间」：下午 3 点打开明细，
            # 里面已有 16:30 的单据，一眼就看出是造的数据。今天只生成到当前小时。
            if ts > now_dt:
                _hmax = max(8, now_dt.hour)
                ts = d.replace(hour=rnd.randint(8, _hmax), minute=rnd.randint(0, 59),
                               second=rnd.randint(0, 59))
                if ts > now_dt:
                    ts = now_dt - timedelta(minutes=rnd.randint(1, 40))
            conn.execute(
                """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,
                   line_name,device_name,ref_type,operator_id,remark,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"TX{ts.strftime('%Y%m%d%H%M%S')}{tx_idx:03d}", pid, typ, qty, price,
                    round(qty * price, 2), loc, out_line, out_device, "direct",
                    uid_map["warehouse01"], "演示数据", ts.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            tx_idx += 1
        d += timedelta(days=1)

    # 工具/夹具的借用台账：同样铺满全年，9/10/11 月偏多。
    # 大部分已归还；近 30 天里留几笔在借（含超期）——「在借/逾期/即将到期」三个预警位都有内容。
    # 注意：没有「待确认」状态，借出在出入库页办理时即完成。
    borrowable = [c for c in codes if cat_of[c] in ("工具", "夹具")]
    borrowers = [("张三", uid_map["user01"]), ("李经理", uid_map["manager01"]),
                 ("王仓管", uid_map["warehouse01"]), ("设备部 陈工", None), ("品质部 刘工", None)]
    br_notes = ["设备点检借用", "产线维修借用", "新机种调试", "客户验机演示", "培训教学使用"]
    br_idx = 1
    d = start
    while d <= today:
        w = WMON.get(d.month, 1.0)
        if rnd.random() < 0.16 * w:
            code = rnd.choice(borrowable)
            pid = part_id_map[code]
            prow = next(r for r in demo_parts if r[0] == code)
            name, unit = prow[1], prow[4]
            qty = rnd.randint(1, 2) if cat_of[code] == "工具" else 1
            bname, buid = rnd.choice(borrowers)
            ts = d.replace(hour=rnd.randint(8, 18), minute=rnd.randint(0, 59))
            exp = ts + timedelta(days=rnd.randint(3, 14))
            if d >= today - timedelta(days=3):
                status = "borrowed"
                returned_at = ""
            elif d >= today - timedelta(days=30):
                status = "borrowed" if rnd.random() < 0.25 else "returned"
                returned_at = (ts + timedelta(days=rnd.randint(1, 5))).strftime("%Y-%m-%d %H:%M:%S") \
                    if status == "returned" else ""
            else:
                status = "returned"
                returned_at = (ts + timedelta(days=rnd.randint(1, 5))).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """INSERT INTO borrows(borrow_no,part_id,part_code,part_name,unit,qty,borrower,borrower_user_id,
                   borrowed_at,expected_return,status,confirmed_at,confirmed_by,returned_at,return_note,
                   warehouse,location,note,op_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"BR{ts.strftime('%Y%m%d')}{br_idx:03d}", pid, code,
                    name, unit, qty, bname, buid,
                    ts.strftime("%Y-%m-%d %H:%M:%S"), exp.strftime("%Y-%m-%d"),
                    status,
                    ts.strftime("%Y-%m-%d %H:%M:%S"),
                    uid_map["warehouse01"],
                    returned_at,
                    "已归还" if returned_at else "",
                    "WH-01", rnd.choice(["A线物料库", "B线物料库"]), rnd.choice(br_notes),
                    uid_map["warehouse01"], ts.strftime("%Y-%m-%d %H:%M:%S"),
                    ts.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            br_idx += 1
        d += timedelta(days=1)

    # 保底三笔近期借用（随机循环不一定落在最近几天）：
    #   正常在借 1 笔、即将到期 1 笔、超期在借 1 笔 —— 台账的预警位都有内容可看
    demo_tail = [
        # (天数偏移[负=过去], part_code, 借用人, user_id, qty, status, 预计归还偏移, 备注)
        (-1, "T-ESD-5000", "张三", uid_map["user01"], 1, "borrowed", 5, "新机种试产拧紧"),
        (-5, "T-DMM-17B", "李经理", uid_map["manager01"], 1, "borrowed", 9, "客户验机演示"),
        (-12, "F-PRS-M1", "设备部 陈工", None, 1, "borrowed", -2, "产线维修借用"),
    ]
    for off, code, bname, buid, qty, status, exp_off, note in demo_tail:
        pid = part_id_map[code]
        prow = next(r for r in demo_parts if r[0] == code)
        name, unit = prow[1], prow[4]
        ts = (today + timedelta(days=off)).replace(hour=9, minute=30, second=0)
        exp = ts + timedelta(days=exp_off)
        returned_at = ""
        conn.execute(
            """INSERT INTO borrows(borrow_no,part_id,part_code,part_name,unit,qty,borrower,borrower_user_id,
               borrowed_at,expected_return,status,confirmed_at,confirmed_by,returned_at,return_note,
               warehouse,location,note,op_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"BR{ts.strftime('%Y%m%d')}{br_idx:03d}", pid, code, name, unit, qty,
                bname, buid, ts.strftime("%Y-%m-%d %H:%M:%S"), exp.strftime("%Y-%m-%d"),
                status, ts.strftime("%Y-%m-%d %H:%M:%S"),
                uid_map["warehouse01"], returned_at,
                "", "WH-01", rnd.choice(["A线物料库", "B线物料库"]), note,
                uid_map["warehouse01"], ts.strftime("%Y-%m-%d %H:%M:%S"),
                ts.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        br_idx += 1
    conn.commit()


def _seed_demo_locations_if_missing(conn):
    """演示库升级：已有物料但无货架位置时补种位置并分配（兼容旧演示库）"""
    if conn.execute("SELECT COUNT(*) c FROM locations").fetchone()["c"] > 0:
        return
    demo_locs = [
        ("WH-01", "R-01", "S-01", "B-01"),
        ("WH-01", "R-01", "S-01", "B-02"),
        ("WH-01", "R-01", "S-02", "B-01"),
        ("WH-01", "R-01", "S-02", "B-02"),
        ("WH-01", "R-02", "S-01", "B-01"),
        ("WH-01", "R-02", "S-01", "B-02"),
        ("WH-01", "R-02", "S-02", "B-01"),
        ("WH-01", "R-02", "S-02", "B-02"),
        ("WH-02", "R-01", "S-01", "B-01"),
        ("WH-02", "R-01", "S-01", "B-02"),
        ("WH-02", "R-01", "S-02", ""),   # 格位号可空：整货位位置
        # 第二库位池（与主位置 0-9 不重叠，保证每物料两个独立库位）
        ("WH-02", "R-02", "S-01", "B-01"),
        ("WH-02", "R-02", "S-01", "B-02"),
        ("WH-02", "R-02", "S-02", "B-01"),
        ("WH-02", "R-02", "S-02", "B-02"),
        ("WH-02", "R-03", "S-01", "B-01"),
        ("WH-02", "R-03", "S-01", "B-02"),
        ("WH-02", "R-03", "S-02", "B-01"),
        ("WH-02", "R-03", "S-02", "B-02"),
        ("WH-03", "R-01", "S-01", "B-01"),
        ("WH-03", "R-01", "S-01", "B-02"),
    ]
    loc_id_map = {}
    for (wh, rk, sf, bn) in demo_locs:
        cur = conn.execute(
            "INSERT INTO locations(warehouse,rack,shelf,bin,created_at) VALUES(?,?,?,?,?)",
            (wh, rk, sf, bn, now_str()),
        )
        loc_id_map[f"{wh}-{rk}-{sf}-{bn}"] = cur.lastrowid
    rows = conn.execute("SELECT id FROM parts").fetchall()
    for idx, r in enumerate(rows):
        key = demo_locs[idx % len(demo_locs)]
        conn.execute(
            "UPDATE parts SET location_id=? WHERE id=?",
            (loc_id_map["-".join(key)], r["id"]),
        )
        # 把库存两行映射到两个货位（多库位演示：主位置 idx + 第二位置 idx+10，互不重叠）
        inv_rows = conn.execute(
            "SELECT id FROM inventory WHERE part_id=? ORDER BY id", (r["id"],)
        ).fetchall()
        key2 = demo_locs[(idx + 10) % len(demo_locs)]
        for k, inv in zip((key, key2), inv_rows):
            lid = loc_id_map["-".join(k)]
            conn.execute(
                "UPDATE inventory SET location_id=?, location=? WHERE id=?",
                (lid, location_text(get_location(conn, lid)), inv["id"]),
            )
    conn.commit()


# ------------------------------------------------------------------
# 业务辅助函数
# ------------------------------------------------------------------
def total_stock(conn, part_id):
    """某物料的总库存（跨库位合计）"""
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity),0) s FROM inventory WHERE part_id=?", (part_id,)
    ).fetchone()
    return row["s"] if row else 0


def stock_locations(conn, part_id):
    """某物料在各库位的分布（每行含 inventory 行 id、位置 id、批次号与入库日期；按入库先后=先进先出排序）
    返回所有绑定记录，包括quantity=0的预留库位行，用于物料详情、编辑弹窗展示"""
    rows = conn.execute(
        """SELECT i.id inv_id, i.location_id, i.location, i.quantity,
                  i.repair_qty, i.batch_no, i.in_date, i.condition,
                  l.attribute AS attribute
           FROM inventory i
           LEFT JOIN locations l ON l.id=i.location_id
           WHERE i.part_id=?
           ORDER BY i.in_date, i.id""",
        (part_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def cond_summary(conn, part_id):
    """v1.0.27：按物料全部在库行聚合配件状态，供「出库子选项动态显隐」与服务端同源校验。

    - 只统计 quantity>0 的库存行；零库存的预留库位行不算。
    - 三态严格分开：只有 defective 算「不良」，repair（维修品）是独立第三态。
    - 状态在 inventory.condition（按库存行），不在 parts 表；库位属性在 locations.attribute。
    - SQL 只用标准 JOIN / COALESCE，不依赖 SQLite 专有语法，切 MySQL/PostgreSQL 无需改动。
    """
    rows = conn.execute(
        """SELECT COALESCE(i.condition,'good') AS cond,
                  COALESCE(l.attribute,'good')  AS attr
           FROM inventory i
           LEFT JOIN locations l ON l.id = i.location_id
           WHERE i.part_id=? AND i.quantity>0""",
        (part_id,),
    ).fetchall()

    def _v(r, key, idx):
        try:
            return r[key] or "good"
        except (KeyError, IndexError):
            return "good"
        except TypeError:
            # 某些驱动返回 tuple 而非 Row/dict，按列序兜底
            try:
                return r[idx] or "good"
            except (IndexError, KeyError, TypeError):
                return "good"

    conds = [_v(r, "cond", 0) for r in rows]
    attrs = [_v(r, "attr", 1) for r in rows]
    return {
        "rows": len(rows),
        "has_defective": any(c == "defective" for c in conds),
        "all_defective": bool(rows) and all(c == "defective" for c in conds),
        "has_pending_scrap": any(a == "pending_scrap" for a in attrs),
        "has_pending_repair": any(a == "pending_repair" for a in attrs),
    }


def part_detail(conn, part_id):
    """物料详情 + 库存汇总（已删除的物料视为不存在）"""
    p = conn.execute("SELECT * FROM parts WHERE id=? AND deleted=0", (part_id,)).fetchone()
    if not p:
        return None
    d = dict(p)
    d["total_stock"] = total_stock(conn, part_id)
    d["locations"] = stock_locations(conn, part_id)
    # v1.0.27：两页（批量出入库 / 出入库调库申请）共用同一份物料级状态汇总，
    # 服务端提交时也用 db.cond_summary 重算，绝不信任前端传来的判定结果。
    d["cond_summary"] = cond_summary(conn, part_id)
    d["stock_status"] = (
        "low" if d["total_stock"] < d["min_stock"]
        else ("high" if (d["max_stock"] and d["total_stock"] > d["max_stock"]) else "ok")
    )
    return d


def gen_no(prefix, conn, table, col):
    """生成单据编号，如 APP20260922-001"""
    ts = datetime.now().strftime("%Y%m%d")
    row = conn.execute(
        f"SELECT {col} v FROM {table} WHERE {col} LIKE ? ORDER BY {col} DESC LIMIT 1",
        (f"{prefix}{ts}-%",),
    ).fetchone()
    seq = 1
    if row:
        try:
            seq = int(str(row["v"]).rsplit("-", 1)[1]) + 1
        except Exception:
            seq = 1
    return f"{prefix}{ts}-{seq:03d}"


def get_setting(conn, key, default=None):
    """读取系统设置（key-value 表）"""
    row = conn.execute("SELECT value FROM sys_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    """写入系统设置"""
    conn.execute(
        "INSERT INTO sys_settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ------------------------------------------------------------------
# 产线 / 设备 / 物料适用对象
# ------------------------------------------------------------------
def list_lines(conn):
    """全部产线"""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM production_lines ORDER BY id").fetchall()]


def list_equipment(conn, line_id=None):
    """设备列表（可按产线过滤）"""
    if line_id:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM equipment WHERE line_id=? ORDER BY id", (line_id,)).fetchall()]
    return [dict(r) for r in conn.execute(
        "SELECT * FROM equipment ORDER BY line_id, id").fetchall()]


def part_apps(conn, part_id):
    """某物料的适用产线/设备列表（含产线/设备名称）"""
    rows = conn.execute(
        """SELECT pa.line_id, pa.equipment_id, pl.name line_name, e.name equip_name, e.model equip_model
           FROM part_applications pa
           LEFT JOIN production_lines pl ON pa.line_id=pl.id
           LEFT JOIN equipment e ON pa.equipment_id=e.id
           WHERE pa.part_id=? ORDER BY pa.id""",
        (part_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_part_apps(conn, part_id, pairs):
    """重设物料适用产线/设备：pairs=[(line_id_or_None, equipment_id_or_None), ...]
    同一物料同一组合只保留一行；
    - (line_id, equipment_id)   该产线下某台设备
    - (line_id, None)           整条产线适用
    - (None, equipment_id)      未分配产线的设备（按设备本身适用）
    """
    conn.execute("DELETE FROM part_applications WHERE part_id=?", (part_id,))
    for line_id, equip_id in pairs:
        if not line_id and not equip_id:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO part_applications(part_id, line_id, equipment_id, created_at) VALUES(?,?,?,?)",
            (part_id, int(line_id) if line_id else None, int(equip_id) if equip_id else None, now_str()),
        )


def get_location(conn, loc_id):
    """按 id 取货架位置"""
    if not loc_id:
        return None
    return conn.execute("SELECT * FROM locations WHERE id=?", (loc_id,)).fetchone()


def location_text(loc):
    """位置显示文本：仓库号-货架号-货位号-格位号（格位为空时只显示三级）
    loc 可为 sqlite3.Row / 统一行对象 或 dict
    """
    if not loc:
        return ""
    bin_v = loc["bin"] if "bin" in loc.keys() else loc.get("bin")
    base = f"{loc['warehouse']}-{loc['rack']}-{loc['shelf']}"
    if bin_v:
        return f"{base}-{bin_v}"
    return base


def move_stock(conn, part_id, loc, delta, batch_no=None, repair=0, condition="good"):
    """在指定库位增减库存（原子 SQL，多用户并发安全）
    loc 可为 int（locations.id）或 str（旧文本库位名，兼容）
    delta>0 入库，delta<0 出库（不允许出负库存）
    batch_no：入库批次号；传了则同批次累加、新批次插新行（出库按插入顺序=先进先出）
    repair：本次入库里有多少件是维修件（v1.0.21，只有「维修还回」会传）；
            出库不用传，扣减时由触发器 trg_inv_repair_out 先扣维修件
    condition：本次入库配件好坏（v1.0.22：good/defective/repair）；仅入库时参与合并
            主键（同 part+库位+批次+condition 才累加，否则插新行），出库忽略
    """
    repair = float(repair or 0)
    condition = condition if condition in ("good", "defective", "repair") else "good"
    if isinstance(loc, int):
        # 按位置 id 操作：位置文本同步刷新
        loc_row = get_location(conn, loc)
        loc_txt = location_text(loc_row) if loc_row else ""
        if not loc_row:
            return False, _xt("库位(id={loc})不存在", loc=loc)
        if delta >= 0:
            if batch_no:
                # 同 part+库位+批次+condition → 累加；否则新增批次行
                row = conn.execute(
                    "SELECT id, quantity FROM inventory WHERE part_id=? AND location_id=? AND batch_no=? AND condition=? ORDER BY id LIMIT 1",
                    (part_id, loc, batch_no, condition),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE inventory SET quantity=quantity+?, "
                        "repair_qty=MIN(quantity+?, IFNULL(repair_qty,0)+?) WHERE id=?",
                        (delta, delta, repair, row["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO inventory(part_id, location_id, location, quantity, repair_qty, batch_no, condition, in_date) VALUES(?,?,?,?,?,?,?,?)",
                        (part_id, loc, loc_txt, delta, max(0.0, min(delta, repair)), batch_no,
                         condition, datetime.now().strftime("%Y-%m-%d")),
                    )
                return True, "ok"
            row = conn.execute(
                "SELECT id, quantity FROM inventory WHERE part_id=? AND location_id=? AND condition=? ORDER BY id LIMIT 1",
                (part_id, loc, condition),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE inventory SET quantity=quantity+?, location=?, "
                    "repair_qty=MIN(quantity+?, IFNULL(repair_qty,0)+?) WHERE id=?",
                    (delta, loc_txt, delta, repair, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO inventory(part_id, location_id, location, quantity, repair_qty, condition) VALUES(?,?,?,?,?,?)",
                    (part_id, loc, loc_txt, delta, max(0.0, min(delta, repair)), condition),
                )
            return True, "ok"
        # 出库：先校验该位置总库存，再按行 id 顺序扣（=先进先出：先入库的批次先出）
        tot = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) s FROM inventory WHERE part_id=? AND location_id=?",
            (part_id, loc),
        ).fetchone()["s"]
        if tot + delta < 0:
            return False, _xt("库存不足：库位[{loc}] 现有 {qty}", loc=loc_txt, qty=tot)
        rem = -delta
        for r in conn.execute(
            "SELECT id, quantity FROM inventory WHERE part_id=? AND location_id=? AND quantity>0 ORDER BY id",
            (part_id, loc),
        ).fetchall():
            if rem <= 0:
                break
            take = min(r["quantity"], rem)
            conn.execute("UPDATE inventory SET quantity=quantity-? WHERE id=?", (take, r["id"]))
            rem -= take
        return True, "ok"
    # 旧文本库位路径（兼容旧流水回滚等；先查后改/插，兼容同位置多行历史数据）
    if delta >= 0:
        # v1.0.79：带批次的入库（借·换·检·修还回）也走同批次+状态合并，
        # 新行写 batch_no / in_date，避免落到 B-LEGACY + 空入库日期。
        if batch_no:
            row = conn.execute(
                "SELECT id FROM inventory WHERE part_id=? AND location=? AND batch_no=? "
                "AND condition=? ORDER BY id LIMIT 1",
                (part_id, loc, batch_no, condition),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE inventory SET quantity=quantity+?, "
                    "repair_qty=MIN(quantity+?, IFNULL(repair_qty,0)+?) WHERE id=?",
                    (delta, delta, repair, row["id"]))
            else:
                conn.execute(
                    "INSERT INTO inventory(part_id, location, quantity, repair_qty, "
                    "batch_no, condition, in_date) VALUES(?,?,?,?,?,?,?)",
                    (part_id, loc, delta, max(0.0, min(delta, repair)), batch_no,
                     condition, datetime.now().strftime("%Y-%m-%d")))
            return True, "ok"
        row = conn.execute(
            "SELECT id FROM inventory WHERE part_id=? AND location=? AND condition=? ORDER BY id LIMIT 1",
            (part_id, loc, condition),
        ).fetchone()
        if row:
            conn.execute("UPDATE inventory SET quantity=quantity+? WHERE id=?", (delta, row["id"]))
        else:
            conn.execute(
                "INSERT INTO inventory(part_id, location, quantity, condition) VALUES(?,?,?,?)",
                (part_id, loc, delta, condition),
            )
        return True, "ok"
    cur = conn.execute(
        """UPDATE inventory SET quantity = quantity + ?
           WHERE part_id=? AND location=? AND quantity + ? >= 0""",
        (delta, part_id, loc, delta),
    )
    if cur.rowcount == 0:
        row = conn.execute(
            "SELECT quantity FROM inventory WHERE part_id=? AND location=?",
            (part_id, loc),
        ).fetchone()
        if row:
            return False, _xt("库存不足：库位[{loc}] 现有 {qty}", loc=loc, qty=row['quantity'])
        return False, _xt("库位[{loc}] 无该物料库存", loc=loc)
    return True, "ok"
