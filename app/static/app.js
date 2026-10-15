from __future__ import annotations

import random
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "pharma_tracker_demo.db"

app = FastAPI(title="Где моё лекарство")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

WAREHOUSES = ["Склад Ростов", "Склад Москва", "Склад Краснодар"]
CLIENTS = ["Аптека Надежда", "Аптека Вита", "Клиника Здоровье+", "МедМаркет"]
STATUSES = {
    "produced": "Произведено",
    "accepted": "На складе",
    "moved": "Перемещено",
    "shipped": "Отгружено",
    "delivered": "У клиента",
    "delayed": "Нет подтверждения",
}
EVENT_TYPES = {
    "produced": "Производство",
    "accepted": "Приёмка",
    "moved": "Перемещение",
    "shipped": "Отгрузка",
    "delivered": "Доставка",
}
DELAY_HOURS = 96


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_code TEXT UNIQUE NOT NULL,
            batch_number TEXT NOT NULL,
            medicine_name TEXT NOT NULL,
            produced_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            location TEXT NOT NULL,
            counterparty TEXT,
            event_time TEXT NOT NULL,
            note TEXT,
            order_number TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (package_id) REFERENCES packages(id)
        );
        """
    )
    if cur.execute("SELECT COUNT(*) FROM packages").fetchone()[0] == 0:
        seed_data(cur)
    conn.commit()
    conn.close()


def seed_data(cur: sqlite3.Cursor) -> None:
    random.seed(42)
    series = [
        ("BATCH-AX2401", "Кардиолекс", 12),
        ("BATCH-BM2402", "Нейрофарм", 11),
        ("BATCH-CT2403", "Иммуновир", 10),
        ("BATCH-DL2404", "Пульсофен", 9),
    ]
    now = datetime.now() - timedelta(days=18)
    package_index = 1

    for batch_number, medicine_name, qty in series:
        for _ in range(qty):
            package_code = f"PACK-{package_index:05d}"
            produced_at = now + timedelta(hours=package_index * 3)
            cur.execute(
                "INSERT INTO packages (package_code, batch_number, medicine_name, produced_at) VALUES (?, ?, ?, ?)",
                (package_code, batch_number, medicine_name, produced_at.isoformat(timespec="minutes")),
            )
            package_id = cur.lastrowid
            current_time = produced_at
            first_warehouse = random.choice(WAREHOUSES)
            timeline = [("produced", "produced", "Производитель", None, current_time, "Выпущено на линии", None)]
            current_time += timedelta(hours=random.randint(6, 18))
            timeline.append(("accepted", "accepted", first_warehouse, "Производитель", current_time, "Первичная приёмка", f"ORD-{package_index:04d}"))

            if random.random() > 0.15:
                current_time += timedelta(hours=random.randint(10, 36))
                next_warehouse = random.choice([w for w in WAREHOUSES if w != first_warehouse])
                timeline.append(("moved", "moved", next_warehouse, first_warehouse, current_time, "Плановое перемещение", f"TR-{package_index:04d}"))
                final_location = next_warehouse
            else:
                final_location = first_warehouse

            fate = random.random()
            if fate > 0.45:
                current_time += timedelta(hours=random.randint(8, 40))
                client = random.choice(CLIENTS)
                timeline.append(("shipped", "shipped", final_location, client, current_time, "Отгружено по заказу", f"SHIP-{package_index:04d}"))
                if fate > 0.72:
                    current_time += timedelta(hours=random.randint(8, 30))
                    timeline.append(("delivered", "delivered", client, final_location, current_time, "Получено клиентом", f"DEL-{package_index:04d}"))
            elif fate < 0.18:
                delayed_time = current_time - timedelta(hours=140)
                last = timeline[-1]
                timeline[-1] = (last[0], last[1], last[2], last[3], delayed_time, "Требует подтверждения местонахождения", last[6])

            for event_type, status, location, counterparty, event_time, note, order_number in timeline:
                cur.execute(
                    "INSERT INTO events (package_id, event_type, status, location, counterparty, event_time, note, order_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (package_id, event_type, status, location, counterparty, event_time.isoformat(timespec="minutes"), note, order_number),
                )
            package_index += 1


def get_last_event(cur: sqlite3.Cursor, package_id: int) -> Optional[sqlite3.Row]:
    return cur.execute("SELECT * FROM events WHERE package_id = ? ORDER BY datetime(event_time) DESC, id DESC LIMIT 1", (package_id,)).fetchone()


def enrich_package(cur: sqlite3.Cursor, package_row: sqlite3.Row) -> dict:
    last_event = get_last_event(cur, package_row["id"])
    events = cur.execute("SELECT * FROM events WHERE package_id = ? ORDER BY datetime(event_time) DESC, id DESC", (package_row["id"],)).fetchall()
    stale = False
    display_status = STATUSES.get(last_event["status"], last_event["status"]) if last_event else "Нет событий"
    warning = None
    if last_event:
        hours_since = (datetime.now() - datetime.fromisoformat(last_event["event_time"])).total_seconds() / 3600
        if hours_since > DELAY_HOURS and last_event["status"] != "delivered":
            stale = True
            display_status = STATUSES["delayed"]
            warning = "Давно не было обновлений, показано последнее подтверждённое место."
    suspicious = False
    if len(events) >= 2:
        newest = datetime.fromisoformat(events[0]["event_time"])
        previous = datetime.fromisoformat(events[1]["event_time"])
        suspicious = (newest - previous).total_seconds() < 10800 and events[0]["location"] != events[1]["location"]
    
    # Форматируем время последней активности: убираем букву "T" для вывода в таблицу
    clean_event_time = None
    if last_event and last_event["event_time"]:
        clean_event_time = last_event["event_time"].replace("T", "   ")

    return {
        "id": package_row["id"],
        "package_code": package_row["package_code"],
        "batch_number": package_row["batch_number"],
        "medicine_name": package_row["medicine_name"],
        "produced_at": package_row["produced_at"].replace("T", "   ") if package_row["produced_at"] else "—",
        "last_location": last_event["location"] if last_event else "—",
        "last_event_type": EVENT_TYPES.get(last_event["event_type"], last_event["event_type"]) if last_event else "—",
        "status_label": display_status,
        "last_event_time": clean_event_time, # Возвращаем строку уже без буквы Т
        "warning": warning,
        "stale": stale,
        "suspicious": suspicious,
    }



def dashboard_data(search_code: str = "", search_batch: str = "") -> dict:
    conn = get_conn()
    cur = conn.cursor()
    query = "SELECT * FROM packages WHERE 1=1"
    params = []
    if search_code:
        query += " AND package_code LIKE ?"
        params.append(f"%{search_code.strip()}%")
    if search_batch:
        query += " AND batch_number LIKE ?"
        params.append(f"%{search_batch.strip()}%")
    package_rows = cur.execute(query + " ORDER BY id", params).fetchall()
    packages = [enrich_package(cur, row) for row in package_rows]

    grouped_batches = defaultdict(lambda: {"warehouses": defaultdict(int), "statuses": defaultdict(int), "items": []})
    for item in packages:
        grouped_batches[item["batch_number"]]["warehouses"][item["last_location"]] += 1
        grouped_batches[item["batch_number"]]["statuses"][item["status_label"]] += 1
        grouped_batches[item["batch_number"]]["items"].append(item)

    metrics = {
        "packages": cur.execute("SELECT COUNT(*) FROM packages").fetchone()[0],
        "events": cur.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "batches": cur.execute("SELECT COUNT(DISTINCT batch_number) FROM packages").fetchone()[0],
        "stale": sum(1 for p in packages if p["stale"]),
    }
    recent_events = cur.execute(
        "SELECT e.*, p.package_code, p.batch_number, p.medicine_name FROM events e JOIN packages p ON p.id = e.package_id ORDER BY datetime(e.event_time) DESC, e.id DESC LIMIT 12"
    ).fetchall()
    conn.close()
    return {
        "packages": packages,
        "grouped_batches": dict(grouped_batches),
        "metrics": metrics,
        "recent_events": recent_events,
        "search_code": search_code,
        "search_batch": search_batch,
    }


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, code: str = Query(default=""), batch: str = Query(default="")):
    data = dashboard_data(code, batch)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            **data,
            "statuses": STATUSES,
            "event_types": EVENT_TYPES,
            "warehouses": WAREHOUSES,
        },
    )


@app.get("/api/package/{package_code}", response_class=JSONResponse)
def package_details(package_code: str):
    conn = get_conn()
    cur = conn.cursor()
    package = cur.execute("SELECT * FROM packages WHERE package_code = ?", (package_code,)).fetchone()
    if not package:
        conn.close()
        return JSONResponse({"error": "not_found"}, status_code=404)
    details = enrich_package(cur, package)
    history = cur.execute("SELECT * FROM events WHERE package_id = ? ORDER BY datetime(event_time) DESC, id DESC", (package["id"],)).fetchall()
    conn.close()
    return {"package": details, "history": [dict(row) for row in history]}


@app.post("/events")
def create_event(
    package_code: str = Form(...),
    event_type: str = Form(...),
    status: str = Form(...),
    location: str = Form(...),
    counterparty: str = Form(default=""),
    event_time: str = Form(...),
    note: str = Form(default=""),
    order_number: str = Form(default=""),
):
    conn = get_conn()
    cur = conn.cursor()
    package = cur.execute("SELECT id FROM packages WHERE package_code = ?", (package_code.strip(),)).fetchone()
    if not package:
        conn.close()
        return RedirectResponse(url="/?error=package_not_found", status_code=303)

    cur.execute(
        "INSERT INTO events (package_id, event_type, status, location, counterparty, event_time, note, order_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            package["id"],
            event_type,
            status,
            location.strip(),
            counterparty.strip() or None,
            event_time,
            note.strip() or None,
            order_number.strip() or None,
        ),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/?code={package_code.strip()}", status_code=303)


@app.post("/packages")
def create_package(
    package_code: str = Form(...),
    batch_number: str = Form(...),
    medicine_name: str = Form(...),
    produced_at: str = Form(...),
    first_location: str = Form(...),
    note: str = Form(default="Создано вручную"),
):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO packages (package_code, batch_number, medicine_name, produced_at) VALUES (?, ?, ?, ?)",
        (package_code.strip(), batch_number.strip(), medicine_name.strip(), produced_at),
    )
    package_id = cur.lastrowid
    cur.execute(
        "INSERT INTO events (package_id, event_type, status, location, counterparty, event_time, note, order_number) VALUES (?, 'accepted', 'accepted', ?, 'Ручной ввод', ?, ?, ?)",
        (package_id, first_location.strip(), produced_at, note.strip(), f"MANUAL-{package_id:04d}"),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/?code={package_code.strip()}", status_code=303)
