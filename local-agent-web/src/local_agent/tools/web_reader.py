"""不依赖目标域名的文档抽取、JSON 定位和正文选取。"""

import hashlib
import json
import re
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from local_agent.tools.web_errors import WebAccessError

_BLOCKS = {"p", "div", "section", "article", "main", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "pre"}
_SKIP = {"script", "style", "noscript", "nav", "footer", "form", "svg", "template"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "embed", "param"}


class _DocumentParser(HTMLParser):
    def __init__(self, url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.url = url
        self.stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []
        self.title: list[str] = []
        self.links: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        skipped = any(flag for _, flag in self.stack) or tag in _SKIP or attributes.get("aria-hidden") == "true"
        if tag not in _VOID:
            self.stack.append((tag, skipped))
        if skipped:
            return
        if tag in _BLOCKS or tag in {"br", "hr"}:
            self.parts.append("\n")
        if tag in {"td", "th"}:
            self.parts.append(" | ")
        if tag == "a":
            href = urljoin(self.url, attributes.get("href") or "")
            self.links.append((href, len(self.parts)))

    def handle_endtag(self, tag: str) -> None:
        skipped = any(flag for _, flag in self.stack)
        if tag == "a" and self.links:
            href, start = self.links.pop()
            if not skipped and urlsplit(href).scheme in {"http", "https"} and len(self.parts) > start:
                self.parts.append(f" ({href})")
        if tag in _BLOCKS and not skipped:
            self.parts.append("\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if any(flag for _, flag in self.stack):
            return
        if any(tag == "title" for tag, _ in self.stack):
            self.title.append(data)
            return
        if any(tag == "head" for tag, _ in self.stack):
            return
        self.parts.append(data)

    def text(self) -> str:
        return "\n".join(line for part in "".join(self.parts).splitlines()
                         if (line := " ".join(part.split())))


def json_at(value: Any, pointer: str) -> Any:
    """按 RFC 6901 定位实际返回结构；不补字段或推断业务。"""
    if not pointer:
        return value
    if not pointer.startswith("/"):
        raise WebAccessError("json_pointer 必须为空或以 / 开头的 JSON Pointer")
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(value, list) and key.isdecimal():
                value = value[int(key)]
            elif isinstance(value, dict):
                value = value[key]
            else:
                raise KeyError(key)
        except (IndexError, KeyError):
            raise WebAccessError(f"JSON 中不存在该定位：{pointer}") from None
    return value


def _structure(value: Any, path: str = "", depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    if isinstance(value, dict):
        lines = [f"{path or '/'}：对象，字段 {', '.join(str(key) for key in list(value)[:30])}"]
        for key, item in list(value.items())[:12]:
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            lines.extend(_structure(item, f"{path}/{escaped}", depth + 1))
        return lines
    if isinstance(value, list):
        return [f"{path or '/'}：数组，{len(value)} 项"] + (_structure(value[0], path + "/0", depth + 1) if value else [])
    return [f"{path or '/'}：{type(value).__name__}"]


def _json_text(value: Any, depth: int = 0) -> str:
    """JSON 选择只改变结构，不把高精度小数先转成二进制浮点数。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        items = [f'{json.dumps(key, ensure_ascii=False)}: {_json_text(item, depth + 1)}'
                 for key, item in value.items()]
        opening, closing = "{", "}"
    elif isinstance(value, list):
        items = [_json_text(item, depth + 1) for item in value]
        opening, closing = "[", "]"
    else:
        return json.dumps(value, ensure_ascii=False)
    if not items:
        return opening + closing
    indent = "  " * (depth + 1)
    return opening + "\n" + indent + (",\n" + indent).join(items) + "\n" + "  " * depth + closing


#: 正文抽取库（2026-10-28 接入 trafilatura）：**惰性导入**——它拖 lxml、htmldate 等依赖，
#: 启动时并不需要；只有真的抓到 HTML 才值得付这份导入开销。导入失败不算致命：如实回退到
#: 内置的标签剥离器，并在 ``structure`` 位置（这里借用第三个返回值）说明用了回退，
#: 不静默降级——"抽出来的东西比以往差"必须能被看见。
_TRAFILATURA: Any = None
_TRAFILATURA_FAILED = False

#: 抽取库给出的正文至少要有这么多字才算可用：再短基本等于没抽到正文。
MIN_EXTRACTED_CHARS = 400
#: 抽取库正文相对标签剥离器的保留比例下限。低于它说明这次抽取把大量内容判成了模板
#: （2026-10-28 实测：某赛程页 28442 字被抽成 964 字，占 3.4%），此时宁可回退到旧的
#: 全量文本——噪声多但至少数据在，比"干净地丢掉战绩表格"更可用。
MIN_RETENTION_RATIO = 0.25


def _trafilatura_module() -> Any:
    global _TRAFILATURA, _TRAFILATURA_FAILED
    if _TRAFILATURA is not None or _TRAFILATURA_FAILED:
        return _TRAFILATURA
    try:
        import trafilatura
    except ImportError:
        _TRAFILATURA_FAILED = True
        return None
    _TRAFILATURA = trafilatura
    return _TRAFILATURA


def _markdown_from_html(raw: str, url: str) -> str | None:
    """用抽取库把 HTML 转成 Markdown；库缺失或抽不出内容时返回 ``None``。"""
    module = _trafilatura_module()
    if module is None:
        return None
    try:
        extracted = module.extract(
            raw,
            url=url or None,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            include_formatting=True,
            favor_recall=True,
        )
    except Exception:  # noqa: BLE001 - 第三方库的异常类型不向外泄漏，统一按"抽取失败"处理
        return None
    return extracted or None


def extract_document(
    raw: str, content_type: str, url: str, *, json_pointer: str = "", json_fields: list[str] | None = None,
) -> tuple[str | None, str, str | None, str | None]:
    """抽取出 ``(标题, 正文, JSON 结构说明, 抽取说明)``。

    第四个返回值（2026-10-28 新增）只说"这份正文是怎么来的"：抽取库成功时为 ``None``，
    回退到内置解析、或 JSON 结构定位失败时给出原因。它与正文一起进 ``note``，因为
    "正文比以往差"和"正文抽取失败"都必须让模型与用户看得见。
    """
    kind = content_type.split(";", 1)[0].strip().lower()
    is_json = kind == "application/json" or kind.endswith("+json")
    if is_json:
        if not json_pointer and not json_fields:
            try:
                parsed = json.loads(raw, parse_float=Decimal)
            except ValueError:
                return None, raw.strip(), "JSON 无法完整解析；未建立结构定位", None
        else:
            try:
                parsed = json.loads(raw, parse_float=Decimal)
            except ValueError:
                raise WebAccessError("响应不是完整可解析的 JSON，不能按结构定位") from None
        selected = json_at(parsed, json_pointer)
        structure = "\n".join(_structure(selected))[:5000]
        if json_fields:
            if len(json_fields) > 20:
                raise WebAccessError("json_fields 最多选择 20 个相对 JSON Pointer")
            rows = selected if isinstance(selected, list) else [selected]
            selected = [{field: json_at(row, field) for field in json_fields} for row in rows]
        body = _json_text(selected) if json_pointer or json_fields else raw.strip()
        return None, body, structure, None
    if json_pointer or json_fields:
        raise WebAccessError("只有 JSON 响应支持 json_pointer/json_fields")
    if kind in {"text/html", "application/xhtml+xml"}:
        parser = _DocumentParser(url)
        parser.feed(raw)
        parser.close()
        baseline = parser.text()
        title = " ".join("".join(parser.title).split()) or None
        markdown = _markdown_from_html(raw, url)
        # 正文短到没有"取舍"可做时（整页就一两行），回退与否结果一样，不必加说明——
        # 说明只在"本可以抽出更多、但选择了回退"时才有信息量。
        trivial = len(baseline) < MIN_EXTRACTED_CHARS
        if markdown is None:
            # 库缺失或抽不出内容：回退不是"失败"，但必须说明，否则用户会把旧噪声当新结果。
            return title, baseline, None, None if trivial else (
                "正文抽取库不可用或未抽到正文，已回退到内置解析（含导航等噪声）"
            )
        if len(markdown) < MIN_EXTRACTED_CHARS:
            return title, baseline, None, None if trivial else (
                f"正文抽取只得到 {len(markdown)} 字（过短），已回退到内置解析（含导航等噪声）"
            )
        if baseline and len(markdown) < len(baseline) * MIN_RETENTION_RATIO:
            return title, baseline, None, (
                f"正文抽取判定为模板的内容过多（{len(markdown)}/{len(baseline)} 字），"
                "已回退到内置解析（含导航等噪声）以免丢失表格或列表数据"
            )
        return title, markdown, None, None
    return None, raw.strip(), None, None


def content_version(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def select_window(body: str, *, query: str, start: int, limit: int) -> tuple[str, int, int | None, bool]:
    if start < 0 or start > len(body):
        raise WebAccessError("start_char 超出当前正文范围")
    # 选取只使用正文词项，不触及来源、业务字段或数组排序。
    if query.strip() and start == 0 and len(body) > limit:
        terms = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", query.lower()))
        chunks = [(match.start(), match.group()) for match in re.finditer(r"[^\n]+(?:\n|$)", body)]
        scored = [(sum(term in text.lower() for term in terms), pos, text) for pos, text in chunks]
        picked: list[tuple[int, str]] = []
        remaining = limit
        for score, pos, text in sorted(scored, key=lambda row: (-row[0], row[1])):
            if score == 0 or remaining < 100:
                break
            # 保留相邻上下文，最后按原文位置排序，不把检索顺序冒充原文顺序。
            first_match = min(text.lower().find(term) for term in terms if term in text.lower())
            excerpt_start = max(0, pos + first_match - 150)
            excerpt_end = min(len(body), max(pos + len(text) + 150, excerpt_start + remaining - 50))
            excerpt = body[excerpt_start:excerpt_end][:max(0, remaining - 50)]
            picked.append((excerpt_start, excerpt))
            remaining -= len(excerpt) + 50
        if picked:
            rendered = "\n\n".join(f"原文字符位置 {pos}：\n{text}" for pos, text in sorted(picked))[:limit]
            return rendered, 0, 0, True
    end = min(start + limit, len(body))
    return body[start:end], start, end if end < len(body) else None, False
