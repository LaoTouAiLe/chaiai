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
2. **用户数据不归安装包管**：配置 / SQLite / 记忆都在 `%LOCALAPPDATA%\chaiai`，卸载**不删**这个目录
   （删数据必须是用户自己的动作）。
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
- **没有代码签名**：安装包会触发 SmartScreen「未知发布者」。签名证书是付费项，未购买前在
  发布说明里写清"点更多信息 → 仍要运行"。
- **没有自动更新**：目前是"下载新版安装包覆盖安装"。`SHA256SUMS.txt` 已经为将来接更新留好了
  校验依据。
- 安装版与开发版**共用同一个数据目录**：在开发机上装一个旧版本安装包，`--diagnose` 会报
  "数据库需要迁移"。给朋友的干净机器不会有这个问题，但自己测的时候要留意。
