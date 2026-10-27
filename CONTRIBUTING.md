# 贡献说明

## 文档基线

先读 [项目总览](../README.md) 确认当前任务；历史证据和后续候选不自动授权新实现。

动手前先读仓库上级目录的 [ARCHITECTURE.md](../ARCHITECTURE.md) 与
[V0_4_DEVELOPMENT_PLAN.md](../docs/plans/V0_4_DEVELOPMENT_PLAN.md)。架构文件定义契约（State、Graph、工具签名、
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

## 规模与复杂度棘轮

后端 `local-agent-web/src/local_agent/**/*.py` 纳入检查；测试、维护脚本、前端与构建产物不在本轮范围内。
在 `local-agent-web/` 下运行：

```powershell
python scripts/check_size.py --check
```

普通 `python -m pytest` 也会检查同一份源码和基线。默认门槛：文件 2000 行、函数 300 行、
圈复杂度 20、控制块嵌套 6 层。行数按物理行计算，包含注释和空行；函数从首个装饰器开始计数。
圈复杂度沿用 [Ruff C901](https://docs.astral.sh/ruff/rules/complex-structure/) 口径，包括嵌套命名函数的控制流；
布尔表达式、推导式和条件表达式不另计决策点。嵌套按 `if`、循环、`with`、`try`、`match` 的
控制块计算，同级 `elif` 不增加深度，嵌套函数和类独立计算。

`scripts/code_size_baseline.json` 只保存当前超标项。未超标的代码可在门槛内增长，
已有超标项只能持平或改善；不同指标之间不能相互抵扣。文件按本次提交的最终行数比较，
同一文件删去的行自然可以抵消新增行。

改善、删除或降至门槛内后，检查会提示收紧基线；审查源码改动后执行：

```powershell
python scripts/check_size.py --update-baseline
```

该命令只收紧或删除已有豁免，拒绝接受新增/恶化超标，失败时不改基线。
默认检查也不自动写入，避免保留过时余量后让规模重新增长。`--init` 仅供首次建档，拒绝覆盖已有基线。
函数标识使用相对路径与限定名，不依赖行号；同一限定名的重复定义按源码顺序追加 `#2` 等后缀。
移动或重命名仍超标的函数时，需要审查改动并迁移原有基线记录，不得放大指标；检查器不会自动新建豁免。
源码不可读、语法错误、基线损坏、源码目录内出现链接或路径重定向都会使检查失败。

## 提交约定

- 一个任务一个提交，使用 Conventional Commits（如 `feat(web): ...`）。
- 提交信息附上本次执行的测试命令与结果。
- 不提交只有空文件或 `pass` / `NotImplementedError` 的任务。
