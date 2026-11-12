"""Cloudflare Worker entrypoint: only the runtime glue lives here."""

from __future__ import annotations

from workers import WorkerEntrypoint

from api.app import app
from infra.activity_retention import ACTIVITY_RETENTION_DAYS, purge_old_activity
from infra.db import Database


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        import asgi

        return await asgi.fetch(app, request.js_object, self.env)

    async def scheduled(self, controller, env, ctx):
        """Daily retention sweep for ``activity_events`` (cron lives in wrangler.jsonc)."""
        # Bindings come from the entrypoint instance: the ``env`` argument is not
        # guaranteed to be populated on every scheduled invocation.
        cron = getattr(controller, "cron", "scheduled")
        deleted = await purge_old_activity(
            Database(self.env.DB), days=ACTIVITY_RETENTION_DAYS
        )
        print(
            f"activity retention ({cron}): "
            f"removed {deleted} rows older than {ACTIVITY_RETENTION_DAYS} days"
        )
