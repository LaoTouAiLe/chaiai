"""Transparent local measurement relay; never records credentials or alters requests."""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from local_agent.config import load_dotenv_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8019)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    load_dotenv_file(Path(__file__).resolve().parents[1] / ".env")
    import os

    secret_values = [value for name, value in os.environ.items()
                     if any(word in name.upper() for word in ("API_KEY", "TOKEN", "SECRET"))
                     and len(value) > 8]
    lock = threading.Lock()
    sequence = 0

    def save(path: Path, value: object) -> None:
        encoded = json.dumps(value, ensure_ascii=False, indent=2)
        for secret in secret_values:
            encoded = encoded.replace(secret, "[REDACTED]")
        path.write_text(encoded + "\n", encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *values: object) -> None:
            pass

        def do_GET(self) -> None:
            self.forward()

        def do_POST(self) -> None:
            self.forward()

        def forward(self) -> None:
            nonlocal sequence
            segments = self.path.split("/", 2)
            if len(segments) != 3 or segments[1] not in {"chaiai", "dsh"}:
                self.send_error(404)
                return
            label, route = segments[1], "/" + segments[2]
            if route not in {"/chat/completions", "/v1/chat/completions", "/models", "/v1/models"}:
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            with lock:
                sequence += 1
                identity = f"{sequence:04d}-{label}"
            started = time.time()
            payload = json.loads(body) if body else {}
            save(args.out / f"{identity}-request.json", {
                "client": label, "route": route, "started_at": started,
                "wire_sha256": hashlib.sha256(body).hexdigest(), "request": payload,
            })
            headers = {name: self.headers[name] for name in
                       ("Authorization", "Content-Type", "Accept") if name in self.headers}
            received = bytearray()
            status = 0
            content_type = ""
            failure: str | None = None
            try:
                with (
                    httpx.Client(timeout=httpx.Timeout(180, connect=30)) as client,
                    client.stream(self.command, "https://api.deepseek.com" + route,
                                  content=body or None, headers=headers) as response,
                ):
                        status = response.status_code
                        content_type = response.headers.get("content-type", "application/json")
                        self.send_response(status)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Connection", "close")
                        self.end_headers()
                        for chunk in response.iter_bytes():
                            received.extend(chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
            except (httpx.HTTPError, OSError) as exc:
                failure = type(exc).__name__
                if status == 0:
                    self.send_error(502, "Upstream measurement relay failed")
            finally:
                usages = []
                wire = received.decode("utf-8", errors="replace")
                if "text/event-stream" in content_type:
                    documents = [line[6:] for line in wire.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
                else:
                    documents = [wire]
                for document in documents:
                    try:
                        value = json.loads(document)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(value, dict) and value.get("usage") is not None:
                        usages.append(value["usage"])
                save(args.out / f"{identity}-response.json", {
                    "client": label, "status": status, "failure": failure,
                    "elapsed_seconds": time.time() - started, "usage": usages,
                    "response_bytes": len(received),
                    "error": wire[:1500] if status >= 400 else None,
                })

    print(f"paired measurement relay http://127.0.0.1:{args.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
