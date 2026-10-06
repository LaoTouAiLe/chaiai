"""Run the paired read-only scenario through the actual local-agent-web HTTP API."""

import argparse
import json
import time
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--turns", type=int, default=1)
    parser.add_argument("--port", type=int, default=8013)
    args = parser.parse_args()
    scenario = json.loads((args.out / "scenario.json").read_text(encoding="utf-8"))
    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=1800) as client:
        connections = client.get("/api/model-connections")
        connections.raise_for_status()
        identity = connections.json()[0]["connection_id"]
        tested = client.post(f"/api/model-connections/{identity}/test")
        tested.raise_for_status()
        (args.out / "chaiai-capabilities.json").write_text(
            json.dumps(tested.json(), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        created = client.post("/api/sessions", json={"title": "paired-live architecture", "mode": "agent"})
        created.raise_for_status()
        session_id = created.json()["session_id"]
        (args.out / "chaiai-session.json").write_text(json.dumps(created.json(), indent=2), encoding="utf-8")
        for index, prompt in enumerate(scenario["turns"][:args.turns], 1):
            started = time.time()
            print(f"chaiai turn {index} started", flush=True)
            result = client.post(f"/api/sessions/{session_id}/messages", json={
                "text": prompt, "client_request_id": f"paired-live-{index}",
            })
            result.raise_for_status()
            snapshot = client.get(f"/api/sessions/{session_id}")
            snapshot.raise_for_status()
            data = snapshot.json()
            (args.out / f"chaiai-turn-{index}.json").write_text(json.dumps({
                "prompt": prompt, "started_at": started, "finished_at": time.time(),
                "command": result.json(), "snapshot": data,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"chaiai turn {index} completed: {result.json().get('status')}; "
                  f"{time.time()-started:.1f}s", flush=True)
            if result.json().get("status") == "awaiting_approval":
                print("Read-only scenario requested approval; preserved and stopped.", flush=True)
                break


if __name__ == "__main__":
    main()
