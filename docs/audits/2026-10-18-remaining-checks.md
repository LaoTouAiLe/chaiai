# 审批回归与类型诊断收口（2026-10-18）

本轮处理上一轮保留的两项审批失败和三条 mypy 诊断。没有改变默认权限、工具能力、Graph、存储或依赖。

## 根因与修改

两项测试验证审批挂起后的快照恢复和审批时钟保留，却依赖旧的“默认删除需审批”。
用户已明确选择工作区删除免审批，当前示例配置也为 `require_approval.delete_file=false`。
测试现在显式设置该项为 true，构造它们要验证的审批场景；产品默认值及安全策略实现未改。

`RuntimeContext.current_time` 允许 None，运行环境渲染却直接调用 astimezone，已复现 AttributeError。
现在缺少冻结时钟时明确显示“当前时间：未记录（本轮未提供冻结时钟）”，不临时读系统时间冒充冻结时刻；
其余环境信息和重复渲染稳定性保留。

轨迹整数转换的输入是反序列化动态字段，原函数用 object 标注却直接传给 int，并使用了不匹配的忽略码，
产生两条类型诊断；无穷数还会抛 OverflowError，已复现。现在把该解析边界标注为 Any，
保留原 int 转换行为，并将 OverflowError 与原有 TypeError/ValueError 一同回退为 None。
合法的 0 继续表示“记录为零”，缺失/不可转换值表示“未记录”，不增加输入限制。

| 文件 | 本轮改动 |
| --- | --- |
| `src/local_agent/context/prompt_context.py` | 缺失冻结时钟时如实回退 |
| `src/local_agent/application/projection.py` | 动态整数解析类型修正，溢出数字返回未知 |
| `tests/test_runtime_snapshot.py` | 审批场景显式开启审批；新增缺失时钟测试 |
| `tests/test_model_usage.py` | 审批时钟场景显式开启审批 |
| `tests/test_projection.py` | 新增 11 项无效数字、零值及数字字符串投影回归 |
| 上级 `ARCHITECTURE.md`、`V1_0_DEVELOPMENT_PLAN.md`、`README.md` | 记录现行测试口径与验收范围 |

## 验证

- 两项原失败审批测试均通过。
- 预算、压力压缩、记忆、运行快照、模型用量、压缩进度、文件投影、大文件、并行及完整 DTO 投影等
  10 个测试文件：**398 passed / 2 skipped**。两项跳过是已安装 Chroma 时不适用的缺依赖负例和
  Windows 不适用的 POSIX 大小写用例。日志 `eval/remaining-checks-regression.txt`。
- 快照、用量、投影、自主策略和权限模型等 5 个测试文件：**194 passed**。
  覆盖默认工作区编辑/覆盖/删除免审批，以及 Windows 沙箱和目录授权；日志 `eval/remaining-checks-focused.txt`。
  两组有重叠，不能相加当作独立用例总数。
- 新增 12 项边界测试均通过；无效数字包括缺失、dict/list、不可解析文本、NaN 和正负无穷。
- `mypy src`：**160 个源码文件全部通过**，三条原诊断已清除。
- `ruff check src tests scripts`：全部通过，日志 `eval/remaining-checks-code-ruff.txt`。

复测，在 `local-agent-web` 执行：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_runtime_snapshot.py tests/test_model_usage.py tests/test_projection.py tests/test_autonomy_policy.py tests/test_permission_model_review.py -q -p no:cacheprovider
.venv/Scripts/python.exe -m mypy src
.venv/Scripts/python.exe -m ruff check src tests scripts
```

本机含原生权限测试的回归在外层沙箱之外运行；记忆项目隔离测试使用仓库之外的新临时目录，避免
两个临时工作区被同一父 Git 项目归并。测试数据库独立，未读取或修改正式会话数据。

## 交付与边界

正式后端确认无活动任务后刷新，健康检查 200；设置版本仍为 5，任务计数仍为 65 completed / 2 cancelled。
检查记录 `eval/remaining-checks-formal-smoke.json`，没有改默认权限或正式设置。

全目录 Ruff 尝试仍扫描到旧 `.pytest-*` 目录中的生成代码，本次原生检查报 10012 项，诊断位置均在这些
测试产物目录；`--extend-exclude '.pytest-*'` 在这次检查中没有排除它们。保留现场，未删除或改写这些文件。
实际源码/测试/脚本检查独立通过，不能将它描述为原始全目录命令通过。

本轮没有重跑整个项目测试全集，之前全库的生命周期/能力清单旧断言尚未在本轮处理；不宣称全库已全绿。
没有前端或接口结构改动，未重复前端构建；没有调用真实模型或重新验收 T43 质量/缓存目标。
