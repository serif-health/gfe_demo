"""Parse inbound PCT resources into domain objects."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any

from ..domain import Coding, EstimateRequest, ProviderHint, RequestedItem
from ..trace import tracer
from . import constants as c


class FhirInputError(Exception):
    """Raised for unprocessable input; rendered as an OperationOutcome."""

    def __init__(self, message: str, *, code: str = "invalid", expression: str | None = None, status: int = 400):
        super().__init__(message)
        self.code = code
        self.expression = expression
        self.status = status


# --- generic helpers ------------------------------------------------------------------------


def now_instant() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def ref_of(resource: dict[str, Any]) -> str:
    return f"{resource['resourceType']}/{resource.get('id')}"


def index_bundle(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map both ``Type/id`` and ``fullUrl`` to each entry resource for reference resolution."""
    idx: dict[str, dict[str, Any]] = {}
    for entry in bundle.get("entry", []) or []:
        res = entry.get("resource")
        if not isinstance(res, dict) or "resourceType" not in res:
            continue
        if res.get("id"):
            idx[ref_of(res)] = res
        if entry.get("fullUrl"):
            idx[entry["fullUrl"]] = res
    return idx


def resolve(reference: dict[str, Any] | None, idx: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not reference or not reference.get("reference"):
        return None
    ref = reference["reference"]
    if ref in idx:
        return idx[ref]
    # absolute URL whose tail is Type/id
    parts = ref.rstrip("/").split("/")
    if len(parts) >= 2:
        return idx.get(f"{parts[-2]}/{parts[-1]}")
    return None


def npi_of(resource_or_reference: dict[str, Any] | None) -> str | None:
    if not resource_or_reference:
        return None
    identifiers = resource_or_reference.get("identifier")
    if isinstance(identifiers, dict):
        identifiers = [identifiers]
    for ident in identifiers or []:
        if ident.get("system") == c.NPI_SYSTEM and ident.get("value"):
            return ident["value"]
    return None


def ext_value(resource: dict[str, Any] | None, url: str) -> Any:
    for ext in (resource or {}).get("extension", []) or []:
        if ext.get("url") == url:
            for k, v in ext.items():
                if k.startswith("value"):
                    return v
    return None


def first_coding(concept: dict[str, Any] | None) -> dict[str, Any] | None:
    codings = (concept or {}).get("coding") or []
    return codings[0] if codings else None


def is_payer_org(org: dict[str, Any]) -> bool:
    for t in org.get("type", []) or []:
        for cd in t.get("coding", []) or []:
            if cd.get("code") == "pay":
                return True
    return False


def unwrap_parameters(body: dict[str, Any], *names: str) -> dict[str, Any]:
    """Accept either the bare resource or a Parameters wrapper (as FHIR operations allow)."""
    if body.get("resourceType") != "Parameters":
        return body
    for p in body.get("parameter", []) or []:
        if p.get("name") in names and isinstance(p.get("resource"), dict):
            return p["resource"]
    raise FhirInputError(f"Parameters must contain a '{names[0]}' parameter with a resource", expression="Parameters.parameter")


# --- Task handling --------------------------------------------------------------------------


def decode_attachment(attachment: dict[str, Any], where: str) -> dict[str, Any]:
    data = attachment.get("data")
    if not data:
        raise FhirInputError("Attachment has no data", expression=where)
    try:
        return json.loads(base64.b64decode(data))
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FhirInputError(f"Attachment is not base64-encoded FHIR JSON: {exc}", expression=where) from exc


def information_bundle_from_task(
    task: dict[str, Any], resolver: Callable[[str], dict[str, Any] | None] | None = None
) -> dict[str, Any] | None:
    """Return the GFE Information Bundle carried in ``Task.input`` (attachment or reference)."""
    for i, inp in enumerate(task.get("input", []) or []):
        coding = first_coding(inp.get("type")) or {}
        if coding.get("code") != "gfe-information-bundle":
            continue
        if "valueAttachment" in inp:
            return decode_attachment(inp["valueAttachment"], f"Task.input[{i}].valueAttachment")
        if "valueReference" in inp and resolver:
            return resolver(inp["valueReference"].get("reference", ""))
    return None


def planned_period_from_task(task: dict[str, Any] | None) -> tuple[date | None, date | None]:
    period = ext_value(task, c.X_PLANNED_SERVICE_PERIOD) or {}
    return to_date(period.get("start")), to_date(period.get("end"))


# --- Information Bundle -> EstimateRequest --------------------------------------------------


_ITEM_CODE_FIELDS = {
    "ServiceRequest": "code",
    "DeviceRequest": "codeCodeableConcept",
    "MedicationRequest": "medicationCodeableConcept",
}


def parse_information_bundle(
    bundle: dict[str, Any],
    *,
    tasks: list[dict[str, Any]] | None = None,
    contributor: ProviderHint | None = None,
    linking_identifier: dict[str, Any] | None = None,
) -> EstimateRequest:
    if bundle.get("resourceType") != "Bundle":
        raise FhirInputError(f"Expected a GFE Information Bundle, got {bundle.get('resourceType')}", expression="resourceType")
    if bundle.get("type") != "collection":
        raise FhirInputError("GFE Information Bundle must have Bundle.type = collection", expression="Bundle.type")

    idx = index_bundle(bundle)
    resources = [e["resource"] for e in bundle.get("entry", []) or [] if isinstance(e.get("resource"), dict)]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in resources:
        by_type.setdefault(r["resourceType"], []).append(r)

    patients = by_type.get("Patient", [])
    if not patients:
        raise FhirInputError("GFE Information Bundle SHALL contain the Patient", code="required", expression="Bundle.entry:patient")
    patient = patients[0]

    coverage = (by_type.get("Coverage") or [None])[0]
    payer_org = None
    if coverage:
        for payor in coverage.get("payor", []) or []:
            candidate = resolve(payor, idx)
            if candidate and candidate.get("resourceType") == "Organization":
                payer_org = candidate
                break

    # Requested items
    items: list[RequestedItem] = []
    for rtype, field in _ITEM_CODE_FIELDS.items():
        for res in by_type.get(rtype, []):
            for cd in (res.get(field) or {}).get("coding", []) or []:
                code_type = c.CODE_SYSTEM_TO_TYPE.get(cd.get("system", ""))
                if code_type and cd.get("code"):
                    qty = ((res.get("quantityQuantity") or {}).get("value")
                           or ((res.get("dispenseRequest") or {}).get("quantity") or {}).get("value")
                           or 1)
                    reasons = [
                        Coding(rc.get("system"), rc["code"], rc.get("display"))
                        for concept in res.get("reasonCode", []) or []
                        for rc in concept.get("coding", []) or []
                        if rc.get("code")
                    ]
                    items.append(RequestedItem(
                        resource_type=rtype, resource_id=res.get("id", ""), code_type=code_type,
                        code=cd["code"], display=cd.get("display"), quantity=float(qty),
                        reasons=reasons, resource=res,
                    ))
                    break
            else:
                tracer.warn(f"{rtype}/{res.get('id')} has no CPT/HCPCS/NDC coding; skipped")
    if not items:
        raise FhirInputError(
            "No requested items with a CPT, HCPCS or NDC code were found "
            "(ServiceRequest.code / DeviceRequest.codeCodeableConcept / MedicationRequest.medicationCodeableConcept)",
            code="required", expression="Bundle.entry:requested-items-service",
        )

    # Providers named in the request
    hints: list[ProviderHint] = []
    seen: set[str] = set()

    def add_hint(res: dict[str, Any] | None) -> None:
        if not res or res.get("resourceType") not in ("Organization", "Practitioner"):
            return
        if res["resourceType"] == "Organization" and (res is payer_org or is_payer_org(res)):
            return
        npi = npi_of(res)
        if npi and npi not in seen:
            seen.add(npi)
            hints.append(ProviderHint(npi=npi, kind=res["resourceType"].lower(), resource=res))

    for item in items:
        for perf in item.resource.get("performer", []) or []:
            add_hint(resolve(perf, idx))
    for role in by_type.get("PractitionerRole", []):
        add_hint(resolve(role.get("practitioner"), idx))
        add_hint(resolve(role.get("organization"), idx))
    for res in by_type.get("Organization", []) + by_type.get("Practitioner", []):
        add_hint(res)

    # Diagnoses: union of item reasons (ICD-10-CM), first is principal
    diagnoses: list[Coding] = []
    for item in items:
        for reason in item.reasons:
            if reason not in diagnoses:
                diagnoses.append(reason)

    # Timing
    tasks = [t for t in (tasks or []) if t]
    initiation = next((ext_value(t, c.X_REQUEST_INITIATION_TIME) for t in tasks if ext_value(t, c.X_REQUEST_INITIATION_TIME)), None)
    initiation = initiation or bundle.get("timestamp") or now_instant()
    planned_start = planned_end = None
    for t in tasks:
        planned_start, planned_end = planned_period_from_task(t)
        if planned_start:
            break
    if not planned_start:
        for item in items:
            occ = item.resource.get("occurrenceDateTime") or (item.resource.get("occurrencePeriod") or {}).get("start")
            if occ:
                planned_start = to_date(occ)
                planned_end = to_date((item.resource.get("occurrencePeriod") or {}).get("end")) or planned_start
                break

    request = EstimateRequest(
        info_bundle=bundle, patient=patient, coverage=coverage, payer_org=payer_org, items=items,
        provider_hints=hints, diagnoses=diagnoses, request_initiation_time=initiation,
        planned_start=planned_start, planned_end=planned_end, linking_identifier=linking_identifier,
        contributor_npi=contributor.npi if contributor else None,
    )
    if contributor and contributor.npi not in seen:
        hints.insert(0, contributor)
    tracer.emit("fhir.parsed", request=request)
    return request
