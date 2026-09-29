"""本机 HTTP/SSE 接口层（T29；契约 ARCHITECTURE §15.1、§15.3，计划 §6.2）。

依赖方向（§15.1，有测试守）：

```text
React → HTTP/SSE → application → LangGraph / Checkpoint / Session Store
```

本层的职责边界：

- **只调用应用服务**：不拼 `AgentState`、不直接 `invoke` 图、不自己造 `Command`；
- 不得导入 `streamlit` 或 `local_agent.ui`（页面宿主），也不得导入 Graph 节点模块；
- 对外的响应体一律是 `application/models.py` 的 DTO——那是 UI/API 唯一的业务契约来源，
  前端不得手写第二套同名枚举（§15.1）；
- 部署拓扑固定**单进程、单 worker**（§15.1）：进程内广播、会话互斥与幂等台账都以此为边界。
"""

from __future__ import annotations

__all__: list[str] = []
