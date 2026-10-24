"""真实模型的本地开发验收：独立数据、公开快照与原始任务留档。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn

from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.config import AppConfig, load_config, load_dotenv_file

PROJECT = Path(__file__).resolve().parents[1]


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def serve(root: Path, port: int) -> None:
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / "workspaces"
    workspace.mkdir()
    load_dotenv_file(PROJECT / ".env")
    original = load_config(PROJECT / "config.yaml")
    source = Path(original.model_settings.path)
    if not source.is_absolute():
        source = PROJECT / source
    with (sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as src,
          sqlite3.connect(root / "settings.sqlite") as dst):
        src.backup(dst)
        # 只改验收副本：清空历史会话选择、禁用记忆读写和 Embedding 下载。
        dst.execute("DELETE FROM model_selection WHERE scope <> 'default'")
        row = dst.execute("SELECT id, values_json FROM app_preferences").fetchone()
        if row:
            preferences = json.loads(row[1])
            for key in ("memory", "medium_term", "embedding"):
                preferences.setdefault(key, {})["enabled"] = False
            dst.execute("UPDATE app_preferences SET values_json=? WHERE id=?",
                        (json.dumps(preferences), row[0]))
        dst.commit()
    data = original.model_dump()
    data["workspace"]["root"] = str(workspace)
    data["memory"]["short_term"]["path"] = str(root / "checkpoints.sqlite")
    for layer in ("long_term", "medium_term"):
        data["memory"][layer].update(enabled=False, path=str(root / layer))
    data["model_settings"]["path"] = str(root / "settings.sqlite")
    data["web"].update(host="127.0.0.1", port=port)
    runtime = open_runtime(AppConfig.model_validate(data), base_dir=root,
                           static_dir=PROJECT / "frontend/dist")
    connections = runtime.service.list_model_connections()
    save(root / "manifest.json", {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "url": f"http://127.0.0.1:{port}", "mode": "real_configured_model",
        "connections": [{k: v for k, v in item.model_dump(mode="json").items()
                         if k not in {"credential_ref"}} for item in connections],
        "isolation": "New chat DB; copied settings; original databases read-only; memory disabled only in copy",
    })
    print(json.dumps({"root": str(root), "port": port,
                      "connections": [(c.model_id, c.is_default, c.key_status) for c in connections]},
                     ensure_ascii=False), flush=True)
    try:
        uvicorn.run(create_app(runtime), host="127.0.0.1", port=port, log_level="warning")
    finally:
        runtime.close()


def run(root: Path, group: str, port: int) -> None:
    case = json.loads((root / "cases" / f"{group}.json").read_text(encoding="utf-8"))
    target = root / "workspaces" / case["workspace"]
    target.mkdir(parents=True, exist_ok=True)
    out = root / "results" / group
    out.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=1800, trust_env=False) as client:
        selection = client.post("/api/workspaces/select", json={"path": str(target)})
        selection.raise_for_status()
        session_id = selection.json()["session_id"]
        save(out / "submission.json", {**case, "session_id": session_id, "submitted_at": time.time()})
        print(json.dumps({"group": group, "session_id": session_id, "cases": case["cases"]}), flush=True)
        response = client.post(f"/api/sessions/{session_id}/messages", json={
            "text": case["prompt"], "client_request_id": f"acceptance-{group}-{time.time_ns()}",
        })
        save(out / "command.json", {"http_status": response.status_code, "body": response.json()})
        snapshot = client.get(f"/api/sessions/{session_id}")
        snapshot.raise_for_status()
        save(out / "snapshot.json", snapshot.json())
        print(json.dumps({"group": group, "http_status": response.status_code,
                          "state": snapshot.json()["session_state"],
                          "result": (snapshot.json().get("final_result") or {}).get("state")},
                         ensure_ascii=True), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["serve", "run"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8147)
    parser.add_argument("--group")
    args = parser.parse_args()
    if args.mode == "serve":
        serve(args.root.resolve(), args.port)
    else:
        run(args.root.resolve(), args.group, args.port)


if __name__ == "__main__":
    main()
