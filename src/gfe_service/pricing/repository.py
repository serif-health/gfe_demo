"""Pricing data access: the seam a provider or data company plugs its own data into.

Two small protocols:

* ``ReferenceData`` - who/what is being priced: payer + plan from the Coverage, the provider
  record for an NPI (billing class, taxonomy), which lines a provider bills for an orderable
  service, and code display names. Usually a small local table you maintain.
* ``PriceSource`` - **one call**: "what does this provider get paid for this code under this
  plan, and what are its published charges?" This is the piece to swap for a pricing API.

A price source answers with a ``PriceQuote`` in one of two shapes:

1. **Pricing terms** (``rate`` and/or ``charge``) - for sources that return their own JSON or
   rows (SQL, the Serif Health Neuron API in ``neuron.py``...). The engine runs the rate
   waterfall (plan-specific > payer-wide, % of charges, self-pay cash, gross charge).
2. **A FHIR-native GFE line** (``claim_item``) - for sources that can already return a
   Da Vinci PCT GFE ``Claim.item`` (``productOrService``, ``modifier``, ``revenue``,
   ``quantity``, ``unitPrice``, ``net``, ``providerEventMethodology``...). It is placed in the
   GFE Claim as-is, with no translation step.

``SqlitePricingRepository`` implements both protocols over ``data/pricing.db``;
``CompositeRepository`` pairs local reference data with any other ``PriceSource``. Every
lookup emits a ``db.query`` trace event so the terminal shows exactly what was fetched.
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
    """An in-network negotiated rate (TiC ``negotiated_type`` / ``negotiated_rate``)."""

    negotiated_type: str        # negotiated | fee schedule | percentage | per diem
    negotiated_rate: float      # dollars, or percent of gross charge when negotiated_type == percentage
    plan_specific: bool         # True: this plan / network; False: every plan of the payer
    source: str
    effective_date: str
    expiration_date: str
    in_effect: bool


@dataclass
class StandardCharge:
    """A provider's published charges (hospital price transparency file / practice fee schedule)."""

    gross_charge: float | None
    discounted_cash: float | None
    source: str


@dataclass
class PriceQuote:
    rate: Rate | None = None
    charge: StandardCharge | None = None
    claim_item: dict[str, Any] | None = None   # FHIR-native: a ready PCT GFE Claim.item


class ReferenceData(Protocol):
    def find_payer(self, etins: list[str], name: str | None) -> Payer | None: ...
    def find_plan(self, payer_id: str, class_values: list[str]) -> Plan | None: ...
    def get_provider(self, npi: str) -> Provider | None: ...
    def service_components(self, code_type: str, code: str) -> list[Component]: ...
    def role_taxonomies(self, role: str) -> list[str]: ...
    def code_display(self, code_type: str, code: str) -> str | None: ...


class PriceSource(Protocol):
    def find_price(
        self, *, provider: Provider, payer: Payer | None, plan: Plan | None,
        code_type: str, code: str, modifier: str, quantity: float, service_date: date,
    ) -> PriceQuote:
        """``payer`` is None for self-pay. Return an empty ``PriceQuote()`` when nothing is on file."""
        ...


class PricingRepository(ReferenceData, PriceSource, Protocol):
    ...


class CompositeRepository:
    """Local reference data + a separate price source (e.g. ``NeuronPriceSource``)."""

    def __init__(self, reference: ReferenceData, prices: PriceSource) -> None:
        self.reference = reference
        self.prices = prices

    def __getattr__(self, name: str) -> Any:
        return getattr(self.reference, name)

    def find_price(self, **kwargs: Any) -> PriceQuote:
        return self.prices.find_price(**kwargs)


def trace_lookup(purpose: str, query: str, params: Any, columns: list[str], rows: list[tuple],
                 elapsed_ms: float, language: str = "sql") -> None:
    tracer.emit("db.query", purpose=purpose, sql=query, params=params, columns=columns, rows=rows,
                elapsed_ms=elapsed_ms, language=language)


def line_label(code_type: str, code: str, modifier: str) -> str:
    return f"{code_type} {code}{'-' + modifier if modifier else ''}"


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

    def _query(self, purpose: str, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        sql = textwrap.dedent(sql).strip()
        start = time.perf_counter()
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        trace_lookup(purpose, sql, list(params), list(rows[0].keys()) if rows else [],
                     [tuple(r) for r in rows], (time.perf_counter() - start) * 1000)
        return rows

    # -- ReferenceData ---------------------------------------------------------------------
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
            SELECT plan_id, payer_id, plan_name, network_name, price_network_id FROM plans
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
        return Provider(**dict(rows[0])) if rows else None

    def service_components(self, code_type: str, code: str) -> list[Component]:
        rows = self._query(
            f"lines billed for {code_type} {code}",
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
            f"taxonomies that bill as '{role}'",
            "SELECT taxonomy_code FROM role_taxonomies WHERE role = ?",
            (role,),
        )
        return [r["taxonomy_code"] for r in rows]

    def code_display(self, code_type: str, code: str) -> str | None:
        row = self._conn.execute(
            "SELECT display FROM code_descriptions WHERE code_type = ? AND code = ?", (code_type, code)
        ).fetchone()
        return row["display"] if row else None

    # -- PriceSource -----------------------------------------------------------------------
    def find_price(
        self, *, provider: Provider, payer: Payer | None, plan: Plan | None,
        code_type: str, code: str, modifier: str, quantity: float = 1, service_date: date,
    ) -> PriceQuote:
        rate = None
        if payer:
            rate = self._find_rate(provider, payer, plan, code_type, code, modifier, service_date)
        return PriceQuote(rate=rate, charge=self._find_charge(provider.npi, code_type, code, modifier))

    def _find_rate(self, provider: Provider, payer: Payer, plan: Plan | None, code_type: str, code: str,
                   modifier: str, service_date: date) -> Rate | None:
        # Plan-specific rates beat payer-wide rates; in-effect rates beat stale ones.
        rows = self._query(
            f"negotiated rate {line_label(code_type, code, modifier)} @ NPI {provider.npi}",
            """
            SELECT negotiated_type, negotiated_rate, plan_id IS NOT NULL AS plan_specific, source,
                   effective_date, expiration_date,
                   (? BETWEEN effective_date AND expiration_date) AS in_effect
            FROM negotiated_rates
            WHERE npi = ? AND payer_id = ? AND billing_class = ?
              AND code_type = ? AND code = ? AND modifier = ?
              AND (plan_id = ? OR plan_id IS NULL)
            ORDER BY in_effect DESC, plan_specific DESC, effective_date DESC
            LIMIT 1
            """,
            (service_date.isoformat(), provider.npi, payer.payer_id, provider.billing_class,
             code_type, code, modifier, plan.plan_id if plan else None),
        )
        if not rows:
            return None
        r = dict(rows[0])
        r["plan_specific"], r["in_effect"] = bool(r["plan_specific"]), bool(r["in_effect"])
        return Rate(**r)

    def _find_charge(self, npi: str, code_type: str, code: str, modifier: str) -> StandardCharge | None:
        rows = self._query(
            f"standard charges {line_label(code_type, code, modifier)} @ NPI {npi}",
            """
            SELECT gross_charge, discounted_cash, source FROM standard_charges
            WHERE npi = ? AND code_type = ? AND code = ? AND modifier = ?
            """,
            (npi, code_type, code, modifier),
        )
        return StandardCharge(**dict(rows[0])) if rows else None


def _json(values: list[Any]) -> str:
    return json.dumps(values)
