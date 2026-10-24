"""本轮联网硬预算；从持久化事件重建，恢复任务也不能重置计数。"""

from collections.abc import Mapping, Sequence

from local_agent.config import WebAccessConfig
from local_agent.graph.state import ToolEvent
from local_agent.tools.web_identity import source_id


def _identity(value: object) -> str:
    text = str(value or "")
    try:
        return source_id(text)
    except ValueError:
        # 参数有效性仍由工具报告；预算检查不能让畸形 URL 中断整轮回答。
        return "invalid:" + text


def web_limit_reason(
    name: str, arguments: Mapping[str, object], *, events: Sequence[ToolEvent],
    run_id: str, config: WebAccessConfig,
) -> str | None:
    if name not in {"web_search", "web_fetch"}:
        return None
    current = [event for event in events if event.run_id == run_id]
    if name == "web_search":
        if config.max_search_calls is not None and sum(event.tool_name == name for event in current) >= config.max_search_calls:
            return f"本轮检索预算已用完（最多 {config.max_search_calls} 次）；请用已有资料回答并说明缺口，不要换工具绕过预算。"
        return None
    identity = _identity(arguments.get("url"))
    reads = [_identity(event.redacted_arguments.get("url"))
             for event in current if event.tool_name == name]
    if config.max_reads_per_page is not None and reads.count(identity) >= config.max_reads_per_page:
        return f"本轮该页面的读取/续读预算已用完（最多 {config.max_reads_per_page} 次）；请据已有内容说明未核实部分。"
    if config.max_read_pages is not None and identity not in reads and len(set(reads)) >= config.max_read_pages:
        return f"本轮读取页面预算已用完（最多 {config.max_read_pages} 个地址）；请用已有资料回答并说明缺口。"
    return None
