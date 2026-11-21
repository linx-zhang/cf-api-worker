"""个人空间的日常消耗品：以周期为主，日常只需要点一次「用完了」。

两条贯穿全文件的约定：

* **不存派生状态**。``该买了/待补货/还剩几天`` 全部由「日期 + 周期 + lead_days」
  现算，库里没有一列会在零点以后自己变成错的。
* **不猜时区**。所有 ``*_on`` 都是本地日 ``YYYY-MM-DD``；客户端上送 ``today``，
  缺省才退回 UTC 今天（只影响提示文案，不影响任何写入）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from api.deps import get_current_user, get_db
from api.models import (
    ConsumableCycleOut,
    ConsumableIn,
    ConsumableOut,
    ConsumableUpdateIn,
    CycleFinishIn,
    CycleStartIn,
    CycleUpdateIn,
)
from api.responses import ApiError, success
from core.constants import (
    CODE_CONFLICT,
    CODE_INTERNAL_ERROR,
    CODE_NOT_FOUND,
    CODE_VALIDATION_ERROR,
    DETAIL_CONSUMABLE_CREATE_FAILED,
    DETAIL_CONSUMABLE_CYCLE_NOT_FOUND,
    DETAIL_CONSUMABLE_CYCLE_OPEN,
    DETAIL_CONSUMABLE_DATE_RANGE,
    DETAIL_CONSUMABLE_DELETED,
    DETAIL_CONSUMABLE_NO_OPEN_CYCLE,
    DETAIL_CONSUMABLE_NOT_FOUND,
    DETAIL_CONSUMABLE_REOPEN_CONFLICT,
    DETAIL_VALIDATION_ERROR,
)
from infra.db import Database

router = APIRouter()

_ITEM_COLUMNS = (
    "i.id, i.name, i.category, i.unit, i.quantity, i.lead_days, i.note, "
    "i.created_at, i.updated_at"
)
_ITEM_SELECT = f"SELECT {_ITEM_COLUMNS} FROM consumable_items i"
_CYCLE_COLUMNS = (
    "c.id, c.item_id, c.started_on, c.ended_on, c.expected_days, c.note, "
    "c.created_at, c.updated_at"
)


def _parse_day(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _today(today: str | None) -> date:
    """客户端本地今天优先；格式不对或没传才用服务端 UTC 今天。"""
    parsed = _parse_day(today)
    return parsed if parsed is not None else date.today()


async def _get_item(db: Database, item_id: int, user_id: int) -> dict:
    item = await db.fetch_one(
        _ITEM_SELECT + " WHERE i.id = ? AND i.user_id = ? AND i.deleted_at IS NULL",
        (item_id, user_id),
    )
    if item is None:
        raise ApiError(
            CODE_NOT_FOUND, DETAIL_CONSUMABLE_NOT_FOUND, status.HTTP_404_NOT_FOUND
        )
    return item


async def _get_open_cycle(db: Database, item_id: int) -> dict | None:
    return await db.fetch_one(
        f"SELECT {_CYCLE_COLUMNS} FROM consumable_cycles c "
        "WHERE c.item_id = ? AND c.ended_on IS NULL AND c.deleted_at IS NULL",
        (item_id,),
    )


async def _get_cycle(db: Database, item_id: int, cycle_id: int) -> dict:
    cycle = await db.fetch_one(
        f"SELECT {_CYCLE_COLUMNS} FROM consumable_cycles c "
        "WHERE c.id = ? AND c.item_id = ? AND c.deleted_at IS NULL",
        (cycle_id, item_id),
    )
    if cycle is None:
        raise ApiError(
            CODE_NOT_FOUND,
            DETAIL_CONSUMABLE_CYCLE_NOT_FOUND,
            status.HTTP_404_NOT_FOUND,
        )
    return cycle


def _actual_days(cycle: dict) -> int | None:
    start = _parse_day(cycle.get("started_on"))
    end = _parse_day(cycle.get("ended_on"))
    if start is None or end is None:
        return None
    return (end - start).days


async def _open_cycles(db: Database, item_ids: list[int]) -> dict[int, dict]:
    if not item_ids:
        return {}
    placeholders = ", ".join("?" for _ in item_ids)
    rows = await db.fetch_all(
        f"SELECT {_CYCLE_COLUMNS} FROM consumable_cycles c "
        f"WHERE c.item_id IN ({placeholders}) "
        "AND c.ended_on IS NULL AND c.deleted_at IS NULL",
        item_ids,
    )
    return {int(row["item_id"]): row for row in rows}


async def _recent_cycles(db: Database, item_ids: list[int]) -> dict[int, list[dict]]:
    """每个物品最近两轮已完成的周期，用来算「实际多久用完」。"""
    if not item_ids:
        return {}
    placeholders = ", ".join("?" for _ in item_ids)
    rows = await db.fetch_all(
        f"SELECT {_CYCLE_COLUMNS} FROM consumable_cycles c "
        f"WHERE c.item_id IN ({placeholders}) "
        "AND c.ended_on IS NOT NULL AND c.deleted_at IS NULL "
        "ORDER BY c.ended_on DESC, c.id DESC",
        item_ids,
    )
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        item_id = int(row["item_id"])
        bucket = grouped.setdefault(item_id, [])
        if len(bucket) < 2:
            bucket.append(row)
    return grouped


def _derive(
    item: dict,
    cycle: dict | None,
    recent: list[dict],
    today: date,
) -> ConsumableOut:
    actuals = [d for d in (_actual_days(row) for row in recent) if d is not None]
    last_days = actuals[0] if actuals else None
    suggested = round(sum(actuals) / len(actuals)) if actuals else None

    days_used: int | None = None
    days_left: int | None = None
    due_on: str | None = None
    progress: float | None = None
    status: str = "idle"

    if cycle is not None:
        start = _parse_day(cycle.get("started_on"))
        if start is not None:
            days_used = max((today - start).days, 0)
        expected = cycle.get("expected_days")
        if start is not None and expected:
            days_left = expected - days_used
            due_on = (start + timedelta(days=expected)).isoformat()
            progress = min(days_used / expected, 1.0)
            lead = int(item["lead_days"])
            if days_left <= lead:
                status = "due"
            elif days_left <= lead * 2:
                status = "soon"
            else:
                status = "ok"
        else:
            status = "unknown"

    return ConsumableOut(
        id=int(item["id"]),
        name=item["name"],
        category=item["category"],
        unit=item.get("unit"),
        quantity=item.get("quantity"),
        lead_days=int(item["lead_days"]),
        note=item.get("note"),
        created_at=item["created_at"],
        updated_at=item["updated_at"],
        cycle=(
            ConsumableCycleOut(
                id=int(cycle["id"]),
                started_on=cycle["started_on"],
                ended_on=cycle.get("ended_on"),
                expected_days=cycle.get("expected_days"),
                note=cycle.get("note"),
                created_at=cycle["created_at"],
                updated_at=cycle["updated_at"],
                actual_days=_actual_days(cycle),
            )
            if cycle is not None
            else None
        ),
        days_used=days_used,
        days_left=days_left,
        due_on=due_on,
        progress=progress,
        status=status,
        last_days=last_days,
        suggested_days=suggested,
    )


async def _load_one(db: Database, item_id: int, user_id: int, today: date):
    item = await _get_item(db, item_id, user_id)
    open_cycle = await _get_open_cycle(db, item_id)
    recent = (await _recent_cycles(db, [item_id])).get(item_id, [])
    return _derive(item, open_cycle, recent, today)


@router.get("")
async def list_consumables(
    today: str | None = Query(default=None, max_length=10),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """一次返回全部：一个人的消耗品通常十几件，分页只会让看板自己拼页。"""
    today_day = _today(today)
    items = await db.fetch_all(
        _ITEM_SELECT + " WHERE i.user_id = ? AND i.deleted_at IS NULL "
        "ORDER BY i.created_at DESC, i.id DESC",
        (user["id"],),
    )
    item_ids = [int(row["id"]) for row in items]
    open_map = await _open_cycles(db, item_ids)
    recent_map = await _recent_cycles(db, item_ids)
    return success(
        {
            "items": [
                _derive(row, open_map.get(int(row["id"])), recent_map.get(int(row["id"]), []), today_day)
                for row in items
            ]
        }
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_consumable(
    payload: ConsumableIn,
    today: str | None = Query(default=None, max_length=10),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    today_day = _today(today)
    started_on = payload.started_on or today_day.isoformat()
    item = await db.insert_returning(
        "INSERT INTO consumable_items "
        "(user_id, name, category, unit, quantity, lead_days, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (
            user["id"],
            payload.name,
            payload.category,
            payload.unit,
            payload.quantity,
            payload.lead_days,
            payload.note,
        ),
    )
    if item is None:
        raise ApiError(
            CODE_INTERNAL_ERROR,
            DETAIL_CONSUMABLE_CREATE_FAILED,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    item_id = int(item["id"])
    await db.execute(
        "INSERT INTO consumable_cycles (item_id, started_on, expected_days) "
        "VALUES (?, ?, ?)",
        (item_id, started_on, payload.expected_days),
    )
    return success(
        await _load_one(db, item_id, user["id"], today_day),
        status_code=status.HTTP_201_CREATED,
    )


@router.patch("/{item_id}")
async def update_consumable(
    item_id: int,
    payload: ConsumableUpdateIn,
    today: str | None = Query(default=None, max_length=10),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    await _get_item(db, item_id, user["id"])

    assignments: list[str] = []
    params: list[object] = []
    for field in ("name", "category", "lead_days"):
        if field in payload.model_fields_set:
            assignments.append(f"{field} = ?")
            params.append(getattr(payload, field))
    # 这三个允许显式置空（空单位、删掉备注、清掉数量）
    for field in ("unit", "quantity", "note"):
        if field in payload.model_fields_set:
            assignments.append(f"{field} = ?")
            params.append(getattr(payload, field))
    params.append(item_id)

    await db.execute(
        f"UPDATE consumable_items SET {', '.join(assignments)}, "
        "updated_at = datetime('now') WHERE id = ? AND deleted_at IS NULL",
        params,
    )
    return success(await _load_one(db, item_id, user["id"], _today(today)))


@router.delete("/{item_id}")
async def delete_consumable(
    item_id: int,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    await _get_item(db, item_id, user["id"])
    await db.execute(
        "UPDATE consumable_cycles SET deleted_at = datetime('now'), "
        "updated_at = datetime('now') WHERE item_id = ? AND deleted_at IS NULL",
        (item_id,),
    )
    await db.execute(
        "UPDATE consumable_items SET deleted_at = datetime('now'), "
        "updated_at = datetime('now') WHERE id = ? AND deleted_at IS NULL",
        (item_id,),
    )
    return success({"id": item_id}, detail=DETAIL_CONSUMABLE_DELETED)


@router.get("/{item_id}/cycles")
async def list_cycles(
    item_id: int,
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """周期历史：新的在前，包含进行中的那一轮。"""
    await _get_item(db, item_id, user["id"])
    rows = await db.fetch_all(
        f"SELECT {_CYCLE_COLUMNS} FROM consumable_cycles c "
        "WHERE c.item_id = ? AND c.deleted_at IS NULL "
        "ORDER BY c.started_on DESC, c.id DESC",
        (item_id,),
    )
    return success(
        {
            "items": [
                ConsumableCycleOut(
                    id=int(row["id"]),
                    started_on=row["started_on"],
                    ended_on=row.get("ended_on"),
                    expected_days=row.get("expected_days"),
                    note=row.get("note"),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    actual_days=_actual_days(row),
                ).model_dump()
                for row in rows
            ]
        }
    )


@router.post("/{item_id}/cycles", status_code=status.HTTP_201_CREATED)
async def start_cycle(
    item_id: int,
    payload: CycleStartIn,
    today: str | None = Query(default=None, max_length=10),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """补货：开新一轮。已有进行中周期时拒绝，避免一个物品两轮并行。"""
    await _get_item(db, item_id, user["id"])
    if await _get_open_cycle(db, item_id) is not None:
        raise ApiError(
            CODE_CONFLICT, DETAIL_CONSUMABLE_CYCLE_OPEN, status.HTTP_409_CONFLICT
        )
    await db.execute(
        "INSERT INTO consumable_cycles (item_id, started_on, expected_days, note) "
        "VALUES (?, ?, ?, ?)",
        (item_id, payload.started_on, payload.expected_days, payload.note),
    )
    await db.execute(
        "UPDATE consumable_items SET updated_at = datetime('now') WHERE id = ?",
        (item_id,),
    )
    return success(
        await _load_one(db, item_id, user["id"], _today(today)),
        status_code=status.HTTP_201_CREATED,
    )


@router.post("/{item_id}/finish")
async def finish_cycle(
    item_id: int,
    payload: CycleFinishIn,
    today: str | None = Query(default=None, max_length=10),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """「用完了」：关掉进行中的那一轮。不开下一轮就是「先不买」。"""
    await _get_item(db, item_id, user["id"])
    cycle = await _get_open_cycle(db, item_id)
    if cycle is None:
        raise ApiError(
            CODE_CONFLICT,
            DETAIL_CONSUMABLE_NO_OPEN_CYCLE,
            status.HTTP_409_CONFLICT,
        )
    ended_on = payload.ended_on or date.today().isoformat()
    if ended_on < cycle["started_on"]:
        raise ApiError(
            CODE_VALIDATION_ERROR,
            DETAIL_CONSUMABLE_DATE_RANGE.format(reason="ended_on < started_on"),
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    await db.execute(
        "UPDATE consumable_cycles SET ended_on = ?, updated_at = datetime('now') "
        "WHERE id = ? AND ended_on IS NULL AND deleted_at IS NULL",
        (ended_on, cycle["id"]),
    )
    await db.execute(
        "UPDATE consumable_items SET updated_at = datetime('now') WHERE id = ?",
        (item_id,),
    )
    return success(await _load_one(db, item_id, user["id"], _today(today)))


@router.patch("/{item_id}/cycles/{cycle_id}")
async def update_cycle(
    item_id: int,
    cycle_id: int,
    payload: CycleUpdateIn,
    today: str | None = Query(default=None, max_length=10),
    user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> JSONResponse:
    """修正周期：改错日期、撤回「用完」、重设预计天数都走这里。"""
    await _get_item(db, item_id, user["id"])
    cycle = await _get_cycle(db, item_id, cycle_id)

    started_on = (
        payload.started_on
        if "started_on" in payload.model_fields_set
        else cycle["started_on"]
    )
    ended_on = (
        payload.ended_on
        if "ended_on" in payload.model_fields_set
        else cycle.get("ended_on")
    )
    if started_on is None:
        raise ApiError(
            CODE_VALIDATION_ERROR,
            DETAIL_VALIDATION_ERROR.format(reason="started_on is required"),
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if ended_on is not None and ended_on < started_on:
        raise ApiError(
            CODE_VALIDATION_ERROR,
            DETAIL_CONSUMABLE_DATE_RANGE.format(reason="ended_on < started_on"),
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    # 撤回「用完」等于重新打开这一轮；同一个物品不能同时有两轮开着
    if ended_on is None:
        other_open = await db.fetch_one(
            "SELECT c.id FROM consumable_cycles c "
            "WHERE c.item_id = ? AND c.ended_on IS NULL AND c.deleted_at IS NULL "
            "AND c.id <> ?",
            (item_id, cycle_id),
        )
        if other_open is not None:
            raise ApiError(
                CODE_CONFLICT,
                DETAIL_CONSUMABLE_REOPEN_CONFLICT,
                status.HTTP_409_CONFLICT,
            )

    assignments = ["started_on = ?", "ended_on = ?"]
    params: list[object] = [started_on, ended_on]
    if "expected_days" in payload.model_fields_set:
        assignments.append("expected_days = ?")
        params.append(payload.expected_days)
    if "note" in payload.model_fields_set:
        assignments.append("note = ?")
        params.append(payload.note)
    params.append(cycle_id)

    await db.execute(
        f"UPDATE consumable_cycles SET {', '.join(assignments)}, "
        "updated_at = datetime('now') WHERE id = ? AND deleted_at IS NULL",
        params,
    )
    await db.execute(
        "UPDATE consumable_items SET updated_at = datetime('now') WHERE id = ?",
        (item_id,),
    )
    return success(await _load_one(db, item_id, user["id"], _today(today)))
