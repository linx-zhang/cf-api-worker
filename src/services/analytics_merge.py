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
                "first_seen": "2026-11-14 01:00:00",
                "last_seen": "2026-11-19 02:00:00",
                "days": {
                    "2026-11-19": {
                        "page_views": 5, "dwell_ms": 30000,
                        "first_seen": "2026-11-19 01:00:00",
                        "last_seen": "2026-11-19 02:00:00",
                        "paths": {"/wiki": [5, 30000]},
                    },
                },
            },
        },
    }

Two shapes here are deliberate:

* ``daily`` keeps the per-day actor **key list** because the API reports
  *distinct* actors per day, and distinct counts cannot be summed — the set has
  to be carried.
* Each actor keeps a **per-day** breakdown rather than one running total. The
  page offers a 7-day and a 30-day window over the same 30-day ledger; a single
  running total would make the 7-day actor numbers silently report 30-day
  figures. Per-day buckets are the smallest thing that can answer both windows.

``paths`` inside a day is ``{path: [views, dwell_ms]}`` — a list, not a dict,
because these records are rewritten on every fold and short keys dominate the
size of a payload that lives in a single D1 row (2 MB ceiling).

Tail shape (rows already ``GROUP BY``-ed by SQL)::

    {
        "max_id": 4230,
        "day_rows":   [{"day", "ident", "page_views", "dwell_ms",
                        "first_seen", "last_seen"}],
        "actor_rows": [{"ident", "user_id", "user_email", "ip", "country",
                        "region", "city", "first_seen", "last_seen"}],
        "path_rows":  [{"day", "ident", "path", "views", "dwell_ms"}],
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


def _day_bucket(actor: dict, day: str) -> dict:
    """The actor's bucket for one day, created empty if missing."""
    days = actor.setdefault("days", {})
    bucket = days.get(day)
    if bucket is None:
        bucket = {
            "page_views": 0,
            "dwell_ms": 0,
            "first_seen": None,
            "last_seen": None,
            "paths": {},
        }
        days[day] = bucket
    return bucket


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
        ident = row["ident"]
        bucket = daily.setdefault(day, {"page_views": 0, "actors": []})
        bucket["page_views"] += _int(row.get("page_views"))
        if ident not in bucket["actors"]:
            bucket["actors"].append(ident)

        target = actors.get(ident)
        if target is None:
            # day_rows carry no profile, so the bucket is created here and filled
            # when the matching actor_rows arrive; an actor with no profile row
            # is dangling data and is dropped by ``_trim``.
            target = _new_actor()
            actors[ident] = target
        day_bucket = _day_bucket(target, day)
        day_bucket["page_views"] += _int(row.get("page_views"))
        day_bucket["dwell_ms"] += _int(row.get("dwell_ms"))
        day_bucket["first_seen"] = _earlier(day_bucket["first_seen"], row.get("first_seen"))
        day_bucket["last_seen"] = _later(day_bucket["last_seen"], row.get("last_seen"))

    for row in tail.get("actor_rows") or []:
        ident = row["ident"]
        target = actors.get(ident)
        if target is None:
            target = _new_actor()
            actors[ident] = target
        # Later rows can fill in a name that was unknown when the actor was first
        # seen; MAX() in the old SQL did the same.
        target["user_id"] = _int(row.get("user_id")) or target.get("user_id")
        target["user_email"] = row.get("user_email") or target.get("user_email")
        target["ip"] = row.get("ip") or target.get("ip")
        target["country"] = _later(target.get("country"), row.get("country"))
        target["region"] = _later(target.get("region"), row.get("region"))
        target["city"] = _later(target.get("city"), row.get("city"))
        target["first_seen"] = _earlier(target.get("first_seen"), row.get("first_seen"))
        target["last_seen"] = _later(target.get("last_seen"), row.get("last_seen"))

    for row in tail.get("path_rows") or []:
        day = row["day"]
        if day < first_day or day > today:
            continue
        ident = row["ident"]
        target = actors.get(ident)
        if target is None:
            # A path row without its day row would be dangling data; skip it
            # rather than invent an actor with no identity.
            continue
        paths = _day_bucket(target, day)["paths"]
        entry = paths.get(row["path"])
        if entry is None:
            paths[row["path"]] = [_int(row.get("views")), _int(row.get("dwell_ms"))]
        else:
            entry[0] += _int(row.get("views"))
            entry[1] += _int(row.get("dwell_ms"))

    return _trim(
        {"cursor": cursor, "daily": daily, "actors": actors},
        today=today,
        window_days=window_days,
        max_stored_actors=max_stored_actors,
    )


def _new_actor() -> dict:
    """An empty actor record: profile fields still unknown, per-day map empty."""
    return {
        "user_id": None,
        "user_email": None,
        "ip": None,
        "country": None,
        "region": None,
        "city": None,
        "first_seen": None,
        "last_seen": None,
        "days": {},
    }


def _trim(ledger: dict, *, today: str, window_days: int, max_stored_actors: int) -> dict:
    """Drop days outside the window and cap the stored actor set."""
    first_day = window_start(today, window_days)
    daily = {
        day: bucket
        for day, bucket in ledger["daily"].items()
        if first_day <= day <= today
    }

    actors: dict = {}
    for ident, actor in ledger["actors"].items():
        days = {
            day: bucket
            for day, bucket in (actor.get("days") or {}).items()
            if first_day <= day <= today
        }
        if not days:
            # Nothing left inside the window: the actor has aged out entirely.
            continue
        record = dict(actor)
        record["days"] = days
        actors[ident] = record

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
    """Copy the actor records through their nested day and path buckets."""
    copied: dict = {}
    for ident, actor in actors.items():
        record = dict(actor)
        record["days"] = {
            day: {
                "page_views": _int(bucket.get("page_views")),
                "dwell_ms": _int(bucket.get("dwell_ms")),
                "first_seen": bucket.get("first_seen"),
                "last_seen": bucket.get("last_seen"),
                "paths": {path: list(entry) for path, entry in (bucket.get("paths") or {}).items()},
            }
            for day, bucket in (actor.get("days") or {}).items()
        }
        copied[ident] = record
    return copied


def _window_days(actor: dict, first_day: str, today: str) -> list[dict]:
    """The actor's day buckets inside ``[first_day, today]``."""
    return [
        bucket
        for day, bucket in (actor.get("days") or {}).items()
        if first_day <= day <= today
    ]


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

    Actor totals are **summed over the requested window**, so the 7-day and
    30-day views of the same ledger report different (correct) numbers.
    Ordering and caps match the SQL version: series by date, actors by
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

    ordered = [
        (ident, actor)
        for ident, actor in ledger["actors"].items()
        if _window_days(actor, first_day, today)
        # Ties on ``last_seen`` break by identity, matching the reference
        # implementation's ``ORDER BY last_seen DESC, ident ASC``.
    ]
    ordered.sort(key=lambda pair: pair[0])
    ordered.sort(key=lambda pair: _last_seen(pair[1]) or "", reverse=True)

    rows = ordered[: max(0, int(max_actors))]
    has_next = len(ordered) > len(rows)

    payload_actors = []
    path_budget = max(0, int(max_path_rows))
    for ident, actor in rows:
        buckets = _window_days(actor, first_day, today)
        paths: dict[str, list[int]] = {}
        for bucket in buckets:
            for path, entry in (bucket.get("paths") or {}).items():
                agg = paths.get(path)
                if agg is None:
                    paths[path] = [int(entry[0]), int(entry[1])]
                else:
                    agg[0] += int(entry[0])
                    agg[1] += int(entry[1])
        path_items = [
            {"path": path, "views": entry[0], "dwell_ms": entry[1]}
            for path, entry in paths.items()
        ]
        # dwell 降序、views 降序、path 升序：最后一项是确定性的平局裁决
        path_items.sort(key=lambda item: (-item["dwell_ms"], -item["views"], item["path"]))
        kept = path_items[: max(0, int(max_paths_per_actor))]
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
                "page_views": sum(_int(bucket.get("page_views")) for bucket in buckets),
                "dwell_ms": sum(_int(bucket.get("dwell_ms")) for bucket in buckets),
                "first_seen": _first_seen(buckets),
                "last_seen": _last_seen(actor),
                "paths": kept,
            }
        )

    return {
        "series": series,
        "actors": payload_actors,
        "has_next": has_next,
        "cursor": int(ledger.get("cursor") or 0),
    }


def _first_seen(buckets: list[dict]) -> str | None:
    """Earliest in-window event of this actor (day buckets carry their own min)."""
    seen: str | None = None
    for bucket in buckets:
        seen = _earlier(seen, bucket.get("first_seen"))
    return seen


def _last_seen(actor: dict) -> str | None:
    """Latest stored event of this actor; day buckets and the record agree."""
    seen = actor.get("last_seen")
    for bucket in (actor.get("days") or {}).values():
        seen = _later(seen, bucket.get("last_seen"))
    return seen
