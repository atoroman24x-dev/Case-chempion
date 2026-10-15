from __future__ import annotations

import random
import sqlite3
from collections import Counter, defaultdict
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

WAREHOUSES = ["Склад Ростов", "Склад Москва", "Склад Краснодар", "Склад Казань", "Склад Воронеж"]
CLIENTS = ["Аптека Надежда", "Аптека Вита", "Клиника Здоровье+", "МедМаркет", "ФармДом", "ЗдравСервис"]
PACKAGE_STATUSES = {
    "produced": "Произведено",
    "accepted": "На складе",
    "moved": "Перемещено",
    "shipped": "Отгружено",
    "delivered": "У клиента",
    "delayed": "Нет подтверждения",
}
ORDER_STATUS_META = {
    "assembling": {"label": "В сборке", "tone": "gray"},
    "seller": {"label": "У продавца", "tone": "gray"},
    "pickup": {"label": "В пункте выдачи", "tone": "yellow"},
    "delivered": {"label": "Получен пользователем", "tone": "green"},
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


def ensure_schema() -> None:
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
    cols = [r[1] for r in cur.execute("PRAGMA table_info(events)").fetchall()]
    if "order_number" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN order_number TEXT")
    if cur.execute("SELECT COUNT(*) FROM packages").fetchone()[0] == 0:
        seed_data(cur)
    conn.commit()
    conn.close()


def seed_data(cur: sqlite3.Cursor) -> None:
    random.seed(42)
    series = [
        ("BATCH-AX2401", "Кардиолекс", 18),
        ("BATCH-BM2402", "Нейрофарм", 16),
        ("BATCH-CT2403", "Иммуновир", 15),
        ("BATCH-DL2404", "Пульсофен", 14),
        ("BATCH-EK2405", "Терафлокс", 17),
        ("BATCH-FR2406", "Гепталин", 13),
        ("BATCH-GS2407", "Реновит", 19),
        ("BATCH-HN2408", "Виромакс", 12),
    ]
    orders = ["ORD-1001", "ORD-1002", "ORD-1003", "ORD-1004", "ORD-1005", "ORD-1006", "ORD-1007", "ORD-1008", "ORD-1009", "ORD-1010"]
    order_statuses = {
        "ORD-1001": "assembling",
        "ORD-1002": "seller",
        "ORD-1003": "pickup",
        "ORD-1004": "delivered",
        "ORD-1005": "seller",
        "ORD-1006": "pickup",
        "ORD-1007": "assembling",
        "ORD-1008": "delivered",
        "ORD-1009": "seller",
        "ORD-1010": "assembling",
    }
    order_pool = []
    for order in orders:
        order_pool.extend([order] * random.randint(4, 8))
    random.shuffle(order_pool)

    now = datetime.now() - timedelta(days=24)
    package_index = 1
    for batch_number, medicine_name, qty in series:
        for item_idx in range(qty):
            package_code = f"PACK-{package_index:05d}"
            produced_at = now + timedelta(hours=package_index * 2)
            is_unassigned = item_idx < max(2, qty // 3)
            assigned_order = None if is_unassigned else (order_pool.pop() if order_pool else f"ORD-{1000 + package_index}")
            cur.execute(
                "INSERT INTO packages (package_code, batch_number, medicine_name, produced_at) VALUES (?, ?, ?, ?)",
                (package_code, batch_number, medicine_name, produced_at.isoformat(timespec="minutes")),
            )
            package_id = cur.lastrowid
            current_time = produced_at
            first_warehouse = random.choice(WAREHOUSES)
            timeline = [
                ("produced", "produced", "Производитель", None, current_time, "Выпущено на линии", assigned_order),
            ]
            current_time += timedelta(hours=random.randint(6, 18))
            timeline.append(("accepted", "accepted", first_warehouse, "Производитель", current_time, "Первичная приёмка", assigned_order))

            if is_unassigned:
                if random.random() > 0.55:
                    current_time += timedelta(hours=random.randint(8, 24))
                    next_warehouse = random.choice([w for w in WAREHOUSES if w != first_warehouse])
                    timeline.append(("moved", "moved", next_warehouse, first_warehouse, current_time, "Перемещено между складами, заказ не создан", None))
            else:
                order_stage = order_statuses.get(assigned_order, "seller")
                if order_stage in {"seller", "pickup", "delivered"}:
                    current_time += timedelta(hours=random.randint(10, 28))
                    next_warehouse = random.choice([w for w in WAREHOUSES if w != first_warehouse])
                    timeline.append(("moved", "moved", next_warehouse, first_warehouse, current_time, "Перемещение внутри цепочки", assigned_order))
                if order_stage in {"pickup", "delivered"}:
                    current_time += timedelta(hours=random.randint(8, 18))
                    client = random.choice(CLIENTS)
                    timeline.append(("shipped", "shipped", "Пункт выдачи", client, current_time, "Подготовлено к выдаче", assigned_order))
                if order_stage == "delivered":
                    current_time += timedelta(hours=random.randint(6, 18))
                    client = random.choice(CLIENTS)
                    timeline.append(("delivered", "delivered", "Получено пользователем", client, current_time, "Заказ завершён", assigned_order))

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
    stale = False
    warning = None
    status_label = PACKAGE_STATUSES.get(last_event["status"], last_event["status"]) if last_event else "Нет событий"
    if last_event:
        hours_since = (datetime.now() - datetime.fromisoformat(last_event["event_time"])).total_seconds() / 3600
        if hours_since > DELAY_HOURS and last_event["status"] != "delivered":
            stale = True
            warning = "Давно не было обновлений, показано последнее подтверждённое место."
    return {
        "id": package_row["id"],
        "package_code": package_row["package_code"],
        "batch_number": package_row["batch_number"],
        "medicine_name": package_row["medicine_name"],
        "produced_at": package_row["produced_at"],
        "last_location": last_event["location"] if last_event else "—",
        "status_label": status_label,
        "last_event_time": last_event["event_time"] if last_event else None,
        "warning": warning,
        "stale": stale,
        "order_number": last_event["order_number"] if last_event and last_event["order_number"] else None,
        "raw_status": last_event["status"] if last_event else "accepted",
    }


def resolve_order_status(items: list[dict]) -> dict:
    raw_statuses = {item["raw_status"] for item in items}
    if raw_statuses == {"delivered"}:
        return {"code": "delivered", **ORDER_STATUS_META["delivered"]}
    if "shipped" in raw_statuses or "delivered" in raw_statuses:
        return {"code": "pickup", **ORDER_STATUS_META["pickup"]}
    if "moved" in raw_statuses:
        return {"code": "seller", **ORDER_STATUS_META["seller"]}
    return {"code": "assembling", **ORDER_STATUS_META["assembling"]}


def build_order_summary(packages: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for item in packages:
        if item["order_number"]:
            grouped[item["order_number"]].append(item)
    orders = []
    for order_number, items in grouped.items():
        items = sorted(items, key=lambda x: x["package_code"])
        locations = Counter(i["last_location"] for i in items)
        latest_time = max((i["last_event_time"] for i in items if i["last_event_time"]), default=None)
        status_meta = resolve_order_status(items)
        orders.append(
            {
                "order_number": order_number,
                "items_count": len(items),
                "medicine_count": len(set(i["medicine_name"] for i in items)),
                "locations": dict(locations),
                "latest_time": latest_time,
                "status_code": status_meta["code"],
                "status_label": status_meta["label"],
                "status_tone": status_meta["tone"],
                "items": items,
            }
        )
    return sorted(orders, key=lambda x: x["order_number"])


def dashboard_data(search_code: str = "", search_batch: str = "", search_order: str = "") -> dict:
    conn = get_conn()
    cur = conn.cursor()
    query = """
        SELECT DISTINCT p.*
        FROM packages p
        LEFT JOIN events e ON e.package_id = p.id
        WHERE 1=1
    """
    params = []
    if search_code:
        query += " AND p.package_code LIKE ?"
        params.append(f"%{search_code.strip()}%")
    if search_batch:
        query += " AND p.batch_number LIKE ?"
        params.append(f"%{search_batch.strip()}%")
    if search_order:
        query += " AND e.order_number LIKE ?"
        params.append(f"%{search_order.strip()}%")
    query += " ORDER BY p.id"
    package_rows = cur.execute(query, params).fetchall()
    packages = [enrich_package(cur, row) for row in package_rows]
    orders = build_order_summary(packages)
    conn.close()
    return {
        "orders": orders,
        "search_code": search_code,
        "search_batch": search_batch,
        "search_order": search_order,
        "event_types": EVENT_TYPES,
        "statuses": PACKAGE_STATUSES,
        "warehouses": WAREHOUSES,
    }


@app.on_event("startup")
def startup() -> None:
    ensure_schema()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, code: str = Query(default=""), batch: str = Query(default=""), order: str = Query(default="")):
    data = dashboard_data(code, batch, order)
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
    history = cur.execute("SELECT * FROM events WHERE package_id = ? ORDER BY datetime(event_time) DESC, id DESC", (package["id"],)).fetchall()
    conn.close()
    return {"package": details, "history": [dict(row) for row in history]}


@app.get("/api/order/{order_number}", response_class=JSONResponse)
def order_details(order_number: str):
    conn = get_conn()
    cur = conn.cursor()
    package_rows = cur.execute(
        """
        SELECT DISTINCT p.*
        FROM packages p
        JOIN events e ON e.package_id = p.id
        WHERE e.order_number = ?
        ORDER BY p.id
        """,
        (order_number,),
    ).fetchall()
    if not package_rows:
        conn.close()
        return JSONResponse({"error": "not_found"}, status_code=404)
    items = [enrich_package(cur, row) for row in package_rows]
    status_meta = resolve_order_status(items)
    conn.close()
    return {
        "order_number": order_number,
        "total_packages": len(items),
        "medicines": sorted(list({i["medicine_name"] for i in items})),
        "warehouses": dict(Counter(i["last_location"] for i in items)),
        "status_label": status_meta["label"],
        "status_tone": status_meta["tone"],
        "items": items,
    }


@app.post("/events")
def create_event(package_code: str = Form(...), event_type: str = Form(...), status: str = Form(...), location: str = Form(...), counterparty: str = Form(default=""), event_time: str = Form(...), note: str = Form(default=""), order_number: str = Form(default="")):
    conn = get_conn()
    cur = conn.cursor()
    package = cur.execute("SELECT id FROM packages WHERE package_code = ?", (package_code.strip(),)).fetchone()
    if not package:
        conn.close()
        return RedirectResponse(url="/?error=package_not_found", status_code=303)
    cur.execute(
        "INSERT INTO events (package_id, event_type, status, location, counterparty, event_time, note, order_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (package["id"], event_type, status, location.strip(), counterparty.strip() or None, event_time, note.strip() or None, order_number.strip() or None),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/?code={package_code.strip()}", status_code=303)


@app.post("/packages")
def create_package(package_code: str = Form(...), batch_number: str = Form(...), medicine_name: str = Form(...), produced_at: str = Form(...), first_location: str = Form(...), order_number: str = Form(default=""), note: str = Form(default="Создано вручную")):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO packages (package_code, batch_number, medicine_name, produced_at) VALUES (?, ?, ?, ?)",
        (package_code.strip(), batch_number.strip(), medicine_name.strip(), produced_at),
    )
    package_id = cur.lastrowid
    cur.execute(
        "INSERT INTO events (package_id, event_type, status, location, counterparty, event_time, note, order_number) VALUES (?, 'accepted', 'accepted', ?, 'Ручной ввод', ?, ?, ?)",
        (package_id, first_location.strip(), produced_at, note.strip(), order_number.strip() or None),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/?code={package_code.strip()}", status_code=303)
