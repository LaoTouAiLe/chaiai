# 安装、维护、升级与卸载

本指南适用于当前 v1.0 候选源码。旧 T40 试用包有自己的构建记录，不代表此候选已经在新机器验收。
源码运行见 [README](../../local-agent-web/README.md)；Windows 目录式分发沿用双击 `chaiai.exe`，本轮没有重新打包。

## 启动与诊断

源码在 `local-agent-web/` 使用已有环境执行：

```powershell
.venv/Scripts/python.exe -m local_agent.api
# 不读 .env/系统 Key、不启动服务、不下载模型；退出 1 表示有 error。
.venv/Scripts/python.exe -m local_agent.api --diagnose --config config.yaml
# 独立入口，输出同一份 JSON：
.venv/Scripts/python.exe scripts/diagnose_environment.py --config config.yaml
.venv/Scripts/python.exe scripts/check_release_contracts.py --check
```

诊断输出是 ASCII 安全的 JSON，适合保存和程序读取。PowerShell 中可把中文提示展开为表格：

```powershell
.venv/Scripts/python.exe -m local_agent.api --diagnose --config config.yaml |
  ConvertFrom-Json | Select-Object -ExpandProperty checks | Format-Table -Wrap
```

失败仍以原诊断进程退出码和 `ok=false` 为准，不能只看表格有没有内容。

新代码构建的分发目录可用 `chaiai.exe --diagnose`；旧包是否含此选项以其构建记录为准。
缺 YAML 可以进入初始设置模式，缺模型不会阻止打开设置。诊断针对 YAML/环境，不读取网页运行期
覆盖层；依赖可见不代表本地模型已缓存，数据库 quick_check 通过不代表业务历史已核对。

## 数据分别存在哪里

**所有应用数据都在一个根下**（`data_root`，2026-10-26）：默认 Windows 是
`%LOCALAPPDATA%\chaiai`，macOS 是 `~/Library/Application Support/chaiai`，其它平台是
`$XDG_DATA_HOME/chaiai` 或 `~/.local/share/chaiai`。源码形态与分发形态**用同一个根**
（不再相对源码目录：数据不跟着代码走）。可用 `data_root` 或环境变量 `CHAIAI_HOME` 改到
任意绝对路径，换根后旧数据不会自动搬。程序资源仍在解压目录。配置可改路径，
下表默认值不能替代你的实际配置。

| 数据 | 默认位置或来源 | 备份与限制 |
| --- | --- | --- |
| 会话、Checkpoint、任务、审批、分支 | `memory.short_term.path`：`<data_root>/checkpoints.sqlite` | 用 SQLite backup；未结束任务恢复后按页面核对，不自动重跑 |
| 模型连接、历史配置、选择、应用设置、外部路径授权 | `model_settings.path`：`<data_root>/model_settings.sqlite` | 用 SQLite backup；其中凭据引用不包含 Key，恢复库不能恢复其它机器上的条目 |
| 长期记忆 | `memory.long_term.path`：`<data_root>/memories.sqlite` | 用 SQLite backup |
| 中期向量记忆 | `memory.medium_term.path`：`<data_root>/chroma` | 停服务后保留完整目录；不由 SQLite 维护脚本处理，不跨模型重建 |
| 附件、文件变更基线、工具目录缓存、指标 | `<data_root>/attachments`、`file-changes`、`mcp_catalog.sqlite`、`metrics` | 与库同根；整根备份即完整 |
| YAML、旧环境变量方式的 Key | `config.yaml`、`.env` 或外部环境 | 手动保留私有副本；不要放进公开附件或提交 Git |
| OS 凭据 | Windows 凭据管理器，服务名 `local-agent-web` | 换机器重新录入；内存模式重启后重新录入 |
| 本地 Embedding 权重/附属缓存 | 显式 `embedding.cache_dir` | 目录独立；换缓存目录按设置提示重启，不自动搬迁或清理 |
| 工作区文件/检索结果/命令产物 | 用户选择的工作区及其实际产物路径 | 单库备份不含它们，可能含原文；按工作区自行备份 |
| 启动错误日志 | `startup.py` 选择的可写目录 | 原始堆栈可能含本机路径；本地排障材料，分享前检查 |

## 停服务、备份与恢复

先在页面确认任务终态，退出当前程序/服务。暂停任务未执行完不能假定无副作用。
聊天库维护命令会取得源锁；设置/长期库没有对应服务锁检测，必须由你确认服务已经停止。
不要通过删除锁文件来绕过活跃服务。

示例在源码项目目录执行；日期后缀只是示例，每次换一个不存在的新文件名。数据路径按你的配置替换。

```powershell
# 默认数据根；改过 data_root 或 CHAIAI_HOME 就换成你的路径
$root = "$env:LOCALAPPDATA\chaiai"
.venv/Scripts/python.exe scripts/database_maintenance.py check --database "$root\checkpoints.sqlite"
.venv/Scripts/python.exe scripts/database_maintenance.py backup --database "$root\checkpoints.sqlite" --output backups/chat-before-upgrade.sqlite
.venv/Scripts/python.exe scripts/database_maintenance.py backup --database "$root\model_settings.sqlite" --output backups/settings-before-upgrade.sqlite
.venv/Scripts/python.exe scripts/database_maintenance.py backup --database "$root\memories.sqlite" --output backups/memory-before-upgrade.sqlite
# 恢复到新路径，原库与备份保留：
.venv/Scripts/python.exe scripts/database_maintenance.py restore --backup backups/chat-before-upgrade.sqlite --output "$root\checkpoints-restored.sqlite"
.venv/Scripts/python.exe scripts/database_maintenance.py check --database "$root\checkpoints-restored.sqlite"
```

未创建/未启用的库不用伪造空库来备份。三次命令是三个单库快照，不是跨库在线一致快照；
服务停止后同时保留配置、Chroma 与工作区，才能形成与你实际启用能力对应的维护副本。
backup 会包含已提交 WAL，不包含未提交事务，成功副本是独立 SQLite 文件；失败不覆盖原库。
不要只复制正在使用的 `.sqlite` 主文件，也不要擅自删除原库的 WAL/SHM。

聊天恢复后修改私有 YAML 的 `memory.short_term.path` 指向新文件；设置/长期库分别修改
`model_settings.path` / `memory.long_term.path`。再运行诊断、启动、读取历史、继续一个会话、
检查任务和审批状态。恢复不撤销已经写入工作区的文件；不确定任务先人工核对，不能重试冒充恢复。
数据损坏时保留原文件和备份，脚本不会自动修复或重新建空库。

## 升级与回退

1. 保存当前程序版本/源码提交与未提交状态，停止服务，按上节制作维护副本。
2. 保留旧程序，准备新程序到独立目录；不要覆盖用户配置或凭据。
3. 在维护副本上运行诊断和新程序，核对历史会话、记忆、任务、审批、模型选择，再继续一个任务。
4. 副本通过后才使用正式数据；程序资源与数据分离不等于迁移已经验证。
5. 回退用旧程序和**升级前**的备份，恢复到新路径；旧程序不能直接使用结构版本更高的新库。

兼容基线当前是聊天/长期/设置结构 10/3/3；迁移是前向流程，没有自动向下迁移。
诊断可识别更高结构版本，但它是显式入口，不声称普通启动已经具有全部降级保护。
旧备份之后的新消息/修改不会随回退自动合并，应保留新库和新工作区产物供人工处理。

## 模型缓存管理与卸载

供应商的提示词缓存由供应商维护，本程序没有清空远端缓存的按钮，也不保证固定命中率。
本地 `embedding.cache_dir` 是权重与 HuggingFace 附属缓存目录；先确认实际目录，停止服务，
保留需要离线使用的权重。手动删除后再次使用可能需要下载；`local_files_only=true` 下缺权重会失败。
它不是会话库或 Chroma，不能把三者混作“清缓存”。本轮未实际删除或下载权重。

卸载有两个入口：开始菜单的「卸载 chaiai」（Inno 卸载器，只删程序），以及程序内
「设置 → 关于 → 卸载」。两者**默认都保留用户数据**；程序内那条可以显式勾选
「同时删除数据目录」，勾选后由卸载交接脚本在卸载器完成之后删除数据根并清掉 `data-root.txt`
指针——不勾选就不删，也没有"点一次就悄悄删"的路径。默认位置是 `%LOCALAPPDATA%\chaiai`，
改过位置时以「关于」页显示的数据目录为准。盘符根、用户主目录及其上级、系统目录会被护栏拒绝，
那时勾选框不可用，只能手动删除；删之前先备份实际配置的 SQLite / Chroma / YAML / .env 与模型缓存。

用户工作区不在删除范围内，也不能作为程序目录一起删除。系统凭据单独在凭据管理器按服务与连接引用识别，
不要批量删除其它应用凭据；本指南不自动导出/删除 Key。保留内存模式没有持久化 Key 可删除。

## 验证范围

备份→恢复→读历史→续聊由 `tests/test_database_maintenance.py` 实际 CLI 回归；分发路径由
`tests/test_app_paths.py` 验证。本机回归不等于全数据跨机器迁移、新分发产物或断电恢复实测。
