"""Serif Health Neuron as a ``PriceSource`` (https://neuron.serifhealth.com/swagger/openapi.json).

One HTTP call per GFE line:

    GET {base}/rates/v1?codes=70551&npis=1922103357&network_template_ids=<plan's network>
        &billing_classes=institutional&additional_fields=hpt_charge_gross,hpt_dollar
    X-API-KEY: <key>

Neuron answers ``{"70551": {"rates": [rateV1, ...]}}``: payer TiC in-network rates and, for
hospitals, the matching price-transparency gross charge (``hpt_charge_gross``). The only
translation needed is ``rate_from_neuron`` / ``charge_from_neuron`` below. The engine then
applies the same waterfall it uses for SQL data, and the FHIR builders turn the result into
the GFE ``Claim.item``.

Neuron is a payer-network rate search, so:

* the Coverage's plan must map to a Neuron ``network_template_id`` (``plans.price_network_id``);
* self-pay (no network) and unmapped plans go to ``fallback``, typically the provider's own
  ``SqlitePricingRepository`` with its cash prices and chargemaster.
"""

from __future__ import annotations

import os
import time
from datetime import date
from typing import Any

import httpx

from ..domain import Payer, Plan, Provider
from ..trace import tracer
from .repository import PriceQuote, PriceSource, Rate, StandardCharge, line_label, trace_lookup

DEFAULT_BASE_URL = "https://neuron.serifhealth.com/api"
_SHOWN = ("entity_name", "modifier_list", "place_of_service", "negotiation_type", "rate", "hpt_charge_gross",
          "network_name")


def row_modifiers(row: dict[str, Any]) -> set[str]:
    return {m.strip() for m in (row.get("modifier_list") or "").split(",") if m.strip()}


def rate_from_neuron(row: dict[str, Any], service_date: date) -> Rate:
    expiration = row.get("expiration") or "9999-12-31"
    ym = str(row.get("network_year_month") or "")
    return Rate(
        negotiated_type=row.get("negotiation_type") or "negotiated",
        negotiated_rate=float(row["rate"]),   # dollars, or percent of gross charge for 'percentage'
        plan_specific=True,                   # the query was scoped to the plan's network
        source=f"Serif Health Neuron - {row.get('payer_name')} {row.get('network_name')} TiC {ym}".strip(),
        effective_date=f"{ym[:4]}-{ym[4:6]}-01" if len(ym) == 6 else "",
        expiration_date=expiration,
        in_effect=expiration >= service_date.isoformat(),
    )


def charge_from_neuron(row: dict[str, Any]) -> StandardCharge | None:
    if row.get("hpt_charge_gross") is None:
        return None
    return StandardCharge(gross_charge=float(row["hpt_charge_gross"]), discounted_cash=None,
                          source="hospital price transparency file via Serif Health Neuron")


class NeuronPriceSource:
    def __init__(self, api_key: str | None = None, *, base_url: str | None = None,
                 fallback: PriceSource | None = None, client: httpx.Client | None = None,
                 timeout: float = 30) -> None:
        api_key = api_key or os.getenv("GFE_NEURON_API_KEY")
        if not api_key and client is None:
            raise ValueError("Serif Health Neuron needs an API key (set GFE_NEURON_API_KEY)")
        self.base_url = (base_url or os.getenv("GFE_NEURON_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.fallback = fallback
        self.client = client or httpx.Client(headers={"X-API-KEY": api_key or ""}, timeout=timeout)

    def find_price(
        self, *, provider: Provider, payer: Payer | None, plan: Plan | None,
        code_type: str, code: str, modifier: str, quantity: float = 1, service_date: date,
    ) -> PriceQuote:
        label = line_label(code_type, code, modifier)
        if payer is None or plan is None or not plan.price_network_id:
            why = "self-pay" if payer is None else f"plan {plan.plan_name if plan else '?'} has no Neuron network id"
            tracer.step(f"{label}: {why} -> provider's own price list")
            return self._fallback(provider, payer, plan, code_type, code, modifier, quantity, service_date)

        rows = self._rates(provider, code, modifier, plan.price_network_id)
        if rows is None:
            return self._fallback(provider, payer, plan, code_type, code, modifier, quantity, service_date)
        wanted = {modifier} if modifier else set()
        matches = [r for r in rows if row_modifiers(r) == wanted and r.get("rate") is not None
                   and r.get("billing_class", provider.billing_class) == provider.billing_class]
        if not matches:
            return PriceQuote()
        # Several rows can match (place of service, arrangement); prefer the provider's POS and
        # fee-for-service, then the highest rate so the estimate errs on the side of not under-quoting.
        best = max(matches, key=lambda r: (r.get("place_of_service") == provider.place_of_service,
                                           r.get("arrangement") == "ffs", float(r["rate"])))
        charge = charge_from_neuron(best)
        if charge is None and self.fallback:
            charge = self._fallback(provider, None, None, code_type, code, modifier, quantity, service_date).charge
        return PriceQuote(rate=rate_from_neuron(best, service_date), charge=charge)

    def _rates(self, provider: Provider, code: str, modifier: str, network_id: str) -> list[dict[str, Any]] | None:
        params = {"codes": code, "npis": provider.npi, "network_template_ids": network_id,
                  "billing_classes": provider.billing_class,
                  "additional_fields": "hpt_charge_gross,hpt_dollar", "limit": "200"}
        if modifier:
            params["modifiers"] = modifier
        url = f"{self.base_url}/rates/v1"
        start = time.perf_counter()
        try:
            response = self.client.get(url, params=params)
            response.raise_for_status()
            rows = (response.json().get(code) or {}).get("rates") or []
        except (httpx.HTTPError, ValueError) as exc:
            tracer.warn(f"Serif Health Neuron lookup failed ({exc}); using fallback prices")
            return None
        trace_lookup(f"Serif Health Neuron rates {code} @ NPI {provider.npi}",
                     f"GET {url}?{httpx.QueryParams(params)}", params, list(_SHOWN),
                     [tuple(r.get(c) for c in _SHOWN) for r in rows], (time.perf_counter() - start) * 1000,
                     language="http")
        return rows

    def _fallback(self, provider: Provider, payer: Payer | None, plan: Plan | None, code_type: str, code: str,
                  modifier: str, quantity: float, service_date: date) -> PriceQuote:
        if self.fallback is None:
            return PriceQuote()
        return self.fallback.find_price(provider=provider, payer=payer, plan=plan, code_type=code_type, code=code,
                                        modifier=modifier, quantity=quantity, service_date=service_date)
