# 大文件读取与检索续页验收（2026-10-18）

本轮处理显式读取、grep/glob 的预算和续查；不调整记忆压缩算法、自动 RAG、写入授权或依赖。
正式后端已刷新，健康检查 200，设置版本仍为 5；任务仍为 65 completed / 2 cancelled，无活动任务。

## 根因与结果

原 `read_file_content` 虽然计算完整文件 Hash，正文却只保留 `workspace.max_file_size_bytes` 前缀
（默认 2,000,000 字节），因此靠后行号无法读取；超长单行的 `next_start_line` 还会指回同一行。
原 grep 跳过大于 1,000,000 字节的文件，单行只匹配前 4,000 字符，且没有续查位置；
max_matches 被钳制到 500、glob max_files 被钳制到 2,000。

现在读取流式解码，内容与写入大小预算分离。结果带全文件解码后的 0 基 `start_char` / `next_start_char`；
续读可传 `expected_sha256` 检查完整文件版本，不能同时传字符位置和行区间。支持显式文本编码，默认 UTF-8 BOM 兼容。
保留完整 Hash 和真实总行数，因此每次读取仍需遍历文件，不声称是随机访问索引。

grep/glob 返回 `has_more` / `next_cursor`，静态目录下可逐页推进；游标绑定查询与检索根，并检查续查文件的大小和修改时间。
不创建持久查询会话，不赋予新路径权限。目录发生增删/重排时应重新检索；不是文件系统快照，元数据检查不等同于搜索文件的完整 Hash。

| 参数 | 现在的语义 |
| --- | --- |
| read_file.max_chars | 默认 20000，可增加，限制本页正文 |
| grep.max_matches | 默认 100，可增加，不再钳制到 500 |
| grep.max_scan_files | 默认 2000，可增加或 null |
| grep.max_file_bytes | 默认 null，不因文件大而跳过；也可显式设置预算 |
| grep.max_line_chars | 默认 4000，省略行明确计数；可增加或 null 后重新搜索 |
| grep/glob.timeout_seconds | 默认 20，可增加或 null；文件/行之间检查 |
| glob.max_files | 默认 500，可增加，不再钳制到 2000 |

跳过的二进制、编码不符、不可读和超出显式大小预算的文件按原因计数。
有省略时不会把结果称为完整“没有匹配”。各页匹配数及每文件计数是本页计数；需要总数时累计各页。
敏感路径、忽略目录/依赖目录和链接边界沿用原策略。显式单文件仍可按原规则定位被忽略的普通文件。
只读档可分页，save_full 仍须写权限且保留原落盘配额。

模型视图沿用已有行号投影，并保留字符位置提示；如果压力裁剪隐藏了本页正文，则清空该页字符续页指针，
提示从 start_char 缩小页面重读，避免跳过未看到的片段。这只是新增续读契约的适配，没有改压缩策略。

## 文件清单

| 路径 | 本轮改动 |
| --- | --- |
| `src/local_agent/security/file_system.py` | 新增经统一路径检查、支持取消的分块读取 |
| `src/local_agent/tools/text_stream.py` | 增量解码、跨块 CRLF、有限单行前缀 |
| `src/local_agent/tools/file_tools.py` | 取消正文前缀大小限制，新增字符续读、显式编码及版本检查 |
| `src/local_agent/tools/search_cursor.py` | 查询绑定、位置编码和续查文件元数据校验 |
| `src/local_agent/tools/search_tools.py` | 流式检索、预算参数、分页及跳过说明 |
| `src/local_agent/tools/result_types.py` | 新增续读/续查坐标与覆盖信息 |
| `src/local_agent/memory/tool_output.py` | 模型投影保留字符位置，裁剪后不提供跨过缺失正文的指针 |
| `tests/test_large_file_search.py` | 39 项新增 I/O、分页、版本、取消和边界用例 |
| `tests/test_tools.py` | 更新本轮明确改变的读大小预算及参数契约 |
| `tests/test_search_spill.py`、`tests/test_search_fairness.py` | 从补丁常量改为使用公开扫描预算参数 |
| 上级 `ARCHITECTURE.md`、`V1_0_DEVELOPMENT_PLAN.md` | 实现前更新参数与结果契约、验收范围 |
| 根目录及本项目 `README.md` | 增加使用入口与本记录链接 |

## 验证

最终直接相关回归 **260 passed / 1 skipped**，跳过 Windows 上不适用的 POSIX 权限位测试。
新增 39 项全部通过，包括 2.2 MB 文件尾部、UTF-8 跨字节块、UTF-16/32/GBK、CRLF 边界、
长单行字符拼接、601 条命中、2003 个文件、三种输出模式跨页完整性、扫描预算推进、无效/过期游标、
只读不落盘、跳过原因、取消和模型投影。

在本项目目录执行：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_large_file_search.py tests/test_tools.py tests/test_search_spill.py tests/test_search_fairness.py tests/test_search_ignore.py tests/test_file_system_sandbox.py tests/test_tool_output_projection.py tests/test_tool_parallel.py -q -p no:cacheprovider
.venv/Scripts/python.exe -m ruff check src tests/test_large_file_search.py tests/test_tools.py tests/test_search_spill.py tests/test_search_fairness.py
```

需要可写的 pytest 临时目录；本机原生用例在外层沙箱之外、项目内隔离目录运行。
本轮 7 个源码文件定向 mypy 通过；src 与相关测试 Ruff 通过。日志：
`eval/large-io-focused-final.txt`、`eval/large-io-new-tests.txt`（最初 37 项）、`eval/large-io-mypy.txt`。

真实模型通过公开消息 API 使用隔离的设置副本、聊天库和工作区，无补救用户消息：

- grep 每页两条，按两个游标查完三页；6 条记录 value 总和 232。
- 实际 read_file 定点读出约 2.5 MB 日志第 180001 行 END_VALUE=17。
- 先收到默认 4000 字符单行预算的省略反馈，再自行传 max_line_chars=null，找到第 90000 字符后的 LONG_MARKER。
- 第二页 read_file 传 start_char=16 与上页完整 sha256；写出 result.json 并重读核验。
- 最终 completed，12 次模型调用，33.12 秒。证据 `eval/large-io-agent-1792319023310020200/result.json` 和 `snapshot.json`。

验收脚本 `eval/verify_agent_large_io.py` 最初把“指定 single.txt”限定为 path 参数，误拒了模型等效的
`path=".", glob="single.txt"`；已纠正断言，并对同一份真实调用记录核验通过，没有重造模型结果。
正式服务验收：`eval/large-io-formal-smoke.json`。

用户可在网页输入：请用 grep 每页 2 条统计一组文件的所有命中，按游标续查；再读取大日志的末行；
对长单行先查看省略反馈，按需扩大预算。无需通过 save_full 才能取回后续结果。

## 未通过的项目级检查与边界

- 扩展回归有 **6 项已有失败**：外部 edit_file 的 3 项用例在进入目录拒绝前先缺少 expected_sha256；
  压力压缩的 3 项用例仍出现固定工具定义挤占 20000-token 小窗口预算/缺少摘要。
  独立进程恢复本轮前 read_file/grep/glob 的参数及说明、旧结果字段及投影说明后，相同 6 项仍失败，
  其余 32 项通过。对照脚本和结果为 `eval/check_large_io_baseline.py`、`eval/large-io-baseline.txt`。
  本轮没有修改外部编辑校验或压缩算法。
- 项目全库入口 `pytest -q --maxfail=20` 跑到 **258 passed / 20 failed / 2 skipped** 后停止（348 秒），
  未完成全库，也未全绿。失败为旧工程评测对 task_control 的工具轨迹/完成状态断言，以及旧能力清单/顺序断言。
  详情 `eval/large-io-suite.txt`；本轮未修改这些生命周期/能力清单实现或旧用例。
- 全量 `mypy src` 仍有 3 条原有诊断，位于 `application/projection.py:1544` 和 `context/prompt_context.py:248`。
- `ruff check .` 扫到 3 处已有 `.pytest-*` 临时目录中的测试生成 Python 文件，报 1838 项；
  诊断位置均在这些临时目录，未清理现场。日志 `eval/large-io-ruff.txt`。
- 文件/行间时间检查不抢占一次 Python 正则；单行预算设 null 时不声称具有病态正则硬超时隔离。
  超大单行的完整匹配与显式 save_full 收集仍有内存成本。未测数十 GB 文件或并发恶意宿主改写路径。
- 自动 RAG、list_dir 深度及记忆策略均未在本轮扩大；本记录不代表完整版本发布验收。
