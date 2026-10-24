"""来源标识由实际 URL 决定，不采信网页或模型提供的编号。"""

import hashlib
from urllib.parse import urlsplit, urlunsplit


def source_id(url: str) -> str:
    parsed = urlsplit(url)
    normalized = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
    return "web-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
