"""Incremental merge for the analytics ledger.

The admin overview used to re-read every ``activity_events`` row on each request
(five full scans per call), which is billed per *row scanned* and grows with the
event log. This module is the pure core of the replacement: keep one small
ledger, fold only the rows that arrived since the last cursor into it, and let
the router render whatever shape it needs from that ledger.

Nothing here touches the database or reads the clock. ``today`` is passed in so
the window arithmetic is testable without freezing time, and every function
returns a new structure instead of mutating its input — the request path folds a
tail into an in-memory copy and never writes back, which keeps concurrent
readers free of races.

Ledger shape::

    {
        "cursor": 4210,
        "daily": {
            "2026-11-19": {"page_views": 12, "actors": ["u:8", "ip:1.2.3.4"]},
        },
        "actors": {
            "ip:1.2.3.4": {
                "user_id": None, "user_email": None, "ip": "1.2.3.4",
                "country": "SG", "region": "Central", "city": "Singapore",
                "page_views": 12, "dwell_ms": 84000,
                "first_seen": "2026-11-19 01:00:00",
                "last_seen": "2026-11-19 02:00:00",
                "paths": {"/wiki": {"views": 5, "dwell_ms": 30000,
                                    "last_seen": "2026-11-19 02:00:00"}},
            },
        },
    }

``daily`` keeps the per-day actor key list because the API reports *distinct*
actors per day, and distinct counts cannot be summed — the set has to be carried.
The list is small (roughly one key per actor per day) and is trimmed with the
window.

Tail shape (rows already ``GROUP BY``-ed by SQL)::

    {
        "max_id": 4230,
        "day_rows":   [{"day", "ident", "page_views"}],
        "actor_rows": [{"ident", "user_id", "user_email", "ip", "country",
                        "region", "city", "page_views", "dwell_ms",
                        "first_seen", "last_seen"}],
        "path_rows":  [{"ident", "path", "views", "dwell_ms", "last_seen"}],
    }
"""

from __future__ import annotations

from datetime import date, timedelta

# Stored actors are capped so a long-lived ledger cannot creep towards D1's 2 MB
# row limit; the ledger is a report cache, not an audit log.
MAX_STORED_ACTORS = 200

# Response caps, mirroring the constants the SQL version used.
MAX_ACTORS = 60
MAX_PATHS_PER_ACTOR = 8
MAX_PATH_ROWS = 400


def empty_ledger() -> dict:
    """A ledger with nothing in it and a cursor at zero."""
    return {"cursor": 0, "daily": {}, "actors": {}}


def _int(value) -> int:
    """SQL NULL and Python ``None`` both mean zero here."""
    return int(value or 0)


def _later(left: str | None, right: str | None) -> str | None:
    """Lexicographic max, matching SQL ``MAX()`` on 'YYYY-MM-DD HH:MM:SS'."""
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _earlier(left: str | None, right: str | None) -> str | None:
    """Lexicographic min, matching SQL ``MIN()``."""
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def window_start(today: str, window_days: int) -> str:
    """First day (inclusive) of the rolling window ending at ``today``."""
    end = date.fromisoformat(today)
    return (end - timedelta(days=max(1, int(window_days)) - 1)).isoformat()


def merge_tail(
    ledger: dict,
    tail: dict,
    *,
    today: str,
    window_days: int,
    max_stored_actors: int = MAX_STORED_ACTORS,
) -> dict:
    """Fold ``tail`` into ``ledger`` and return a new ledger.

    Folding is by cursor, never by deduplication: a tail delivered twice would be
    counted twice. The caller owns that guarantee by reading ``id > cursor`` and
    by advancing the cursor to ``tail["max_id"]`` exactly when it persists.
    """
    cursor = max(int(ledger.get("cursor") or 0), _int(tail.get("max_id")))
    daily = _copy_daily(ledger.get("daily") or {})
    actors = _copy_actors(ledger.get("actors") or {})

    first_day = window_start(today, window_days)

    for row in tail.get("day_rows") or []:
        day = row["day"]
        if day < first_day or day > today:
            continue
        bucket = daily.setdefault(day, {"page_views": 0, "actors": []})
        bucket["page_views"] += _int(row.get("page_views"))
        ident = row["ident"]
        if ident not in bucket["actors"]:
            bucket["actors"].append(ident)

    for row in tail.get("actor_rows") or []:
        ident = row["ident"]
        target = actors.get(ident)
        if target is None:
            target = {
                "user_id": row.get("user_id"),
                "user_email": row.get("user_email"),
                "ip": row.get("ip"),
                "country": row.get("country"),
                "region": row.get("region"),
                "city": row.get("city"),
                "page_views": 0,
                "dwell_ms": 0,
                "first_seen": row.get("first_seen"),
                "last_seen": row.get("last_seen"),
                "paths": {},
            }
            actors[ident] = target
        # Later rows can fill in a name that was unknown when the actor was
        # first seen, and can move the geo fields forward; MAX() in the old SQL
        # did the same.
        target["user_id"] = _int(row.get("user_id")) or target.get("user_id")
        target["user_email"] = row.get("user_email") or target.get("user_email")
        target["ip"] = row.get("ip") or target.get("ip")
        target["country"] = _later(target.get("country"), row.get("country"))
        target["region"] = _later(target.get("region"), row.get("region"))
        target["city"] = _later(target.get("city"), row.get("city"))
        target["page_views"] = _int(target.get("page_views")) + _int(row.get("page_views"))
        target["dwell_ms"] = _int(target.get("dwell_ms")) + _int(row.get("dwell_ms"))
        target["first_seen"] = _earlier(target.get("first_seen"), row.get("first_seen"))
        target["last_seen"] = _later(target.get("last_seen"), row.get("last_seen"))

    for row in tail.get("path_rows") or []:
        ident = row["ident"]
        target = actors.get(ident)
        if target is None:
            # A path row without its actor row would be dangling data; skip it
            # rather than invent an actor with no identity.
            continue
        path = row["path"]
        bucket = target["paths"].get(path)
        if bucket is None:
            bucket = {"views": 0, "dwell_ms": 0, "last_seen": row.get("last_seen")}
            target["paths"][path] = bucket
        bucket["views"] = _int(bucket.get("views")) + _int(row.get("views"))
        bucket["dwell_ms"] = _int(bucket.get("dwell_ms")) + _int(row.get("dwell_ms"))
        bucket["last_seen"] = _later(bucket.get("last_seen"), row.get("last_seen"))

    return _trim(
        {"cursor": cursor, "daily": daily, "actors": actors},
        today=today,
        window_days=window_days,
        max_stored_actors=max_stored_actors,
    )


def _trim(ledger: dict, *, today: str, window_days: int, max_stored_actors: int) -> dict:
    """Drop days outside the window and cap the stored actor set."""
    first_day = window_start(today, window_days)
    daily = {
        day: bucket
        for day, bucket in ledger["daily"].items()
        if first_day <= day <= today
    }

    # Only actors seen inside the window are worth keeping; an identity whose
    # rows have all aged out would otherwise sit in the ledger forever.
    live = [
        ident
        for ident, actor in ledger["actors"].items()
        if (actor.get("last_seen") or "") >= f"{first_day} 00:00:00"
    ]
    actors = {ident: ledger["actors"][ident] for ident in live}
    if max_stored_actors > 0 and len(actors) > max_stored_actors:
        keep = sorted(
            actors.keys(),
            key=lambda key: actors[key].get("last_seen") or "",
            reverse=True,
        )[:max_stored_actors]
        actors = {key: actors[key] for key in keep}
        # 被挤掉的是「明细」，不是「来过」这个事实：daily 里的 key 一律保留，
        # 否则一个流量日就能让历史访客数凭空变小（和全量扫描的参考实现不一致）。

    return {"cursor": ledger["cursor"], "daily": daily, "actors": actors}


def _copy_daily(daily: dict) -> dict:
    """Shallow-copy the day buckets and their actor lists (lists are mutated)."""
    return {
        day: {"page_views": _int(bucket.get("page_views")), "actors": list(bucket.get("actors") or [])}
        for day, bucket in daily.items()
    }


def _copy_actors(actors: dict) -> dict:
    """Copy the actor records and their nested path buckets."""
    copied: dict = {}
    for ident, actor in actors.items():
        record = dict(actor)
        record["paths"] = {path: dict(bucket) for path, bucket in (actor.get("paths") or {}).items()}
        copied[ident] = record
    return copied


def render_summary(
    ledger: dict,
    *,
    today: str,
    window_days: int,
    max_actors: int = MAX_ACTORS,
    max_paths_per_actor: int = MAX_PATHS_PER_ACTOR,
    max_path_rows: int = MAX_PATH_ROWS,
) -> dict:
    """Shape the ledger into the payload the admin page consumes.

    The same ordering and caps the SQL used: series by date, actors by
    ``last_seen`` descending, paths by dwell then views, with one row past the
    actor cap signalling truncation.
    """
    first_day = window_start(today, window_days)
    series = [
        {
            "date": day,
            "page_views": _int(bucket["page_views"]),
            "actors": len(bucket["actors"]),
        }
        for day, bucket in sorted(ledger["daily"].items())
        if first_day <= day <= today
    ]

    # Ties on ``last_seen`` are broken by identity, matching the reference
    # implementation's ``ORDER BY last_seen DESC, ident ASC``: without a
    # tie-breaker the actor list order depends on dict insertion order and the
    # two paths diverge on equal timestamps.
    ordered = [
        (ident, actor)
        for ident, actor in ledger["actors"].items()
        if (actor.get("last_seen") or "") >= f"{first_day} 00:00:00"
    ]
    ordered.sort(key=lambda pair: pair[0])
    ordered.sort(key=lambda pair: pair[1].get("last_seen") or "", reverse=True)

    rows = ordered[: max(0, int(max_actors))]
    has_next = len(ordered) > len(rows)

    payload_actors = []
    path_budget = max(0, int(max_path_rows))
    for ident, actor in rows:
        paths = [
            {
                "path": path,
                "views": _int(bucket.get("views")),
                "dwell_ms": _int(bucket.get("dwell_ms")),
            }
            for path, bucket in (actor.get("paths") or {}).items()
        ]
        # dwell 降序、views 降序、path 升序：最后一项是确定性的平局裁决
        paths.sort(key=lambda item: (-item["dwell_ms"], -item["views"], item["path"]))
        kept = paths[: max(0, int(max_paths_per_actor))]
        if path_budget:
            kept = kept[:path_budget]
            path_budget -= len(kept)
        else:
            kept = []
        payload_actors.append(
            {
                "key": ident,
                "user_id": actor.get("user_id"),
                "user_email": actor.get("user_email"),
                "ip": actor.get("ip"),
                "country": actor.get("country"),
                "region": actor.get("region"),
                "city": actor.get("city"),
                "page_views": _int(actor.get("page_views")),
                "dwell_ms": _int(actor.get("dwell_ms")),
                "first_seen": actor.get("first_seen"),
                "last_seen": actor.get("last_seen"),
                "paths": kept,
            }
        )

    return {
        "series": series,
        "actors": payload_actors,
        "has_next": has_next,
        "cursor": int(ledger.get("cursor") or 0),
    }
