"""Bounded terminal transport. Approval remains in the application service."""

import asyncio
import json
from contextlib import suppress

from anyio import CancelScope
from fastapi import APIRouter, HTTPException, Response, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from local_agent.api.container import ApiRuntime
from local_agent.application.terminal_models import (
    TerminalDecision,
    TerminalList,
    TerminalRequest,
    TerminalView,
)
from local_agent.application.terminals import TerminalError, TerminalManager


def build_terminal_router(runtime: ApiRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/terminals", tags=["terminals"])

    def manager(response: Response) -> TerminalManager:
        response.headers["Cache-Control"] = "no-store"
        return runtime.service.user_terminals()

    @router.get("", response_model=TerminalList)
    def listing(workspace_id: str, response: Response) -> TerminalList:
        return manager(response).listing(workspace_id)

    @router.post("/requests", response_model=TerminalView)
    def request(body: TerminalRequest, response: Response) -> TerminalView:
        try:
            return manager(response).request(body.workspace_id)
        except TerminalError as exc:
            raise HTTPException(exc.status, str(exc), headers={"Cache-Control": "no-store"}) from exc

    @router.post("/requests/{approval_id}/decision", response_model=TerminalView)
    def decide(approval_id: str, body: TerminalDecision, response: Response) -> TerminalView:
        try:
            return manager(response).decide(approval_id, body)
        except TerminalError as exc:
            raise HTTPException(exc.status, str(exc), headers={"Cache-Control": "no-store"}) from exc

    @router.delete("/{terminal_id}", response_model=TerminalView)
    def stop(terminal_id: str, response: Response) -> TerminalView:
        try:
            return manager(response).stop(terminal_id)
        except TerminalError as exc:
            raise HTTPException(exc.status, str(exc), headers={"Cache-Control": "no-store"}) from exc

    @router.websocket("/{terminal_id}/stream")
    async def stream(websocket: WebSocket, terminal_id: str) -> None:
        terminals = runtime.service.user_terminals()
        try:
            controller = terminals.connect(terminal_id)
        except TerminalError as exc:
            await websocket.close(code=1008, reason=str(exc))
            return
        tasks: list[asyncio.Task] = []
        try:
            await websocket.accept()

            async def send_output() -> None:
                cursor = 0
                await websocket.send_json({"type": "reset"})
                while True:
                    view, data, cursor, truncated = terminals.snapshot(terminal_id, cursor)
                    if truncated:
                        await websocket.send_json({"type": "truncated"})
                    if data:
                        # A slow browser cannot block the native output reader.
                        await asyncio.wait_for(websocket.send_bytes(data), timeout=10)
                    if view.status != "running":
                        await websocket.send_json({"type": "exit", "terminal": view.model_dump()})
                        return
                    await asyncio.sleep(.04)

            async def receive_input() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    data = message.get("bytes")
                    if data is not None:
                        terminals.input(terminal_id, controller, data)
                        continue
                    text = message.get("text", "")
                    if len(text) > 256:
                        raise TerminalError("控制消息过大。")
                    payload = json.loads(text)
                    if not isinstance(payload, dict) or payload.pop("type", None) != "resize":
                        raise TerminalError("未知终端控制消息。")
                    size = TerminalDecision.model_validate({**payload, "decision": "approve"})
                    await asyncio.to_thread(terminals.resize, terminal_id, controller, size.cols, size.rows)

            tasks = [asyncio.create_task(send_output()), asyncio.create_task(receive_input())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            pass  # ASGI connection cancelled; synchronous ownership cleanup still runs below.
        except (TerminalError, ValidationError, ValueError, OSError, RuntimeError,
                asyncio.TimeoutError, WebSocketDisconnect):
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=1008, reason="终端连接结束，请检查状态后重连。")
        finally:
            terminals.disconnect(terminal_id, controller)
            for task in tasks:
                task.cancel()
            with CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)
                with suppress(RuntimeError, WebSocketDisconnect):
                    await websocket.close()

    return router
