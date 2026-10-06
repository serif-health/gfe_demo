import copy

from conformance import check_gfe_bundle, check_packet, code, decode_output, ext, resources
from conftest import post

from gfe_service.db import seed_data as s
from gfe_service.demo import examples as ex
from gfe_service.fhir import constants as c
from gfe_service.platform.worker import ContributorWorker


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
    assert {"url": "plannedPeriodOfService", "valueDate": "2026-11-20"} in linking["extension"]


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


def test_coordination_workflow_end_to_end(client, app):
    r = post(client, "/fhir/$gfe-coordination-request", ex.coordination_bundle())
    assert r.status_code == 200, r.text
    resp = r.json()
    assert resp["type"] == "transaction-response"
    assert all(e["response"]["status"].startswith("201") for e in resp["entry"])
    tasks = {e["resource"]["id"]: e["resource"] for e in resp["entry"]}
    assert tasks["contrib-surgeon-1"]["partOf"] == [{"reference": "Task/coord-knee-scope-1"}]

    # Before anyone contributes, every contributor is a Missing Bundle
    parts = check_packet(client.get("/fhir/Task/coord-knee-scope-1/$gfe-retrieve").json())
    assert len(parts["missing"]) == 4 and not parts["gfe"]

    for npi in (s.BEDFORD, s.CUTTER, s.STHESIA):
        done = ContributorWorker(app.state.service, npi, client=client, path_prefix="fhir/", trace_http=False).run_once()
        assert [t["status"] for t in done] == ["completed"]

    r = client.get("/fhir/Task/coord-knee-scope-1/$gfe-retrieve")
    packet = r.json()
    parts = check_packet(packet)
    assert len(parts["gfe"]) == 3 and len(parts["missing"]) == 1
    assert resources(parts["missing"][0], "Practitioner")[0]["id"] == f"npi-{s.THERA}"
    totals = sorted(resources(b, "Claim")[0]["total"]["value"] for b in parts["gfe"])
    assert totals == [655.0, 910.0, 6660.0]

    # All GFEs share the coordination linking identifier
    for bundle in parts["gfe"]:
        for claim in resources(bundle, "Claim")[1:]:
            linking = ext(claim, c.X_GFE_SERVICE_LINKING_INFO)
            ident = next(e for e in linking["extension"] if e["url"] == "linkingIdentifier")
            assert ident["valueIdentifier"]["value"] == "GFE-COORD-2026-0001"
    # Requester is the initiating provider at the packet's top level
    assert any(o["id"] == f"npi-{s.BEDFORD}" for o in resources(packet, "Organization"))

    # Task search
    found = client.get("/fhir/Task", params={"owner": f"Practitioner/npi-{s.THERA}", "status": "requested"}).json()
    assert found["total"] == 1


def test_platform_rejects_completed_task_without_gfe_bundle(client):
    post(client, "/fhir/$gfe-coordination-request", ex.coordination_bundle())
    task = copy.deepcopy(client.get("/fhir/Task/contrib-surgeon-1").json())
    task["status"] = "completed"
    r = client.put("/fhir/Task/contrib-surgeon-1", json=task)
    assert r.status_code == 422 and r.json()["resourceType"] == "OperationOutcome"


def test_coordination_bundle_requires_one_coordination_task(client):
    bundle = ex.coordination_bundle()
    bundle["entry"] = bundle["entry"][1:]
    assert post(client, "/fhir/$gfe-coordination-request", bundle).status_code == 400


def test_directory_search_by_npi(client):
    r = client.get("/fhir/Practitioner", params={"identifier": f"http://hl7.org/fhir/sid/us-npi|{s.OLOGIST}"})
    assert r.json()["total"] == 1
    assert client.get("/fhir/Organization/does-not-exist").status_code == 404
