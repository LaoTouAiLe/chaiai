"""应用层契约与投影（v0.4）。

依赖方向是单向的：React → HTTP/SSE → `application` → LangGraph / Checkpoint / Session Store。
`models` / `events` / `errors` 纯契约模块**不得**导入 LangChain 消息类、LangGraph、
Streamlit、FastAPI、`local_agent.graph` 或 `local_agent.ui`。T28 的 `projection` / `service`
需要单向调用 Graph 与 Checkpoint，但 Graph 不得反向导入应用层（ARCHITECTURE §15.1）。
`local_agent.application.models` 是 UI/API 的唯一业务契约来源。
"""

from __future__ import annotations

__all__: list[str] = []
