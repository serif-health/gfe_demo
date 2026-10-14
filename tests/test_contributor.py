import copy

import pytest

from conformance import check_gfe_bundle, code, decode_output, ext
from conftest import THERA, post

from gfe_service.db import seed_data as s
from gfe_service.db import connect
from gfe_service.demo import examples as ex
from gfe_service.fhir import constants as c


def test_contributor_task_returns_completed_task_with_only_its_lines(client):
    r = post(client, "/fhir/Task/$gfe-contribute", ex.contributor_task_surgeon())
    assert r.status_code == 200, r.text
    task = r.json()
    assert task["status"] == "completed" and task["lastModified"]
    bundle = decode_output(task)
    details = check_gfe_bundle(bundle)
    assert len(details) == 1
    claim = details[0]
    assert claim["provider"]["reference"] == f"Practitioner/npi-{s.CUTTER}"
    assert [code(i["productOrService"]) for i in claim["item"]] == ["29881"]
    assert claim["total"]["value"] == 910.0
    # Planned service period from the Task flows into the claim
    linking = ext(claim, c.X_GFE_SERVICE_LINKING_INFO)
    assert linking["extension"] == [{"url": "plannedPeriodOfService", "valueDate": "2026-11-20"}]


def test_facility_contributor_prices_only_the_facility_line(client):
    task = ex.contributor_task_surgeon()
    task["owner"] = {"reference": f"Organization/npi-{s.BEDFORD}", "identifier": {"system": c.NPI_SYSTEM, "value": s.BEDFORD}}
    claim = check_gfe_bundle(decode_output(post(client, "/fhir/Task/$gfe-contribute", task).json()))[0]
    assert [(code(i["productOrService"]), code(i["revenue"])) for i in claim["item"]] == [("29881", "0360")]
    assert claim["total"]["value"] == 6660.0


def test_contributor_npi_resolved_from_owner_reference_pattern(client):
    task = ex.contributor_task_surgeon()
    del task["owner"]["identifier"]
    assert post(client, "/fhir/Task/$gfe-contribute", task).json()["status"] == "completed"


def test_contributor_with_nothing_to_bill_fails_task(client, additional_test_providers):
    task = ex.contributor_task_surgeon()
    task["owner"] = {"reference": f"Practitioner/npi-{THERA}"}
    out = post(client, "/fhir/Task/$gfe-contribute", task).json()
    assert out["status"] == "failed"
    assert "billed by NPI" in out["statusReason"]["text"]


def test_partially_priced_contributor_fails_without_a_partial_gfe(client, settings):
    task = ex.contributor_task_surgeon()
    task["owner"] = {"reference": f"Organization/npi-{s.BEDFORD}"}
    task["input"][0]["valueAttachment"] = ex._attachment(ex.mri_knee_insured())
    conn = connect(settings.db_path)
    try:
        conn.execute("DELETE FROM negotiated_rates WHERE code = 'L1820'")
        conn.execute("DELETE FROM standard_charges WHERE code = 'L1820'")
        conn.commit()
    finally:
        conn.close()

    response = post(client, "/fhir/Task/$gfe-contribute", task)
    assert response.status_code == 200
    failed = response.json()
    assert failed["status"] == "failed"
    assert "GFE cannot be priced completely" in failed["statusReason"]["text"]
    assert "L1820" in failed["statusReason"]["text"]
    assert "output" not in failed


def test_failed_contributor_preserves_context_and_removes_stale_gfe(client):
    task = ex.contributor_task_cleveland_clinic_ct()
    task["status"] = "failed"
    other_output = {"type": {"text": "Requester note"}, "valueString": "Keep this context"}
    task["output"] = [
        other_output,
        {"type": {"coding": [
            {"system": "http://example.org/types", "code": "old"},
            {"system": c.CS_DOCUMENT_TYPE, "code": "gfe-bundle"},
        ]}, "valueAttachment": {"contentType": "application/fhir+json", "data": "e30="}},
    ]
    original = copy.deepcopy(task)
    response = post(client, "/fhir/Task/$gfe-contribute", task)
    assert response.status_code == 200
    failed = response.json()
    assert failed["status"] == "failed"
    assert failed["output"] == [other_output]
    assert failed["lastModified"]
    for field in ("id", "meta", "partOf", "requester", "owner", "input", "extension"):
        assert failed[field] == task[field]
    assert task == original
    for cpt in ("70450", "71250", "74176"):
        assert cpt in failed["statusReason"]["text"]


@pytest.mark.parametrize("status", ["draft", "rejected", "completed", "entered-in-error"])
def test_contributor_does_not_overwrite_ineligible_task_status(client, status):
    task = ex.contributor_task_surgeon()
    task["status"] = status
    response = post(client, "/fhir/Task/$gfe-contribute", task)
    assert response.status_code == 409
    assert response.json()["issue"][0]["code"] == "conflict"
    assert response.json()["issue"][0]["expression"] == ["Task.status"]


def test_contributor_task_requires_information_bundle_attachment(client):
    task = ex.contributor_task_surgeon()
    del task["input"]
    r = post(client, "/fhir/Task/$gfe-contribute", task)
    assert r.status_code == 400 and r.json()["issue"][0]["code"] == "required"


def test_coordination_endpoints_are_gone(client):
    assert client.post("/fhir/$gfe-coordination-request", json={}).status_code in (404, 405)
    assert client.get("/fhir/Task/x/$gfe-retrieve").status_code in (404, 405)
