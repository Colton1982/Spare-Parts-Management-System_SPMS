# Contributing（贡献指南）

适用版本：SPMS v1.2

感谢你参与 SPMS。请按以下约定提交问题、代码和文档，保持代码库清晰可维护。

---

## 1. 环境准备

```bash
git clone <仓库地址>
cd SPMS
pip install -r requirements.txt        # SQLite 最小依赖
python app.py                          # http://127.0.0.1:5000
```

需要 MySQL、MariaDB、PostgreSQL、SQL Server、Oracle 时：

```bash
pip install -r requirements-full.txt
```

Windows 用户也可以直接使用发布包内的 `runtime/python.exe`，无需另装 Python。

---

## 2. 分支与提交

- 从 `main` 拉取分支：`feat/xxx`、`fix/xxx`、`docs/xxx`。
- 提交信息简洁明了，例如 `fix: 修复盘点合并报告合计不一致`。
- 一次提交只做一件事；不要夹带无关重构或数据文件。
- 不要提交 `settings.json`、`data/*.db`、`logs/`、密钥或公司内部信息。

---

## 3. 代码约定

- 保持现有模块边界：路由和业务逻辑在 `app.py`，建表升级在 `database.py`/`db.py`，配置在 `config.py`，翻译入口在 `i18n.py`。
- SQL 一律参数化，禁止字符串拼接用户输入。
- 模板继承 `base.html`，复用已有组件和样式。
- 前端图表沿用 Canvas 自绘方案，不引入第三方 JS/CSS 框架。
- 涉及数据库结构时必须同时更新建表逻辑和升级路径，保证旧库可平滑打开。
- 金额字段是敏感字段，新增页面和导出时必须沿用现有权限判断。

---

## 4. 国际化约定

界面文案必须走翻译通道：

- Jinja 模板：`{{ tr('中文') }}`
- JavaScript：`TR('中文')`
- Python：`_i18n.t('中文', lang)`

新增文案需要同步四路：

1. `locale/legacy_map.json`
2. `locale/catalog/<语言>.json`
3. `locale/i18n/<语言>.json`
4. `locale/pages.json`

提交前执行：

```bash
python -m i18n2.scan
```

要求 `unwrapped`、`unregistered`、`untranslated`、`shard_gap`、`runtime_miss` 全部为 0。

拼接式文案必须改成带占位符的整句词条，不同语言语序不同，不能依赖中文语序拼接。业务数据（备件名、部门名、用户名、申请原因等）保持原文，不进入翻译词典。

---

## 5. 提交前检查

- [ ] `python app.py` 可正常启动，关键页面无 500 错误。
- [ ] 全量回归门禁异常为 0。
- [ ] 翻译扫描五项计数全 0。
- [ ] 数据库改动包含升级路径。
- [ ] 模板和静态资源有改动时，JS/CSS 缓存版本号已递增。
- [ ] 不包含数据库、配置、日志、密钥和临时文件。
- [ ] 文档与代码行为一致。

---

## 6. 议题与安全问题

提交 Issue 时请说明：

- 复现步骤
- 期望行为与实际行为
- 环境（操作系统、浏览器、Python 版本、数据库类型）
- 相关页面或模块路径

建议类议题请说明使用场景，便于评估优先级。

涉及安全问题时不要公开披露漏洞细节，请按 `SECURITY.md` 的渠道联系维护者。

---

## 7. 行为准则

保持友善、专业、就事论事。尊重不同使用环境和使用者，避免带有歧视、攻击或泄露隐私的内容。

Copyright © 2026 菜鸟大叔 (rookie-uncle / Colton1982). All rights reserved.
