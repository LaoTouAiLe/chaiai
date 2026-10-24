"""把本轮实际来源标识解析为链接；不从任意模型网址创建来源。"""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from local_agent.tools.web_errors import WebAccessError
from local_agent.tools.web_identity import source_id
from local_agent.tools.web_tools import check_url

SOURCE_MARKER = re.compile(r"\[(web-[a-f0-9]{16})\]")


def cited_source(source: str, url: str, answer: str) -> bool:
    answer = re.sub(r"```[\s\S]*?```|`[^`\n]*`", "", answer)
    if f"[{source}]" in answer:
        return True
    # 旧回答仍允许显式 URL 引用，但不接受相似网址或嵌套的跟踪链接。
    for link in re.findall(r"https?://[^\s<>\])\"'`]+", answer):
        try:
            if source_id(link.rstrip("，。；！？")) == source_id(url):
                return True
        except ValueError:
            continue
    return False


def payload_sources(tool_name: str, payload: Mapping[str, Any]) -> list[dict[str, str]]:
    if tool_name == "web_search":
        raw = payload.get("results")
        items = raw if isinstance(raw, list) else []
    elif tool_name == "web_fetch":
        items = [{"url": payload.get("final_url") or payload.get("url"), "title": payload.get("title")}]
    else:
        return []
    sources: list[dict[str, str]] = []
    for item in items[:20]:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        try:
            url = check_url(item["url"], resolve=False)
        except (ValueError, WebAccessError):
            continue
        sources.append({"source_id": source_id(url), "url": url, "title": str(item.get("title") or url)})
    return sources


def resolve_web_citations(state: Mapping[str, Any], answer: str) -> str:
    run = state.get("run_id")
    allowed = {event.tool_call_id for event in state.get("tool_events", [])
               if getattr(event, "run_id", None) == run and getattr(event, "status", None) == "ok"}
    # 节点 State 用 dataclass，直接投影测试也可传映射。
    allowed.update(str(event.get("tool_call_id")) for event in state.get("tool_events", [])
                   if isinstance(event, Mapping) and event.get("run_id") == run and event.get("status") == "ok")
    messages: Sequence[Any] = state.get("messages") or []
    anchor = state.get("run_user_message_id")
    if anchor:
        start = next((index for index, message in enumerate(messages) if message.id == anchor), len(messages))
        messages = messages[start:]
    else:
        start = next((index for index in range(len(messages) - 1, -1, -1)
                      if isinstance(messages[index], HumanMessage)), 0)
        messages = messages[start:]
    sources: dict[str, dict[str, str]] = {}
    calls: dict[str, str] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            calls.update((str(call.get("id")), str(call.get("name"))) for call in message.tool_calls)
        if not isinstance(message, ToolMessage) or message.tool_call_id not in allowed:
            continue
        try:
            payload = json.loads(message.text)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        name = message.name or calls.get(message.tool_call_id, "")
        for item in payload_sources(name, payload):
            sources[item["source_id"]] = item
    numbers: dict[str, int] = {}

    def replace(match: re.Match[str]) -> str:
        identity = match.group(1)
        item = sources.get(identity)
        if item is None:
            return "[来源未核实]"
        number = numbers.setdefault(identity, len(numbers) + 1)
        url = quote(item["url"], safe=":/?&=%#@+;,!$*'~-._")
        return f"[{number}]({url})"

    # 代码块与行内代码中的示例编号不是答案引用。
    pieces = re.split(r"(```[\s\S]*?```|`[^`\n]*`)", answer)
    return "".join(piece if index % 2 else SOURCE_MARKER.sub(replace, piece)
                   for index, piece in enumerate(pieces))
