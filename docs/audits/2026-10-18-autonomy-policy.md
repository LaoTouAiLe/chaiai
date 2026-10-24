# 用户裁定的执行限制调整（2026-10-18）

本文记录用户对前一日限制盘点的决定、实际实现与验证。原盘点保留为历史，不再代表当前设置。

## 已实现的裁定

| 编号 | 当前行为 |
| --- | --- |
| E1 | 默认没有主循环决策次数上限；移除框架默认递归次数对长期任务的隐含截断。设置留空表示不限；用户仍可主动填写有限预算。 |
| E2 | 重复失败超过设置阈值时向主模型反馈，不强制停止或转交用户。 |
| E3 | 命令默认没有截止时间；单条调用仍可指定超时，用户取消仍有效。 |
| E6、E9 | 主模型通过 `task_control` 声明继续、完成或需要输入，不增加完成度判官调用；可以和其它工具同批调用。普通进度文字不再自动结束任务。 |
| E7 | 新自主循环不执行机械完成度校验，不因计划状态或证据等级压制模型的收尾决定；实际错误与审计仍记录。 |
| E8、E10 | 计划没有五步或单个进行中步骤限制；可同批更新、撤回已完成步骤、删除或清空计划。 |
| E11–E13 | 新子任务可联网和运行只读命令；不提供文件写入、Git 提交或再次委派能力，Windows 只读令牌阻止通过命令写代码。旧冻结子任务仍按旧协议恢复。 |
| 子任务资源 | 默认最多 8 个任务、并发 4、每个 900 秒。父总额度扩大为 512 步、256 次工具、128 个源文件、各 256000 字符证据/报告、1048576 模型 token；配置不再设原来很小的数值上限。实际模型窗口和输出能力仍由连接决定。 |
| P2–P4、T5 | 工作区编辑、整文件覆盖、删除默认免审批；编辑不强制提供旧 Hash，允许整文件替换，可指定 GBK、UTF-16 等编码。提供 Hash 时仍核对版本，发布时仍检查并发修改。 |
| P5 | `taskkill` 从破坏性分类移除。系统 `taskkill.exe` 的 argv 直调用可操作进程；同名程序、脚本、管道和重定向不适用该例外。 |
| P7 | 专用 Git 工具提交工作区文件无需审批，main/master 不再额外确认；仍尊重仓库 hook 和显式路径范围。 |
| P8–P9 | 外部绝对路径 cwd 免审批；外部写入仍需目录授权，包含命令内部写入。原 danger-full-access 保留存储标识，界面改称“常规免审”，Windows 同样强制写边界。 |
| T6 | 每轮搜索次数、读取地址数、单页读取次数默认不限。 |
| T9 | 取消图片字节、单次张数和每日张数配额，上传路由也取消原请求体大小门槛；仍检查格式、会话归属和模型视觉兼容性。 |
| S1–S8 | 清理外部不可及、不掌握实时信息、白名单优先、避免长命令、过度串行、最小一次补查、最多两轮联网及全局 Git 禁令等旧提示；同步工具描述。 |

其它未获取消的边界保留，包括受保护目录、敏感值、破坏性 Shell 分类、退出沙箱审批、HTTP 来源校验和网页读取地址策略。

## 本次验证发现并修复的问题

- Windows 写授权缺少 DELETE 权限，npm 在可写缓存中仍无法删除临时文件；补齐删除权限，受保护子树保留拒绝项。
- Job Object 在启动器正常退出时杀掉后台服务；可写任务正常返回后释放退出清理标志，异常退出、取消、超时仍清理进程树，只读子任务仍清理后代。
- 受限令牌无法读取部分外部 cwd；按已允许的外部读取补齐只读 ACE，不授予外部写权限。
- 新收尾工具的回答可能形成空消息或失去流式显示；交付文本投影到原消息，保持推理、消息 ID、刷新恢复与取消后的部分回答。
- 执行后未清空待执行调用，可能在审批队列中重复询问已完成动作；执行后清除已处理调用及其审批状态。

## 修改文件

以下路径相对于 `E:/pylianxi/chaiai/local-agent-web`；不包含本次任务开始前已有的其它修改。

| 文件 | 用途 |
| --- | --- |
| `src/local_agent/config.py`、`config.yaml`、`config.yaml.example` | 不限默认值、可选审批和扩大后的子任务资源。 |
| `src/local_agent/model_settings/preferences.py` | 保存与加载 nullable 执行/联网预算。 |
| `src/local_agent/capabilities.py` | 任务控制能力和子任务的联网、只读命令能力。 |
| `src/local_agent/graph/state.py`、`main_loop.py`、`nodes.py`、`plan_updates.py`、`builder.py`、`web_budget.py` | 模型收尾、失败反馈、自由计划、可选预算与执行状态清理。 |
| `src/local_agent/graph/delegated_workflow.py`、`src/local_agent/workflow_contracts.py`、`src/local_agent/tools/delegate_tools.py` | 自主子任务、资源配置、父子审计与旧协议恢复。 |
| `src/local_agent/security/policy.py`、`path_guard.py`、`shell_guard.py`、`process_guard.py`、`win_sandbox.py` | 免审批策略、外部写边界、无默认超时、文件删除权限和进程生命周期。 |
| `src/local_agent/tools/file_tools.py`、`git_tools.py`、`shell_tools.py`、`plan_tools.py`、`skill_tools.py` | 工具执行行为和模型可见说明。 |
| `src/local_agent/context/prompt_context.py` | 清理重复或与现有权限矛盾的提示。 |
| `src/local_agent/media.py`、`attachments.py`、`tools/image_tools.py`、`api/schemas.py`、`api/security.py` | 移除图片配额和上传请求体门槛。 |
| `src/local_agent/ui/decision_streaming.py`、`application/projection.py`、`application/models.py`、`application/service.py`、`api/routes.py` | 流式答案、用户界面投影和接口说明。 |
| `frontend/src/features/settings/PreferenceSection.tsx`、`PermissionSection.tsx` | 不限预算输入和权限边界说明。 |
| `docs/api/openapi.json`、`frontend/src/api/schema.gen.ts` | 重新生成接口契约。 |
| `tests/test_autonomy_policy.py` 及相关原有回归、两个前端设置测试文件 | 验证新规则；旧审批恢复用例显式开启可选审批，避免把旧默认断言当成新产品规则。 |

## 验证与复现

隔离验证材料位于 `eval/autonomy-policy-20261018/`，未纳入 Git。正式设置库先备份到被忽略的 `data/backups/`；未复制正式聊天库，未改模型连接和密钥。

- 真实模型任务：原始 Vite 开发任务，49 次模型调用、70 次工具调用、其中 32 次 Shell；396.19 秒，0 条追问、0 次手工批准，最终 completed。
- 依赖真实下载到工作区，保留锁文件；真实生产构建成功。后台服务在命令返回后保持运行。
- 慢构建实测 75945 毫秒，15 次中文进度，最后真实执行生产构建并写出结果。
- 独立 Edge 浏览器确认 `http://127.0.0.1:5199/` 返回 200，按钮从 0 点击到 1、2；独立核对锁文件、构建结果与产物 Hash，没有修改生成的项目。
- 确定性回归覆盖超过 20 次决策、无工具进度继续执行、失败反馈、明确请求输入、计划撤回/删除、GBK/UTF-16 编辑、图片大请求、外部写入授权、子任务命令写入拒绝、后台启动/停止、真实 main 分支提交和 65 秒无默认超时命令。

最终回归：执行与权限 520 通过、2 跳过；主循环/恢复/流式 167 通过；HTTP/文件/权限接口 242 通过、1 跳过；Windows 原生 143 通过；前端 427 通过。各分组有交叉，不累加计算。Ruff 与九个主要模块的定向 mypy 通过，前端生产构建成功。

常用复验命令（在项目目录执行；原生 Windows 令牌测试需在有相应权限的本机环境运行）：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_autonomy_policy.py -q -p no:cacheprovider --basetemp E:/tmp/chaiai-autonomy-manual-unique
cd frontend
npm test
npm run build
```

正式设置现为版本 5：执行次数、默认命令超时和三项联网次数均为 null。后端已刷新至最终代码，HTTP 实测新前端产物与本地构建 Hash 一致；正式任务数量保持 65 完成、2 取消，无运行中或等待审批任务。真实 Edge 只读检查确认：任务执行页的次数/超时显示“ 不限 ”，权限页已选“常规免审”；截图位于验证目录。界面刷新后可用，新任务按这些设置执行。

## 实证限制

- Windows 受限令牌下 Node 的部分 pipe spawn、WMI、Schannel 调用存在兼容性问题。本次任务依据实际错误自行改用文件重定向等方式完成，没有为此取消外部写边界。
- 非 Windows 尚无本项目的内核写沙箱，不将 Windows 的外部写阻断实测结论推广到其它平台。
- 全项目 mypy 仍报告三条本次范围外的类型诊断（`application/projection.py:1541` 的 object→int 两条、`context/prompt_context.py:247` 的可空时间一条，此处按最终源码核对）；本次九个主要实现模块的定向类型检查通过。未顺手改无关类型代码。
- 构建仍有大包提示；不影响构建结果。本次没有升级依赖或调整分包。
