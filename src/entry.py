"""Cloudflare Worker entrypoint: only the runtime glue lives here."""

from __future__ import annotations

from workers import WorkerEntrypoint

from api.app import app
from tasks.cron import run_cron

# Cloudflare geo fields re-emitted as request headers. Country is also sent by
# Cloudflare itself as ``cf-ipcountry``; city and region exist only inside
# ``request.cf``, which the ASGI adapter does not forward into the scope.
_GEO_HEADERS = (
    ("x-cf-country", "country"),
    ("x-cf-region", "region"),
    ("x-cf-city", "city"),
    ("x-cf-colo", "colo"),
)


def _with_geo_headers(request):
    """The same request with ``request.cf`` geo values added as headers.

    ``workers.asgi`` builds the ASGI scope from method/path/headers only, so
    ``request.cf`` is invisible inside FastAPI. Cloning the Request with four
    extra headers is the narrowest bridge that keeps the app framework-agnostic.

    Anything unexpected here must not break the site: the original request is
    passed through, and the API still reads country from ``cf-ipcountry`` —
    city and region are then simply absent.
    """
    try:
        cf = request.cf
    except Exception:
        return request.js_object
    if cf is None:
        return request.js_object
    try:
        import js

        headers = js.Headers.new(request.js_object.headers)
        for header, field in _GEO_HEADERS:
            value = getattr(cf, field, None)
            if value:
                headers.set(header, str(value))
        init = js.Object.new()
        init.headers = headers
        return js.Request.new(request.js_object, init)
    except Exception:
        return request.js_object


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        import asgi

        return await asgi.fetch(app, _with_geo_headers(request), self.env)

    async def scheduled(self, controller, env, ctx):
        # The method name is fixed by the Workers runtime (like ``fetch``);
        # the job logic lives in tasks/cron.py.
        return await run_cron(self.env, getattr(controller, "cron", "") or "cron")
