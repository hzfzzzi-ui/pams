"""Same-origin protection for imports within the private PAMS application."""
from fastapi import HTTPException, Request


def same_origin(request: Request):
    origin = request.headers.get("origin", "")
    expected = str(request.base_url).rstrip("/")
    if origin != expected:
        raise HTTPException(403, detail={"code": "same_origin_required", "message": "导入操作仅允许同源请求"})
