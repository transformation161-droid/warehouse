"""Browser acceptance on synthetic data and isolated PostgreSQL 17.

Never connects to production or a public address. The server binds localhost.
"""
import base64
from io import BytesIO
import os
from pathlib import Path
import threading
import time
from urllib.request import urlopen

import psycopg
from openpyxl import Workbook
from playwright.sync_api import sync_playwright, expect
import uvicorn

from inventory_pg_smoke import CI_DB, MANAGER, ELMAN, ADMIN_DSN, setup_database
from server.database import digest
import server.app as app_module

HOST="http://127.0.0.1:8765"
SCREEN=Path("browser-acceptance-evidence")


def workbook(count):
    book=Workbook()
    sheet=book.active
    sheet["B2"]="Отчёт по позициям"
    names=["ID","Наименование","№","Стоимость","Склад"]
    for idx,name in enumerate(names,2):
        sheet.cell(4,idx,name)
    for index in range(1,count+1):
        cells=[str(index),f"Инструмент {index:03d}",f"И-{index:03d}",1200+index,
               "Событие / 01.572 / КСУ"]
        for j,value in enumerate(cells,2):
            sheet.cell(index+4,j,value)
    out=BytesIO();book.save(out);return out.getvalue()


def environment():
    os.environ.update(
        KSU_INVENTORY_PLAN_ENABLED="on",KSU_OFFLINE_MODE="off",
        KSU_OFFLINE_DEPLOYMENT_ID="a"*32,KSU_VIEWER_ACCESS="off",
        KSU_SECURE_COOKIES="0",
    )
    setup_database()
    db=CI_DB()
    db.path=Path("/tmp/ksu-browser-acceptance")/"staging"
    db.path.parent.mkdir(parents=True,exist_ok=True)
    app_module.Database=lambda _:db
    app=app_module.create_app("postgresql",origin=HOST,secure=False)
    secrets={}
    with psycopg.connect(ADMIN_DSN) as con:
        for actor in (MANAGER,ELMAN):
            token="browser-"+actor["id"]+"-synthetic-only"
            csrf="csrf-"+actor["id"]+"-synthetic-only"
            secrets[actor["id"]]=token
            con.execute("INSERT INTO ksu.sessions VALUES(%s,%s,%s,%s)",
                        (digest(token),actor["id"],csrf,time.time()+3600))
    server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=8765,
                                        log_level="error",access_log=False))
    thread=threading.Thread(target=server.run,daemon=True)
    thread.start()
    for _ in range(120):
        try:
            urlopen(HOST+"/api/health",timeout=1).close()
            return server,secrets
        except Exception:time.sleep(.2)
    raise RuntimeError("Synthetic test server did not start")


def logged_page(browser,token):
    context=browser.new_context(viewport={"width":1440,"height":950},
                                accept_downloads=True,base_url=HOST)
    context.add_cookies([{"name":"ksu_session","value":token,"url":HOST}])
    page=context.new_page()
    page.goto(HOST,wait_until="domcontentloaded")
    expect(page.locator("#sync-status")).to_contain_text("синхронизировано",timeout=10000)
    return context,page


def upload_plan(page,data,title,date):
    page.locator('[data-action="inventory-plan-open"]').click()
    page.locator("#ip-site").select_option("site_alpha")
    page.locator("#ip-date").fill(date)
    page.locator("#ip-title").fill(title)
    page.locator("#ip-file").set_input_files({
        "name":"synthetic-positions.xlsx",
        "mimeType":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "buffer":data,
    })
    page.locator('[data-action="inventory-plan-check"]').click()
    expect(page.locator("#ip-preview")).to_contain_text("позиций:",timeout=15000)
    expect(page.locator('[data-action="ip-commit"]')).to_be_enabled()
    page.locator('[data-action="ip-commit"]').click()
    expect(page.locator(".modal-head h3")).to_have_text(title,timeout=15000)


def main():
    SCREEN.mkdir(exist_ok=True)
    server,secrets=environment()
    browser_errors=[]
    try:
        with sync_playwright() as instance:
            browser=instance.chromium.launch(headless=True)
            manager,mp=logged_page(browser,secrets[MANAGER["id"]])
            mp.on("pageerror",lambda e:browser_errors.append("manager: "+str(e)))
            mp.locator('[data-nav="inventory"]').click()
            upload_plan(mp,workbook(69),"Синтетическая инвентаризация 69","2026-11-11")
            expect(mp.locator(".modal-body")).to_contain_text("0 / 69")
            expect(mp.locator(".modal-body")).to_contain_text("Не проверено")
            mp.screenshot(path=str(SCREEN/"01-manager-plan-69.png"))

            # The 69th entry must remain searchable; prior broken filter ignored query.
            mp.locator("#ip-filter-name").fill("Инструмент 069")
            mp.locator('[data-action="ip-filter-apply"]').click()
            expect(mp.locator(".modal-body tbody tr")).to_have_count(1)
            expect(mp.locator(".modal-body tbody tr")).to_contain_text("Инструмент 069")
            mp.screenshot(path=str(SCREEN/"02-manager-search-last-item.png"))

            # Inspector may see and count the manager's plan at the same site.
            inspector,ep=logged_page(browser,secrets[ELMAN["id"]])
            ep.on("pageerror",lambda e:browser_errors.append("inspector: "+str(e)))
            ep.locator('[data-nav="inventory"]').click()
            ep.locator('[data-action="inventory-detail"]').first.click()
            ep.locator('[data-action="ip-count-open"]').first.click()
            ep.locator("#ip-actual").fill("0")
            ep.locator("#ip-comment").fill("Тестовое отсутствие, только синтетические данные")
            ep.locator('[data-action="ip-save-count"]').click()
            expect(ep.locator(".modal-body")).to_contain_text("Недостача",timeout=10000)
            expect(ep.locator(".modal-body")).to_contain_text("1 / 69")
            ep.screenshot(path=str(SCREEN/"03-inspector-counted-zero.png"))

            # Inspector can also create an independent inventory with >100 rows.
            ep.locator('[data-action="close-modal"]').last.click()
            upload_plan(ep,workbook(121),"Синтетическая инвентаризация 121","2026-11-12")
            expect(ep.locator(".modal-body tbody tr")).to_have_count(100)
            ep.locator('[data-action="ip-next-page"]').click()
            expect(ep.locator(".modal-body tbody tr")).to_have_count(21)
            expect(ep.locator(".modal-body")).to_contain_text("Страница 2 из 2")
            ep.screenshot(path=str(SCREEN/"04-inspector-page-two.png"))
            ep.locator("#ip-filter-name").fill("Инструмент 121")
            ep.locator('[data-action="ip-filter-apply"]').click()
            expect(ep.locator(".modal-body tbody tr")).to_have_count(1)
            expect(ep.locator(".modal-body tbody tr")).to_contain_text("Инструмент 121")
            ep.screenshot(path=str(SCREEN/"05-inspector-search-page-two.png"))

            if browser_errors:
                raise AssertionError("Browser JavaScript failures: "+repr(browser_errors))
            manager.close();inspector.close();browser.close()
    finally:
        server.should_exit=True
    print("PASS real Chromium: manager's 69-row plan, inspector fact, 121-row pagination and filtering")


if __name__=="__main__":
    main()
