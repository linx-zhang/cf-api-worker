"""FastAPI application: middleware wiring and router registration."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

from api.responses import install_error_handlers, success
from api.routers import activity, admin, auth, clips, comments, memos, posts


class Preflight204CORSMiddleware(CORSMiddleware):
    """Starlette answers preflight with 200; this API returns an empty 204."""

    def preflight_response(self, request_headers):
        response = super().preflight_response(request_headers)
        if response.status_code == 200:
            return Response(status_code=204, headers=dict(response.headers))
        return response


app = FastAPI(title="api-worker")

app.add_middleware(
    Preflight204CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=86400,
)

# 业务接口统一在 /api 下。根路径留给平台自己的东西：/ 是健康检查，/docs 与
# /openapi.json 是 FastAPI 自带的，以后若要加 /rss、/og 这类非 API 端点也不打架。
# 前端的路径常量集中在 src/api/client.ts 的 API_ROOT，两边靠 check-contract 之外的
# 约定对齐：改这里必须同步改前端与 docs/api.md。
API_PREFIX = "/api"

api = APIRouter(prefix=API_PREFIX)

api.include_router(auth.router, prefix="/auth", tags=["auth"])
api.include_router(posts.router, prefix="/posts", tags=["posts"])
api.include_router(comments.router, prefix="/posts", tags=["comments"])
api.include_router(comments.delete_router, prefix="/comments", tags=["comments"])
api.include_router(admin.router, prefix="/admin", tags=["admin"])
api.include_router(activity.router, prefix="/activity", tags=["activity"])
api.include_router(memos.router, prefix="/memos", tags=["memos"])
api.include_router(clips.router, prefix="/clips", tags=["clips"])

app.include_router(api)

install_error_handlers(app)


@app.get("/")
async def root():
    return success({"service": "api-worker", "status": "ok"})
