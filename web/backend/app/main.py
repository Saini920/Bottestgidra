"""FastAPI application — wires everything together."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import config, db
from .auth import router as auth_router
from .jobs import router as jobs_router
from .setup import router as setup_router
from .storage import router as storage_router
from .ws import ConnectionManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.get_conn()  # init schema
    log = logging.getLogger("app")
    log.info("API_ID set: %s", bool(config.get_api_id()))
    log.info("GITHUB_TOKEN set: %s", bool(config.get_github_token()))
    log.info("GITHUB_REPO: %s", config.get_github_repo())
    log.info("Admins: %s", config.get_admin_ids() or "(first login becomes admin)")
    yield


app = FastAPI(title="Telegram Web — RE Tools + Storage", lifespan=lifespan)
app.state.ws = ConnectionManager()
app.state.tmp_dir = config.TEMP_DIR

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production (frontend origin)
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(storage_router)
app.include_router(jobs_router)
app.include_router(setup_router)


@app.get("/health")
async def health():
    return {"ok": True}


@app.websocket("/ws/progress/{channel}")
async def ws_progress(websocket: WebSocket, channel: str):
    manager: ConnectionManager = websocket.app.state.ws
    await manager.connect(channel, websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive / ignore inbound
    except WebSocketDisconnect:
        manager.disconnect(channel, websocket)
    except Exception:
        manager.disconnect(channel, websocket)
