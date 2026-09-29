"""轮次作用域键（架构 §4.2、§4.3）。

工具调用（以及它的结果）的作用域是 **`run_id` + `tool_call_id`**：同一次运行内重放同一个
调用是同一件事；不同轮次即使复用了同一个 `tool_call_id`，也是两件事——既不能被误判成
"已执行"而跳过，也不能把新结果当成重复项丢掉。

这个模块只放**纯函数**（无 ImportError 风险、不依赖 langgraph/langchain）：Graph 侧
（`state` / `nodes`）与应用边界层（投影、服务）共用同一份定义，避免两处各拼一遍字符串。
"""

from __future__ import annotations

__all__ = ["tool_scope_key"]


def tool_scope_key(run_id: str, tool_call_id: str) -> str:
    """工具调用的作用域键：``run_id`` + ``tool_call_id``（架构 §4.2、§4.3）。

    同一次运行内重放同一个调用 → 同一个键，据此去重；不同轮次即使复用了同一个
    ``tool_call_id``，也是不同的键：既不会被误判成"已执行"而跳过，也不会把新结果
    当成重复项丢掉。

    升级前的 Checkpoint 没有 ``run_id``：调用方按空轮次处理（``""``），因此不会与任何
    真实轮次混在一起。
    """
    return f"{run_id}:{tool_call_id}"
