"""Cloudflare Worker entrypoint: only the runtime glue lives here."""

from __future__ import annotations

from workers import WorkerEntrypoint

from api.app import app


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        import asgi

        return await asgi.fetch(app, request.js_object, self.env)
