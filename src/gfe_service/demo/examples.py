"""Generate the example request payloads in ``examples/``.

The Information Bundles are written as readable JSON; the Contributor Task embeds an
Information Bundle as a base64 ``Task.input.valueAttachment`` (the PCT IG's preferred
encoding), so it is generated from the readable source.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from ..db import seed_data as s
from ..fhir import constants as c

BASE = "http://example.org/fhir"


def _entry(res: dict[str, Any]) -> dict[str, Any]:
    return {"fullUrl": f"{BASE}/{res['resourceType']}/{res['id']}", "resource": res}


def _patient(pid: str, family: str, given: str, gender: str, dob: str, address: str) -> dict[str, Any]:
    return {
        "resourceType": "Patient", "id": pid, "meta": {"profile": [c.P_US_CORE_PATIENT]},
        "identifier": [{"system": "http://example.org/identifiers/mrn", "value": pid.upper()}],
        "name": [{"text": f"{given} {family}", "family": family, "given": [given]}],
        "gender": gender, "birthDate": dob, "address": [{"text": address}],
    }


def _payer_umbrella() -> dict[str, Any]:
    return {
        "resourceType": "Organization", "id": "org1001", "meta": {"profile": [c.P_ORGANIZATION]},
        "identifier": [{"type": {"coding": [{"system": c.CS_V2_0203, "code": "ETIN"}]}, "value": "ETIN-3200002"}],
        "active": True, "type": [{"coding": [{"system": c.CS_ORG_TYPE, "code": "pay", "display": "Payer"}]}],
        "name": "Umbrella Insurance Company",
    }


def _payer_acme() -> dict[str, Any]:
    return {
        "resourceType": "Organization", "id": "payer-acme", "meta": {"profile": [c.P_ORGANIZATION]},
        "identifier": [{"type": {"coding": [{"system": c.CS_V2_0203, "code": "ETIN"}]}, "value": "ETIN-4400001"}],
        "active": True, "type": [{"coding": [{"system": c.CS_ORG_TYPE, "code": "pay", "display": "Payer"}]}],
        "name": "Acme Health Plan",
    }


def _payer_bcbsma() -> dict[str, Any]:
    return {
        "resourceType": "Organization", "id": "payer-bcbsma", "meta": {"profile": [c.P_ORGANIZATION]},
        "active": True, "type": [{"coding": [{"system": c.CS_ORG_TYPE, "code": "pay", "display": "Payer"}]}],
        "name": "Blue Cross Blue Shield of Massachusetts",
    }


def _coverage(cid: str, patient_id: str, payer_id: str, plan_value: str, subscriber: str) -> dict[str, Any]:
    return {
        "resourceType": "Coverage", "id": cid, "meta": {"profile": [c.P_COVERAGE]},
        "extension": [{"url": c.X_COVERAGE_KIND, "valueCode": "insurance"}],
        "status": "active", "subscriberId": subscriber,
        "beneficiary": {"reference": f"Patient/{patient_id}"},
        "relationship": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/subscriber-relationship", "code": "self"}]},
        "period": {"start": "2026-01-01", "end": "2026-12-31"},
        "payor": [{"reference": f"Organization/{payer_id}"}],
        "class": [{"type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/coverage-class", "code": "plan"}]},
                   "value": plan_value, "name": plan_value}],
    }


def _provider_org(npi: str, name: str) -> dict[str, Any]:
    return {
        "resourceType": "Organization", "id": f"npi-{npi}", "meta": {"profile": [c.P_ORGANIZATION]},
        "identifier": [{"system": c.NPI_SYSTEM, "value": npi}], "active": True,
        "type": [{"coding": [{"system": c.CS_ORG_TYPE, "code": "prov", "display": "Healthcare Provider"}]}],
        "name": name,
    }


def _service_request(sid: str, patient_id: str, cpt: str, display: str, dx: tuple[str, str], when: str | None,
                     performer: str | None = None) -> dict[str, Any]:
    res: dict[str, Any] = {
        "resourceType": "ServiceRequest", "id": sid,
        "meta": {"profile": [f"{c.SD}/davinci-pct-servicerequest"]},
        "status": "active", "intent": "proposal",
        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": cpt, "display": display}]},
        "subject": {"reference": f"Patient/{patient_id}"},
    }
    if when:
        res["occurrenceDateTime"] = when
    if performer:
        res["performer"] = [{"reference": performer}]
    res["reasonCode"] = [{"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": dx[0], "display": dx[1]}]}]
    return res


def _info_bundle(bid: str, timestamp: str, resources: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle", "id": bid, "meta": {"profile": [c.P_GFE_INFORMATION_BUNDLE]},
        "identifier": {"system": f"{BASE}/identifiers/bundle", "value": bid},
        "type": "collection", "timestamp": timestamp, "entry": [_entry(r) for r in resources],
    }


def mri_knee_insured() -> dict[str, Any]:
    patient = _patient("patient1001", "Betterhalf", "Eve", "female", "1955-07-23", "222 Burlington Road, Bedford MA 01730")
    return _info_bundle("info-mri-knee-1", "2026-10-06T09:00:00-04:00", [
        patient,
        _coverage("coverage1001", "patient1001", "org1001", "Premim Family Plus", "PFP123450000"),
        _payer_umbrella(),
        _provider_org(s.BEDFORD, "Bedford Regional Medical Center"),
        _service_request("sr-mri-knee", "patient1001", "73722",
                         "Magnetic resonance (eg, proton) imaging, any joint of lower extremity; with contrast material(s)",
                         ("M25.561", "Pain in right knee"), "2026-11-12", f"Organization/npi-{s.BEDFORD}"),
        {
            "resourceType": "DeviceRequest", "id": "dr-knee-brace",
            "meta": {"profile": [f"{c.SD}/davinci-pct-devicerequest"]},
            "status": "active", "intent": "proposal",
            "codeCodeableConcept": {"coding": [{"system": "http://www.cms.gov/Medicare/Coding/HCPCSReleaseCodeSets", "code": "L1820",
                                                "display": "Knee orthosis, elastic with condylar pads and joint"}]},
            "subject": {"reference": "Patient/patient1001"},
        },
    ])


def brain_mri_self_pay() -> dict[str, Any]:
    patient = _patient("patient2002", "Cashman", "Riley", "male", "1988-03-14", "9 Muzzey Street, Lexington MA 02421")
    return _info_bundle("info-brain-mri-selfpay-1", "2026-10-06T09:05:00-04:00", [
        patient,
        _provider_org(s.LEXINGTON, "Lexington Imaging Center"),
        _service_request("sr-brain-mri", "patient2002", "70551",
                         "Magnetic resonance (eg, proton) imaging, brain (including brain stem); without contrast material",
                         ("R51.9", "Headache, unspecified"), None, f"Organization/npi-{s.LEXINGTON}"),
    ])


def knee_arthroscopy() -> dict[str, Any]:
    patient = _patient("patient3003", "Rivera", "Alex", "female", "1979-11-02", "41 Great Road, Bedford MA 01730")
    return _info_bundle("info-knee-scope-1", "2026-10-06T09:10:00-04:00", [
        patient,
        _coverage("coverage3003", "patient3003", "payer-acme", "Acme HMO Gold", "ACM778899001"),
        _payer_acme(),
        _provider_org(s.BEDFORD, "Bedford Regional Medical Center"),
        _service_request("sr-knee-scope", "patient3003", "29881",
                         "Arthroscopy, knee, surgical; with meniscectomy (medial OR lateral)",
                         ("M23.221", "Derangement of posterior horn of medial meniscus due to old tear, right knee"),
                         "2026-11-20", f"Organization/npi-{s.BEDFORD}"),
    ])


def _attachment(bundle: dict[str, Any]) -> dict[str, Any]:
    return {"contentType": "application/fhir+json",
            "data": base64.b64encode(json.dumps(bundle, separators=(",", ":")).encode()).decode()}


def _task_timing() -> list[dict[str, Any]]:
    return [
        {"url": c.X_REQUEST_INITIATION_TIME, "valueInstant": "2026-10-06T09:10:00-04:00"},
        {"url": c.X_TASK_REQUESTED_PERIOD, "valuePeriod": {"end": "2026-10-09T17:00:00-04:00"}},
        {"url": c.X_PLANNED_SERVICE_PERIOD, "valuePeriod": {"start": "2026-11-20T08:00:00-05:00", "end": "2026-11-20T12:00:00-05:00"}},
    ]


def contributor_task_surgeon() -> dict[str, Any]:
    return {
        "resourceType": "Task", "id": "contrib-task-surgeon-1", "meta": {"profile": [c.P_GFE_CONTRIBUTOR_TASK]},
        "extension": _task_timing(),
        "status": "requested", "intent": "order",
        "code": {"coding": [{"system": c.CS_GFE_REQUEST_TYPE, "code": "gfe-contributor-task"}]},
        "requester": {"reference": f"Organization/npi-{s.BEDFORD}", "display": "Bedford Regional Medical Center"},
        "owner": {"reference": f"Practitioner/npi-{s.CUTTER}", "display": "Sam Cutter, MD",
                  "identifier": {"system": c.NPI_SYSTEM, "value": s.CUTTER}},
        "input": [{"type": {"coding": [{"system": c.CS_DOCUMENT_TYPE, "code": "gfe-information-bundle"}]},
                   "valueAttachment": _attachment(knee_arthroscopy())}],
    }


def emerson_brain_mri_bcbsma() -> dict[str, Any]:
    """A real hospital + network, priced live from Serif Health Neuron (``--prices neuron``)."""
    patient = _patient("patient4004", "Walden", "Henry", "male", "1967-07-12", "12 Main Street, Concord MA 01742")
    return _info_bundle("info-emerson-brain-mri-1", "2026-10-06T09:15:00-04:00", [
        patient,
        _coverage("coverage4004", "patient4004", "payer-bcbsma", "Blue Care Elect", "BCE123456789"),
        _payer_bcbsma(),
        _provider_org(s.EMERSON, "Emerson Hospital"),
        _service_request("sr-brain-mri-emerson", "patient4004", "70551",
                         "Magnetic resonance (eg, proton) imaging, brain (including brain stem); without contrast material",
                         ("R51.9", "Headache, unspecified"), "2026-11-18", f"Organization/npi-{s.EMERSON}"),
    ])


EXAMPLES = {
    "information-bundle-mri-knee.json": mri_knee_insured,
    "information-bundle-brain-mri-self-pay.json": brain_mri_self_pay,
    "information-bundle-knee-arthroscopy.json": knee_arthroscopy,
    "contributor-task-surgeon.json": contributor_task_surgeon,
    "information-bundle-emerson-brain-mri-neuron.json": emerson_brain_mri_bcbsma,
}


def write_examples(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, fn in EXAMPLES.items():
        path = directory / name
        path.write_text(json.dumps(fn(), indent=2) + "\n")
        written.append(path)
    return written
