# THIRD-PARTY NOTICES（第三方依赖声明）

适用版本：SPMS v1.2

SPMS 自研代码使用 Apache-2.0 许可证。本项目依赖的第三方组件版权归各自作者所有，并按各自许可证条款使用。以下清单同时覆盖源码运行依赖、Windows 内置运行时依赖和可选数据库驱动。

---

## 1. Python 运行时

| 组件 | 版本 | 许可证 | 说明 |
| --- | --- | --- | --- |
| CPython | 3.13.x | PSF License Agreement | Windows 包内置运行时 |
| pip / setuptools | 随运行时 | MIT / PSF | 运行时自带安装工具 |

Windows 包只包含运行时二进制和标准库，不分发这些组件的修改版本。

---

## 2. 核心运行依赖

以下依赖是 SPMS 运行所必需的，源码运行通过 `requirements.txt` 安装，Windows 包已内置。

| 库 | 内置版本 | 许可证 | 用途 |
| --- | --- | --- | --- |
| Flask | 3.1.3 | BSD-3-Clause | Web 框架 |
| Werkzeug | 3.1.8 | BSD-3-Clause | WSGI 工具与请求处理 |
| Jinja2 | 3.1.6 | BSD-3-Clause | 模板引擎 |
| MarkupSafe | 3.0.3 | BSD-3-Clause | HTML 转义 |
| click | 8.5.0 | BSD-3-Clause | 命令行参数 |
| blinker | 1.9.0 | BSD-3-Clause | 信号机制 |
| itsdangerous | 2.2.0 | BSD-3-Clause | 会话签名 |
| ldap3 | 2.9.1 | LGPL-3.0 | AD 域登录 |
| pyasn1 | 0.4.8 | BSD-2-Clause | ASN.1 编解码（ldap3 依赖） |
| Pillow | 12.3.0 | HPND（MIT 兼容） | 备件图片压缩 |
| openpyxl | 3.1.5 | MIT | Excel 导入导出 |
| et-xmlfile | 2.0.0 | MIT | openpyxl 依赖 |
| packaging | 26.3 | Apache-2.0 / BSD-2-Clause | 版本比较与包信息 |
| cryptography | 50.0.2 | Apache-2.0 / BSD-3-Clause | 加密通信支持 |
| cffi | 2.1.1 | MIT | 本地扩展接口 |
| pycparser | 3.12 | BSD-3-Clause | C 解析器（cffi 依赖） |

LGPL 组件（`ldap3`）以普通运行期依赖方式使用，未修改其源码，也未静态链接进 SPMS 自有代码。

---

## 3. 数据库驱动

SQLite 由 Python 标准库提供，无需额外驱动。以下驱动按需安装，源码用户可参考 `requirements-full.txt`。

| 库 | 许可证 | 目标数据库 | Windows 包是否内置 |
| --- | --- | --- | --- |
| PyMySQL | MIT | MySQL / MariaDB | 是 |
| psycopg2-binary | LGPL-3.0 | PostgreSQL | 否 |
| pymssql | LGPL-2.1 | SQL Server | 否 |
| python-oracledb | UPL-1.0 / Apache-2.0 | Oracle | 否 |

使用非 SQLite 数据库时，请在源码环境安装对应驱动；Windows 免安装包默认面向 SQLite 场景。

---

## 4. 前端资源

SPMS 前端为原生 HTML、CSS 和 JavaScript。图表由 `static/js/charts.js` 基于 Canvas 自绘，不依赖 jQuery、ECharts、Bootstrap 等第三方库，不使用 CDN 外链，不分发第三方字体文件。

---

## 5. 使用方注意

- 源码安装：`pip install -r requirements.txt`；需要其它数据库时使用 `requirements-full.txt`。
- 若部署环境额外引入了本清单之外的库，请自行核对其许可证。
- 本清单随版本发布整理；如发现遗漏，请通过 Issue 告知维护者。

Copyright © 2026 菜鸟大叔 (rookie-uncle / Colton1982). All rights reserved.
