"""生产静态托管（T29；计划 §6.2「生产同源」）。

前端构建产物由本服务托管，所以生产环境**只需要启动 Python**（§15.1 的三种运行形态之一）。
开发时仍走 Vite（`npm run dev`，`/api` 代理到本服务）——两条路径都不需要额外的 Web 服务器。

三条硬规则（都由 `tests/test_api.py` 守着）：

1. **`/api` 优先**：业务路由先注册，本模块的兜底路由最后注册；`/api/...` 的不存在路径一律
   JSON 404，**绝不**回退成页面——否则前端会把一个 HTML 文档当 JSON 解析。
2. **看起来像静态资源的路径必须 404**：浏览器拿到 MIME 不符的正文时（比如把 HTML 当 JS 模块）
   报的是"语法错误在第 1 行 `<`"，那是无法诊断的故障；带资源后缀的路径不存在就直接 404。
3. **路径不越界**：候选文件解析**真实路径**后必须仍在构建目录之内，`..`、编码过的 `..` 与
   符号链接逃逸都拒绝。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response

#: 资源后缀：命中这些后缀又不存在的路径按"缺资源"404，不做 SPA 回退。
ASSET_SUFFIXES = frozenset(
    {
        ".js",
        ".mjs",
        ".cjs",
        ".css",
        ".map",
        ".json",
        ".txt",
        ".html",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".wasm",
        ".mp3",
        ".mp4",
        ".webmanifest",
    }
)

#: 后缀 → MIME。**刻意不依赖 `mimetypes`**：它在本机读注册表，实测把 `.js` 猜成 `text/plain`
#: （`mimetypes.guess_type("a.js")` 在这台 Windows 上就返回 `text/plain`），浏览器随即以
#: "Expected a JavaScript-or-Wasm module script" 拒绝加载，整个页面空白——这是**产品故障**：
#: 生产同源托管在那种机器上根本起不来。规则 2 说的"MIME 不符是无法诊断的故障"，这里就是那一半。
MEDIA_TYPES: Final[dict[str, str]] = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".css": "text/css",
    ".map": "application/json",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
}

#: Vite 只把**内容哈希过**的文件放进 `assets/`，因此可以长期缓存；
#: 其余文件（`index.html` 等）必须每次核对。
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"

NO_BUILD_DETAIL = (
    "前端产物不存在：先在 frontend/ 目录执行 npm run build 重新构建，"
    "或用 npm run dev 走 Vite 开发服务器（它会把 /api 代理到本服务）。"
)
NO_INDEX_DETAIL = "前端产物不完整：构建目录里没有 index.html，请重新执行 npm run build。"
UNKNOWN_API_DETAIL = "接口不存在。"


def _json_404(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=404, headers={"Cache-Control": NO_CACHE})


def _is_api_path(path: str) -> bool:
    return path == "api" or path.startswith("api/")


def _is_asset_path(path: str) -> bool:
    return Path(path).suffix.lower() in ASSET_SUFFIXES


def _resolve_within(root: Path, path: str) -> Path | None:
    """把请求路径解析成 `root` **之内**的真实文件；越界或解析失败返回 `None`。"""
    if not path or path.endswith("/"):
        return None
    try:
        candidate = (root / path).resolve()
    except OSError:  # pragma: no cover - 解析失败（路径过长/非法）一律按不存在处理
        return None
    resolved_root = root.resolve()
    if candidate == resolved_root or resolved_root in candidate.parents:
        return candidate
    return None


def _media_type(file: Path) -> str:
    """按后缀给出 MIME；没有映射时回落 `application/octet-stream`，**不用平台猜的**。"""
    return MEDIA_TYPES.get(file.suffix.lower(), "application/octet-stream")


def _cache_headers(file: Path, root: Path) -> dict[str, str]:
    """`assets/` 下是内容哈希文件 → 长期缓存；其余（index.html 等）→ 每次核对。"""
    try:
        relative = file.relative_to(root.resolve())
    except ValueError:  # pragma: no cover - `_resolve_within` 已保证在 root 之内
        return {"Cache-Control": NO_CACHE}
    if relative.parts and relative.parts[0] == "assets":
        return {"Cache-Control": IMMUTABLE_CACHE}
    return {"Cache-Control": NO_CACHE}


def mount_frontend(app: FastAPI, static_dir: Path | None) -> None:
    """挂上兜底路由（必须**最后**注册：业务路由优先）。

    Args:
        app: 已注册业务路由的应用。
        static_dir: 前端构建产物目录；`None` 表示没有产物（只提供 API，页面给出可执行提示）。
    """
    index = None if static_dir is None else static_dir / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str) -> Response:
        if _is_api_path(path):
            return _json_404(UNKNOWN_API_DETAIL)
        if static_dir is None or index is None or not index.is_file():
            return _json_404(NO_BUILD_DETAIL if static_dir is None else NO_INDEX_DETAIL)

        candidate = _resolve_within(static_dir, path)
        if candidate is not None and candidate.is_file():
            return FileResponse(
                candidate,
                media_type=_media_type(candidate),
                headers=_cache_headers(candidate, static_dir),
            )
        if _is_asset_path(path):
            return _json_404(f"静态资源不存在：{path}")
        # 其余路径交给前端路由（刷新深链接不能 404）。
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": NO_CACHE})
