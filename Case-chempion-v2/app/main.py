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
DB_PATH = BASE_DIR / "data" / "pharma_tracker_v2.db"

app = FastAPI(title="Где моё лекарство V2")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

WAREHOUSES = ["Склад Ростов", "Склад Москва", "Склад Краснодар"]
CLIENTS = ["Аптека Надежда", "Аптека Вита", "Клиника Здоровье+", "МедМаркет", "ФармСеть Юг"]

STATUS_LABELS = {
    "produced": "Произведено",
    "accepted": "Принято",
    "at_warehouse": "На складе",
    "in_transit": "В пути",
    "reserved": "Зарезервировано",
    "shipped": "Отгружено",
    "delivered": "У клиента",
    "quarantine": "Карантин",
    "returned": "Возврат",
    "delayed": "Нет подтверждения",
}

EVENT_LABELS = {
    "produced": "Производство",
    "acceptance": "Приёмка",
    "transfer": "Перемещение",
    "reservation": "Резерв",
    "shipment": "Отгрузка",
    "delivery": "Доставка",
    "quarantine": "Проверка",
    "return": "Возврат",
}

STATUS_COLORS = {
    "produced": "slate",
    "accepted": "blue",
    "at_warehouse": "green",
    "in_transit": "orange",
    "reserved": "violet",
    "shipped": "indigo",
    "delivered": "emerald",
    "quarantine": "red",
    "returned": "rose",
    "delayed": "amber",
}

DELAY_HOURS = 96


def format_dt(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return value.replace("T", " ")


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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

    count = cur.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
    if count == 0:
        seed_data(cur)

    conn.commit()
    conn.close()


def seed_data(cur: sqlite3.Cursor) -> None:
    random.seed(24)
    series = [
        ("BATCH-AX2401", "Кардиолекс", 12),
        ("BATCH-BM2402", "Нейрофарм", 10),
        ("BATCH-CT2403", "Иммуновир", 11),
        ("BATCH-DL2404", "Пульсофен", 9),
    ]

    now = datetime.now() - timedelta(days=14)
    package_index = 1

    for batch_number, medicine_name, qty in series:
        for _ in range(qty):
            package_code = f"PKG-V2-{package_index:05d}"
            produced_at = now + timedelta(hours=package_index * 2)

            cur.execute(
                "INSERT INTO packages (package_code, batch_number, medicine_name, produced_at) VALUES (?, ?, ?, ?)",
                (package_code, batch_number, medicine_name, produced_at.isoformat(timespec="minutes")),
            )
            package_id = cur.lastrowid

            first_warehouse = random.choice(WAREHOUSES)
            current_time = produced_at

            timeline = [
                ("produced", "produced", "Производитель", None, current_time, "Упаковка произведена", None)
            ]

            current_time += timedelta(hours=random.randint(4, 12))
            timeline.append(
                ("acceptance", "accepted", first_warehouse, "Производитель", current_time, "Принято на склад", f"ACC-{package_index:04d}")
            )

            current_time += timedelta(hours=random.randint(2, 10))
            timeline.append(
                ("transfer", "at_warehouse", first_warehouse, "Внутренний учёт", current_time, "Подтверждено размещение на складе", f"WH-{package_index:04d}")
            )

            fate = random.random()

            if fate > 0.20:
                current_time += timedelta(hours=random.randint(6, 18))
                next_warehouse = random.choice([w for w in WAREHOUSES if w != first_warehouse])
                timeline.append(
                    ("transfer", "in_transit", next_warehouse, first_warehouse, current_time, "Передано в перемещение между складами", f"TR-{package_index:04d}")
                )

                current_time += timedelta(hours=random.randint(4, 16))
                timeline.append(
                    ("acceptance", "at_warehouse", next_warehouse, "Логистика", current_time, "Подтверждена приёмка на новом складе", f"RCV-{package_index:04d}")
                )

                current_warehouse = next_warehouse
            else:
                current_warehouse = first_warehouse

            if fate > 0.35:
                current_time += timedelta(hours=random.randint(3, 14))
                timeline.append(
                    ("reservation", "reserved", current_warehouse, random.choice(CLIENTS), current_time, "Упаковка зарезервирована под заказ", f"RSV-{package_index:04d}")
                )

            if fate > 0.48:
                current_time += timedelta(hours=random.randint(4, 20))
                client = random.choice(CLIENTS)
                timeline.append(
                    ("shipment", "shipped", current_warehouse, client, current_time, "Отгружено клиенту", f"SHP-{package_index:04d}")
                )

            if fate > 0.68:
                current_time += timedelta(hours=random.randint(5, 22))
                client = random.choice(CLIENTS)
                timeline.append(
                    ("delivery", "delivered", client, current_warehouse, current_time, "Подтверждено получение клиентом", f"DLV-{package_index:04d}")
                )

            if fate < 0.10:
                current_time += timedelta(hours=random.randint(3, 12))
                timeline.append(
                    ("quarantine", "quarantine", current_warehouse, "Контроль качества", current_time, "Упаковка отправлена на дополнительную проверку", f"QRT-{package_index:04d}")
                )

            if 0.10 <= fate < 0.15:
                current_time += timedelta(hours=random.randint(3, 12))
                timeline.append(
                    ("return", "returned", current_warehouse, "Клиент", current_time, "Оформлен возврат по заказу", f"RTN-{package_index:04d}")
                )

            if 0.15 <= fate < 0.22:
                last = timeline[-1]
                timeline[-1] = (
                    last[0],
                    last[1],
                    last[2],
                    last[3],
                    (datetime.fromisoformat(last[4].isoformat(timespec="minutes")) - timedelta(hours=150)),
                    "Давно не было обновления по этой упаковке",
                    last[6],
                )

            for event_type, status, location, counterparty, event_time, note, order_number in timeline:
                cur.execute(
                    "INSERT INTO events (package_id, event_type, status, location, counterparty, event_time, note, order_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (package_id, event_type, status, location, counterparty, event_time.isoformat(timespec="minutes"), note, order_number),
                )

            package_index += 1


def get_last_event(cur: sqlite3.Cursor, package_id: int):
    return cur.execute(
        "SELECT * FROM events WHERE package_id = ? ORDER BY datetime(event_time) DESC, id DESC LIMIT 1",
        (package_id,),
    ).fetchone()


def build_event_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "event_type": row["event_type"],
        "event_label": EVENT_LABELS.get(row["event_type"], row["event_type"]),
        "status": row["status"],
        "status_label": STATUS_LABELS.get(row["status"], row["status"]),
        "status_color": STATUS_COLORS.get(row["status"], "slate"),
        "location": row["location"],
        "counterparty": row["counterparty"],
        "event_time": format_dt(row["event_time"]),
        "note": row["note"],
        "order_number": row["order_number"],
    }


def enrich_package(cur: sqlite3.Cursor, package_row: sqlite3.Row) -> dict:
    events_raw = cur.execute(
        "SELECT * FROM events WHERE package_id = ? ORDER BY datetime(event_time) DESC, id DESC",
        (package_row["id"],),
    ).fetchall()

    last_event = events_raw[0] if events_raw else None
    stale = False
    warning = None

    if last_event:
        hours_since = (datetime.now() - datetime.fromisoformat(last_event["event_time"])).total_seconds() / 3600
        if hours_since > DELAY_HOURS and last_event["status"] != "delivered":
            stale = True
            warning = "Давно не было обновлений, ниже показано последнее подтверждённое местонахождение."

    suspicious = False
    if len(events_raw) >= 2:
        newest = datetime.fromisoformat(events_raw[0]["event_time"])
        previous = datetime.fromisoformat(events_raw[1]["event_time"])
        suspicious = (newest - previous).total_seconds() < 7200 and events_raw[0]["location"] != events_raw[1]["location"]

    status_key = "delayed" if stale else (last_event["status"] if last_event else "produced")

    return {
        "id": package_row["id"],
        "package_code": package_row["package_code"],
        "batch_number": package_row["batch_number"],
        "medicine_name": package_row["medicine_name"],
        "produced_at": format_dt(package_row["produced_at"]),
        "last_location": last_event["location"] if last_event else "—",
        "last_event_label": EVENT_LABELS.get(last_event["event_type"], "—") if last_event else "—",
        "status_key": status_key,
        "status_label": STATUS_LABELS.get(status_key, status_key),
        "status_color": STATUS_COLORS.get(status_key, "slate"),
        "last_event_time": format_dt(last_event["event_time"]) if last_event else "—",
        "warning": warning,
        "stale": stale,
        "suspicious": suspicious,
        "events_count": len(events_raw),
    }


def dashboard_data(search_code: str = "", search_batch: str = "", status_filter: str = "") -> dict:
    conn = get_conn()
    cur = conn.cursor()

    package_rows = cur.execute("SELECT * FROM packages ORDER BY id").fetchall()
    packages_all = [enrich_package(cur, row) for row in package_rows]

    filtered = []
    for item in packages_all:
        if search_code and search_code.strip().lower() not in item["package_code"].lower():
            continue
        if search_batch and search_batch.strip().lower() not in item["batch_number"].lower():
            continue
        if status_filter and item["status_key"] != status_filter:
            continue
        filtered.append(item)

    grouped_batches = defaultdict(lambda: {"warehouses": defaultdict(int), "statuses": defaultdict(int), "items": []})
    for item in filtered:
        grouped_batches[item["batch_number"]]["warehouses"][item["last_location"]] += 1
        grouped_batches[item["batch_number"]]["statuses"][item["status_label"]] += 1
        grouped_batches[item["batch_number"]]["items"].append(item)

    recent_events_raw = cur.execute(
        "SELECT e.*, p.package_code, p.batch_number, p.medicine_name "
        "FROM events e JOIN packages p ON p.id = e.package_id "
        "ORDER BY datetime(e.event_time) DESC, e.id DESC LIMIT 10"
    ).fetchall()

    recent_events = []
    for row in recent_events_raw:
        event = build_event_view(row)
        event["package_code"] = row["package_code"]
        event["batch_number"] = row["batch_number"]
        event["medicine_name"] = row["medicine_name"]
        recent_events.append(event)

    metrics = {
        "packages": len(packages_all),
        "filtered": len(filtered),
        "batches": len({item["batch_number"] for item in packages_all}),
        "delayed": sum(1 for item in packages_all if item["status_key"] == "delayed"),
        "delivered": sum(1 for item in packages_all if item["status_key"] == "delivered"),
        "in_transit": sum(1 for item in packages_all if item["status_key"] == "in_transit"),
    }

    conn.close()
    return {
        "packages": filtered,
        "grouped_batches": dict(grouped_batches),
        "metrics": metrics,
        "recent_events": recent_events,
        "search_code": search_code,
        "search_batch": search_batch,
        "status_filter": status_filter,
        "status_labels": STATUS_LABELS,
        "event_labels": EVENT_LABELS,
        "warehouses": WAREHOUSES,
    }


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    code: str = Query(default=""),
    batch: str = Query(default=""),
    status: str = Query(default=""),
):
    data = dashboard_data(code, batch, status)
    return templates.TemplateResponse("index.html", {"request": request, **data})


@app.get("/api/package/{package_code}", response_class=JSONResponse)
def package_details(package_code: str):
    conn = get_conn()
    cur = conn.cursor()

    package = cur.execute("SELECT * FROM packages WHERE package_code = ?", (package_code,)).fetchone()
    if not package:
        conn.close()
        return JSONResponse({"error": "not_found"}, status_code=404)

    details = enrich_package(cur, package)
    history_raw = cur.execute(
        "SELECT * FROM events WHERE package_id = ? ORDER BY datetime(event_time) DESC, id DESC",
        (package["id"],),
    ).fetchall()
    history = [build_event_view(row) for row in history_raw]

    conn.close()
    return {"package": details, "history": history}


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
        "INSERT INTO events (package_id, event_type, status, location, counterparty, event_time, note, order_number) VALUES (?, 'acceptance', 'accepted', ?, 'Ручной ввод', ?, ?, ?)",
        (package_id, first_location.strip(), produced_at, note.strip(), f"MANUAL-{package_id:04d}"),
    )

    cur.execute(
        "INSERT INTO events (package_id, event_type, status, location, counterparty, event_time, note, order_number) VALUES (?, 'transfer', 'at_warehouse', ?, 'Система', ?, ?, ?)",
        (package_id, first_location.strip(), produced_at, "Подтверждено размещение на складе", f"PLACE-{package_id:04d}"),
    )

    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/?code={package_code.strip()}", status_code=303)
