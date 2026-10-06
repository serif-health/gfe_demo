"""Database bootstrap: create schema, seed synthetic data, bulk-load CSVs."""

from __future__ import annotations

import csv
import sqlite3
from importlib import resources
from pathlib import Path

from . import seed_data as s

_INSERTS: list[tuple[str, int, list[tuple]]] = [
    ("payers", 9, s.PAYERS),
    ("plans", 6, s.PLANS),
    ("providers", 15, s.PROVIDERS),
    ("provider_affiliations", 2, s.AFFILIATIONS),
    ("role_taxonomies", 2, s.ROLE_TAXONOMIES),
    ("code_descriptions", 3, s.CODE_DESCRIPTIONS),
    ("service_components", 10, s.SERVICE_COMPONENTS),
    ("standard_charges", 7, s.STANDARD_CHARGES),
]

_RATE_COLUMNS = (
    "payer_id, plan_id, npi, tin, billing_class, code_type, code, modifier, "
    "negotiated_type, negotiated_rate, effective_date, expiration_date, source"
)


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str, reset: bool = False, seed: bool = True) -> Path:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset and db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    try:
        schema = resources.files(__package__).joinpath("schema.sql").read_text()
        conn.executescript(schema)
        already_seeded = conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] > 0
        if seed and not already_seeded:
            for table, ncols, rows in _INSERTS:
                placeholders = ", ".join("?" * ncols)
                conn.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)
            conn.executemany(
                f"INSERT INTO negotiated_rates ({_RATE_COLUMNS}) VALUES ({', '.join('?' * 13)})",
                s.NEGOTIATED_RATES,
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def load_csv(db_path: Path | str, table: str, csv_path: Path | str) -> int:
    """Append rows from a CSV whose header names match the table's columns."""
    allowed = {"negotiated_rates", "standard_charges", "providers", "service_components",
               "provider_affiliations", "code_descriptions", "payers", "plans", "role_taxonomies"}
    if table not in allowed:
        raise ValueError(f"table must be one of {sorted(allowed)}")
    conn = connect(db_path)
    try:
        table_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        with open(csv_path, newline="") as fh:
            reader = csv.DictReader(fh)
            cols = [c for c in (reader.fieldnames or []) if c in table_cols]
            if not cols:
                raise ValueError(f"CSV header has no columns matching {table}: {sorted(table_cols)}")
            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
            rows = [
                tuple(row[c] if row[c] != "" else ("" if c == "modifier" else None) for c in cols)
                for row in reader
            ]
        conn.executemany(sql, rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()
