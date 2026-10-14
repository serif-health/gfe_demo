"""Build Da Vinci PCT resources (GFE Claims, GFE Bundle, Missing Bundle, GFE Packet).

Profiles targeted (PCT IG 2.0.x):
  * davinci-pct-gfe-summary / -institutional / -professional  (Claim)
  * davinci-pct-gfe-bundle / -gfe-missing-bundle               (Bundle, collection)
  * davinci-pct-gfe-composition + davinci-pct-gfe-packet       (Bundle, document)
"""

from __future__ import annotations

import base64
import copy
import json
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..domain import EstimateRequest, PricedLine, Provider, ProviderEstimate
from . import constants as c
from .parse import now_instant, npi_of, ref_of


def new_id() -> str:
    return str(uuid.uuid4())


def money(value: float) -> dict[str, Any]:
    return {"value": round(value, 2), "currency": "USD"}


def coding(system: str, code: str, display: str | None = None) -> dict[str, Any]:
    out = {"system": system, "code": code}
    if display:
        out["display"] = display
    return out


def concept(system: str, code: str, display: str | None = None) -> dict[str, Any]:
    return {"coding": [coding(system, code, display)]}


def code_system_for(code_type: str) -> str:
    return c.TYPE_TO_CODE_SYSTEM.get(code_type, f"urn:code-type:{code_type}")


def modifier_system(modifier: str) -> str:
    # 26 is a CPT modifier; TC / AA etc. are HCPCS Level II modifiers.
    return c.TYPE_TO_CODE_SYSTEM["CPT"] if modifier.isdigit() else c.TYPE_TO_CODE_SYSTEM["HCPCS"]


def reference(resource: dict[str, Any], display: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"reference": ref_of(resource)}
    if display or resource.get("name"):
        out["display"] = display or display_name(resource)
    return out


def logical_reference(resource: dict[str, Any]) -> dict[str, Any]:
    """Reference by NPI identifier, for authors whose resource lives inside a nested GFE Bundle."""
    out: dict[str, Any] = {"type": resource["resourceType"], "display": display_name(resource)}
    npi = npi_of(resource)
    if npi:
        out["identifier"] = {"system": c.NPI_SYSTEM, "value": npi}
    else:
        out = reference(resource)
    return out


def display_name(resource: dict[str, Any]) -> str:
    name = resource.get("name")
    if isinstance(name, str):
        return name
    if isinstance(name, list) and name:
        n = name[0]
        return n.get("text") or " ".join([*n.get("given", []), n.get("family", "")]).strip()
    return ref_of(resource)


def entry(base: str, resource: dict[str, Any]) -> dict[str, Any]:
    return {"fullUrl": f"{base}/{resource['resourceType']}/{resource['id']}", "resource": resource}


# --- supporting resources --------------------------------------------------------------------


def provider_resource(provider: Provider, supplied: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reuse the resource from the request if the requester sent one, else build from the DB."""
    if supplied:
        return copy.deepcopy(supplied)
    telecom = [{"system": "phone", "value": provider.phone, "use": "work"}] if provider.phone else []
    address = [{
        "line": [provider.address_line] if provider.address_line else [],
        "city": provider.city, "state": provider.state, "postalCode": provider.postal_code, "country": "US",
    }] if provider.address_line else []
    identifiers = [{"system": c.NPI_SYSTEM, "value": provider.npi}]
    if provider.tin:
        identifiers.append({"type": concept(c.CS_V2_0203, "TAX"), "system": c.TIN_SYSTEM, "value": provider.tin})
    if provider.entity_type == "organization":
        res: dict[str, Any] = {
            "resourceType": "Organization", "id": f"npi-{provider.npi}",
            "meta": {"profile": [c.P_ORGANIZATION]},
            "identifier": identifiers, "active": True,
            "type": [concept(c.CS_ORG_TYPE, "prov", "Healthcare Provider")],
            "name": provider.name,
        }
    else:
        res = {
            "resourceType": "Practitioner", "id": f"npi-{provider.npi}",
            "meta": {"profile": [c.P_PRACTITIONER]},
            "identifier": identifiers, "active": True,
            "name": [{"text": provider.name, "family": provider.family_name or provider.name,
                      "given": [provider.given_name] if provider.given_name else []}],
        }
    if telecom:
        res["telecom"] = telecom
    if address:
        res["address"] = address
    return res


def self_pay_coverage(patient: dict[str, Any]) -> dict[str, Any]:
    return {
        "resourceType": "Coverage", "id": f"self-pay-{patient.get('id', 'patient')}",
        "meta": {"profile": [c.P_COVERAGE]},
        "extension": [{"url": c.X_COVERAGE_KIND, "valueCode": "self-pay"}],
        "status": "active",
        "beneficiary": reference(patient),
        "payor": [reference(patient)],
    }


def operation_outcome(issues: list[tuple[str, str, str]] | None = None, *, expression: str | None = None) -> dict[str, Any]:
    """issues: (severity, code, diagnostics)."""
    out_issues = []
    for severity, code, diagnostics in issues or []:
        issue: dict[str, Any] = {"severity": severity, "code": code, "diagnostics": diagnostics}
        if expression:
            issue["expression"] = [expression]
        out_issues.append(issue)
    return {"resourceType": "OperationOutcome", "id": new_id(), "issue": out_issues}


# --- Packet context -------------------------------------------------------------------------


@dataclass
class PacketContext:
    """Everything shared by the GFE Bundles and Packet built for one request."""

    base: str
    request: EstimateRequest
    disclaimer: str
    patient: dict[str, Any] = field(init=False)
    coverage: dict[str, Any] = field(init=False)
    payer_org: dict[str, Any] | None = field(init=False)
    created: str = field(default_factory=lambda: date.today().isoformat())

    def __post_init__(self) -> None:
        self.patient = self.request.patient
        self.coverage = self.request.coverage or self_pay_coverage(self.patient)
        self.payer_org = self.request.payer_org

    def insurance(self) -> list[dict[str, Any]]:
        return [{"sequence": 1, "focal": True, "coverage": {"reference": ref_of(self.coverage)}}]

    def diagnoses(self) -> list[dict[str, Any]]:
        out = []
        for i, dx in enumerate(self.request.diagnoses, start=1):
            out.append({
                "sequence": i,
                "diagnosisCodeableConcept": concept(dx.system or c.TYPE_TO_CODE_SYSTEM["ICD10CM"], dx.code, dx.display),
                "type": [concept(c.CS_DIAGNOSIS_TYPE, "principal" if i == 1 else "secondary")],
            })
        return out

    def serviced(self) -> dict[str, Any]:
        start = self.request.planned_start or date.today()
        end = self.request.planned_end
        if end and end != start:
            return {"servicedPeriod": {"start": start.isoformat(), "end": end.isoformat()}}
        return {"servicedDate": start.isoformat()}

    def linking_extension(self) -> dict[str, Any] | None:
        if not self.request.planned_start:
            return None
        start, end = self.request.planned_start, self.request.planned_end
        if end and end != start:
            part = {"url": "plannedPeriodOfService", "valuePeriod": {"start": start.isoformat(), "end": end.isoformat()}}
        else:
            part = {"url": "plannedPeriodOfService", "valueDate": start.isoformat()}
        return {"url": c.X_GFE_SERVICE_LINKING_INFO, "extension": [part]}


# --- Claims ---------------------------------------------------------------------------------


def build_summary_claim(ctx: PacketContext, total: float) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "resourceType": "Claim", "id": new_id(),
        "meta": {"profile": [c.P_GFE_SUMMARY]},
        "extension": [{"url": c.X_GFE_DISCLAIMER, "valueString": ctx.disclaimer}],
        "status": "active",
        "type": concept(c.CS_ESTIMATE_TYPE_SUMMARY, "estimate-summary", "Estimate Summary"),
        "use": "predetermination",
        "patient": reference(ctx.patient),
    }
    if ctx.request.planned_start:
        claim["billablePeriod"] = {"start": ctx.request.planned_start.isoformat(),
                                   "end": (ctx.request.planned_end or ctx.request.planned_start).isoformat()}
    claim["created"] = ctx.created
    if ctx.payer_org:
        claim["insurer"] = reference(ctx.payer_org)
    claim["provider"] = {"extension": [{"url": c.X_DATA_ABSENT_REASON, "valueCode": "not-applicable"}]}
    claim["priority"] = concept(c.CS_PROCESS_PRIORITY, "normal")
    if ctx.request.diagnoses:
        claim["diagnosis"] = ctx.diagnoses()
    claim["insurance"] = ctx.insurance()
    claim["total"] = money(total)
    return claim


def _location(est: ProviderEstimate) -> dict[str, Any]:
    pos = est.provider.place_of_service or "11"
    return concept(c.CS_POS, pos, {"11": "Office", "22": "On Campus-Outpatient Hospital"}.get(pos))


def _native_claim_item(ctx: PacketContext, seq: int, line: PricedLine, est: ProviderEstimate) -> dict[str, Any]:
    """A Claim.item from a FHIR-native price source: keep it, fill only request context it can't know."""
    item = copy.deepcopy(line.claim_item or {})
    item["sequence"] = seq
    extensions = item.setdefault("extension", [])
    urls = {e.get("url") for e in extensions}
    if c.X_SERVICE_DESCRIPTION not in urls:
        extensions.append({"url": c.X_SERVICE_DESCRIPTION, "valueString": line.component.service_category})
    if c.X_GFE_LINE_CTRL_NUM not in urls:
        extensions.append({"url": c.X_GFE_LINE_CTRL_NUM, "valueIdentifier": {"value": f"{est.provider.npi}-{seq:03d}"}})
    if "servicedDate" not in item and "servicedPeriod" not in item:
        item.update(ctx.serviced())
    if est.billing_class == "professional":
        item.setdefault("locationCodeableConcept", _location(est))
    elif "revenue" not in item:
        item["revenue"] = {"extension": [{"url": c.X_DATA_ABSENT_REASON, "valueCode": "unknown"}]}
    item.setdefault("quantity", {"value": line.quantity})
    item.setdefault("net", money(line.net))
    return item


def _claim_item(ctx: PacketContext, seq: int, line: PricedLine, est: ProviderEstimate) -> dict[str, Any]:
    if line.claim_item:
        return _native_claim_item(ctx, seq, line, est)
    comp = line.component
    source = f". Source: {line.rate_source}" if line.rate_source else ""
    item: dict[str, Any] = {
        "extension": [
            {"url": c.X_SERVICE_DESCRIPTION, "valueString": comp.service_category},
            {"url": c.X_GFE_LINE_CTRL_NUM, "valueIdentifier": {"value": f"{est.provider.npi}-{seq:03d}"}},
            {"url": c.X_PROVIDER_EVENT_METHODOLOGY, "valueString": f"{line.methodology}{source}"},
        ],
        "sequence": seq,
    }
    if est.billing_class == "institutional":
        if comp.revenue_code:
            item["revenue"] = concept(c.CS_REVENUE, comp.revenue_code, line.revenue_display)
        else:
            item["revenue"] = {"extension": [{"url": c.X_DATA_ABSENT_REASON, "valueCode": "unknown"}]}
    item["productOrService"] = concept(code_system_for(comp.code_type), comp.code, line.code_display)
    if line.modifier:
        item["modifier"] = [concept(modifier_system(line.modifier), line.modifier)]
    item.update(ctx.serviced())
    if est.billing_class == "professional":
        item["locationCodeableConcept"] = _location(est)
    item["quantity"] = {"value": line.quantity}
    item["unitPrice"] = money(line.unit_price)
    item["net"] = money(line.net)
    return item


def build_gfe_claim(ctx: PacketContext, est: ProviderEstimate, provider_res: dict[str, Any]) -> dict[str, Any]:
    institutional = est.billing_class == "institutional"
    methodologies = sorted({line.methodology.split(" - ")[0] for line in est.lines})
    extensions = [
        {"url": c.X_PROVIDER_EVENT_METHODOLOGY, "valueString": "; ".join(methodologies)},
        {"url": c.X_GFE_DISCLAIMER, "valueString": ctx.disclaimer},
    ]
    linking = ctx.linking_extension()
    if linking:
        extensions.append(linking)
    claim: dict[str, Any] = {
        "resourceType": "Claim", "id": new_id(),
        "meta": {"profile": [c.P_GFE_INSTITUTIONAL if institutional else c.P_GFE_PROFESSIONAL]},
        "extension": extensions,
        "identifier": [{
            "type": concept(c.CS_V2_0203, "PLAC", "Placer Identifier"),
            "system": f"{ctx.base}/identifiers/gfe",
            "value": f"GFE-{est.provider.npi}-{uuid.uuid4().hex[:10].upper()}",
        }],
        "status": "active",
        "type": concept(c.CS_CLAIM_TYPE, "institutional" if institutional else "professional",
                        "Institutional" if institutional else "Professional"),
        "use": "predetermination",
        "patient": reference(ctx.patient),
        "created": ctx.created,
    }
    if ctx.payer_org:
        claim["insurer"] = reference(ctx.payer_org)
    claim["provider"] = {
        "extension": [{"url": c.X_PROVIDER_TAXONOMY,
                       "valueCodeableConcept": concept(c.CS_TAXONOMY, est.provider.taxonomy_code, est.provider.taxonomy_display)}],
        **reference(provider_res),
    }
    claim["priority"] = concept(c.CS_PROCESS_PRIORITY, "normal")
    claim["payee"] = {"type": concept(c.CS_PAYEE_TYPE, "provider")}
    if institutional:
        claim["supportingInfo"] = [{
            "sequence": 1,
            "category": concept(c.CS_SUPPORTING_INFO_TYPE, "typeofbill"),
            "code": concept(c.CS_TYPE_OF_BILL, "0131", "Hospital Outpatient - Admit through Discharge Claim"),
        }]
    if ctx.request.diagnoses:
        claim["diagnosis"] = ctx.diagnoses()
    claim["insurance"] = ctx.insurance()
    claim["item"] = [_claim_item(ctx, i, line, est) for i, line in enumerate(est.lines, start=1)]
    claim["total"] = money(est.total)
    return claim


# --- Bundles --------------------------------------------------------------------------------


def _bundle(ctx: PacketContext, profile: str, resources: list[dict[str, Any]]) -> dict[str, Any]:
    bid = new_id()
    return {
        "resourceType": "Bundle", "id": bid,
        "meta": {"profile": [profile]},
        "identifier": {"system": f"{ctx.base}/identifiers/bundle", "value": bid},
        "type": "collection",
        "timestamp": now_instant(),
        "entry": [entry(ctx.base, r) for r in resources],
    }


def _context_resources(ctx: PacketContext) -> list[dict[str, Any]]:
    out = [ctx.patient, ctx.coverage]
    if ctx.payer_org:
        out.append(ctx.payer_org)
    return out


def build_gfe_bundle(ctx: PacketContext, est: ProviderEstimate) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (GFE Bundle, provider resource) for one billing provider."""
    provider_res = provider_resource(est.provider, est.resource)
    gfe_claim = build_gfe_claim(ctx, est, provider_res)
    summary = build_summary_claim(ctx, est.total)
    bundle = _bundle(ctx, c.P_GFE_BUNDLE, [summary, gfe_claim, *_context_resources(ctx), provider_res])
    return bundle, provider_res


def build_missing_bundle(ctx: PacketContext, provider_res: dict[str, Any] | None,
                         requested_items: list[dict[str, Any]]) -> dict[str, Any]:
    resources = _context_resources(ctx)
    if provider_res:
        resources.append(provider_res)
    seen: set[str] = set()
    for item in requested_items:
        key = ref_of(item)
        if key not in seen:
            seen.add(key)
            resources.append(item)
    return _bundle(ctx, c.P_GFE_MISSING_BUNDLE, resources)


@dataclass
class PacketSection:
    bundle: dict[str, Any]
    author: dict[str, Any]          # provider resource (Organization / Practitioner)
    missing: bool = False


def build_packet(ctx: PacketContext, sections: list[PacketSection],
                 initiating_provider: dict[str, Any] | None) -> dict[str, Any]:
    top_level = [ctx.patient, ctx.coverage]
    top_refs = {ref_of(ctx.patient), ref_of(ctx.coverage)}
    if initiating_provider:
        top_level.append(initiating_provider)
        top_refs.add(ref_of(initiating_provider))
    if ctx.payer_org:
        top_level.append(ctx.payer_org)
        top_refs.add(ref_of(ctx.payer_org))

    def author_ref(res: dict[str, Any]) -> dict[str, Any]:
        return reference(res) if ref_of(res) in top_refs else logical_reference(res)

    authors: list[dict[str, Any]] = []
    for sec in sections:
        ref = author_ref(sec.author)
        if ref not in authors:
            authors.append(ref)
    if initiating_provider and author_ref(initiating_provider) not in authors:
        authors.insert(0, author_ref(initiating_provider))

    req = ctx.request
    comp_ext = [
        {"url": c.X_REQUEST_INITIATION_TIME, "valueInstant": req.request_initiation_time},
        {"url": c.X_REQUEST_ORIGINATION_TYPE, "valueCodeableConcept": concept(
            c.CS_GFE_REQUEST_TYPE, "scheduled-request" if req.scheduled else "nonscheduled-request")},
    ]
    linking = ctx.linking_extension()
    if linking:
        comp_ext.append(linking)

    patient_name = display_name(ctx.patient)
    composition = {
        "resourceType": "Composition", "id": new_id(),
        "meta": {"profile": [c.P_GFE_COMPOSITION]},
        "extension": comp_ext,
        "identifier": {"system": f"{ctx.base}/identifiers/composition", "value": new_id()},
        "status": "final",
        "type": concept(c.CS_LOINC, "111480-0", "Good faith estimate"),
        "category": [concept(c.CS_DOCUMENT_TYPE, "estimate")],
        "subject": reference(ctx.patient),
        "date": now_instant(),
        "author": authors or [reference(ctx.patient)],
        "title": f"Good Faith Estimate Packet for {patient_name} - {ctx.created}",
        "section": [
            {"code": concept(c.CS_DOCUMENT_SECTION, "coverage-section"), "entry": [reference(ctx.coverage)]},
            *[
                {"code": concept(c.CS_DOCUMENT_SECTION, "gfe-section"),
                 "author": [author_ref(sec.author)],
                 "entry": [{"reference": ref_of(sec.bundle)}]}
                for sec in sections
            ],
        ],
    }
    packet_id = new_id()
    return {
        "resourceType": "Bundle", "id": packet_id,
        "meta": {"profile": [c.P_GFE_PACKET]},
        "identifier": {"system": f"{ctx.base}/identifiers/bundle", "value": packet_id},
        "type": "document",
        "timestamp": now_instant(),
        "entry": [entry(ctx.base, r) for r in [composition, *top_level, *(s.bundle for s in sections)]],
    }


# --- Task updates (GFE Contributor) ---------------------------------------------------------


def encode_attachment(resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "contentType": "application/fhir+json",
        "data": base64.b64encode(json.dumps(resource, separators=(",", ":")).encode()).decode(),
        "creation": now_instant(),
    }


def _without_gfe_output(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        output for output in task.get("output", []) or []
        if not any(
            coding.get("system") == c.CS_DOCUMENT_TYPE and coding.get("code") == "gfe-bundle"
            for coding in (output.get("type") or {}).get("coding", [])
        )
    ]


def complete_contributor_task(task: dict[str, Any], gfe_bundle: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(task)
    outputs = _without_gfe_output(updated)
    outputs.append({"type": concept(c.CS_DOCUMENT_TYPE, "gfe-bundle"), "valueAttachment": encode_attachment(gfe_bundle)})
    updated["output"] = outputs
    updated["status"] = "completed"
    updated.pop("statusReason", None)
    updated["lastModified"] = now_instant()
    return updated


def fail_contributor_task(task: dict[str, Any], reason: str) -> dict[str, Any]:
    updated = copy.deepcopy(task)
    outputs = _without_gfe_output(updated)
    if outputs:
        updated["output"] = outputs
    else:
        updated.pop("output", None)
    updated["status"] = "failed"
    updated["statusReason"] = {"text": reason}
    updated["lastModified"] = now_instant()
    return updated
