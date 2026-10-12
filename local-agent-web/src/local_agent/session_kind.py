"""会话来源（session kind）的闭集（架构 §10.6、§17.1，v0.6 T57）。

**这是叶子模块**：只放常量，不导入任何项目内模块。理由与 :mod:`local_agent.capabilities` 相同——
``persistence.session_store``（建列与写入校验）与 ``graph.nodes``（记忆候选的来源判定）都要读同一份
定义；把定义放在任一侧都会引入一条新的依赖边（``graph → persistence`` 或 ``persistence → memory``），
而叶子模块谁都能导入、也不会成环。

它回答"**这个会话从哪来**"，与 ``mode``（"这一轮怎么跑"，``chat`` / ``agent``）是两件事：
``mode`` 决定工具集与提示段落，``kind`` 只决定记忆候选的**来源级别**（架构 §17.1 的三步优先级链）。
"""

from __future__ import annotations

from typing import Literal

#: 用户从页面上创建的会话——唯一会产生**可自动注入**记忆的来源。
SESSION_KIND_INTERACTIVE = "interactive"
#: 定时任务触发的会话（v0.6 起由后台任务产生）。
SESSION_KIND_SCHEDULED = "scheduled"
#: 心跳 / 周期巡检触发的会话（v0.6 起由后台任务产生）。
SESSION_KIND_HEARTBEAT = "heartbeat"
SESSION_KIND_SUBAGENT = "subagent"

#: 会话来源闭集（顺序固定；写入校验、迁移的 ``CHECK`` 与测试共用同一份）。
#:
#: T67 的隔离子图是真实 subagent 来源生产者；聊天库 v9 同步 CHECK。
SESSION_KINDS: tuple[str, ...] = (
    SESSION_KIND_INTERACTIVE,
    SESSION_KIND_SCHEDULED,
    SESSION_KIND_HEARTBEAT,
    SESSION_KIND_SUBAGENT,
)

#: 缺省来源：历史行（迁移的列默认值）与用户在页面上新建的会话都是它。
DEFAULT_SESSION_KIND = SESSION_KIND_INTERACTIVE

#: 会话来源的**类型别名**（与 :data:`SESSION_KINDS` 同源）。
#: 闭集只在这一个模块里写死，`AgentState.session_kind` 直接用它；两者一致由测试守住
#: （写法与 :data:`local_agent.capabilities.CapabilityReason` 相同）。
SessionKind = Literal["interactive", "scheduled", "heartbeat", "subagent"]
