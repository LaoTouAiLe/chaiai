# 打包与安装包

两种交付形态，产物都不进版本库（`build/` 已被忽略）：

| 形态 | 产物 | 谁在用 |
| --- | --- | --- |
| 目录包（zip） | `build/packaging/<out>/chaiai/`（`chaiai.exe` + `_internal/`） | 不想装、解压即用 |
| 安装包 | `build/packaging/installer/chaiai-setup-<version>.exe` | 给普通用户，带开始菜单、卸载项、升级覆盖 |

## 构建

分两步，分别在两个目录执行（相对路径都写清楚，别混）。

```powershell
# 1) 目录包 —— 在 local-agent-web/ 下（它会先重建前端产物）
cd local-agent-web
$env:CHAIAI_OUT_DIR = "build/packaging/out-$(Get-Date -Format yyyyMMdd)"
.\.venv\Scripts\python.exe scripts\build_windows_package.py --frontend

# 2) 安装包 —— 回到仓库根（相对路径以 packaging/ 为基准）
cd ..
ISCC.exe /DAppVersion=0.1.0 `
  /DSourceDir=..\local-agent-web\build\packaging\out-20261023\chaiai `
  packaging\chaiai.iss
```

- 版本号**由命令行传入**，不要在 `.iss` 里写第二份；发布时应该与 `pyproject.toml` 的 `version` 一致。
- 换 `CHAIAI_OUT_DIR` 到一个新目录可以避免"原地重建要删两千多个文件"（本机对批量删除有审批限制）。
- 目录包构建脚本自带四道检查：前端产物存在、DLL 自包含、敏感数据扫描、MANIFEST/BUILD_INFO。

## 安装包的三个约定

1. **按用户安装**（`PrivilegesRequired=lowest`）：装进 `%LOCALAPPDATA%\Programs\chaiai`，不要管理员权限。
2. **用户数据默认不归安装包管**：配置 / SQLite / 记忆都在 `%LOCALAPPDATA%\chaiai`，Inno 卸载
   **不删**这个目录。程序内「设置 → 关于 → 卸载」另有**显式的「同时删除数据目录」勾选**（默认不勾）：
   那是用户自己按下的动作，且后端会先过护栏——盘符根、用户主目录及其上级、系统目录一律拒绝。
3. **升级＝再装一次**：`AppId` 固定，新版本直接覆盖；`CloseApplications=yes` 会提示关闭正在运行的
   `chaiai.exe`（Windows 不能替换运行中的 exe）。

## 发布（GitHub Actions）

`.github/workflows/release.yml`：打 tag `v0.2.0` → 构建 → 建 Release，产物三件：

```
chaiai-0.2.0-win64.zip          绿色包（解压即用）
chaiai-setup-0.2.0.exe          安装包
SHA256SUMS.txt                  上面两个的校验值
```

手动触发（workflow_dispatch）时要填版本号；tag 触发时从 tag 里取（`v0.2.0` → `0.2.0`）。
版本号的唯一真源仍应是 `pyproject.toml`，发布前请确认 tag 与它一致。

## 已知缺口

- **安装向导的中文是"英文骨架 + 主要页面中文"**：Inno 官方发行包不含简体中文语言文件
  （`Unofficial/ChineseSimplified.isl`），本机尝试 GitHub raw / 各类镜像均被拦截，没有取到。
  现在靠 `.iss` 里的 `[Messages]` 覆盖了欢迎、目录、任务、准备、安装、完成、卸载与按钮。
  拿到完整 `.isl` 后放进 `packaging/languages/`，在 `[Languages]` 里加一行即可整体替换，
  同时可以删掉那段 `[Messages]`。
- **没有代码签名**：安装包会触发 SmartScreen「未知发布者」。**不必自己买证书**——SignPath.io 与
  Certum 都对开源项目提供免费代码签名（需审核，1–2 周），Azure Trusted Signing 约 $9.99/月。
  证书到位之前，Release 说明与 README 必须写清「点更多信息 → 仍要运行」，否则第一波用户会当成流氓软件。
- **只检查更新，不自动安装**（2026-10-27）：`设置 → 关于` 会查 GitHub 最新 Release 并提醒，
  有新版就打开 Release 页由用户自己下载、覆盖安装。**不下载、不静默升级**——本项目没有自升级器，
  替用户替换正在运行的 exe 是另一整套东西。`SHA256SUMS.txt` 仍是"下载到的那份是不是我们发的"
  的校验依据。
- **构建机的 locale 必须能输出 UTF-8**（2026-10-27 实测并已修）：GitHub 的 Windows runner 是 en-US，
  Python 的 stdout 默认 cp1252，而构建脚本每条进度都是中文——第一次跑 release workflow 就崩在
  `[2/5] 打包：PyInstaller onedir → …` 的 `print` 上（`UnicodeEncodeError: 'charmap'`）。
  现在脚本自己 `reconfigure(encoding="utf-8")`、两个 workflow 也都设了 `PYTHONIOENCODING=utf-8`。
  换构建机或新写 CI 时不要去掉这两处；本地中文 Windows 是 cp936，看不出这个问题。
- **CI 与本机的依赖布局不同**：本机是 conda 系 Python，`[3/5]` 的 DLL 补齐会从
  `<base_prefix>/Library/bin` 取 `libcrypto-3-x64.dll` 等；标准 CPython（CI）里这些位置不存在。
  目前 CI 能通过，是因为 PyInstaller 在标准布局下会自己收进 `_internal`。换 runner 镜像后若报
  "缺 XXX.dll"，先看是分析漏收还是环境真的没有。
- 安装版与开发版**共用同一个数据目录**：在开发机上装一个旧版本安装包，`--diagnose` 会报
  "数据库需要迁移"。给朋友的干净机器不会有这个问题，但自己测的时候要留意。
