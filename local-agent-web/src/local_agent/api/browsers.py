"""同源浏览器控制与有界实时画面传输。"""

import asyncio
import queue
import time
from contextlib import suppress

from anyio import CancelScope
from fastapi import APIRouter, HTTPException, Response, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from local_agent.application.browser_models import BrowserCommand, BrowserRequest, BrowserView
from local_agent.application.browsers import BrowserError, BrowserManager
from local_agent.tools.browser_tools import checked_url


def build_browser_router(manager: BrowserManager) -> APIRouter:
    router = APIRouter(prefix="/api/browsers", tags=["browsers"])

    @router.post("", response_model=BrowserView)
    def create(body: BrowserRequest, response: Response) -> BrowserView:
        response.headers["Cache-Control"] = "no-store"
        try:
            return manager.create(body)
        except BrowserError as exc:
            raise HTTPException(exc.status, str(exc), headers={"Cache-Control": "no-store"}) from exc

    @router.delete("/requests/{request_id}", status_code=204)
    def stop(request_id: str) -> Response:
        manager.stop(request_id)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @router.get("/{browser_id}", response_model=BrowserView)
    def state(browser_id: str, response: Response) -> BrowserView:
        response.headers["Cache-Control"] = "no-store"
        try:
            item = manager.get(browser_id)
            with item.lock:
                return item.view.model_copy(deep=True)
        except BrowserError as exc:
            raise HTTPException(exc.status, str(exc), headers={"Cache-Control": "no-store"}) from exc

    @router.websocket("/{browser_id}/stream")
    async def stream(socket: WebSocket, browser_id: str, take_over: bool = False) -> None:
        try:
            item, controller = await run_in_threadpool(manager.connect, browser_id, take_over=take_over)
        except BrowserError as exc:
            await socket.close(code=1008, reason=str(exc))
            return
        tasks: list[asyncio.Task] = []
        try:
            await socket.accept()

            async def send_frames() -> None:
                last_frame = -1
                last_state: dict | None = None
                while True:
                    with item.lock:
                        if item.controller != controller:
                            return
                        state = item.view.model_dump()
                        frame_number, frame = item.frame_number, item.frame
                    if state != last_state:
                        await asyncio.wait_for(socket.send_json({"type": "state", **state}), timeout=5)
                        last_state = state
                    if frame is not None and frame_number != last_frame:
                        await asyncio.wait_for(socket.send_json(frame), timeout=5)
                        last_frame = frame_number
                    if item.ending.is_set():
                        return
                    await asyncio.sleep(.025)

            async def receive_commands() -> None:
                while True:
                    text = await socket.receive_text()
                    if len(text) > 20000:
                        raise BrowserError("浏览器输入过长。")
                    body = BrowserCommand.model_validate_json(text)
                    if body.action == "navigate":
                        try:
                            checked_url(body.url)
                        except ValueError as exc:
                            with item.lock:
                                item.view.error = str(exc)
                            continue
                    with item.lock:
                        if item.controller != controller:
                            raise BrowserError("浏览器已由另一页面接管。")
                        item.activity = time.monotonic()
                    item.commands.put_nowait(body)

            tasks = [asyncio.create_task(send_frames()), asyncio.create_task(receive_commands())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (asyncio.CancelledError, WebSocketDisconnect, ValidationError, ValueError,
                RuntimeError, queue.Full, asyncio.TimeoutError):
            pass
        finally:
            with item.lock:
                if item.controller == controller:
                    item.controller = None
                    item.activity = time.monotonic()
            for task in tasks:
                task.cancel()
            with CancelScope(shield=True):
                await asyncio.gather(*tasks, return_exceptions=True)
                with suppress(RuntimeError, WebSocketDisconnect):
                    await socket.close()

    return router
