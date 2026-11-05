# 对话正文与执行过程呈现验收（2026-11-04）

## 范围与参照

本轮经用户确认，补齐代码正文、思考/工具过程与最终回答的展示分层。参照本机 DSH 的 `CodeBlock.tsx`、`AssistantMarkdown.tsx`、`ReasoningRow.tsx`、`TurnProcessNodeView.tsx` 与 `ToolCallTree.tsx`；其 MIT 许可保留在 `docs/licenses/deepseek-harness-MIT.txt`。

没有重建 exe，没有修改模型提示词、供应商协议、权限决策、工具执行或模型历史。已有的读文件、搜索、命令、diff、委派卡片继续复用，本轮不宣称重写了所有工具卡。

## 用户可见变化

- 代码围栏独立为圆角代码面板：语言、可选文件名、真实语法高亮、明暗主题、行号、自动换行、复制。
- 点击行号标记一行，Shift 点击标记连续行；可复制标记行或清除。标准围栏元信息 `title="src/app.ts" {2,4-6}` 可以显示文件入口和重点行。
- 代码尚未闭合时也立即显示原文；围栏闭合后结束该块的生成状态，后续正文可以继续流式出现。复制保留代码源文的末尾换行，不带行号或工具栏文本；复制失败如实提示。
- 工具前后的中间叙述留在过程区；最后的回答单独呈现。最终消息中的思考在显示层进入过程区，不再重复放在最终正文前。
- 成功收尾后，过程可折叠；执行中、工具失败、记录不确定或存在需关注信息时保持可见。展开工具结果后不会因自动折叠丢掉已打开的内容。
- 思考预览使用最近完整段落的首行，减少尾部逐字变化；正文保留 Markdown 标题、列表、引用、表格等原有能力。
- 行内文件名、围栏文件名和已知文件工具的“打开文件”入口复用右栏预览。打开文件与展开工具详情是独立操作，不嵌套按钮。

## 后端与兼容

新增可空的 `presentation_phase` 展示提示：`process` / `response`。完整消息有业务工具调用时标为过程；`task_control` 是隐藏收尾信号，不能因此把最终正文标为过程。尚未确定的在途消息保持空值。

字段只添加到公开展示 DTO、完成事件和对应 OpenAPI/生成类型。Checkpoint、真实消息、工具结果与执行语义不变。前端对缺少该字段的历史快照沿用按消息位置判断的兼容路径。

流式尾部仍按稳定消息 ID、块序号、快照对账；重连先取快照，重复事件不重复拼接。本轮没有新增任意思考/正文交错的内容块协议。

## 变更文件

相对仓库根目录：

| 文件 | 用途 |
| --- | --- |
| `local-agent-web/frontend/src/features/conversation/AnswerCodeBlock.tsx`、`.module.css`、`.test.tsx` | 代码块与操作、高亮及专项测试（新增） |
| `local-agent-web/frontend/src/features/conversation/MarkdownMessage.tsx`、`.module.css`、`.test.tsx` | 围栏元信息、流式状态、行内文件入口与兼容测试 |
| `local-agent-web/frontend/src/features/conversation/responsePresentation.ts`、`.test.ts` | 过程/回答纯展示投影及历史兼容（新增） |
| `local-agent-web/frontend/src/features/conversation/Conversation.tsx`、`.module.css`、`.test.tsx` | 过程区、最终回答区、可见性与时间线测试 |
| `local-agent-web/frontend/src/features/conversation/ReasoningRow.tsx`、`.test.tsx` | 稳定思考预览与流式正文 |
| `local-agent-web/frontend/src/features/conversation/adapters.ts`、`streamTail.ts` | 展示字段映射；完成对账参数仅要求实际使用字段 |
| `local-agent-web/frontend/src/features/tool/ToolRow.tsx`、`.module.css`、`ToolCards.test.tsx` | 工具入口、详情关联、文件操作隔离测试 |
| `local-agent-web/frontend/src/fixtures/types.ts` | 可选展示阶段 |
| `local-agent-web/frontend/e2e/dsh-workbench.spec.ts` | 明暗主题、代码操作、受控真实 SSE 帧、断线/刷新/失败场景 |
| `local-agent-web/frontend/e2e/conversation-streaming.spec.ts` | 实际后端命令测试适配完成后过程折叠 |
| `local-agent-web/src/local_agent/application/models.py`、`events.py`、`projection.py`、`service.py` | 派生并传递可选阶段 |
| `local-agent-web/tests/test_projection.py` | 阶段、隐藏收尾信号、旧载荷与不修改模型历史 |
| `docs/api/openapi.json`、`local-agent-web/frontend/src/api/schema.gen.ts` | 重新导出/生成的接口契约 |
| `ARCHITECTURE.md` | 展示口径与限制 |

工作树中此前已有的字号/间距、Composer、Sidebar、tokens 和其他审查记录继续保留，未纳入本轮功能改造。

## 验证记录

- 前端全套：76 个文件、754 项测试通过（最终轮）；改动文件 ESLint 通过。
- 浏览器四种视口（1440/1280/768/390）：52 项通过，包含 8 项新增代码/流式场景。明暗主题与手机截图已逐张查看。
- `npm run typecheck`、改动文件 ESLint、`npm run check:api`、后端 `scripts/export_openapi.py --check` 已通过。
- 后端定向 `test_projection.py`、`test_events.py`、`test_streaming.py`、`test_tool_stream.py`：145 passed，1 deselected。排除项是下面已确认的原有失败；未排除的首次执行为 145 passed / 1 failed。
- 实际后端假模型浏览器：停止、取消终态、立即下一问、增量与刷新去重的 2 项通过；命令逐行输出、生成中刷新和完成后展开结果的 1 项在修正测试展开步骤后复跑通过。三种场景均已验证，不是真实付费端点。
- 初次在沙箱内运行命令场景时，Windows `CreateRestrictedToken` 返回 87，命令退出 127；在普通 Windows 测试进程下可实际执行，不修改产品安全策略规避该问题。

完整后端套件并非全绿：`tests/test_events.py::test_pure_contract_modules_do_not_import_runtime_layers` 检测到 `application/models.py` 原有的 `from local_agent.security.policy import sandbox_boundary_note`。已用 `git show HEAD:...` 确认导入在本轮之前存在；仅报告，没有跨范围修改。

源码服务已在确认 50 个历史任务均完成、无等待任务后重启。`8731/api/health` 返回 `ok`，运行中的 OpenAPI 已包含 `presentation_phase`，历史任务仍为 50 个，继续使用 `E:\chaiai-data`。前端生产构建已更新，不需要重建 exe。

可复跑命令（分别在前端目录与 `local-agent-web` 目录执行）：

```powershell
npm test
npm run typecheck
npm run check:api
npx playwright test -c playwright.dsh.config.ts
npx playwright test -c playwright.streaming.config.ts
```

```powershell
.venv\Scripts\python.exe scripts/export_openapi.py --check
.venv\Scripts\python.exe -m pytest tests/test_projection.py tests/test_events.py tests/test_streaming.py tests/test_tool_stream.py -k "not pure_contract_modules_do_not_import_runtime_layers" --basetemp .playwright-cli/pytest-response-recheck -p no:cacheprovider -q
```

命令流浏览器测试需要能正常创建 Windows 受限令牌的运行环境；继承了外部 `SSLKEYLOGFILE` 的测试进程应先清除此进程环境变量，避免 TLS 库写入沙箱外路径。

## 可手动复核

1. 刷新 `http://127.0.0.1:8731/`，打开一条含代码的历史对话，或让模型生成一段 TypeScript/Python 代码。
2. 开启行号与换行，点击一行，再 Shift 点击另一行，复制标记行；切换明暗主题检查源码颜色。
3. 提交需要读文件再回答的任务，观察运行中过程保持可见，完成后答案保留、过程可展开。
4. 生成中刷新页面，确认已有正文和工具记录恢复，最终回答只有一份；停止一次慢回复，再发送下一问。
5. 对比失败的工具记录，确认仍可查看；点击文件入口只打开右栏，不触发工具再次执行。

## 限制

- 未用真实付费模型端点逐家验收，也未验证打包后的 WebView2；exe 没有重建。
- 超过 60,000 字符的代码停止高亮并保留完整原文；未知语言或高亮失败同样回退纯文本。代码行没有虚拟化，极大代码块仍有 DOM 成本。
- 构建成功但保留现有大资源块提示：文件预览组件仍静态导入 Shiki，因此代码块的动态导入不能把整个 Shiki 单独延迟下载；本轮延迟的是进入视口后的高亮计算。
- 行标记是临时显示状态，不是持久批注；文件入口打开文件，不跳到具体行。
- 只展示供应商实际提供、经过后端脱敏和限长的思考。没有思考字段时不伪造。
- 原始 HTML 继续不解析；正文图片继续不自动请求。没有改成“模型返回 JSON 才能渲染”。
