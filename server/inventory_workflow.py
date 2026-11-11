"""Explicit inventory PLAN -> counted FACT workflow.

Only signed-in manager or inspector may create/count plans on assigned sites.
The runtime DB role is unchanged. Cross-author fact patch is limited again by
PG row security and trigger installed manually from 005_inventory_count.sql.
No owner role, DDL, SQLite fallback, or unconfirmed fact on import.
"""
import base64
import binascii
import json
import os
from uuid import uuid4

from .inventory_plan import parse_plan, PlanError
from .models import Inventory, InventoryLine
from .service import accessible, fail, get_record, save_record


def require_feature(db):
    if os.getenv("KSU_INVENTORY_PLAN_ENABLED") != "on":
        fail("Новый пересчёт пока не включён администратором", 503)
    if db.backend == "postgresql":
        with db.connect() as con:
            if con.execute("select to_regprocedure('ksu.inventory_count_version()')").fetchone()[0] is None:
                fail("Не установлены правила доступа к планам инвентаризации", 503)


def create_plan(db, actor, payload):
    """Atomic creation of one plan with all positions, no fictional physical count."""
    require_feature(db)
    if actor["role"] not in ("manager", "inspector"):
        fail("Нет права создавать инвентаризацию", 403)
    try:
        file_bytes = base64.b64decode(payload.content, validate=True)
        source = parse_plan(file_bytes, payload.name, mapping=payload.mapping)
    except (ValueError, binascii.Error) as exc:
        if isinstance(exc, PlanError):
            fail(str(exc))
        fail("Не удалось прочитать список инвентаризации")
    import_date = payload.date.isoformat()
    with db.connect(actor) as con:
        con.execute("begin immediate")
        site = get_record(con, "sites", payload.siteId)
        if site is None or not accessible(con, actor, payload.siteId):
            fail("Объект не найден или не назначен пользователю", 403)
        # A repeated upload of the same source on the same object and day
        # returns the existing plan, never overwrites any counted facts.
        existing = con.execute(
            "select id from records where collection='inventories' and site_id=? "
            "and json_extract(data,'$.sourceHash')=? and json_extract(data,'$.date')=? "
            "order by created_at limit 1",
            (payload.siteId, source["planSha256"], import_date),
        ).fetchone()
        if existing:
            return {"duplicate": True, "inventoryId": existing["id"],
                    "state": db.snapshot(con, actor)}
        if db.backend == "postgresql":
            db.check_capacity(con, 262144 + len(source["rows"]) * 16384)
        inventory_id = uuid4().hex
        inventory = Inventory.model_validate({
            "siteId": payload.siteId, "date": import_date,
            "title": payload.title, "status": "draft", "stage": "primary",
            "plannedPositions": source["positions"],
            "sourceHash": source["planSha256"], "sourceName": payload.name,
        })
        save_record(con, actor, "inventories", inventory_id,
                    inventory.model_dump(mode="json"), 0)
        for record in source["rows"]:
            try:
                line = InventoryLine.model_validate({
                    "inventoryId": inventory_id,
                    "item": record["item"],
                    "sku": record["sku"],
                    "category": record["category"],
                    "unit": record["unit"],
                    "expected": record["expected"],
                    "actual": None,
                    "price": record["purchasePrice"],
                    "owner": record["owner"],
                    "reason": "",
                    "sourceId": record["externalId"],
                    "inventoryNumber": record["inventoryNumber"],
                    "brand": record["brand"],
                    "sourceRow": record["row"],
                })
            except Exception:
                fail("Некорректные данные строки " + str(record["row"]))
            save_record(con, actor, "inventoryLines", uuid4().hex,
                        line.model_dump(mode="json"), 0)
        state = db.snapshot(con, actor)
    return {"duplicate": False, "inventoryId": inventory_id, "state": state,
            "positions": source["positions"]}


def count_line(db, actor, line_id, payload):
    """Version-checked fact-only update; inspector can count a manager's plan."""
    require_feature(db)
    if actor["role"] not in ("manager", "inspector"):
        fail("Нет права проводить пересчёт", 403)
    with db.connect(actor) as con:
        con.execute("begin immediate")
        row = get_record(con, "inventoryLines", line_id)
        if row is None or not accessible(con, actor, row["site_id"]):
            fail("Позиция недоступна", 404)
        values = json.loads(row["data"])
        parent = get_record(con, "inventories", values["inventoryId"])
        if parent is None or parent["site_id"] != row["site_id"]:
            fail("Инвентаризация не найдена", 404)
        parent_values = json.loads(parent["data"])
        if parent_values.get("status") != "draft" or parent_values["stage"] != "primary":
            fail("Инвентаризация завершена; пересчёт закрыт", 409)
        values["actual"] = payload.actual
        values["reason"] = payload.reason
        values["photo"] = payload.photo
        save_record(con, actor, "inventoryLines", line_id, values, payload.expectedVersion)
        return {"state": db.snapshot(con, actor), "inventoryId": parent["id"]}


def finalize_inventory(db, actor, inventory_id, payload):
    """Manager or the authoring inspector may finalize after counting every line."""
    require_feature(db)
    if actor["role"] not in ("manager", "inspector"):
        fail("Нет права завершить инвентаризацию", 403)
    with db.connect(actor) as con:
        con.execute("begin immediate")
        parent = get_record(con, "inventories", inventory_id)
        if parent is None or not accessible(con, actor, parent["site_id"]):
            fail("Инвентаризация недоступна", 404)
        if actor["role"] == "inspector" and parent["author_id"] != actor["id"]:
            fail("Завершить план другого сотрудника может руководитель", 403)
        data = json.loads(parent["data"])
        if data["status"] != "draft" or data["stage"] != "primary":
            fail("Инвентаризация уже завершена", 409)
        rows = [json.loads(r["data"]) for r in con.execute(
            "select data from records where collection='inventoryLines' and site_id=?",
            (parent["site_id"],))
            if json.loads(r["data"]).get("inventoryId") == inventory_id]
        if not rows or len(rows) != data["plannedPositions"] or any(
                line.get("actual") is None for line in rows):
            fail("Остались непроверенные позиции — завершение запрещено", 409)
        data["status"] = "final"
        save_record(con, actor, "inventories", inventory_id, data, payload.expectedVersion)
        return {"state": db.snapshot(con, actor), "inventoryId": inventory_id}
