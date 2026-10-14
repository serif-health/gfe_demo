from conformance import check_gfe_bundle, code, decode_output, ext
from conftest import post

from gfe_service.db import seed_data as s
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


def test_contributor_with_nothing_to_bill_fails_task(client):
    task = ex.contributor_task_surgeon()
    task["owner"] = {"reference": f"Practitioner/npi-{s.THERA}"}
    out = post(client, "/fhir/Task/$gfe-contribute", task).json()
    assert out["status"] == "failed"
    assert "billed by NPI" in out["statusReason"]["text"]


def test_contributor_task_requires_information_bundle_attachment(client):
    task = ex.contributor_task_surgeon()
    del task["input"]
    r = post(client, "/fhir/Task/$gfe-contribute", task)
    assert r.status_code == 400 and r.json()["issue"][0]["code"] == "required"


def test_coordination_endpoints_are_gone(client):
    assert client.post("/fhir/$gfe-coordination-request", json={}).status_code in (404, 405)
    assert client.get("/fhir/Task/x/$gfe-retrieve").status_code in (404, 405)
