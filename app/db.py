"""SQLite storage: runs, live stage events, and the invoice ledger (what has been billed against each PO)."""

import csv
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from app.config import get_settings
from app.identifiers import invoice_number_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    file_name TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    file_path TEXT NOT NULL,
    source TEXT NOT NULL,
    extraction_mode TEXT NOT NULL,
    extraction_method TEXT,
    status TEXT NOT NULL,
    decision TEXT,
    headline TEXT,
    vendor_id TEXT,
    vendor_name TEXT,
    invoice_number TEXT,
    po_number TEXT,
    total REAL,
    duration_ms INTEGER,
    extracted_json TEXT,
    result_json TEXT,
    error TEXT,
    reviewer_action TEXT,
    reviewer_note TEXT,
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    state TEXT NOT NULL,
    message TEXT,
    data_json TEXT,
    at TEXT NOT NULL,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_run ON run_events(run_id, id);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    vendor_id TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    invoice_key TEXT NOT NULL,
    po_number TEXT,
    invoice_date TEXT,
    subtotal REAL,
    tax REAL,
    total REAL,
    file_hash TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_lines (
    ledger_id INTEGER NOT NULL,
    po_line_no INTEGER NOT NULL,
    quantity REAL NOT NULL,
    amount REAL NOT NULL
);
"""

_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(get_settings().db_path, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


@contextmanager
def connect():
    connection = _connect()
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db() -> None:
    with connect() as db:
        db.executescript(SCHEMA)
        if db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0:
            _seed_ledger(db)


def _seed_ledger(db: sqlite3.Connection) -> None:
    """Load invoices that were billed before this system existed (history the checks rely on)."""
    with open(get_settings().master_dir / "seed_ledger.csv", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            cursor = db.execute(
                """INSERT INTO ledger (vendor_id, invoice_number, invoice_key, po_number, invoice_date,
                   subtotal, tax, total, status, source, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?, 'history', ?, ?)""",
                (
                    row["vendor_id"], row["invoice_number"], invoice_number_key(row["invoice_number"]),
                    row["po_number"], row["invoice_date"], float(row["subtotal"]), float(row["tax"]),
                    float(row["total"]), row["status"], row["note"], now_iso(),
                ),
            )
            for part in filter(None, row["lines"].split("|")):
                line_no, quantity, amount = part.split(":")
                db.execute(
                    "INSERT INTO ledger_lines (ledger_id, po_line_no, quantity, amount) VALUES (?,?,?,?)",
                    (cursor.lastrowid, int(line_no), float(quantity), float(amount)),
                )


def reset_demo_data() -> None:
    """Wipe all runs and processed invoices; restore the ledger to its seeded history."""
    with _lock, connect() as db:
        for table in ("runs", "run_events", "ledger", "ledger_lines"):
            db.execute(f"DELETE FROM {table}")
        db.execute("DELETE FROM sqlite_sequence")
        _seed_ledger(db)


# ---------- runs and events ----------

def create_run(file_name: str, file_hash: str, file_path: str, source: str, extraction_mode: str) -> int:
    with connect() as db:
        cursor = db.execute(
            """INSERT INTO runs (created_at, file_name, file_hash, file_path, source, extraction_mode, status)
               VALUES (?,?,?,?,?,?, 'running')""",
            (now_iso(), file_name, file_hash, file_path, source, extraction_mode),
        )
        return cursor.lastrowid


def add_event(run_id: int, stage: str, state: str, message: str, data: dict | None = None,
              duration_ms: int | None = None) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO run_events (run_id, stage, state, message, data_json, at, duration_ms) VALUES (?,?,?,?,?,?,?)",
            (run_id, stage, state, message, json.dumps(data or {}), now_iso(), duration_ms),
        )


def update_run(run_id: int, **fields) -> None:
    if not fields:
        return
    columns = ", ".join(f"{name} = ?" for name in fields)
    with connect() as db:
        db.execute(f"UPDATE runs SET {columns} WHERE id = ?", (*fields.values(), run_id))


def get_run(run_id: int) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _run_dict(row) if row else None


def list_runs(limit: int = 200) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_run_dict(row, full=False) for row in rows]


def _run_dict(row: sqlite3.Row, full: bool = True) -> dict:
    run = dict(row)
    for key in ("extracted_json", "result_json"):
        value = run.pop(key)
        if full:
            run[key.removesuffix("_json")] = json.loads(value) if value else None
    run.pop("file_path", None)
    return run


def events_after(run_id: int, after_id: int) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM run_events WHERE run_id = ? AND id > ? ORDER BY id", (run_id, after_id)
        ).fetchall()
    return [{**dict(row), "data": json.loads(row["data_json"] or "{}")} for row in rows]


# ---------- ledger ----------

def ledger_for_vendor(vendor_id: str) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM ledger WHERE vendor_id = ? ORDER BY id", (vendor_id,)).fetchall()
    return [dict(row) for row in rows]


def ledger_by_hash(file_hash: str) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM ledger WHERE file_hash = ?", (file_hash,)).fetchall()
    return [dict(row) for row in rows]


def po_consumption(po_number: str) -> dict:
    """What has already been billed on a PO: approved and pending totals, and approved quantity per PO line."""
    with connect() as db:
        totals = {
            row["status"]: (row["subtotal"] or 0, row["count"])
            for row in db.execute(
                "SELECT status, SUM(subtotal) AS subtotal, COUNT(*) AS count FROM ledger "
                "WHERE po_number = ? GROUP BY status",
                (po_number,),
            )
        }
        lines = {
            row["po_line_no"]: row["quantity"]
            for row in db.execute(
                "SELECT l.po_line_no, SUM(l.quantity) AS quantity FROM ledger_lines l "
                "JOIN ledger g ON g.id = l.ledger_id WHERE g.po_number = ? AND g.status = 'approved' "
                "GROUP BY l.po_line_no",
                (po_number,),
            )
        }
        invoices = [
            dict(row)
            for row in db.execute(
                "SELECT id, invoice_number, invoice_date, subtotal, total, status, source, run_id FROM ledger "
                "WHERE po_number = ? ORDER BY id",
                (po_number,),
            )
        ]
    return {
        "approved_subtotal": round(totals.get("approved", (0, 0))[0], 2),
        "pending_subtotal": round(totals.get("pending", (0, 0))[0], 2),
        "approved_qty_by_line": lines,
        "invoices": invoices,
    }


def record_in_ledger(run_id: int, entry: dict, lines: list[dict]) -> None:
    with connect() as db:
        cursor = db.execute(
            """INSERT INTO ledger (run_id, vendor_id, invoice_number, invoice_key, po_number, invoice_date,
               subtotal, tax, total, file_hash, status, source, note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'run', ?, ?)""",
            (
                run_id, entry["vendor_id"], entry["invoice_number"], invoice_number_key(entry["invoice_number"]),
                entry["po_number"], entry["invoice_date"], entry["subtotal"], entry["tax"], entry["total"],
                entry["file_hash"], entry["status"], entry.get("note"), now_iso(),
            ),
        )
        for line in lines:
            db.execute(
                "INSERT INTO ledger_lines (ledger_id, po_line_no, quantity, amount) VALUES (?,?,?,?)",
                (cursor.lastrowid, line["po_line_no"], line["quantity"], line["amount"]),
            )


def set_ledger_status_for_run(run_id: int, status: str) -> None:
    with connect() as db:
        db.execute("UPDATE ledger SET status = ? WHERE run_id = ?", (status, run_id))


def all_ledger() -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM ledger ORDER BY id DESC")]
