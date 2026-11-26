# SPMS 开源与发布操作手册

**版本：v1.2**

本项目当前状态作为 v1.2 发布。本文说明如何把仓库源码和 Windows 免安装包分别交付，不记录中间版本演进。

---

## 1. 两类交付物

| 交付物 | 面向 | 包含 | 不包含 |
| --- | --- | --- | --- |
| 源码仓库 | 开发者、二次开发者 | Python 源码、模板、静态资源、词条、文档、requirements | `data/`、`settings.json`、`runtime/`、日志、测试库 |
| Windows 发布包 | 最终用户 | 程序 + 内置 Python 运行时 + `Start.bat` + 文档 | 客户数据库、客户配置、密码、AD 配置 |

开发库的演示数据只用于本地回归，不进入源码仓库和发布包。

---

## 2. 发布前检查

### 2.1 数据与隐私

- [ ] `data/*.db`、`*.db-wal`、`*.db-shm` 不进入仓库或发布包。
- [ ] `settings.json` 不进入仓库或发布包，仅保留 `settings.example.json`。
- [ ] `logs/`、`__pycache__/`、`dist/_pkg_*`、`dist/_smoke_*` 不进入包。
- [ ] 代码、词条和文档中没有公司名、人员姓名、密码、令牌或内网域名。
- [ ] AD、SMTP、API 等敏感配置使用示例值或留空。

### 2.2 文档一致性

- [ ] `README.md`、`RELEASE_NOTES.md` 均声明 v1.2。
- [ ] `OpenSourceGuide.md` 中的命令与脚本路径一致。
- [ ] `BusinessWorkflow.md` 与当前业务规则一致。
- [ ] `THIRD-PARTY-NOTICES.md` 与 `requirements*.txt`、内置运行时依赖一致。
- [ ] 模板或静态资源有改动时，JS/CSS 缓存版本号已递增。

### 2.3 回归与翻译

在 `SPMS_v2` 目录执行：

```bash
./SPMS/runtime/python.exe tools/_gate.py
```

目标：脚本异常 0。

在 `SPMS` 目录执行翻译检查：

```bash
runtime/python.exe -m i18n2.scan
```

目标计数全 0：`unwrapped`、`unregistered`、`untranslated`、`shard_gap`、`runtime_miss`。新增文案必须同步四路：

1. `locale/legacy_map.json`
2. `locale/catalog/<语言>.json`
3. `locale/languages.json`
4. `locale/pages.json`

禁止只写中文让其它语言静默回退。

---

## 3. 源码开源流程

### 3.1 初始化仓库

在 `SPMS_v2/` 目录执行：

```bash
git init
git add SPMS/README.md SPMS/LICENSE SPMS/OpenSourceGuide.md SPMS/CONTRIBUTING.md \
        SPMS/RELEASE_NOTES.md SPMS/THIRD-PARTY-NOTICES.md SPMS/BusinessWorkflow.md \
        SPMS/requirements.txt SPMS/requirements-full.txt SPMS/settings.example.json \
        SPMS/app.py SPMS/config.py SPMS/database.py SPMS/db.py SPMS/settings.py \
        SPMS/notify.py SPMS/share_guard.py SPMS/i18n.py \
        SPMS/i18n2 SPMS/locale SPMS/templates SPMS/static
git commit -m "Release: SPMS v1.2"
git branch -M main
```

若已有历史提交，先确认其中没有数据库、配置文件或密钥；必要时用 `git rm --cached` 从索引移除，并完善 `.gitignore`。

### 3.2 推送与打标签

```bash
git remote add origin https://github.com/<用户名>/<仓库名>.git
git remote add gitee  https://gitee.com/<用户名>/<仓库名>.git
git push -u origin main
git push gitee main

git tag -a v1.2 -m "SPMS v1.2"
git push origin v1.2
git push gitee v1.2
```

---

## 4. Windows 发布包流程

### 4.1 准备基线包

打包脚本以一份已审计的 Windows 基线包为输入，用它提供内置 Python 运行时和二进制依赖；随后只覆盖工作树中发生变化的文本与程序文件。

基线包必须满足：

- zip 内所有条目位于 `SPMS/` 下。
- 不含数据库、`settings.json`、日志和用户数据。
- 内置运行时可正常启动。

### 4.2 构建

在 `SPMS_v2/dist` 目录执行：

```bash
mkdir -p _pkg_v100/SPMS
../SPMS/runtime/python.exe tools/build_package.py \
  --zip <已审计基线包>.zip \
  --src ../SPMS \
  --out _pkg_v100/SPMS \
  --zip-out SPMS_v1.2_Windows_x64.zip \
  --clean
```

输出目录名必须是 `SPMS`，否则 zip 根目录名会出错。

### 4.3 冒烟验证

在 `SPMS_v2` 目录执行：

```bash
./SPMS/runtime/python.exe tools/check_package_smoke.py \
  --zip dist/SPMS_v1.2_Windows_x64.zip \
  --keep --work dist/_smoke_v100
```

目标：`RESULT: OK`，失败 0。

存在透明加密客户端时，还要用非白名单进程扫描：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\check_encrypted.ps1 -Root "dist\_pkg_v100\SPMS" -OutFile "tools\_encrypted_v100.txt"
```

目标：`Encrypted: 0`。

### 4.4 发布到 Release

1. 进入 GitHub/Gitee 仓库的 Releases 页面。
2. Tag 填 `v1.2`，标题填 `SPMS v1.2`。
3. 说明复制 `RELEASE_NOTES.md` 的发布说明，不要只写“bug 修复”。
4. 上传 `dist/SPMS_v1.2_Windows_x64.zip`。
5. 两个平台使用同一份 zip，避免用户拿到不同二进制。

---

## 5. 使用方流程

### 5.1 免安装包

1. 解压 `SPMS_v1.2_Windows_x64.zip`。
2. 双击 `Start.bat`。
3. 首次运行进入初始化向导：选择数据库目录、端口、是否加载演示数据。
4. 使用 `admin / admin123` 登录并立即修改密码。
5. 在系统设置中配置审批级别、备份、语言和部署参数。

### 5.2 源码运行

```bash
pip install -r requirements.txt
python app.py
# 浏览器打开 http://127.0.0.1:5000
```

需要其它数据库时：

```bash
pip install -r requirements-full.txt
```

---

## 6. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 打开后仍是旧界面 | 旧进程未退出或浏览器缓存 | 停止所有 `python.exe app.py`，刷新浏览器缓存 |
| 打开后进入初始化向导 | 覆盖了现场 `settings.json` | 从备份恢复配置；发布包不应带该文件 |
| 文件被透明加密 | 用非白名单工具复制过文件 | 只用打包脚本生成 zip，并用加密扫描脚本验收 |
| 多语言缺字 | 四路词条未同步 | 运行 `python -m i18n2.scan`，计数必须全 0 |
| 包内出现测试库 | 手工向包目录拷文件 | 只用 `build_package.py`，不要手工塞文件 |

---

## 7. 版本规范

- 当前版本为 `v1.2`。
- 后续版本遵循 `vMAJOR.MINOR.PATCH`：不兼容变更递增 MAJOR，兼容新增功能递增 MINOR，修复递增 PATCH。
- 发布说明统一维护在 `RELEASE_NOTES.md`，新增版本时追加段落，不拆分多个历史文件。
- 当前版本由 `README.md` 和 `RELEASE_NOTES.md` 共同声明。

Copyright © 2026 菜鸟大叔 (rookie-uncle / Colton1982). All rights reserved.
