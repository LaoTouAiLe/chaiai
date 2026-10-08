# 贡献说明

## 文档基线

先读 [项目总览](../README.md) 确认当前任务；历史证据和后续候选不自动授权新实现。

动手前先读仓库上级目录的 [ARCHITECTURE.md](../ARCHITECTURE.md) 与
[V0_4_DEVELOPMENT_PLAN.md](../V0_4_DEVELOPMENT_PLAN.md)。架构文件定义契约（State、Graph、工具签名、
安全语义），开发文档定义任务顺序与验收方式。两者冲突时先核对架构文档。

修改 State 字段、Graph 边、工具签名或存储字段，必须先改架构文档，再改开发文档与实现，
不允许在代码里悄悄新增字段或配置键。

## 开发环境

```powershell
uv lock --check
uv sync                 # 基础依赖 + dev 组
uv sync --extra memory  # 可选组：中期向量记忆与本地 ONNX Embedding
```


## 质量门槛

每个任务提交前都要通过：

```powershell
python -m pytest
python -m ruff check .
python -m mypy src
```

- 所有公开函数与方法都有类型标注和 docstring。
- 每个新增模块至少覆盖正常、错误和安全边界三类测试。
- 自动测试使用临时工作区、隔离数据库和假模型，不得依赖密钥、网络或模型输出稳定性。
- 不提交 `config.yaml`、`.env`、`data/`；`uv.lock` 必须提交。
- 可选组的处理口径：装了就真跑、没装就**跳过并写明原因**（`uv sync --extra memory` 会跑全部记忆与
  Embedding 用例；只装基础依赖时它们跳过，套件**不因此变红**）。`ruff` 与 `mypy src` 在两种环境下都要干净。
- 真实模型验证（冒烟、演示）属于人工记录，不要塞进默认门槛：默认 `pytest` 不联网、不产生费用。

## 提交约定

- 一个任务一个提交，使用 Conventional Commits（如 `feat(web): ...`）。
- 提交信息附上本次执行的测试命令与结果。
- 不提交只有空文件或 `pass` / `NotImplementedError` 的任务。
