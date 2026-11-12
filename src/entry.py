"""Cloudflare Worker entrypoint: only the runtime glue lives here."""

from __future__ import annotations

from workers import WorkerEntrypoint

from api.app import app
from tasks.cron import run_cron


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        import asgi

        return await asgi.fetch(app, request.js_object, self.env)

    async def scheduled(self, controller, env, ctx):
        # The method name is fixed by the Workers runtime (like ``fetch``);
        # the job logic lives in tasks/cron.py.
        return await run_cron(self.env, getattr(controller, "cron", "") or "cron")
