"""RE jobs — dispatch to GitHub Actions (same workflows as the bot project)."""
import asyncio
import datetime
import logging
import re
import time

import httpx
from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse

from . import config, db, security
from .auth import current_user
from .ws import ConnectionManager

log = logging.getLogger("jobs")

router = APIRouter(prefix="/api", tags=["jobs"])

GH = "https://api.github.com"


def gh_headers() -> dict:
    """Built per call so Settings changes apply without a restart."""
    return {
        "Authorization": f"token {config.get_github_token()}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "file")[:80] or "file"


@router.get("/engines")
async def engines():
    return {"engines": [
        {"id": eid, **cfg} for eid, cfg in config.ENGINES.items()
    ]}


@router.post("/jobs")
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    engine: str = Form(...),
    channel: str = Form(default=""),
    user_id: int = Depends(current_user),
):
    if not config.get_github_token():
        raise HTTPException(500, "GITHUB_TOKEN not configured — set it in Settings")
    cfg = config.ENGINES.get(engine)
    if not cfg:
        raise HTTPException(400, f"Unknown engine: {engine}")
    if cfg["premium"] and user_id not in config.get_admin_ids():
        user = db.get_user(user_id)
        if not user or not user.get("is_premium"):
            raise HTTPException(403, "This engine requires a Premium plan")

    filename = _safe_name(file.filename or "upload.bin")
    job_id = db.create_job(user_id, engine, filename)
    src = request.app.state.tmp_dir / f"job_{job_id}.bin"
    try:
        with open(src, "wb") as fh:
            while chunk := await file.read(1 << 20):
                fh.write(chunk)
    except Exception as e:
        db.update_job(job_id, status="error", error=str(e))
        raise HTTPException(500, "Failed to store upload")

    payload = {
        "event_type": cfg["event"],
        "client_payload": {
            "chat_id": str(user_id),
            "message_id": str(job_id),
            "original_message_id": str(job_id),
            "filename": filename,
            "file_url": security.make_signed_url(job_id, user_id),
            "job_id": str(job_id),
            "user_id": str(user_id),
            "is_premium": "true",
            "report_url": f"{config.get_public_url()}/api/status",
            "bot_token": config.get_bot_token() or "",
        },
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{GH}/repos/{config.get_github_repo()}/dispatches",
                                  headers=gh_headers(), json=payload)
        if r.status_code not in (200, 201, 202, 204):
            db.update_job(job_id, status="error", error=f"Dispatch failed: {r.status_code} {r.text[:200]}")
            raise HTTPException(502, f"GitHub dispatch failed ({r.status_code})")
    except httpx.HTTPError as e:
        db.update_job(job_id, status="error", error=f"Dispatch error: {e}")
        raise HTTPException(502, f"GitHub dispatch failed: {e}")

    ws: ConnectionManager = request.app.state.ws
    if channel:
        await ws.broadcast(channel, {"type": "queued", "job_id": job_id, "engine": engine})
    return {"ok": True, "job_id": job_id, "engine": engine}


@router.get("/jobs")
async def list_jobs(user_id: int = Depends(current_user)):
    return {"jobs": db.list_jobs(user_id)}


@router.get("/jobs/{job_id}/result")
async def get_result(job_id: int, request: Request, user_id: int = Depends(current_user)):
    job = db.get_job(job_id)
    if not job or job["user_id"] != user_id:
        raise HTTPException(404, "Job not found")
    if not job.get("result_path"):
        raise HTTPException(404, "No result yet")
    path = request.app.state.tmp_dir / job["result_path"]
    if not path.exists():
        raise HTTPException(404, "Result expired")
    return FileResponse(path, filename=f"{_safe_name(job['filename'])}_result.zip",
                        media_type="application/zip", background=_unlink(path))


def _unlink(path):
    import os
    async def _bg():
        await asyncio.sleep(5)
        try:
            os.remove(path)
        except OSError:
            pass
    return _bg


@router.post("/status")
async def status_webhook(request: Request, x_count_token: str = Header(default="")):
    """Called by GitHub Actions workers with progress updates and result uploads."""
    if not x_count_token or x_count_token != config.get_webhook_token():
        raise HTTPException(401, "Bad webhook token")
    ws: ConnectionManager = request.app.state.ws
    ct = request.headers.get("content-type", "")
    if ct.startswith("multipart/"):
        form = await request.form()
        job_id = int(form.get("job_id", 0))
        up = form.get("file")
        if up and job_id:
            path = request.app.state.tmp_dir / f"result_{job_id}.zip"
            with open(path, "wb") as fh:
                while chunk := await up.read(1 << 20):
                    fh.write(chunk)
            db.update_job(job_id, status="done", result_path=f"result_{job_id}.zip",
                          finished_at=datetime.datetime.utcnow().isoformat(timespec="seconds"))
            await ws.broadcast(f"job:{job_id}", {"type": "done", "job_id": job_id})
        return {"ok": True}

    body = await request.json()
    job_id = int(body.get("job_id", 0))
    ev = {"type": "status", "job_id": job_id}
    if "pct" in body:
        ev["pct"] = body["pct"]
    if "label" in body:
        ev["label"] = body["label"]
    if "status" in body:
        ev["status"] = body["status"]
        db.update_job(job_id, status=body["status"])
    await ws.broadcast(f"job:{job_id}", ev)
    return {"ok": True}


@router.post("/jobs/{job_id}/stop")
async def stop_job(job_id: int, user_id: int = Depends(current_user)):
    job = db.get_job(job_id)
    if not job or job["user_id"] != user_id:
        raise HTTPException(404, "Job not found")
    if not config.get_github_token():
        raise HTTPException(500, "GITHUB_TOKEN not configured")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            runs = await client.get(
                f"{GH}/repos/{config.get_github_repo()}/actions/runs",
                headers=gh_headers(),
                params={"event": "repository_dispatch", "per_page": 20},
            )
            for run in runs.json().get("workflow_runs", []):
                name = run.get("name", "")
                if f"job-{job_id}" in name or f"-{user_id}-{job_id}" in name:
                    await client.post(
                        f"{GH}/repos/{config.get_github_repo()}/actions/runs/{run['id']}/cancel",
                        headers=gh_headers(),
                    )
                    break
    except Exception as e:
        log.warning("cancel failed: %s", e)
    db.update_job(job_id, status="cancelled", finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    return {"ok": True}


@router.get("/transfer/{job_id}/{user_id}/{exp}/{sig}")
async def transfer(job_id: int, user_id: int, exp: int, sig: str, request: Request):
    """Serves the uploaded file to GitHub Actions workers via a signed URL."""
    if not security.verify_signed_url(job_id, user_id, exp, sig):
        raise HTTPException(403, "Invalid or expired link")
    path = request.app.state.tmp_dir / f"job_{job_id}.bin"
    if not path.exists():
        raise HTTPException(404, "File expired")
    return FileResponse(path, filename="input.bin", media_type="application/octet-stream")
