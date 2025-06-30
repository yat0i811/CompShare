from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response as StarletteResponse, JSONResponse
import time
from core.config import settings
from utils.security import get_client_ip

# 1GB
MAX_SIZE_EXTERNAL = 1024 * 1024 * 1024
# 60秒
RATE_LIMIT_SECONDS = 60

# グローバル変数としてのupload_timesはmain.pyから移動させるか、より良い状態管理方法を検討
# ここでは一旦、このファイルスコープで定義するが、アプリケーションインスタンスやRedis等で管理する方が望ましい
upload_times = {}

class ConditionalUploadLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        content_length = int(request.headers.get("content-length", 0))
        host = request.headers.get("host", "")
        client_ip = get_client_ip(request)

        # ローカルホストまたはローカルIPからのアクセスは制限をスキップ
        if host.startswith("localhost") or host.startswith("127.0.0.1") or client_ip.startswith("127."):
            return await call_next(request)

        if content_length > MAX_SIZE_EXTERNAL:
            return StarletteResponse(f"Request too large ({MAX_SIZE_EXTERNAL / (1024*1024)}MB max via external access)", status_code=413)

        return await call_next(request)

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        # Rate limit only for specific paths
        # Determine target paths (e.g., /upload, /compress and /register only)
        # Consider reading from settings or hardcoding
        # Here we target /upload/, /compress/async/ and /auth/register
        if request.url.path not in ["/upload/", "/compress/async/", "/auth/register"]:
            return await call_next(request)

        client_id = get_client_ip(request) # Simple IP-based limiting
        current_time = time.time()

        # Remove old entries
        upload_times[client_id] = [t for t in upload_times.get(client_id, []) if current_time - t < RATE_LIMIT_SECONDS]

        print(f"Client ID: {client_id}, Upload count: {len(upload_times.get(client_id, []))}")
        if len(upload_times.get(client_id, [])) >= 3:
            # Allow only 1 upload per RATE_LIMIT_SECONDS for simplicity
            response = JSONResponse(
                {"detail": f"Rate limit exceeded. Please wait {RATE_LIMIT_SECONDS} seconds before uploading again."},
                status_code=429
            )
            # Add CORS headers to the 429 response
            origin = request.headers.get("origin")
            if origin and origin in settings.CORS_ALLOWED_ORIGINS:
                response.headers["Access-Control-Allow-Origin"] = origin
            return response

        upload_times.setdefault(client_id, []).append(current_time)

        try:
            response = await call_next(request)
            return response
        except Exception as ex:
            # Remove time on failure? Depends on desired behavior
            raise ex 