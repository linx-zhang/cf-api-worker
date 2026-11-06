import asyncio
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from workers import WorkerEntrypoint

from submodule import get_hello_message

app = FastAPI()


@app.get("/")
async def root():
    return PlainTextResponse(get_hello_message())


@app.get("/hello")
async def hello():
    await asyncio.sleep(1)
    return PlainTextResponse("H~~~~~~~~~~~~")


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        import asgi

        return await asgi.fetch(app, request.js_object, self.env)
