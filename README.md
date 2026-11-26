# 备件管理系统 Spare Parts Management System（SPMS）

中文版 | [English](#english)

**当前版本：v1.2（2026-11-25）**

本项目以当前代码状态作为 v1.2 发布。旧开发过程中的版本演进不再计入发布历史，全部能力作为 v1.2 一次性交付。

---

## 中文版

### 项目简介

SPMS 是面向制造工厂产线的备件,辅料，工具的管理系统，覆盖备件建档、四级库位、库存三态、出入库与调库、借换检修、审批、盘点、呆滞分析、数据大屏和备份恢复。系统提供 11 种界面语言，支持 SQLite、MySQL、MariaDB、PostgreSQL、SQL Server、Oracle，可单机离线使用，也可指向共享数据库在局域网内多人协作。

Windows 交付包内置 Python 运行时与全部依赖，解压后双击 `Start.bat` 即可运行，无需安装 Python 或第三方包。

### 核心能力

- **备件主数据**：建档、编辑、停用恢复、Excel 导入导出、多库位录入、整数/小数数量类型、图片压缩存库、字段级操作审计。
- **四级库位与三态管理**：仓库-货架-货位-格位；库位区分良品、待报废、待维修三态，待报废区只允许报废出库，待维修区只允许走借·换·检·修。
- **报废复判调库**：待报废区的件复判可修时可转入待维修区，必须填写复判原因，且执行人须持「待报废转待维修复判」权限；流水固定留痕，配件状态与批次原样保留，不会被当作良品。
- **库存与批次**：每个库存行保留批次与入库日期；调库支持整库位和批次两级选择；先进先出提示可在系统设置中开关和自定义。
- **出入库与审批**：领用、直接出入库、调库、借出、换货、检测、维修及其还回；贵重件强制审批；撤销请求走仓管审批回滚。
- **借换检修**：借出保留原批次；换/检/修还回生成 `RT` 新批次，保证追溯链路完整。
- **申请单批量提交**：出库、入库、调库、借换检修四类分批成单；逐行校验，错误行标红，成功与失败结果分别说明。
- **盘点管理**：按全部、仓库、货架、分类等范围盘点；手机扫码录入；周期节点、复盘单、合并盘点报告；正常行和差异行都展示，差异可永久留痕并生成原因分析。
- **分析图表**：库存、入库、出库、领用多维分析；设备型号领用趋势可点击柱子下钻柏拉图，数量与金额双图随 Excel 导出。
- **数据大屏**：8 套预设布局（每套带金额/无金额双版本）、6 套配色与自定义配色；卡片拖拽排版；分类占比双饼图；刷新间隔、模块增减均可配。
- **权限与安全**：角色 × 模块 × 子操作三级矩阵；金额为敏感字段，无权限者全站不可见；API Key 鉴权；操作审计。
- **多语言**：中、英、日、韩、泰、越、俄、法、德、西班牙、印尼 11 种语言，词条外置，缺译回退中文，带翻译覆盖率和运行期漏词管理。
- **运维**：在线备份、自动备份、整库恢复审批流、回收站、AD 域登录、局域网部署。

### 功能模块

| 模块 | 路径 | 说明 |
| --- | --- | --- |
| 数据图表主页 | `/dashboard` | 库存/入库/出库/领用分析，趋势下钻柏拉图，图表与明细导出 Excel |
| 备件信息维护 | `/parts/manage` | 建档、编辑、删除、导入导出、多库位、数量类型、停用恢复、操作记录 |
| 备件查询 | `/parts` | 按料号/名称/规格/分类/状态筛选，一键发起申请 |
| 出入库调库申请 | `/applications` | 领用、直接出入库、调库、借换检修申请与批量提交 |
| 出入库管理 | `/inout` | 仓管执行、驳回、审批、导出；整库位/批次两级调库；FIFO 提示 |
| 货架管理 | `/locations` | 四级库位、三态属性、库位码打印、操作记录 |
| 借换检修台账 | `/borrows` | 借出、换出、检修、还回与批次追溯 |
| 产线与设备 | `/lines` | 产线、设备配置及适用关系 |
| 部门配置 | `/departments` | 部门管理 |
| 用户管理 | `/users` | 用户增删改、重置密码、角色分配 |
| 权限配置 | `/permissions` | 角色 × 模块 × 子操作权限矩阵 |
| 审批中心 | `/approval` | 贵重件、盘点差异、恢复等业务审批 |
| 呆滞物料 | `/stale` | 呆滞判定（默认 90 天可配）与清单导出 |
| 盘点管理 | `/stocktakes` | 盘点单、扫码录入、周期节点、合并报告、差异审批与校准 |
| 回收站 | `/recycle` | 备件/库位/部门/用户分组回收与恢复 |
| 备份恢复 | `/restore` | 手动/自动备份与整库恢复审批 |
| 系统设置 | `/settings` | 数据库、部署、语言、AD、API、主题、表头、库存规则 |
| 数据大屏 | `/screen` | 车间大屏，布局、配色、卡片与金额开关均可配置 |

### 技术结构

```text
SPMS/
├── app.py                    # Flask 主程序：路由与业务逻辑
├── config.py                 # 角色、权限模块、大屏卡片/布局/配色
├── database.py / db.py       # 建表升级与多数据库适配
├── settings.py / notify.py   # 运行配置与通知
├── i18n.py                   # 兼容适配层（代理到 i18n2）
├── i18n2/                    # 多语言内核：引擎/扫描/翻译/语言管理
├── locale/                   # 词条真源：catalog、legacy_map、pages
├── templates/                # Jinja2 模板
├── static/                   # CSS、JavaScript、自绘图表
├── data/                     # 运行期数据目录（包内不带数据库）
├── settings.example.json     # 配置示例
├── Start.bat                 # Windows 双击启动
├── README.md / BusinessWorkflow.md / RELEASE_NOTES.md
├── OpenSourceGuide.md / CONTRIBUTING.md / THIRD-PARTY-NOTICES.md
└── requirements*.txt         # 源码运行依赖
```

### 多语言机制

- 词条真源在 `locale/catalog/<语言>.json`，中文原文到语义 ID 的映射在 `locale/legacy_map.json`，页面分片在 `locale/pages.json`，语言注册表在 `locale/languages.json`。
- 模板使用 `{{ tr('中文') }}`，JavaScript 使用 `TR('中文')`，Python 使用 `_i18n.t('中文', lang)`。
- 缺译自动回退中文，不显示空白；业务数据（备件名、部门名、用户名、申请原因等）不翻译。
- 新增文案需同步四路词条，并执行扫描确认 `unwrapped`、`unregistered`、`untranslated`、`shard_gap`、`runtime_miss` 均为 0。

### 运行

源码运行：

```bash
pip install -r requirements.txt        # SQLite 最小依赖
python app.py                          # 默认端口 5000
```

需要 MySQL、MariaDB、PostgreSQL、SQL Server、Oracle 时安装：

```bash
pip install -r requirements-full.txt
```

Windows 免安装包：

1. 解压 `SPMS_v1.2_Windows_x64.zip`。
2. 双击 `Start.bat`。
3. 首次运行按初始化向导选择数据库路径、端口和是否加载演示数据。
4. 用管理员账号登录后立即修改密码。

演示账号：`admin / admin123`、`warehouse01 / 123456`、`manager01 / 123456`、`user01 / 123456`。

生成模拟流水：

```bash
python seed_monthly_demo.py              # 今年 1 月至当前月
python seed_monthly_demo.py --year 2025
python seed_monthly_demo.py --force
```

### 数据与部署

- SQLite 为默认数据库；其它数据库在系统设置中切换并安装相应驱动。
- 局域网共享：各客户端指向同一个共享盘数据库文件；图片以二进制存库，随数据库文件一起备份。
- 部署模式开启后，同一网络内其他电脑可通过浏览器访问。
- 备份支持手动与自动（分钟至月），超量自动清理；整库恢复需走审批流。
- 交付包不含数据库和 `settings.json`，避免覆盖现场数据。

### 开源信息

- 许可证：Apache-2.0，详见 `LICENSE`。
- 第三方依赖声明：`THIRD-PARTY-NOTICES.md`。
- 发布说明：`RELEASE_NOTES.md`（v1.2 发布说明）。
- 交付包：`SPMS_v1.2_Windows_x64.zip`（免安装）与 `SPMS_v1.2_Source.zip`（纯源码）。
- 开源与发版流程：`OpenSourceGuide.md`。
- 业务流程：`BusinessWorkflow.md`。
- 贡献指南：`CONTRIBUTING.md`。

软件按“现状”提供，不提供任何明示或默示担保。使用、复制、修改和再分发请遵守 Apache-2.0 与当地法律法规。

## 关于作者

**菜鸟大叔**（@rookie-uncle / @Colton1982）
- Gitee：https://gitee.com/rookie-uncle
- GitHub：https://github.com/Colton1982
- Email：510658927@qq.com｜colton.zhou@outlook.com｜colton.zhou@email.cn
- QQ：510658927

Copyright © 2026 菜鸟大叔 (rookie-uncle / Colton1982). All rights reserved.

---

## English

### Overview

SPMS is a management system for spare parts, consumables and tools on manufacturing production lines. It covers part master data, a four-level location model, three stock conditions, inbound/outbound and transfer workflows, borrow/exchange/verify/maintenance flows, approvals, stocktaking, stagnant-material analysis, shop-floor dashboards and backup/restore. The UI supports 11 languages and the data layer supports SQLite, MySQL, MariaDB, PostgreSQL, SQL Server and Oracle.

The Windows package bundles Python and all dependencies. Unzip it and double-click `Start.bat`; no Python installation is required.

### Key features

- Part master data with multi-location stock, integer/decimal quantities, image compression, Excel import/export and audit logs.
- Warehouse–rack–slot–cell locations with three conditions: good, pending scrap and pending repair. Pending-scrap stock can only be scrapped; pending-repair stock can only move through borrow/exchange/verify/maintenance.
- Batch and inbound-date tracking; two-level transfer source selection (whole location or batch); configurable FIFO placement tip.
- Issue, direct in/out, transfer, borrow, exchange, verification and maintenance flows with approvals and rollback requests.
- Borrow returns keep the original batch; exchange/verify/maintenance returns create a new `RT` batch.
- Per-type batch submission with row-level validation and explicit success/failure results.
- Stocktaking by scope, mobile barcode entry, cycle nodes, merged period reports and permanent difference traceability.
- Analytics with trend-to-Pareto drill-down and Excel export.
- Shop-floor screen with eight layouts, price/no-price variants, six palettes and draggable cards.
- Role/module/operation permission matrix, sensitive price fields, API key authentication and audit trails.
- Eleven UI languages with externalized dictionaries and missing-translation reporting.
- Online/scheduled backups, restore approval workflow, recycle bin, AD login and LAN deployment.

### Modules

| Module | Path | Description |
| --- | --- | --- |
| Dashboard | `/dashboard` | Inventory/in/out/issue analytics, Pareto drill-down, Excel export |
| Part Master | `/parts/manage` | Create/edit/delete, import/export, multi-location stock, audit log |
| Part Search | `/parts` | Search by code/name/spec/category/state; launch requests |
| Applications | `/applications` | Issue, in/out, transfer, borrow/exchange/verify/maintenance requests |
| In/Out | `/inout` | Warehouse execution, approval, export, two-level transfer, FIFO tip |
| Locations | `/locations` | Four-level location tree, three conditions, location-code printing |
| Borrow Register | `/borrows` | Borrow/exchange/verify/maintenance and batch traceability |
| Lines & Equipment | `/lines` | Lines, equipment and applicability |
| Users & Permissions | `/users`, `/permissions` | User management and role/module/operation matrix |
| Approval | `/approval` | Valuable parts, stocktake differences, restores |
| Stagnant | `/stale` | Configurable stagnant threshold and export |
| Stocktaking | `/stocktakes` | Orders, scanning, cycle nodes, merged reports, difference approval |
| Backup & Restore | `/restore` | Scheduled backup and approval-controlled restore |
| Settings | `/settings` | Database, deployment, languages, AD, API, theme, stock rules |
| Screen | `/screen` | Configurable shop-floor dashboard |

### Run from source

```bash
pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5000
```

Use `requirements-full.txt` for non-SQLite database drivers.

Demo accounts: `admin / admin123`, `warehouse01 / 123456`, `manager01 / 123456`, `user01 / 123456`.

### Open source

- License: Apache-2.0 (see `LICENSE`).
- Third-party notices: `THIRD-PARTY-NOTICES.md`.
- Release notes: `RELEASE_NOTES.md` (v1.2).
- Open-source and release workflow: `OpenSourceGuide.md`.
- Business rules: `BusinessWorkflow.md`.
- Contributing: `CONTRIBUTING.md`.

The software is provided “AS IS”, without warranty of any kind.

### About the Author

**Rookie Uncle** (@rookie-uncle / @Colton1982)
- Gitee: https://gitee.com/rookie-uncle
- GitHub: https://github.com/Colton1982
- Email: 510658927@qq.com | colton.zhou@outlook.com | colton.zhou@email.cn
- QQ: 510658927

Copyright © 2026 菜鸟大叔 (rookie-uncle / Colton1982). All rights reserved.
