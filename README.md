# chaiai

**跑在你自己电脑上的 AI Agent。** 会话、记忆、文件、工具执行全在本机，模型端点由你自己填。

*chaiai is a local-first AI agent for Windows: your conversations, memory and files never leave your machine — you bring your own model endpoint.*

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

> **状态：v0.1.0 早期版本。** 契约与接口仍在变动，不建议作为生产依赖。
> 现在开源是为了先听意见——结构、契约、安全边界上的问题，欢迎直接开 issue。

## 它能做什么

| 能力 | 说明 |
| --- | --- |
| 对话与工具循环 | 多轮对话 + 受控工具调用，过程、思考、用量都在页面上可见 |
| 读写文件 | 读类默认免审；写文件、跑命令要你点批准，批准只对那一次生效 |
| 命令执行 | Windows 写侧沙箱（按权限档位派生），长任务可转后台、可中断 |
| 浏览器自动化 | 独立的 Edge/Chrome 实例，13 个动作：打开、点击、填表、截图… |
| 三层记忆 | 短期（上下文压缩）、长期（事实）、中期（向量检索，可选依赖） |
| 联网检索 | Tavily / DeepSeek 搜索 API / Bing、DuckDuckGo 网页版；抓取可走本机或 DeepSeek |
| 工作区面板 | 文件树、搜索、Git 状态与 diff、右栏改动预览、会话地图 |
| 其它 | MCP 服务器、技能（skills）、定时任务、数据中台（记忆与用量） |

## 快速开始

### 方式一：安装包（推荐，Windows 10/11）

1. 到 [Releases](https://github.com/LaoTouAiLe/chaiai/releases) 下载 `chaiai-setup-<版本>.exe`
2. 双击安装（可以改安装位置；装到用户目录，不需要管理员权限）
3. 启动后是一个程序窗口，在「设置 → 模型连接」里填你自己的模型端点与密钥

安装、卸载、数据位置、降级说明见 [使用说明](packaging/使用说明.txt)。

### 方式二：从源码跑

前置：**Python 3.10+**、**Node 22.20+**、[uv](https://docs.astral.sh/uv/)。

```powershell
cd local-agent-web
uv sync                 # 需要中期记忆/本地向量时：uv sync --extra memory
cd frontend
npm ci
npm run build
cd ..
uv run python -m local_agent.api
```

打开 http://127.0.0.1:8000 。开发时在 `frontend/` 跑 `npm run dev`（5173，代理到 8000）。
想要桌面窗口而不是浏览器：`uv run python -m local_agent.api --window`。

配置：首次可以不建 `config.yaml`，直接在网页设置里添加模型连接；需要改文件时从
`config.yaml.example` / `.env.example` 复制，**不要覆盖已有的 `config.yaml`**。

## 安全边界（请先读）

- **只监听回环地址**，没有账号体系，也没有登录页。想跨机器访问必须先自己做认证——现在把端口暴露到局域网等于把终端交给同网段所有人。
- **模型要自己配**：项目不提供 API Key，也不代购。
- **审批不是可选项**：写文件、跑命令走人工批准；Windows 上有写侧沙箱，但它只约束**写**，不隔离读和网络。
- **命令可以自行联网**，当前没有 CPU/内存上限与网络隔离。
- **安装包没有代码签名**，SmartScreen 会提示"未知发布者"，需要点「更多信息 → 仍要运行」。
- 只面向 **Windows**：写侧沙箱、桌面窗口、系统查询都依赖 Windows 能力。

## 数据放在哪

| 内容 | 位置 |
| --- | --- |
| 程序 | `%LOCALAPPDATA%\Programs\chaiai` |
| 设置库（含密钥）、会话、记忆 | `%LOCALAPPDATA%\chaiai` |
| 运行日志 | 同目录下 `启动错误.log`、`模型调用失败.log` |

**卸载不会删数据**。升级＝用新安装包覆盖安装，数据目录不受影响。

## 项目结构

```
chaiai/
├─ local-agent-web/     主程序：Python 后端（src/）+ 前端（frontend/）+ 测试（tests/）
├─ docs/                设计与记录：plans / audits / lessons / benchmarks / archive
├─ packaging/           发布物料：Inno Setup 脚本、使用说明、随包二进制
├─ skills/              随包技能
├─ website/             静态文档站
└─ ARCHITECTURE.md      契约与边界（改行为前先读这个）
```

## 文档

| 你想了解 | 去哪看 |
| --- | --- |
| 启动、配置、页面怎么用 | [使用与开发指南](docs/DEVELOPING.md) |
| 契约、职责边界、安全模型 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 提交代码的规矩 | [CONTRIBUTING.md](CONTRIBUTING.md) |
| 当前阶段与下一步 | [v1.0 计划](docs/plans/V1_0_DEVELOPMENT_PLAN.md) |
| 后续方向（不承诺排期） | [后续规划](docs/planning/FUTURE_WORK.md) |
| 源码教程 | [教程目录](docs/lessons/README.md)（旧行号需对照当前代码） |
| 历史交付记录 | [交付日志](docs/archive/delivery-log.md) |

> 文档以中文为主。代码注释也是中文——这不影响阅读代码，但确实抬高了外部贡献的门槛，欢迎 PR 逐步改进。

## 参与贡献

先读 [CONTRIBUTING.md](CONTRIBUTING.md)。提 issue 时请附上：

- 你做了什么、期望什么、实际发生了什么
- `chaiai.exe --diagnose` 的输出（它不读密钥，只报配置、产物与数据库状态）
- Windows 版本与程序版本

## 许可

[MIT](LICENSE)
