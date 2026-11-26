# -*- coding: utf-8 -*-
# SPMS 备件辅料仓管系统 (Spare Parts Management System)
# 作者：菜鸟大叔 (@rookie-uncle / @Colton1982)
# 邮箱：510658927@qq.com
# QQ：510658927
# Gitee：https://gitee.com/rookie-uncle
# GitHub：https://github.com/Colton1982
# Copyright (c) 2026 rookie-uncle. All rights reserved.
# 开源协议：Apache-2.0
"""月度模拟数据生成工具
用途：为演示/测试环境补齐「今年逐月」的入库、出库流水，
      出库分布到不同的产线与设备，便于数据图表主页分析演示。
幂等：已存在 remark='模拟月度' 的流水时默认跳过（--force 可强制重生成）。
用法：
    python3 seed_monthly_demo.py                # 补齐今年 1 月~当前月
    python3 seed_monthly_demo.py --year 2025    # 指定年份
    python3 seed_monthly_demo.py --force        # 先清掉旧的模拟月度流水再重生成
"""
import argparse
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "SPMS.db")
MARK = "模拟月度"


def connect():
    if not os.path.exists(DB_PATH):
        sys.exit(f"未找到数据库: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn, t):
    return conn.execute(
        "SELECT COUNT(*) n FROM sqlite_master WHERE type='table' AND name=?", (t,)
    ).fetchone()["n"] > 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=datetime.now().year)
    ap.add_argument("--force", action="store_true", help="清空旧模拟月度流水后重新生成")
    args = ap.parse_args()

    conn = connect()
    for t in ("parts", "transactions", "inventory", "production_lines", "equipment", "locations", "users"):
        if not table_exists(conn, t):
            sys.exit(f"表 {t} 不存在，请先初始化系统（启动向导）")

    if args.force:
        cur = conn.execute("DELETE FROM transactions WHERE remark=?", (MARK,))
        print(f"[force] 已删除旧模拟月度流水 {cur.rowcount} 笔")
    # 清理浮点微负残留（|x|<1e-9 视为 0）
    conn.execute("UPDATE inventory SET quantity=0 WHERE ABS(quantity)<1e-9")

    # 幂等检查：该年已有模拟月度流水则跳过
    existing = conn.execute(
        "SELECT COUNT(*) n FROM transactions WHERE remark=? AND created_at LIKE ?",
        (MARK, f"{args.year}%"),
    ).fetchone()["n"]
    if existing and not args.force:
        print(f"跳过：{args.year} 年已有 {existing} 笔模拟月度流水（如需重建加 --force）")
        return

    parts = [dict(r) for r in conn.execute(
        "SELECT id, part_code, name, unit, unit_price, qty_decimal, min_stock, max_stock, is_valuable FROM parts WHERE deleted=0"
    )]
    if not parts:
        sys.exit("备件为空")
    lines = [dict(r) for r in conn.execute("SELECT id, name FROM production_lines")]
    equips = [dict(r) for r in conn.execute("SELECT id, name, line_id FROM equipment")]
    locs = [dict(r) for r in conn.execute(
        "SELECT DISTINCT location_id, location FROM inventory WHERE location!='' ORDER BY location"
    )]
    if not locs:
        sys.exit("无可用库位")
    operator = conn.execute(
        "SELECT id FROM users WHERE username='warehouse01'"
    ).fetchone()
    operator_id = operator["id"] if operator else conn.execute(
        "SELECT id FROM users WHERE username='admin'"
    ).fetchone()["id"]

    # 已用 tx_no 集合（保证唯一）
    used = {r["tx_no"] for r in conn.execute("SELECT tx_no FROM transactions")}
    rnd = random.Random(args.year * 10000 + 9)

    month_last = datetime.now().year if args.year == datetime.now().year else None
    months = range(1, (datetime.now().month if month_last == datetime.now().year else 12) + 1)
    if args.year < datetime.now().year:
        months = range(1, 13)

    total_in = total_out = 0
    for m in months:
        # 每个月：入库 6~12 笔，出库 12~18 笔
        n_in = rnd.randint(6, 12)
        n_out = rnd.randint(12, 18)
        for _ in range(n_in):
            p = rnd.choice(parts)
            if p["qty_decimal"]:
                qty = round(rnd.uniform(0.5, 50), 2)
            else:
                qty = rnd.choice([10, 20, 30, 50, 80, 100, 150, 200, 300])
            loc_row = rnd.choice(locs)
            loc = loc_row["location"]
            loc_id = loc_row["location_id"]
            d = rnd_day(rnd, args.year, m)
            tx_no = make_tx_no(d, used, rnd)
            # 批次号：按到货日期生成，模拟不同入库批次
            batch_no = f"B{d.strftime('%Y%m%d')}-{rnd.randint(1,9)}"
            conn.execute(
                """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,ref_type,operator_id,remark,batch_no,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tx_no, p["id"], "in", qty, p["unit_price"], round(qty * p["unit_price"], 2),
                 loc, "direct", operator_id, MARK, batch_no, d.strftime("%Y-%m-%d %H:%M:%S")),
            )
            cur = conn.execute(
                "UPDATE inventory SET quantity=quantity+? WHERE part_id=? AND location=? AND batch_no=?",
                (qty, p["id"], loc, batch_no),
            )
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO inventory(part_id,location_id,location,quantity,batch_no,in_date) VALUES(?,?,?,?,?,?)",
                    (p["id"], loc_id, loc, qty, batch_no, d.strftime("%Y-%m-%d")),
                )
            total_in += 1

        for _ in range(n_out):
            p = rnd.choice(parts)
            stock = conn.execute(
                "SELECT location, quantity FROM inventory WHERE part_id=? AND quantity>0 ORDER BY quantity DESC",
                (p["id"],),
            ).fetchall()
            if not stock:
                continue
            line = rnd.choice(lines)
            line_equips = [e for e in equips if e["line_id"] == line["id"]] or [None]
            dev = rnd.choice(line_equips)
            max_q = max(r["quantity"] for r in stock)
            if p["qty_decimal"]:
                qty = round(min(rnd.uniform(0.5, 20), max_q), 2)
            else:
                qty = min(rnd.choice([5, 10, 15, 20, 30, 50, 80]), max_q)
            if qty > max_q:  # round 四舍五入可能超出库存，clamp 防负
                qty = max_q
            qty = round(qty, 2)
            if qty <= 0:
                continue
            loc = stock[0]["location"]
            d = rnd_day(rnd, args.year, m)
            tx_no = make_tx_no(d, used, rnd)
            # 出库流水记录实际扣减的批次（按最早批次=先进先出）
            batches = conn.execute(
                "SELECT id, batch_no, quantity FROM inventory WHERE part_id=? AND location=? AND quantity>0 ORDER BY in_date, id",
                (p["id"], loc),
            ).fetchall()
            remain = qty
            out_batch = batches[0]["batch_no"] if batches else ""
            for b in batches:
                if remain <= 0:
                    break
                take = min(b["quantity"], remain)
                conn.execute("UPDATE inventory SET quantity=quantity-? WHERE id=?", (take, b["id"]))
                remain -= take
            conn.execute(
                """INSERT INTO transactions(tx_no,part_id,tx_type,quantity,unit_price,amount,location,ref_type,operator_id,line_name,device_name,remark,batch_no,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tx_no, p["id"], "out", qty, p["unit_price"], round(qty * p["unit_price"], 2),
                 loc, "direct", operator_id, line["name"], (dev["name"] if dev else ""),
                 MARK, out_batch, d.strftime("%Y-%m-%d %H:%M:%S")),
            )
            total_out += 1

    conn.commit()
    print(f"完成：{args.year} 年 {len(months)} 个月，入库 {total_in} 笔、出库 {total_out} 笔（remark={MARK}）")
    conn.close()


def rnd_day(rnd, year, month):
    day = rnd.randint(1, 28)
    h = rnd.randint(8, 18)
    minute = rnd.randint(0, 59)
    return datetime(year, month, day, hour=h, minute=minute)


def make_tx_no(d, used, rnd):
    base = f"TX{d.strftime('%Y%m%d%H%M%S')}"
    n = 0
    while f"{base}{n:03d}" in used:
        n += 1
        if n > 999:
            base = f"TX{d.strftime('%Y%m%d%H%M%S')}{rnd.randint(10, 99)}"
            n = 0
    used.add(f"{base}{n:03d}")
    return f"{base}{n:03d}"


if __name__ == "__main__":
    main()
