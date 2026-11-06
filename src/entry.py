from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from workers import WorkerEntrypoint

from submodule import get_hello_message


class NoteIn(BaseModel):
    title: str
    content: str = ""


class Preflight204CORSMiddleware(CORSMiddleware):
    """Starlette answers preflight with 200; this app wants an empty 204."""

    def preflight_response(self, request_headers):
        response = super().preflight_response(request_headers)
        if response.status_code == 200:
            return Response(status_code=204, headers=dict(response.headers))
        return response


app = FastAPI()

app.add_middleware(
    Preflight204CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=86400,
)


@app.get("/")
async def root():
    return PlainTextResponse(get_hello_message())


@app.get("/hi")
async def hello():
    return PlainTextResponse("Hi~~~")


@app.get("/notes")
async def list_notes(request: Request):
    db = request.scope["env"].DB
    result = await db.prepare(
        "SELECT id, title, content, created_at FROM notes ORDER BY id DESC LIMIT 100"
    ).all()
    return {"notes": result.results}


@app.post("/notes", status_code=201)
async def create_note(note: NoteIn, request: Request):
    db = request.scope["env"].DB
    row = (
        await db.prepare(
            "INSERT INTO notes (title, content) VALUES (?, ?)"
            " RETURNING id, title, content, created_at"
        )
        .bind(note.title, note.content)
        .first()
    )
    return row


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        import asgi

        return await asgi.fetch(app, request.js_object, self.env)
