"""内置浏览器真实端到端：独立数据目录、无模型、带禁止 iframe 的本地预览站。"""

from __future__ import annotations

import argparse
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn

from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.config import AppConfig

HTML = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>本地预览验收</title>
<style>body{margin:0;background:#f4f7fc;color:#132743;font:16px/24px system-ui}header{height:72px;box-sizing:border-box;padding:24px;background:#193f75;color:white}
main{padding:24px}input,button{box-sizing:border-box;height:40px;font:16px system-ui}input{width:260px}button{display:block;width:140px;margin-top:16px}
a{display:block;margin-top:24px}footer{margin-top:1200px;padding:24px;background:#c7e5d0}</style>
<header>项目预览 · 交互验证</header><main><input aria-label="验收输入" placeholder="输入中文或密码" id="entry">
<button onclick="document.cookie='panel_login=verified;path=/';document.title='已登录:'+document.querySelector('input').value">验证登录</button>
<a href="/second">打开第二页</a><button onclick="alert('网页提示正常')">网页提示</button>
<footer>页面底部 · 滚动正常</footer></main>
<script>document.querySelector('input').addEventListener('keydown',e=>{if(e.key==='Enter')document.title='回车:'+e.target.value})</script></html>"""


class Preview(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/title-change"):
            payload = '''<meta charset="utf-8"><title>短</title>
<button style="position:absolute;left:24px;top:24px;width:220px;height:80px"
onclick="setTimeout(()=>document.title='一个足够长的网页标题用于触发标签栏溢出状态变化',500)">更新标题</button>'''
        elif self.path.startswith("/double-click"):
            payload = '''<meta charset="utf-8"><title>双击验收</title>
<button style="position:absolute;left:24px;top:24px;width:220px;height:80px"
onclick="window.clicks=(window.clicks||0)+1;document.title='点击:'+window.clicks"
ondblclick="document.title='双击:'+window.clicks">双击测试</button>'''
        elif self.path.startswith("/second"):
            payload = '<meta charset="utf-8"><title>第二页预览</title><h1>第二页</h1><a href="/">返回项目</a>'
        else:
            payload = HTML
            if "panel_login=verified" in self.headers.get("Cookie", ""):
                payload = payload.replace("<title>本地预览验收</title>", "<title>已登录项目</title>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, _format: str, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8146)
    parser.add_argument("--temp-root", type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if args.temp_root is not None:
        args.temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chaiai-browser-test-", dir=args.temp_root) as temporary:
        root = Path(temporary)
        config = AppConfig()
        config.workspace.root = str(root)
        config.memory.short_term.enabled = True
        config.memory.short_term.path = str(root / "checkpoints.sqlite")
        config.memory.long_term.path = str(root / "memories.sqlite")
        config.memory.medium_term.enabled = False
        config.memory.medium_term.path = str(root / "chroma")
        config.model_settings.path = str(root / "settings.sqlite")
        config.model_settings.credential_store = "memory"
        runtime = open_runtime(config, base_dir=root, static_dir=project / "frontend" / "dist")
        preview = ThreadingHTTPServer(("127.0.0.1", args.port + 1), Preview)
        threading.Thread(target=preview.serve_forever, daemon=True).start()
        try:
            uvicorn.run(create_app(runtime), host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            preview.shutdown()
            preview.server_close()
            runtime.close()


if __name__ == "__main__":
    main()
