from __future__ import annotations

import random
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

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


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_batch_prefix(medicine_name: str) -> str:
    cleaned = "".join(ch for ch in medicine_name.upper() if ch.isalnum())
    return (cleaned[:2] or "MD").ljust(2, "X")


def generate_batch_number(cur: sqlite3.Cursor, medicine_name: str) -> str:
    prefix = get_batch_prefix(medicine_name)
    base = datetime.now().strftime("%y%m%d%H%M")
    candidate = f"BATCH-{prefix}{base}"
    n = 1
    while cur.execute("SELECT 1 FROM packages WHERE batch_number = ? LIMIT 1", (candidate,)).fetchone():
        n += 1
        candidate = f"BATCH-{prefix}{base}-{n}"
    return candidate


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
            status_code TEXT NOT NULL,
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
    if cur.execute("SELECT COUNT(*) FROM packages").fetchone()[0] == 0:
        seed_all(cur)
    conn.commit()
    conn.close()


def latest_event(cur: sqlite3.Cursor, package_id: int):
    return cur.execute(
        "SELECT * FROM events WHERE package_id = ? ORDER BY datetime(event_time) DESC, id DESC LIMIT 1",
        (package_id,),
    ).fetchone()


def create_package(cur: sqlite3.Cursor, package_code: str, batch_number: str, medicine_name: str, produced_at: datetime):
    cur.execute(
        "INSERT INTO packages (package_code, batch_number, medicine_name, produced_at) VALUES (?, ?, ?, ?)",
        (package_code, batch_number, medicine_name, produced_at.isoformat(timespec="minutes")),
    )
    return cur.lastrowid


def add_event(
    cur: sqlite3.Cursor,
    package_id: int,
    event_type: str,
    status: str,
    location: str,
    event_time: datetime,
    note: str,
    order_number: str | None = None,
    counterparty: str | None = None,
):
    cur.execute(
        """
        INSERT INTO events (package_id, event_type, status, location, counterparty, event_time, note, order_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            package_id,
            event_type,
            status,
            location,
            counterparty,
            event_time.isoformat(timespec="minutes"),
            note,
            order_number,
        ),
    )


def create_manual_package_for_order(cur: sqlite3.Cursor, package_code: str, medicine_name: str, warehouse: str, created_at: str):
    existing = cur.execute("SELECT * FROM packages WHERE package_code = ?", (package_code,)).fetchone()
    if existing:
        return existing

    dt = datetime.fromisoformat(created_at)
    batch_number = generate_batch_number(cur, medicine_name)
    package_id = create_package(cur, package_code, batch_number, medicine_name, dt)
    add_event(
        cur,
        package_id,
        "accepted",
        "accepted",
        warehouse,
        dt,
        "Создана новая партия из одной упаковки по ручному номеру",
        None,
        "Ручной ввод",
    )
    return cur.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()


def free_packages_by_medicine(cur: sqlite3.Cursor, medicine_name: str, limit: int):
    rows = cur.execute(
        """
        SELECT p.*
        FROM packages p
        JOIN (
            SELECT e1.package_id, e1.status, e1.location, e1.order_number, e1.event_time
            FROM events e1
            JOIN (
                SELECT package_id, MAX(datetime(event_time)) AS max_time
                FROM events
                GROUP BY package_id
            ) x ON x.package_id = e1.package_id AND datetime(e1.event_time) = x.max_time
        ) last_event ON last_event.package_id = p.id
        WHERE p.medicine_name = ?
          AND (last_event.order_number IS NULL OR TRIM(last_event.order_number) = '')
          AND last_event.status IN ('accepted', 'moved', 'produced')
        ORDER BY datetime(p.produced_at) ASC, p.id ASC
        LIMIT ?
        """,
        (medicine_name, limit),
    ).fetchall()
    return rows


def seed_all(cur: sqlite3.Cursor) -> None:
    random.seed(42)

    series_plan = [
        ("BATCH-AX2401", "Кардиолекс", 28),
        ("BATCH-BM2402", "Нейрофарм", 26),
        ("BATCH-CT2403", "Иммуновир", 24),
        ("BATCH-DL2404", "Пульсофен", 22),
        ("BATCH-EK2405", "Терафлокс", 27),
        ("BATCH-FR2406", "Гепталин", 21),
        ("BATCH-GS2407", "Реновит", 30),
        ("BATCH-HN2408", "Виромакс", 20),
        ("BATCH-JP2409", "Кардиолекс", 18),
        ("BATCH-KQ2410", "Нейрофарм", 17),
        ("BATCH-LR2411", "Иммуновир", 16),
        ("BATCH-MS2412", "Пульсофен", 15),
    ]

    base_time = datetime.now() - timedelta(days=24)
    package_index = 1

    for batch_number, medicine_name, qty in series_plan:
        for item_idx in range(qty):
            package_code = f"PACK-{package_index:05d}"
            produced_at = base_time + timedelta(hours=package_index * 2)
            package_id = create_package(cur, package_code, batch_number, medicine_name, produced_at)

            first_wh = random.choice(WAREHOUSES)
            add_event(cur, package_id, "produced", "produced", "Производитель", produced_at, "Выпущено на линии")
            add_event(
                cur,
                package_id,
                "accepted",
                "accepted",
                first_wh,
                produced_at + timedelta(hours=random.randint(6, 18)),
                "Первичная приёмка на складе",
            )

            if item_idx % 4 == 0:
                second_wh = random.choice([w for w in WAREHOUSES if w != first_wh])
                add_event(
                    cur,
                    package_id,
                    "moved",
                    "moved",
                    second_wh,
                    produced_at + timedelta(hours=random.randint(20, 40)),
                    "Перемещено между складами, заказ не создан",
                )

            package_index += 1

    order_number_list = [f"ORD-{n}" for n in range(1001, 1125)]

    for idx, order_number in enumerate(order_number_list, start=1):
        if idx <= 35:
            requested_positions = random.randint(2, 4)
        elif idx <= 85:
            requested_positions = random.randint(2, 3)
        else:
            requested_positions = random.randint(2, 3)

        if idx % 7 == 0:
            status_code = "delivered"
        elif idx % 5 == 0:
            status_code = "pickup"
        elif idx % 2 == 0:
            status_code = "seller"
        else:
            status_code = "assembling"

        client_name = random.choice(CLIENTS)
        created_at = (base_time + timedelta(days=idx // 3, hours=idx % 11)).isoformat(timespec="minutes")

        cur.execute(
            "INSERT INTO orders (order_number, client_name, status_code, created_at, comment) VALUES (?, ?, ?, ?, ?)",
            (
                order_number,
                client_name,
                status_code,
                created_at,
                "Сгенерировано для демо-показа",
            ),
        )
        order_id = cur.lastrowid

        selected_medicines = random.sample(MEDICINES, k=min(requested_positions, len(MEDICINES)))
        for med_idx, medicine_name in enumerate(selected_medicines):
            quantity = 1
            if idx <= 35:
                quantity = random.randint(2, 4)
            elif idx <= 85:
                quantity = random.randint(2, 3)
            else:
                quantity = random.randint(1, 2)

            packages = free_packages_by_medicine(cur, medicine_name, quantity)
            assigned = list(packages)

            while len(assigned) < quantity:
                manual_code = f"PACK-X{idx:03d}{med_idx}{len(assigned)+1}"
                manual_package = create_manual_package_for_order(
                    cur,
                    manual_code,
                    medicine_name,
                    random.choice(WAREHOUSES),
                    created_at,
                )
                assigned.append(manual_package)

            cur.execute(
                "INSERT INTO order_items (order_id, medicine_name, quantity, assigned_quantity) VALUES (?, ?, ?, ?)",
                (order_id, medicine_name, quantity, len(assigned)),
            )

            for p_idx, package in enumerate(assigned):
                pack_row = cur.execute("SELECT * FROM packages WHERE id = ?", (package["id"],)).fetchone()
                last = latest_event(cur, pack_row["id"])
                event_time = datetime.fromisoformat(created_at) + timedelta(minutes=(med_idx * 20) + (p_idx + 1) * 3)
                start_location = last["location"] if last else random.choice(WAREHOUSES)

                add_event(
                    cur,
                    pack_row["id"],
                    "accepted",
                    "accepted",
                    start_location,
                    event_time,
                    "Упаковка зарезервирована под заказ",
                    order_number,
                    client_name,
                )

                if status_code in {"seller", "pickup", "delivered"}:
                    seller_location = random.choice(WAREHOUSES)
                    add_event(
                        cur,
                        pack_row["id"],
                        "moved",
                        "moved",
                        seller_location,
                        event_time + timedelta(hours=8),
                        "Передано продавцу",
                        order_number,
                        client_name,
                    )

                if status_code in {"pickup", "delivered"}:
                    add_event(
                        cur,
                        pack_row["id"],
                        "shipped",
                        "shipped",
                        "Пункт выдачи",
                        event_time + timedelta(hours=18),
                        "Подготовлено к выдаче клиенту",
                        order_number,
                        client_name,
                    )

                if status_code == "delivered":
                    add_event(
                        cur,
                        pack_row["id"],
                        "delivered",
                        "delivered",
                        "Получено пользователем",
                        event_time + timedelta(hours=28),
                        "Клиент подтвердил получение",
                        order_number,
                        client_name,
                    )


def package_payload(cur: sqlite3.Cursor, package_row: sqlite3.Row) -> dict:
    last = latest_event(cur, package_row["id"])
    return {
        "id": package_row["id"],
        "package_code": package_row["package_code"],
        "batch_number": package_row["batch_number"],
        "medicine_name": package_row["medicine_name"],
        "produced_at": package_row["produced_at"],
        "last_location": last["location"] if last else "—",
        "status_label": PACKAGE_STATUSES.get(last["status"], "Нет статуса") if last else "Нет статуса",
        "last_event_time": last["event_time"] if last else None,
        "order_number": last["order_number"] if last else None,
        "raw_status": last["status"] if last else "accepted",
    }


def stock_summary(cur: sqlite3.Cursor) -> list[dict]:
    rows = cur.execute(
        """
        SELECT p.medicine_name, COUNT(*) AS qty
        FROM packages p
        JOIN (
            SELECT e1.package_id, e1.status, e1.order_number, e1.event_time
            FROM events e1
            JOIN (
                SELECT package_id, MAX(datetime(event_time)) AS max_time
                FROM events
                GROUP BY package_id
            ) x ON x.package_id = e1.package_id AND datetime(e1.event_time) = x.max_time
        ) last_event ON last_event.package_id = p.id
        WHERE last_event.order_number IS NULL OR TRIM(last_event.order_number) = ''
        GROUP BY p.medicine_name
        ORDER BY qty DESC, p.medicine_name ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def order_summary_rows(cur: sqlite3.Cursor, search_order: str = "") -> list[dict]:
    params = []
    query = """
        SELECT *
        FROM orders
        WHERE 1=1
    """
    if search_order:
        query += " AND order_number LIKE ?"
        params.append(f"%{search_order.strip()}%")
    query += " ORDER BY id ASC"

    rows = cur.execute(query, params).fetchall()
    result = []

    for order in rows:
        packages = cur.execute(
            """
            SELECT DISTINCT p.*
            FROM packages p
            JOIN events e ON e.package_id = p.id
            WHERE e.order_number = ?
            ORDER BY p.id
            """,
            (order["order_number"],),
        ).fetchall()

        items = [package_payload(cur, pkg) for pkg in packages]
        if not items:
            continue

        if search_order:
            pass

        result.append(
            {
                "order_number": order["order_number"],
                "items_count": len(items),
                "medicine_count": len(set(item["medicine_name"] for item in items)),
                "locations": dict(Counter(item["last_location"] for item in items)),
                "latest_time": max(item["last_event_time"] for item in items if item["last_event_time"]),
                "status_code": order["status_code"],
                "status_label": ORDER_STATUS_META[order["status_code"]]["label"],
                "status_tone": ORDER_STATUS_META[order["status_code"]]["tone"],
                "items": items,
            }
        )
    return result


def dashboard_data(search_code: str = "", search_batch: str = "", search_order: str = "") -> dict:
    conn = get_conn()
    cur = conn.cursor()

    orders = order_summary_rows(cur, search_order)
    if search_code:
        orders = [o for o in orders if any(search_code.strip().lower() in item["package_code"].lower() for item in o["items"])]
    if search_batch:
        orders = [o for o in orders if any(search_batch.strip().lower() in item["batch_number"].lower() for item in o["items"])]

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

    payload = package_payload(cur, package)
    history = cur.execute(
        "SELECT * FROM events WHERE package_id = ? ORDER BY datetime(event_time) DESC, id DESC",
        (package["id"],),
    ).fetchall()
    conn.close()
    return {"package": payload, "history": [dict(row) for row in history]}


@app.get("/api/order/{order_number}", response_class=JSONResponse)
def order_details(order_number: str):
    conn = get_conn()
    cur = conn.cursor()

    order = cur.execute("SELECT * FROM orders WHERE order_number = ?", (order_number,)).fetchone()
    if not order:
        conn.close()
        return JSONResponse({"error": "not_found"}, status_code=404)

    packages = cur.execute(
        """
        SELECT DISTINCT p.*
        FROM packages p
        JOIN events e ON e.package_id = p.id
        WHERE e.order_number = ?
        ORDER BY p.id
        """,
        (order_number,),
    ).fetchall()

    items = [package_payload(cur, pkg) for pkg in packages]
    warehouses = dict(Counter(item["last_location"] for item in items))
    conn.close()

    return {
        "order_number": order_number,
        "total_packages": len(items),
        "medicines": sorted(list({item["medicine_name"] for item in items})),
        "warehouses": warehouses,
        "status_label": ORDER_STATUS_META[order["status_code"]]["label"],
        "status_tone": ORDER_STATUS_META[order["status_code"]]["tone"],
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
    conn = get_conn()
    cur = conn.cursor()

    last = cur.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 1").fetchone()
    next_num = (last["id"] + 1001) if last else 1001
    order_number = f"ORD-{next_num}"

    cur.execute(
        "INSERT INTO orders (order_number, client_name, status_code, created_at, comment) VALUES (?, ?, ?, ?, ?)",
        (order_number, client_name.strip(), "assembling", created_at, comment.strip() or None),
    )
    order_id = cur.lastrowid

    current_time = datetime.fromisoformat(created_at)

    for idx in range(max(len(medicine_name), len(quantity), len(package_code) if package_code else 0)):
        med = medicine_name[idx].strip() if idx < len(medicine_name) else ""
        qty = int(quantity[idx]) if idx < len(quantity) else 0
        code = package_code[idx].strip() if idx < len(package_code) else ""

        if not med or qty <= 0:
            continue

        assigned = []

        if code:
            manual_or_existing = create_manual_package_for_order(cur, code, med, warehouse, created_at)
            assigned.append(manual_or_existing)

        while len(assigned) < qty:
            free_pack = free_packages_by_medicine(cur, med, 1)
            if free_pack:
                assigned.append(free_pack[0])
            else:
                generated_code = f"PACK-N{order_id:03d}{idx}{len(assigned)+1}"
                assigned.append(create_manual_package_for_order(cur, generated_code, med, warehouse, created_at))

        cur.execute(
            "INSERT INTO order_items (order_id, medicine_name, quantity, assigned_quantity) VALUES (?, ?, ?, ?)",
            (order_id, med, qty, len(assigned)),
        )

        for p_idx, package in enumerate(assigned):
            package_id = package["id"]
            current_time += timedelta(minutes=3)
            add_event(
                cur,
                package_id,
                "accepted",
                "accepted",
                warehouse,
                current_time,
                "Упаковка зарезервирована под новый заказ",
                order_number,
                client_name.strip(),
            )

    cur.execute("UPDATE orders SET status_code = 'seller' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()

    return RedirectResponse(url=f"/?order={order_number}", status_code=303)


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
        """
        INSERT INTO events (package_id, event_type, status, location, counterparty, event_time, note, order_number)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
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
