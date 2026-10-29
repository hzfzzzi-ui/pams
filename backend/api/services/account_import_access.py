"""Opt-in, same-origin import session; never reuse a broker credential."""
import hashlib
import hmac
import os
import secrets
import time

from fastapi import HTTPException, Request

COOKIE = "pams_import_session"
TTL = 1800


def configured() -> bool:
    return len(os.environ.get("PAMS_IMPORT_ACCESS_TOKEN", "")) >= 24


def signed_session() -> str:
    value = str(int(time.time()) + TTL) + "." + secrets.token_hex(16)
    key = os.environ["PAMS_IMPORT_ACCESS_TOKEN"].encode()
    return value + "." + hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def authenticated(request: Request) -> bool:
    if not configured():
        return False
    token = request.cookies.get(COOKIE, "")
    try:
        expires, nonce, signature = token.split(".")
        payload = expires + "." + nonce
        key = os.environ["PAMS_IMPORT_ACCESS_TOKEN"].encode()
        expected = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        return int(time.time()) < int(expires) <= int(time.time()) + TTL and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def same_origin(request: Request):
    origin = request.headers.get("origin", "")
    expected = str(request.base_url).rstrip("/")
    if origin != expected:
        raise HTTPException(403, detail={"code": "same_origin_required", "message": "导入操作仅允许同源请求"})


def require_access(request: Request):
    if not authenticated(request):
        raise HTTPException(401, detail={"code": "import_locked", "message": "请先解锁账户导入"})
