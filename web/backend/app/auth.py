"""Auth routes — MTProto login: phone → OTP → 2FA password."""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from . import config, db, mtproto, security

router = APIRouter(prefix="/auth", tags=["auth"])


class PhoneIn(BaseModel):
    phone: str


class CodeIn(BaseModel):
    phone: str
    code: str


class PasswordIn(BaseModel):
    phone: str
    password: str


def current_user(authorization: str = Header(default="")) -> int:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    user_id = security.verify_token(authorization[7:].strip())
    if user_id is None:
        raise HTTPException(401, "Invalid or expired token")
    return user_id


@router.post("/send-code")
async def send_code(body: PhoneIn):
    return await mtproto.send_code(body.phone.strip())


@router.post("/verify-code")
async def verify_code(body: CodeIn):
    return await mtproto.verify_code(body.phone.strip(), body.code.strip())


@router.post("/check-password")
async def check_password(body: PasswordIn):
    """2FA password screen — only called when verify-code returned password_required."""
    return await mtproto.check_password(body.phone.strip(), body.password)


@router.post("/logout")
async def logout(user_id: int = Depends(current_user)):
    await mtproto.logout(user_id)
    return {"ok": True}


@router.get("/me")
async def me(user_id: int = Depends(current_user)):
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(401, "User not found")
    is_admin = bool(user.get("is_admin")) or bool(user_id in config.get_admin_ids())
    return {
        "id": user["user_id"],
        "name": user["name"],
        "username": user["username"],
        "phone": user["phone"],
        "is_admin": is_admin,
        "usage_today": db.get_usage(user_id),
    }


def require_admin(user_id: int = Depends(current_user)) -> int:
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(401, "User not found")
    if not (user.get("is_admin") or user_id in config.get_admin_ids()):
        raise HTTPException(403, "Admins only")
    return user_id
