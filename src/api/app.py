"""FastAPI application: middleware wiring and router registration."""

from __future__ import annotations

from fastapi import FastAPI
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

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(posts.router, prefix="/posts", tags=["posts"])
app.include_router(comments.router, prefix="/posts", tags=["comments"])
app.include_router(comments.delete_router, prefix="/comments", tags=["comments"])
app.include_router(admin.router, prefix="/admin", tags=["admin"])
app.include_router(activity.router, prefix="/activity", tags=["activity"])
app.include_router(memos.router, prefix="/memos", tags=["memos"])
app.include_router(clips.router, prefix="/clips", tags=["clips"])

install_error_handlers(app)


@app.get("/")
async def root():
    return success({"service": "api-worker", "status": "ok"})
