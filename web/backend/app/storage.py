"""Storage routes — files live in the user's own Telegram Saved Messages.

The web server only keeps metadata; file bytes are streamed through on
upload/download and never stored server-side.
"""
import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse
from pyrogram.errors import FloodWait

from . import db, mtproto
from .auth import current_user
from .ws import ConnectionManager

log = logging.getLogger("storage")

router = APIRouter(prefix="/storage", tags=["storage"])


async def _progress_bridge(channel: str, ws: ConnectionManager):
    async def progress(current: int, total: int):
        if total <= 0:
            return
        pct = round(current * 100.0 / total, 1)
        try:
            await ws.broadcast(channel, {"type": "progress", "pct": pct,
                                         "current": current, "total": total})
        except Exception:
            pass
    return progress


@router.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    channel: str = Form(default=""),
    user_id: int = Depends(current_user),
):
    ws: ConnectionManager = request.app.state.ws
    client = await mtproto.get_client(user_id)
    if client is None:
        raise HTTPException(401, "Telegram session not available. Please log in again.")

    filename = file.filename or "upload.bin"
    tmp = request.app.state.tmp_dir / f"up_{user_id}_{int(time.time() * 1000)}_{filename}"
    size = 0
    try:
        with open(tmp, "wb") as fh:
            while chunk := await file.read(1 << 20):
                fh.write(chunk)
                size += len(chunk)
        if size == 0:
            raise HTTPException(400, "Empty file")

        prog = await _progress_bridge(channel or f"up:{user_id}", ws)
        try:
            sent = await client.send_document("me", str(tmp), caption="", progress=prog)
        except FloodWait as e:
            raise HTTPException(429, f"Telegram rate limit: wait {int(e.value)}s")

        msg_id = getattr(sent, "id", 0) or 0
        row_id = db.add_file(user_id, msg_id, filename, size)
        if channel:
            await ws.broadcast(channel, {"type": "done", "file_id": row_id, "filename": filename})
        return {"ok": True, "file_id": row_id, "filename": filename, "size": size}
    finally:
        tmp.unlink(missing_ok=True)


async def _chat_history(client, limit: int):
    """get_chat_history (Pyrogram 2.x) with get_history fallback."""
    try:
        return client.get_chat_history("me", limit=limit)
    except AttributeError:
        return client.get_history("me", limit=limit)


@router.get("/files")
async def list_files(user_id: int = Depends(current_user)):
    """Return metadata; optionally syncs new docs from Saved Messages."""
    client = await mtproto.get_client(user_id)
    if client is not None:
        known = {f["msg_id"] for f in db.list_files(user_id) if f["msg_id"]}
        try:
            async for m in await _chat_history(client, 50):
                if m.document and m.id not in known:
                    db.add_file(user_id, m.id, m.document.file_name or f"doc_{m.id}",
                                m.document.file_size or 0)
        except Exception as e:
            log.warning("sync failed: %s", e)
    return {"files": db.list_files(user_id)}


@router.get("/download/{file_id}")
async def download(file_id: int, channel: str = "", user_id: int = Depends(current_user),
                   request: Request = None):
    rec = db.get_file(user_id, file_id)
    if not rec or not rec.get("msg_id"):
        raise HTTPException(404, "File not found")
    client = await mtproto.get_client(user_id)
    if client is None:
        raise HTTPException(401, "Telegram session not available. Please log in again.")

    ws: ConnectionManager = request.app.state.ws
    tmp = request.app.state.tmp_dir / f"dl_{user_id}_{file_id}_{rec['filename']}"
    try:
        msg = await client.get_messages("me", rec["msg_id"])
        if not msg or not msg.document:
            raise HTTPException(404, "File missing on Telegram")
        prog = await _progress_bridge(channel or f"dl:{user_id}", ws)
        path = await client.download_media(msg, file_name=str(tmp), progress=prog)
        if not path:
            raise HTTPException(500, "Download failed")
        return FileResponse(path, filename=rec["filename"],
                            media_type="application/octet-stream",
                            background=_unlink(path))


def _unlink(path):
    import os
    async def _bg():
        await asyncio.sleep(5)
        try:
            os.remove(path)
        except OSError:
            pass
    return _bg


@router.delete("/files/{file_id}")
async def delete(file_id: int, user_id: int = Depends(current_user)):
    rec = db.get_file(user_id, file_id)
    if not rec:
        raise HTTPException(404, "File not found")
    if rec.get("msg_id"):
        client = await mtproto.get_client(user_id)
        if client is not None:
            try:
                await client.delete_messages("me", [rec["msg_id"]])
            except Exception as e:
                log.warning("telegram delete failed: %s", e)
    db.delete_file(user_id, file_id)
    return {"ok": True}
