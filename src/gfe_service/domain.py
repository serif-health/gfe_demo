"""Plain domain objects passed between the FHIR layer and the pricing layer.

The FHIR parser turns an inbound GFE Information Bundle (or Task) into an
``EstimateRequest``; the pricing engine turns that into an ``EstimateResult``; the FHIR
builders turn the result into GFE Bundles / a GFE Packet. Nothing in here knows about
FHIR JSON or SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Coding:
    system: str | None
    code: str
    display: str | None = None


@dataclass
class RequestedItem:
    """A ServiceRequest / DeviceRequest / MedicationRequest from the Information Bundle."""

    resource_type: str
    resource_id: str
    code_type: str          # normalized: CPT | HCPCS | NDC | ...
    code: str
    display: str | None
    quantity: float = 1.0
    reasons: list[Coding] = field(default_factory=list)
    resource: dict[str, Any] = field(default_factory=dict)
    performer_npi: str | None = None   # provider named on the request (ServiceRequest.performer)


@dataclass
class ProviderHint:
    """A provider named in the request (Organization / Practitioner / Task.owner)."""

    npi: str
    kind: str               # organization | practitioner
    resource: dict[str, Any] | None = None


@dataclass
class EstimateRequest:
    info_bundle: dict[str, Any]
    patient: dict[str, Any]
    coverage: dict[str, Any] | None
    payer_org: dict[str, Any] | None
    items: list[RequestedItem]
    provider_hints: list[ProviderHint]
    diagnoses: list[Coding]
    request_initiation_time: str
    planned_start: date | None = None
    planned_end: date | None = None
    # Set when answering a GFE Contributor Task: price only the lines this provider bills.
    contributor_npi: str | None = None

    @property
    def scheduled(self) -> bool:
        return self.planned_start is not None

    @property
    def self_pay(self) -> bool:
        if self.coverage is None:
            return True
        for ext in self.coverage.get("extension", []):
            if ext.get("url", "").endswith("extension-Coverage.kind"):
                return ext.get("valueCode") == "self-pay"
        return False


# --- pricing-side records --------------------------------------------------------------


@dataclass
class Provider:
    npi: str
    entity_type: str        # organization | practitioner
    name: str
    taxonomy_code: str
    taxonomy_display: str | None
    billing_class: str      # institutional | professional
    place_of_service: str | None = None
    tin: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    phone: str | None = None
    address_line: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None


@dataclass
class Payer:
    payer_id: str
    name: str
    etin: str | None


@dataclass
class Plan:
    plan_id: str
    payer_id: str
    plan_name: str
    network_name: str | None
    # The price source's id for this plan's network, e.g. a Serif Health Neuron network_template_id.
    price_network_id: str | None = None


@dataclass
class Component:
    role: str
    code_type: str
    code: str
    modifier: str
    revenue_code: str | None
    quantity: float
    service_category: str


@dataclass
class PricedLine:
    item: RequestedItem
    component: Component
    code_display: str | None
    revenue_display: str | None
    modifier: str
    quantity: float
    unit_price: float
    methodology: str        # human-readable "how we got this number"
    rate_source: str        # provenance (file / feed)
    # A PCT GFE Claim.item supplied as-is by a FHIR-native price source; used verbatim by the builder.
    claim_item: dict[str, Any] | None = None

    @property
    def net(self) -> float:
        return round(self.unit_price * self.quantity, 2)


@dataclass
class ProviderEstimate:
    provider: Provider
    billing_class: str
    lines: list[PricedLine] = field(default_factory=list)
    resource: dict[str, Any] | None = None   # FHIR resource from the request, if one was supplied

    @property
    def total(self) -> float:
        return round(sum(line.net for line in self.lines), 2)


@dataclass
class UnpricedComponent:
    item: RequestedItem
    component: Component
    provider: Provider | None
    reason: str


@dataclass
class EstimateResult:
    request: EstimateRequest
    payer: Payer | None
    plan: Plan | None
    provider_estimates: list[ProviderEstimate]
    unpriced: list[UnpricedComponent]
    warnings: list[str]

    @property
    def total(self) -> float:
        return round(sum(p.total for p in self.provider_estimates), 2)
