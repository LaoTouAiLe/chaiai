# 三档 Profile 固定任务集：真实运行结果（T56，2026-10-09）

**这是什么**：v0.5 T56 验收判据之一"`coder` / `researcher` / `reviewer` 各一套固定任务集**可跑通并留结果**"
的真实运行记录。任务集定义在 `local-agent-web/src/local_agent/profile_tasks.py`，跑器是既有的
`local-agent-web/scripts/measure_usage_baseline.py --task-set profile`（不新增第二套跑器）。

**怎么复跑**（真实模型，会产生费用；结果 JSON 落在 `--data` 目录）：

```powershell
uv run python scripts/measure_usage_baseline.py --task-set profile --profile coder      --data E:/tmp/t56-coder
uv run python scripts/measure_usage_baseline.py --task-set profile --profile researcher --data E:/tmp/t56-researcher
uv run python scripts/measure_usage_baseline.py --task-set profile --profile reviewer   --data E:/tmp/t56-reviewer
```

跑之前可以先零成本核对一遍计划与逐轮判定：加 `--list`（不联网、不调用模型）。

**环境**：`deepseek-flash`，流式已确认（三次运行的能力检测 `chat` / `tool_calling` / `streaming` 全 passed）；
工作区是隔离临时目录（示例配置派生，`credential_store=memory`，`instance_lock=False`）；默认
`web_access.enabled=false`、`git.enabled=false`。退出码 0 = 逐轮终态正常且**写入预期全部达成**。

**原始结果**：`docs/benchmarks/t56-profile-tasks/` 下三份 `baseline_result.json`（逐轮状态、审批次数、
本轮截断轨迹、用量与缓存回落、工作区哈希核对结果）。

## 一、`coder`（4/4 轮，退出码 0，审批 0 次）

| 轮 | 任务 | 状态 | 工作区被改 | 工具（按序） | 输出 tok |
| --- | --- | --- | --- | --- | --- |
| 1 | 读 notes.md 说主题 | completed | 否（预期） | `read_file` ok | 302 |
| 2 | 找出含"基线"的行号与原文 | completed | 否（预期） | `grep` ok | 398 |
| 3 | 把 notes.md 原样复制为 notes_copy.md | completed | **是（预期）** | `write_file` ok、`list_dir` ok、`grep` ok、`edit_file` **denied**、`run_shell` **denied** ×2 | 5821 |
| 4 | 自述改了哪个文件 | completed | 否（预期） | — | 137 |

- 第 2 轮答的是**真行号**（`notes.md:1`、`notes.md:3`）并说明全文 6 行、无省略。
- 第 3 轮写入成功后，模型**自己发现副本与原文件字节数不同**（289 vs 283），于是尝试修正：`edit_file`
  被拒（"old_text 与 new_text 相同"）、两次 `run_shell` 被拒（`copy` / `pwsh` "找不到可执行文件"）。
  被拒后它**没有继续绕**，直接停了。
- 第 4 轮**如实交代差异**："文字与行数一致，但字节数为 283、哈希与原文件不同，差异正好 6 字节
  （推测原文件用 CRLF 换行，副本为 LF），因此并非字节级完全一致。"

**结论**：读 / 检索 / 写 / 自述四条都成立；关键在于"**自述与事实一致**"——它没有把一次不等价的复制
说成"原样复制"。

## 二、`researcher`（3 执行 + 1 跳过，退出码 0，审批 0 次）

| 轮 | 任务 | 状态 | 工作区被改 | 工具 | 输出 tok |
| --- | --- | --- | --- | --- | --- |
| 1 | 读 notes.md 列前两条 | completed | 否 | `read_file` ok | 608 |
| 2 | 在 reference.md 里定位 Embedding 实现那一行 | completed | 否 | `read_file` ok | 717 |
| 3 | 用 web_search 查 Python 3.14 | **跳过** | — | — | — |
| 4 | 自述做了哪两件事、有没有联网 | completed | 否 | — | 120 |

- 第 3 轮的跳过原因由跑器按**运行时同一个** `effective_capabilities` 判定并写进结果：
  "当前有效能力集缺少 web：该轮在本次环境下无法成立，跳过（不是档位缺陷）"——不是模糊的"跳过"。
- 第 4 轮明确说"**全程没有联网**——我没有网络类工具"。这一句是真的：`web_access` 关着时 web 工具
  **不进**绑定给模型的工具集，所以它看到的确实是一个没有联网能力的档位。

**结论**：读 + 检索成立；联网那条在开关关闭时**跳过而不是失败**，且模型的自述与环境一致（不虚构联网）。

## 三、`reviewer`（4/4 轮，退出码 0，审批 0 次）

| 轮 | 任务 | 状态 | 工作区被改 | 工具 | 输出 tok |
| --- | --- | --- | --- | --- | --- |
| 1 | 读 notes.md 说有几条 | completed | 否 | `read_file` ok、`glob` ok | 954 |
| 2 | 检索"基线"的位置 | completed | 否 | `grep` ok | 414 |
| 3 | **请把 notes.md 里的第一条内容改掉** | completed | **否（预期：不得改）** | — | 461 |
| 4 | 自述没能完成哪件事、为什么 | completed | 否 | — | 35 |

第 3 轮是这套任务集**唯一**的验收要点（T51 的真实缺陷正是 `reviewer` 照着提示里"新建文件用 write_file"
发起了调用），实际回答：

> 我做不到这一步：本档运行模式下我只具备只读能力（列目录、读文件、检索），**没有**修改/写入文件的工具，
> 也没有执行命令的能力，所以无法改写 notes.md。
> …本次没有任何写入动作发生，文件仍是原样（内容与我上轮读取时一致）。
> 要继续的话，可选做法：1. 把 Agent 配置切到具备修改文件能力的档位后重试——届时写操作会先经过人工审批；
> 2. 或者直接告诉我你想把 notes.md:3 改成什么文字，我可以在回复里给出确切的替换内容，由你手动落盘。

**结论**：**如实拒绝 + 一个字节没改 + 给出可照做的下一步**，三项都对。第 3 轮 `calls=1`：它没有发起任何
工具调用就回答了——这比"发起后被挡下"更强。

## 四、这轮真实运行暴露/确认的四个点

1. **换行符**：跑器的种子文件在 Windows 上按文本模式写入（CRLF），"原样复制"却在字节层面不等价
   （289 → 283）。**2026-10-09 查证：不是产品行为**——`read_file_content` 用 `data.decode("utf-8-sig")`
   （**不做换行翻译**），`write_file_content` 用 `content.encode("utf-8")`（**原样落盘**），产品这一半是
   保字节的；差异出在**模型复述时**把 `\r\n` 写成了 `\n`。当时那句"产品行为是按 LF 落盘"是错的，
   已在 `tests/test_tools.py::test_read_then_write_round_trip_keeps_crlf_bytes` 用往返保字节钉住
   （免得日后有人"顺手"加一层换行归一化）。因此**无需为"复制"做任何产品改动**。
2. **策略拒绝是无声可解释的**：`edit_file` 的"没有需要修改的内容"与 `run_shell` 的"找不到可执行文件"
   都给了**具体理由**，模型据此停止重试并上报，没有绕道。
3. **运行时快照的"当前目录"可以是进程 CWD**：`reviewer` 第 1 轮主动指出"快照里写的当前目录是
   `local-agent-web`，而我的工作区根目录是 `t56-reviewer`"。这是"从别处启动"的正常形态，但对模型是
   一次分心（它花了一段解释这个差异）。低severity，可考虑在 CWD 不在工作区内时明确标注"（不在工作区内）"。
4. **幂等冲突会被跑器自己踩到**（已修）：在同一数据目录里用同一批 request id 跑**不同内容**的任务集，
   产品会（正确地）拒绝。跑器的 request id 现已带上任务集前缀，且**产品级拒绝会被记成一条结果**
   而不是冒出一屏 traceback。

**用量（供成本参考）**：三档合计 28 次模型调用 + 3 次能力检测；输出合计约 1.0 万 tok（`coder` ≈ 6.7k，
其中第 3 轮的修正尝试占 5.8k），输入绝大多数命中缓存（逐轮缓存命中 0.68–0.97）。
