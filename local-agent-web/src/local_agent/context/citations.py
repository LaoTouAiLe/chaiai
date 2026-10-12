"""Complete citation paths using files actually read, without changing line numbers."""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import PurePosixPath, PureWindowsPath

from langchain_core.messages import AnyMessage, ToolMessage

_FILE = r"[A-Za-z0-9_./\\-]+\.(?:py|md|ya?ml(?:\.example)?|tsx?|jsx?|json|toml|ps1|sh|txt)"
_REFERENCE = re.compile(rf"(?<![\w/\\:])(?P<path>{_FILE}):(?P<line>\d+(?:-\d+)?)"
                        r"|(?P<same>同文件\s*)(?P<same_line>\d+(?:-\d+)?)"
                        r"|(?P<continuation>:\d+(?:-\d+)?)"
                        r"|(?<![\w.:\-])(?P<list_line>\d+(?:-\d+)?)(?![\w.:\-]|\s+[A-Za-z])")
_MENTION = re.compile(_FILE)


def complete_citation_paths(text: str, messages: Sequence[AnyMessage]) -> str:
    """Expand only unambiguous current-turn file evidence paths; retain raw line numbers."""
    paths: set[str] = set()
    for message in messages:
        if not isinstance(message, ToolMessage) or message.name not in {"read_file", "grep"}:
            continue
        try:
            value = json.loads(message.content) if isinstance(message.content, str) else None
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        observed: list[str] = []
        if message.name == "read_file":
            if (isinstance(value.get("path"), str) and isinstance(value.get("content"), str)
                    and value["content"]):
                observed.append(value["path"])
        elif isinstance(value.get("matches"), list):
            for item in value["matches"]:
                if (isinstance(item, dict) and isinstance(item.get("path"), str)
                        and isinstance(item.get("line_number"), int) and item["line_number"] > 0
                        and isinstance(item.get("text"), str) and item["text"]):
                    observed.append(item["path"])
        for name in observed:
            path = name.replace("\\", "/")
            if (PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive
                    or ".." in PurePosixPath(path).parts):
                continue
            paths.add(path)

    def resolve(name: str) -> str | None:
        normalized = name.replace("\\", "/")
        if normalized in paths:
            return normalized
        normalized = normalized.removeprefix(".../")
        candidates = [path for path in paths if path.endswith("/" + normalized)]
        return candidates[0] if len(candidates) == 1 else None

    previous_path: str | None = None
    previous_end = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal previous_path, previous_end
        name = match.group("path")
        if name is not None:
            previous_path = resolve(name)
            previous_end = match.end()
            return f"{previous_path}:{match.group('line')}" if previous_path else match.group()
        if match.group("same"):
            mentions = list(_MENTION.finditer(text, 0, match.start()))
            path = resolve(mentions[-1].group()) if mentions else None
            previous_path, previous_end = path, match.end()
            return f"{path}:{match.group('same_line')}" if path else match.group()
        gap = text[previous_end:match.start()]
        if match.group("list_line") is not None:
            if previous_path and re.fullmatch(r"[ \t`]*[,、][ \t`]*", gap):
                previous_end = match.end()
                return f"{previous_path}:{match.group('list_line')}"
            return match.group()
        if previous_path and gap and re.fullmatch(r"(?:[,、\s`]|和)+", gap):
            previous_end = match.end()
            return previous_path + match.group()
        # 仅识别单独反引号引用；在当前段落内取最近明确且已观测的文件。
        # 不跨段，不把时间/比值或未知文件后的数字猜成源码坐标。
        if text[max(0, match.start() - 1):match.start()] == "`":
            break_at = text.rfind("\n\n", 0, match.start())
            paragraph = break_at + 2 if break_at >= 0 else 0
            mentions = list(_MENTION.finditer(text, paragraph, match.start()))
            path = resolve(mentions[-1].group()) if mentions else None
            if path:
                previous_path, previous_end = path, match.end()
                return path + match.group()
        return match.group()

    return _REFERENCE.sub(replace, text)
