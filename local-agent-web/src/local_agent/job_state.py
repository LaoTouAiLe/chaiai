"""任务状态的闭集与类型别名（架构 §17.1、§17.5、§17.6）。

**这是叶子模块**：不导入任何项目内模块。放这里的理由与 :mod:`local_agent.session_kind` 相同——
闭集有三个消费者分处不同层：``persistence.job_store``（写侧校验、行解析）、``application.jobs``
（迁移表与事件映射）、``application.models``（任务中心的 DTO）。

而**契约模块不得依赖持久化层**：``tests/test_events.py::test_pure_contract_modules_do_not_import_runtime_layers``
明令 ``application/models.py`` 不准导入 ``local_agent.persistence``（那一层会拉进 sqlite 连接与运行期
依赖，契约模块要能被单独引用与生成 Schema）。定义落在任意一侧都会让另一侧多出一条它不该有的依赖边，
所以单独放一个两者都能引用的叶子模块。

``persistence.job_store`` 仍然把下面这些名字**再导出**一次（它的 ``__all__`` 里列着），因此既有
``from local_agent.persistence.job_store import JOB_STATES`` 之类的引用不需要改。
"""

from __future__ import annotations

from typing import Literal

JOB_STATE_QUEUED = "queued"
JOB_STATE_RUNNING = "running"
JOB_STATE_WAITING_USER = "waiting_user"
JOB_STATE_COMPLETED = "completed"
JOB_STATE_FAILED = "failed"
JOB_STATE_CANCELLED = "cancelled"
#: 重启后"无法证明终态"的任务（架构 §17.5，T61 加）：**可能已部分执行**、可能仍有残留进程，
#: 不自动重放，等人工核对。它是终态——否则该会话永远开不出新任务。
JOB_STATE_NEEDS_REVIEW = "needs_review"

#: 任务状态闭集（架构 §17.1，T57 冻结；T61 按"先改契约"追加 ``needs_review``）。顺序固定：
#: 页面与测试都按它对照。
JOB_STATES: tuple[str, ...] = (
    JOB_STATE_QUEUED,
    JOB_STATE_RUNNING,
    JOB_STATE_WAITING_USER,
    JOB_STATE_COMPLETED,
    JOB_STATE_FAILED,
    JOB_STATE_CANCELLED,
    JOB_STATE_NEEDS_REVIEW,
)

#: 终态：**一经写入不可改写**（同 ``graph.state.merge_turn_records`` 的口径）。
TERMINAL_JOB_STATES: frozenset[str] = frozenset(
    {
        JOB_STATE_COMPLETED,
        JOB_STATE_FAILED,
        JOB_STATE_CANCELLED,
        JOB_STATE_NEEDS_REVIEW,
    }
)

#: 任务状态的**类型别名**（与 :data:`JOB_STATES` 同源；写法同 :data:`local_agent.session_kind.SessionKind`）。
#: 任务索引的 ``Job.state`` 与任务中心的 DTO 都用它，因此"索引能写出的状态"与"契约允许页面看到的状态"
#: 是同一份闭集；两者一致由测试守住（``get_args(JobState) == JOB_STATES``）。
JobState = Literal[
    "queued",
    "running",
    "waiting_user",
    "completed",
    "failed",
    "cancelled",
    "needs_review",
]
