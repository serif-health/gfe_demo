"""Pricing data access.

``PricingRepository`` is the seam a provider or data company would re-implement against
their own store (Postgres, Snowflake, BigQuery, an internal pricing API...). The SQLite
implementation emits a ``db.query`` trace event for every lookup so the terminal shows
exactly which data was fetched to build each estimate.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from ..db import connect
from ..domain import Component, Payer, Plan, Provider
from ..trace import tracer


@dataclass
class Rate:
    negotiated_type: str
    negotiated_rate: float
    plan_id: str | None
    source: str
    effective_date: str
    expiration_date: str
    in_effect: bool


@dataclass
class StandardCharge:
    gross_charge: float | None
    discounted_cash: float | None
    source: str


class PricingRepository(Protocol):
    def find_payer(self, etins: list[str], name: str | None) -> Payer | None: ...
    def find_plan(self, payer_id: str, class_values: list[str]) -> Plan | None: ...
    def get_provider(self, npi: str) -> Provider | None: ...
    def list_providers(self) -> list[Provider]: ...
    def service_components(self, code_type: str, code: str) -> list[Component]: ...
    def role_taxonomies(self, role: str) -> list[str]: ...
    def affiliated_practitioners(self, facility_npi: str, taxonomies: list[str]) -> list[Provider]: ...
    def affiliated_facilities(self, practitioner_npi: str) -> list[Provider]: ...
    def code_display(self, code_type: str, code: str) -> str | None: ...
    def find_rate(
        self, *, npi: str, payer_id: str, plan_id: str | None, billing_class: str,
        code_type: str, code: str, modifier: str, service_date: date,
    ) -> Rate | None: ...
    def find_standard_charge(self, npi: str, code_type: str, code: str, modifier: str) -> StandardCharge | None: ...


_PROVIDER_COLS = (
    "npi, entity_type, name, taxonomy_code, taxonomy_display, billing_class, place_of_service, "
    "tin, given_name, family_name, phone, address_line, city, state, postal_code"
)


class SqlitePricingRepository:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._conn = connect(self.db_path)

    def close(self) -> None:
        self._conn.close()

    # -- traced query helper ---------------------------------------------------------------
    def _query(self, purpose: str, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        sql = textwrap.dedent(sql).strip()
        start = time.perf_counter()
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        elapsed_ms = (time.perf_counter() - start) * 1000
        tracer.emit(
            "db.query",
            purpose=purpose,
            sql=sql,
            params=list(params),
            columns=list(rows[0].keys()) if rows else [],
            rows=[tuple(r) for r in rows],
            elapsed_ms=elapsed_ms,
        )
        return rows

    @staticmethod
    def _provider(row: sqlite3.Row | None) -> Provider | None:
        return Provider(**dict(row)) if row else None

    # -- lookups ---------------------------------------------------------------------------
    def find_payer(self, etins: list[str], name: str | None) -> Payer | None:
        rows = self._query(
            "resolve payer from Coverage.payor Organization",
            """
            SELECT payer_id, name, etin FROM payers
            WHERE etin IN (SELECT value FROM json_each(?)) OR lower(name) = lower(?)
            ORDER BY CASE WHEN etin IN (SELECT value FROM json_each(?)) THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (_json(etins), name or "", _json(etins)),
        )
        return Payer(**dict(rows[0])) if rows else None

    def find_plan(self, payer_id: str, class_values: list[str]) -> Plan | None:
        rows = self._query(
            "resolve plan from Coverage.class",
            """
            SELECT plan_id, payer_id, plan_name, network_name FROM plans
            WHERE payer_id = ?
              AND (coverage_class_value IN (SELECT value FROM json_each(?))
                   OR plan_name IN (SELECT value FROM json_each(?)))
            LIMIT 1
            """,
            (payer_id, _json(class_values), _json(class_values)),
        )
        return Plan(**dict(rows[0])) if rows else None

    def get_provider(self, npi: str) -> Provider | None:
        rows = self._query("look up provider by NPI", f"SELECT {_PROVIDER_COLS} FROM providers WHERE npi = ?", (npi,))
        return self._provider(rows[0] if rows else None)

    def list_providers(self) -> list[Provider]:
        rows = self._conn.execute(f"SELECT {_PROVIDER_COLS} FROM providers ORDER BY npi").fetchall()
        return [Provider(**dict(r)) for r in rows]

    def service_components(self, code_type: str, code: str) -> list[Component]:
        rows = self._query(
            f"expand {code_type} {code} into billable components",
            """
            SELECT role, code_type, code, modifier, revenue_code, quantity, service_category
            FROM service_components
            WHERE primary_code_type = ? AND primary_code = ?
            ORDER BY seq
            """,
            (code_type, code),
        )
        return [Component(**dict(r)) for r in rows]

    def role_taxonomies(self, role: str) -> list[str]:
        rows = self._query(
            f"taxonomies that can fill role '{role}'",
            "SELECT taxonomy_code FROM role_taxonomies WHERE role = ?",
            (role,),
        )
        return [r["taxonomy_code"] for r in rows]

    def affiliated_practitioners(self, facility_npi: str, taxonomies: list[str]) -> list[Provider]:
        cols = ", ".join(f"p.{c.strip()}" for c in _PROVIDER_COLS.split(","))
        rows = self._query(
            "find affiliated practitioner for role",
            f"""
            SELECT {cols}
            FROM provider_affiliations a JOIN providers p ON p.npi = a.practitioner_npi
            WHERE a.facility_npi = ? AND p.taxonomy_code IN (SELECT value FROM json_each(?))
            ORDER BY p.family_name, p.npi
            """,
            (facility_npi, _json(taxonomies)),
        )
        return [Provider(**dict(r)) for r in rows]

    def affiliated_facilities(self, practitioner_npi: str) -> list[Provider]:
        cols = ", ".join(f"p.{c.strip()}" for c in _PROVIDER_COLS.split(","))
        rows = self._query(
            "find facility where practitioner renders",
            f"""
            SELECT {cols}
            FROM provider_affiliations a JOIN providers p ON p.npi = a.facility_npi
            WHERE a.practitioner_npi = ?
            ORDER BY p.npi
            """,
            (practitioner_npi,),
        )
        return [Provider(**dict(r)) for r in rows]

    def code_display(self, code_type: str, code: str) -> str | None:
        row = self._conn.execute(
            "SELECT display FROM code_descriptions WHERE code_type = ? AND code = ?", (code_type, code)
        ).fetchone()
        return row["display"] if row else None

    def find_rate(
        self, *, npi: str, payer_id: str, plan_id: str | None, billing_class: str,
        code_type: str, code: str, modifier: str, service_date: date,
    ) -> Rate | None:
        # Plan-specific rates beat payer-wide rates; in-effect rates beat stale ones.
        rows = self._query(
            f"negotiated rate {code_type} {code}{'-' + modifier if modifier else ''} @ NPI {npi}",
            """
            SELECT negotiated_type, negotiated_rate, plan_id, source, effective_date, expiration_date,
                   (? BETWEEN effective_date AND expiration_date) AS in_effect
            FROM negotiated_rates
            WHERE npi = ? AND payer_id = ? AND billing_class = ?
              AND code_type = ? AND code = ? AND modifier = ?
              AND (plan_id = ? OR plan_id IS NULL)
            ORDER BY in_effect DESC, (plan_id IS NULL) ASC, effective_date DESC
            LIMIT 1
            """,
            (service_date.isoformat(), npi, payer_id, billing_class, code_type, code, modifier, plan_id),
        )
        if not rows:
            return None
        r = dict(rows[0])
        r["in_effect"] = bool(r["in_effect"])
        return Rate(**r)

    def find_standard_charge(self, npi: str, code_type: str, code: str, modifier: str) -> StandardCharge | None:
        rows = self._query(
            f"standard charges {code_type} {code}{'-' + modifier if modifier else ''} @ NPI {npi}",
            """
            SELECT gross_charge, discounted_cash, source FROM standard_charges
            WHERE npi = ? AND code_type = ? AND code = ? AND modifier = ?
            """,
            (npi, code_type, code, modifier),
        )
        return StandardCharge(**dict(rows[0])) if rows else None


def _json(values: list[Any]) -> str:
    return json.dumps(values)
