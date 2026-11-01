"""
Web layer: serves the browser UI and relays engine events over a WebSocket.

Every browser tab is a peer of the terminal UI, not a separate simulation -
actions it sends (catch/release/dismiss/presence/filter) go straight to the
same RainEngine the terminal is reading, and every change from either side
is broadcast back out to all connected sockets.
"""
from __future__ import annotations

import asyncio
import hmac
from typing import Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from playback import WINDOWS
from rain_engine import RainEngine

WEB_DIR = Path(__file__).parent / "web"


def create_app(engine: RainEngine, token: Optional[str] = None) -> FastAPI:
    app = FastAPI()

    def authorized(given: Optional[str]) -> bool:
        return token is None or (given is not None and hmac.compare_digest(given, token))

    @app.on_event("startup")
    async def _startup() -> None:
        if not engine.started:
            await engine.start()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (WEB_DIR / "index.html").read_text()

    @app.get("/api/state")
    async def state(token: Optional[str] = Query(None)) -> dict:
        if not authorized(token):
            raise HTTPException(status_code=401)
        return engine.snapshot()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        if not authorized(websocket.query_params.get("token")):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        owner = f"ws{id(websocket)}"
        queue = engine.subscribe()
        await websocket.send_json(engine.snapshot())

        async def forward() -> None:
            while True:
                event = await queue.get()
                await websocket.send_json(event)

        forward_task = asyncio.create_task(forward())
        try:
            while True:
                msg = await websocket.receive_json()
                action = msg.get("action")
                if action == "catch":
                    engine.catch(msg["id"], owner)
                elif action == "release":
                    engine.release(msg["id"])
                elif action == "explain":      # to this client only: it is asking about the entry it just caught
                    s = engine.find(msg["id"])
                    if s:
                        await websocket.send_json({"type": "explain", "id": s.id, **engine.explain(s)})
                elif action == "live_pause":
                    engine.pause_live()
                elif action == "live_resume":
                    engine.resume_live()
                elif action == "dismiss":
                    engine.dismiss(msg["id"])
                elif action == "toggle_presence":
                    engine.toggle_presence()
                elif action == "desk":
                    engine.desk(msg.get("kind"), msg.get("detail"))
                elif action == "playback":
                    w = msg.get("window")
                    seconds = WINDOWS.get(w) if isinstance(w, str) else None
                    if seconds:
                        engine.start_playback(seconds)
                elif action == "playback_pause":
                    engine.pause_playback()
                elif action == "playback_resume":
                    engine.resume_playback()
                elif action == "playback_stop":
                    engine.stop_playback()
                elif action == "set_filter":
                    engine.set_filter(msg.get("level", "all"))
        except WebSocketDisconnect:
            pass
        finally:
            forward_task.cancel()
            engine.unsubscribe(queue)
            engine.release_owner(owner)          # closing the window must not leave its catch frozen forever

    return app
