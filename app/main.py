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
MEDICINES = ["Кардиолекс", "Нейрофарм", "Иммуновир", "Пульсофен", "Терафлокс", "Гепталин", "Реновит", "Виромакс"]

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


def get_batch_prefix(medicine_name: str) -> str:
    cleaned = "".join(ch for ch in medicine_name.upper() if ch.isalnum())
    return (cleaned[:2] or "NP").ljust(2, "X")


def generate_batch_number(cur: sqlite3.Cursor, medicine_name: str) -> str:
    prefix = get_batch_prefix(medicine_name)
    suffix = datetime.now().strftime("%y%m%d%H%M")
    candidate = f"BATCH-{prefix}{suffix}"
    existing = cur.execute("SELECT 1 FROM packages WHERE batch_number = ? LIMIT 1", (candidate,)).fetchone()
    if not existing:
        return candidate

    counter = 2
    while True:
        candidate = f"BATCH-{prefix}{suffix}-{counter}"
        existing = cur.execute("SELECT 1 FROM packages WHERE batch_number = ? LIMIT 1", (candidate,)).fetchone()
        if not existing:
            return candidate
        counter += 1


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

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT UNIQUE NOT NULL,
            client_name TEXT NOT NULL,
            status_code TEXT NOT NULL DEFAULT 'assembling',
            created_at TEXT NOT NULL,
            comment TEXT
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            medicine_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            assigned_quantity INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (order_id) REFERENCES orders(id)
        );
        """
    )
    cols = [r[1] for r in cur.execute("PRAGMA table_info(events)").fetchall()]
    if "order_number" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN order_number TEXT")
    if cur.execute("SELECT COUNT(*) FROM packages").fetchone()[0] == 0:
        seed_data(cur)
    backfill_orders(cur)
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
    return cur.execute(
        "SELECT * FROM events WHERE package_id = ? ORDER BY datetime(event_time) DESC, id DESC LIMIT 1",
        (package_id,),
    ).fetchone()


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


def next_order_number(cur: sqlite3.Cursor) -> str:
    row = cur.execute("SELECT order_number FROM orders ORDER BY id DESC LIMIT 1").fetchone()
    if row and row["order_number"].startswith("ORD-"):
        try:
            return f"ORD-{int(row['order_number'].split('-')[1]) + 1}"
        except ValueError:
            pass
    event_row = cur.execute(
        "SELECT order_number FROM events WHERE order_number IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if event_row and event_row["order_number"] and event_row["order_number"].startswith("ORD-"):
        try:
            return f"ORD-{int(event_row['order_number'].split('-')[1]) + 1}"
        except ValueError:
            pass
    return "ORD-1011"


def backfill_orders(cur: sqlite3.Cursor) -> None:
    existing_orders = {row["order_number"] for row in cur.execute("SELECT order_number FROM orders").fetchall()}
    event_orders = cur.execute(
        "SELECT DISTINCT order_number FROM events WHERE order_number IS NOT NULL AND TRIM(order_number) != '' ORDER BY order_number"
    ).fetchall()

    for row in event_orders:
        order_number = row["order_number"]
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
            continue

        items = [enrich_package(cur, pkg) for pkg in package_rows]
        status_meta = resolve_order_status(items)
        latest_event = cur.execute(
            "SELECT event_time FROM events WHERE order_number = ? ORDER BY datetime(event_time) DESC, id DESC LIMIT 1",
            (order_number,),
        ).fetchone()
        created_at = latest_event["event_time"] if latest_event else datetime.now().isoformat(timespec="minutes")

        order_row = cur.execute("SELECT id FROM orders WHERE order_number = ?", (order_number,)).fetchone()
        if not order_row:
            cur.execute(
                "INSERT INTO orders (order_number, client_name, status_code, created_at, comment) VALUES (?, ?, ?, ?, ?)",
                (
                    order_number,
                    random.choice(CLIENTS),
                    status_meta["code"],
                    created_at,
                    "Импортировано из истории событий",
                ),
            )
            order_id = cur.lastrowid
        else:
            order_id = order_row["id"]
            cur.execute(
                "UPDATE orders SET status_code = ?, created_at = ?, comment = COALESCE(comment, ?) WHERE id = ?",
                (status_meta["code"], created_at, "Импортировано из истории событий", order_id),
            )
            cur.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))

        grouped = Counter(item["medicine_name"] for item in items)
        for medicine_name, qty in grouped.items():
            cur.execute(
                "INSERT INTO order_items (order_id, medicine_name, quantity, assigned_quantity) VALUES (?, ?, ?, ?)",
                (order_id, medicine_name, qty, qty),
            )


def list_free_packages(cur: sqlite3.Cursor, medicine_name: str, limit: int) -> list[sqlite3.Row]:
    return cur.execute(
        """
        SELECT p.*
        FROM packages p
        JOIN (
            SELECT e1.package_id, e1.order_number, e1.status, e1.event_time
            FROM events e1
            JOIN (
                SELECT package_id, MAX(datetime(event_time)) AS max_time
                FROM events
                GROUP BY package_id
            ) latest ON latest.package_id = e1.package_id AND datetime(e1.event_time) = latest.max_time
        ) last_event ON last_event.package_id = p.id
        WHERE p.medicine_name = ?
          AND (last_event.order_number IS NULL OR TRIM(last_event.order_number) = '')
          AND last_event.status IN ('accepted', 'moved', 'produced')
        ORDER BY datetime(p.produced_at) ASC, p.id ASC
        LIMIT ?
        """,
        (medicine_name, limit),
    ).fetchall()


def create_manual_package(
    cur: sqlite3.Cursor,
    package_code: str,
    medicine_name: str,
    warehouse: str,
    created_at: str,
) -> sqlite3.Row:
    batch_number = generate_batch_number(cur, medicine_name)
    cur.execute(
        "INSERT INTO packages (package_code, batch_number, medicine_name, produced_at) VALUES (?, ?, ?, ?)",
        (package_code, batch_number, medicine_name, created_at),
    )
    package_id = cur.lastrowid
    cur.execute(
        "INSERT INTO events (package_id, event_type, status, location, counterparty, event_time, note, order_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            package_id,
            "accepted",
            "accepted",
            warehouse,
            "Ручное создание",
            created_at,
            "Создана новая партия из одной упаковки по номеру, введённому вручную",
            None,
        ),
    )
    return cur.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()


def reserve_package_for_order(
    cur: sqlite3.Cursor,
    package_id: int,
    warehouse: str,
    client_name: str,
    event_time: str,
    order_number: str,
    note: str,
) -> None:
    cur.execute(
        "INSERT INTO events (package_id, event_type, status, location, counterparty, event_time, note, order_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            package_id,
            "accepted",
            "accepted",
            warehouse,
            client_name,
            event_time,
            note,
            order_number,
        ),
    )


def create_order_with_items(
    cur: sqlite3.Cursor,
    client_name: str,
    requested_items: list[dict],
    warehouse: str,
    created_at: str,
    comment: str,
) -> tuple[str, list[dict]]:
    order_number = next_order_number(cur)
    cur.execute(
        "INSERT INTO orders (order_number, client_name, status_code, created_at, comment) VALUES (?, ?, ?, ?, ?)",
        (order_number, client_name, "assembling", created_at, comment or None),
    )
    order_id = cur.lastrowid
    allocation_report = []

    current_time = datetime.fromisoformat(created_at)

    for item in requested_items:
        medicine_name = item["medicine_name"]
        quantity = item["quantity"]
        package_code = item.get("package_code", "").strip()

        assigned_packages = []

        if package_code:
            existing_package = cur.execute(
                "SELECT * FROM packages WHERE package_code = ? LIMIT 1",
                (package_code,),
            ).fetchone()

            if existing_package:
                assigned_packages.append(existing_package)
            else:
                created_package = create_manual_package(cur, package_code, medicine_name, warehouse, created_at)
                assigned_packages.append(created_package)

            additional_needed = max(0, quantity - len(assigned_packages))
            if additional_needed > 0:
                extra_packages = list_free_packages(cur, medicine_name, additional_needed)
                assigned_packages.extend(extra_packages)
        else:
            assigned_packages = list_free_packages(cur, medicine_name, quantity)

        unique_packages = []
        seen_ids = set()
        for pkg in assigned_packages:
            if pkg["id"] not in seen_ids:
                unique_packages.append(pkg)
                seen_ids.add(pkg["id"])

        assigned_quantity = len(unique_packages)

        cur.execute(
            "INSERT INTO order_items (order_id, medicine_name, quantity, assigned_quantity) VALUES (?, ?, ?, ?)",
            (order_id, medicine_name, quantity, assigned_quantity),
        )

        allocation_report.append(
            {
                "medicine_name": medicine_name,
                "requested": quantity,
                "assigned": assigned_quantity,
                "missing": quantity - assigned_quantity,
                "package_code": package_code or None,
            }
        )

        for package in unique_packages:
            current_time += timedelta(minutes=3)
            reserve_package_for_order(
                cur,
                package["id"],
                warehouse,
                client_name,
                current_time.isoformat(timespec="minutes"),
                order_number,
                "Упаковка зарезервирована под новый заказ",
            )

    return order_number, allocation_report


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


def stock_summary(cur: sqlite3.Cursor) -> list[dict]:
    rows = cur.execute(
        """
        SELECT p.medicine_name, COUNT(*) AS qty
        FROM packages p
        JOIN (
            SELECT e1.package_id, e1.order_number, e1.status, e1.event_time
            FROM events e1
            JOIN (
                SELECT package_id, MAX(datetime(event_time)) AS max_time
                FROM events
                GROUP BY package_id
            ) latest ON latest.package_id = e1.package_id AND datetime(e1.event_time) = latest.max_time
        ) last_event ON last_event.package_id = p.id
        WHERE last_event.order_number IS NULL OR TRIM(last_event.order_number) = ''
        GROUP BY p.medicine_name
        ORDER BY qty DESC, p.medicine_name ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


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
    stock_items = stock_summary(cur)
    conn.close()
    return {
        "orders": orders,
        "search_code": search_code,
        "search_batch": search_batch,
        "search_order": search_order,
        "event_types": EVENT_TYPES,
        "statuses": PACKAGE_STATUSES,
        "warehouses": WAREHOUSES,
        "clients": CLIENTS,
        "stock_items": stock_items,
        "medicines": MEDICINES,
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
    history = cur.execute(
        "SELECT * FROM events WHERE package_id = ? ORDER BY datetime(event_time) DESC, id DESC",
        (package["id"],),
    ).fetchall()
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


@app.post("/orders/create")
def create_order(
    client_name: str = Form(...),
    warehouse: str = Form(...),
    created_at: str = Form(...),
    comment: str = Form(default=""),
    medicine_name: list[str] = Form(...),
    quantity: list[int] = Form(...),
    package_code: list[str] = Form(default=[]),
):
    requested_items = []
    max_len = max(len(medicine_name), len(quantity), len(package_code) if package_code else 0)

    for idx in range(max_len):
        med = medicine_name[idx].strip() if idx < len(medicine_name) else ""
        qty_raw = quantity[idx] if idx < len(quantity) else 0
        pkg_code = package_code[idx].strip() if idx < len(package_code) else ""

        if not med:
            continue

        qty_value = int(qty_raw)
        if qty_value <= 0:
            continue

        requested_items.append(
            {
                "medicine_name": med,
                "quantity": qty_value,
                "package_code": pkg_code,
            }
        )

    if not requested_items:
        return RedirectResponse(url="/?error=empty_order_items", status_code=303)

    conn = get_conn()
    cur = conn.cursor()
    order_number, allocation_report = create_order_with_items(
        cur,
        client_name.strip(),
        requested_items,
        warehouse.strip(),
        created_at,
        comment.strip(),
    )
    has_shortage = any(row["missing"] > 0 for row in allocation_report)
    status_code = "assembling"
    if all(row["missing"] == 0 for row in allocation_report):
        status_code = "seller"
    cur.execute("UPDATE orders SET status_code = ? WHERE order_number = ?", (status_code, order_number))
    conn.commit()
    conn.close()

    suffix = "created_with_shortage" if has_shortage else "created"
    return RedirectResponse(url=f"/?order={order_number}&created_order={order_number}&result={suffix}", status_code=303)


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
        (package["id"], event_type, status, location.strip(), counterparty.strip() or None, event_time, note.strip() or None, order_number.strip() or None),
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
    order_number: str = Form(default=""),
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
        (package_id, first_location.strip(), produced_at, note.strip(), order_number.strip() or None),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(url=f"/?code={package_code.strip()}", status_code=303)
