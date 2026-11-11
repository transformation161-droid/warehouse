"""Real PostgreSQL 17 PR40 integration acceptance, isolated CI-only database.

No external managed PostgreSQL, production source records or passwords.
"""
import base64
from contextlib import contextmanager
from datetime import date
from io import BytesIO
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from openpyxl import Workbook
from fastapi import HTTPException

from server.pg_admin import upgrade_schema, grant_runtime, grant_backup
from server.postgres_backend import Connection
from server.database import Database
from server.service import save_record
from server.inventory_workflow import create_plan, count_line, finalize_inventory
from server.models import InventoryPlanUpload, InventoryCountUpdate, InventoryFinalize
from server.inventory_statement import summarize_inventory,export_inventory_xlsx,export_inventory_pdf

ADMIN_DSN="host=127.0.0.1 port=5432 dbname=ksu_test user=postgres password=test-only-password sslmode=disable"
RUNTIME_DSN="host=127.0.0.1 port=5432 dbname=ksu_test user=ksu_runtime password=test-only-runtime sslmode=disable"

MANAGER={"id":"u_manager","role":"manager","name":"Руководитель"}
ELMAN={"id":"u_elman","role":"inspector","name":"КСУ"}
OUTSIDER={"id":"u_other","role":"inspector","name":"Иной объект"}


class CI_DB:
    backend="postgresql"

    @contextmanager
    def connect(self,actor=None):
        with psycopg.connect(RUNTIME_DSN,row_factory=dict_row) as raw:
            raw.execute("select set_config('search_path','ksu,pg_catalog',true)")
            raw.execute("select set_config('ksu.actor',%s,true)",(actor["id"] if actor else "",))
            yield Connection(raw)

    def check_capacity(self,con,expected=0):
        assert expected < 100_000_000

    def revision(self,con):
        return con.execute("select state_revision()").fetchone()[0]

    snapshot=Database.snapshot


def setup_database():
    with psycopg.connect(ADMIN_DSN) as con:
        con.execute("create role ksu_runtime login nosuperuser nobypassrls noinherit nocreatedb nocreaterole noreplication password 'test-only-runtime'")
        con.execute("create role ksu_backup login nosuperuser nobypassrls noinherit nocreatedb nocreaterole noreplication password 'test-only-backup'")
        con.execute(Path("server/postgres/001_initial.sql").read_text())
        upgrade_schema(con)
        grant_runtime(con,"ksu_runtime")
        grant_backup(con,"ksu_backup")
        con.execute(Path("server/postgres/005_inventory_count.sql").read_text())
        con.execute("""
            INSERT INTO ksu.users(id,username,name,role,password,active) VALUES
            ('u_manager','u_manager','Руководитель','manager','synthetic',1),
            ('u_elman','u_elman','КСУ','inspector','synthetic',1),
            ('u_other','u_other','Другой объект','inspector','synthetic',1)
        """)
    with CI_DB().connect(MANAGER) as con:
        save_record(con,MANAGER,"sites","site_alpha",
          {"name":"Событие","code":"A","storekeeper":"","pm":""},0)
        save_record(con,MANAGER,"sites","site_beta",
          {"name":"Иной объект","code":"B","storekeeper":"","pm":""},0)
    with psycopg.connect(ADMIN_DSN) as con:
        con.execute("INSERT INTO ksu.user_sites(user_id,site_id) VALUES('u_elman','site_alpha'),('u_other','site_beta')")


def make_workbook(stamp="A"):
    book=Workbook()
    s=book.active
    s["A2"]="Отчёт по позициям"
    cols=["ID","Наименование","№","Стоимость","Склад"]
    for col,name in enumerate(cols,1):s.cell(4,col,name)
    rows=[["1"+stamp,"Дрель","A1",12500,"Событие / 01.572 / МОЛ"],
          ["2"+stamp,"Перфоратор","B2",18000,"Событие / 01.572 / МОЛ"]]
    for index,row in enumerate(rows,5):
        for col,value in enumerate(row,1):s.cell(index,col,value)
    buf=BytesIO();book.save(buf);return base64.b64encode(buf.getvalue()).decode()


def import_plan(actor,day="2026-11-11",stamp="A"):
    return create_plan(CI_DB(),actor,InventoryPlanUpload(
        name="synthetic.xlsx",content=make_workbook(stamp),
        siteId="site_alpha",date=day,title="Пробная инвентаризация"))


def assert_http(code,fn):
    try:fn()
    except HTTPException as error:
        assert error.status_code==code, (error.status_code,error.detail)
        return
    raise AssertionError("Expected HTTPException "+str(code))


def acceptance():
    os.environ["KSU_INVENTORY_PLAN_ENABLED"]="on"
    setup_database()
    # Simulate runtime privileges being rebuilt during independent pg_restore.
    # Existing v4 databases without the marker must also remain supported.
    with psycopg.connect(ADMIN_DSN) as admin:
        grant_runtime(admin, "ksu_runtime")
        assert admin.execute(
            "select has_function_privilege('ksu_runtime', "
            "'ksu.inventory_count_version()','EXECUTE')").fetchone()[0]
        assert not admin.execute(
            "select has_function_privilege('public', "
            "'ksu.inventory_count_version()','EXECUTE')").fetchone()[0]
    db=CI_DB()

    # A manager creates a plan; both tools are unchecked, not false shortages.
    first=import_plan(MANAGER)
    ident=first["inventoryId"]
    assert len(first["state"]["inventoryLines"])==2
    assert all(row["actual"] is None for row in first["state"]["inventoryLines"])
    duplicate=import_plan(ELMAN)
    assert duplicate["duplicate"] is True and duplicate["inventoryId"]==ident
    assert len(duplicate["state"]["inventories"])==1

    # An inspector with a site grant may count the manager's plan.
    by_id={r["sku"]:r for r in first["state"]["inventoryLines"]}
    a=by_id["A1"];b=by_id["B2"]
    updated=count_line(db,ELMAN,a["id"],InventoryCountUpdate(expectedVersion=a["version"],actual=1))
    assert next(x for x in updated["state"]["inventoryLines"] if x["id"]==a["id"])["actual"]==1
    assert_http(409,lambda:count_line(db,ELMAN,a["id"],InventoryCountUpdate(
        expectedVersion=a["version"],actual=0,reason="Старая версия")))
    assert_http(409,lambda:finalize_inventory(db,MANAGER,ident,InventoryFinalize(expectedVersion=1)))

    # The inspector cannot change purchasing price, expected amount or SKU.
    with db.connect(ELMAN) as con:
        current=next(x for x in updated["state"]["inventoryLines"] if x["id"]==a["id"])
        tampered={k:v for k,v in current.items() if k not in ("id","version","createdBy","createdAt","updatedAt")}
        tampered["price"]=1
        assert_http(403,lambda:save_record(con,ELMAN,"inventoryLines",a["id"],tampered,current["version"]))

    # An unassigned inspector cannot count or create the same site's plan.
    assert_http(404,lambda:count_line(db,OUTSIDER,b["id"],
       InventoryCountUpdate(expectedVersion=b["version"],actual=1)))
    assert_http(403,lambda:import_plan(OUTSIDER,day="2026-11-12",stamp="OUT"))

    # Actual zero means physical shortage, unlike unchecked/null.
    counted=count_line(db,ELMAN,b["id"],InventoryCountUpdate(
        expectedVersion=b["version"],actual=0,reason="При пересчёте не обнаружен"))
    summary=summarize_inventory([x for x in counted["state"]["inventoryLines"]
                                if x["inventoryId"]==ident])
    assert summary["complete"]
    assert summary["shortagePurchaseValue"]==18000
    assert summary["surplusPurchaseValue"]==0
    assert summary["knownFactPurchaseValue"]==12500

    # The other inspector can independently create a plan on the assigned site.
    own=import_plan(ELMAN,day="2026-11-12",stamp="ELM")
    assert not own["duplicate"] and own["inventoryId"]!=ident

    final=finalize_inventory(db,MANAGER,ident,InventoryFinalize(expectedVersion=1))
    assert next(x for x in final["state"]["inventories"] if x["id"]==ident)["status"]=="final"
    assert_http(409,lambda:count_line(db,ELMAN,b["id"],InventoryCountUpdate(
        expectedVersion=2,actual=1)))

    # Real export functions produce valid PDF and XLSX, Cyrillic included.
    paper={"site":"Событие","date":"2026-11-11","title":"Проверка"}
    xlsx=export_inventory_xlsx(paper,summary)
    pdf=export_inventory_pdf(paper,summary)
    assert xlsx[:2]==b"PK" and pdf[:4]==b"%PDF"
    assert len(pdf)>1000 and len(xlsx)>1000

    # HTTP-level regression: identical browser API + release JS assets.
    # Test uses a synthetic, isolated PG17 container only.
    import time
    from fastapi.testclient import TestClient
    from server.database import digest
    import server.app as app_module

    os.environ.update(KSU_OFFLINE_MODE="off",KSU_OFFLINE_DEPLOYMENT_ID="a"*32,
                      KSU_VIEWER_ACCESS="off",KSU_SECURE_COOKIES="1")
    db.path=Path("/tmp/ksu-ci-inventory")/"staging"
    db.path.parent.mkdir(parents=True,exist_ok=True)
    original_database=app_module.Database
    app_module.Database=lambda _:db
    token="test-session-cookie-only"
    csrf="test-csrf-only"
    with psycopg.connect(ADMIN_DSN) as con:
        con.execute("insert into ksu.sessions values(%s,%s,%s,%s)",
                    (digest(token),MANAGER["id"],csrf,time.time()+3600))
    try:
        app=app_module.create_app("postgresql",origin="https://ksu.example.test",secure=True)
        with TestClient(app,base_url="https://ksu.example.test") as client:
            client.cookies.set("ksu_session",token,domain="ksu.example.test")
            headers={"Origin":"https://ksu.example.test",
                     "X-CSRF-Token":csrf,"X-KSU-Expected-User":MANAGER["id"]}
            assert client.get("/api/me",headers=headers).status_code==200
            assert "inventory-count.js" in client.get("/").text
            assert client.get("/inventory-count.js").status_code==200
            plan={"name":"synthetic.xlsx","content":make_workbook("HTTP"),
                  "siteId":"site_alpha","date":"2026-11-13",
                  "title":"Тест HTTP"}
            read=client.post("/api/inventory-plan/preview",json={"name":plan["name"],"content":plan["content"]},headers=headers)
            assert read.status_code==200,(read.status_code,read.text)
            assert read.json()["positions"]==2
            put=client.post("/api/inventory-plan/commit",json=plan,headers=headers)
            assert put.status_code==200,(put.status_code,put.text)
            plan_id=put.json()["inventoryId"]
            pdf=client.get("/api/inventories/"+plan_id+"/statement.pdf",headers=headers)
            xlsx=client.get("/api/inventories/"+plan_id+"/statement.xlsx",headers=headers)
            assert pdf.status_code==200 and pdf.content.startswith(b"%PDF")
            assert xlsx.status_code==200 and xlsx.content.startswith(b"PK")
            lines=[row for row in put.json()["state"]["inventoryLines"]
                   if row["inventoryId"]==plan_id]
            assert len(lines)==2 and all(row["actual"] is None for row in lines)
            firstline=lines[0]
            from PIL import Image
            picture=BytesIO()
            Image.new("RGB",(24,24),(90,105,130)).save(picture,format="JPEG")
            attached=client.post("/api/media",json={"siteId":"site_alpha",
                  "content":base64.b64encode(picture.getvalue()).decode()},headers=headers)
            assert attached.status_code==200,(attached.status_code,attached.text)
            photo=attached.json()["url"]
            fact=client.patch("/api/inventory-lines/"+firstline["id"]+"/count",
                 json={"expectedVersion":firstline["version"],"actual":1,
                       "reason":"Проверен на месте","photo":photo},headers=headers)
            assert fact.status_code==200,(fact.status_code,fact.text)
            assert next(row for row in fact.json()["state"]["inventoryLines"]
                        if row["id"]==firstline["id"])["photo"]==photo
            blocked=client.post("/api/inventories/"+plan_id+"/finalize",
                    json={"expectedVersion":1},headers=headers)
            assert blocked.status_code==409,(blocked.status_code,blocked.text)
            print("PASS: secured HTTP preview/commit/count, unchanged null, "
                  "partial-finalization block, PDF/XLSX and browser asset")
    finally:
        app_module.Database=original_database

    print("PASS: PG17 RLS manager+inspector creation, shared counting, versions, ACL, "
          "duplicate import, finalization and Cyrillic PDF/XLSX")


if __name__=="__main__":
    acceptance()
