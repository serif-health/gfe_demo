import json

from conformance import check_packet, code, ext, profile, resources
from conftest import post

from gfe_service.config import PROJECT_ROOT
from gfe_service.demo import examples as ex
from gfe_service.fhir import constants as c


def _claims_by_provider(parts):
    out = {}
    for bundle in parts["gfe"]:
        for claim in resources(bundle, "Claim"):
            if profile(claim) != c.P_GFE_SUMMARY:
                out[claim["provider"]["display"]] = claim
    return out


def test_insured_mri_packet_prices_facility_contrast_device_and_radiologist(client):
    r = post(client, "/fhir/$gfe-estimate", ex.mri_knee_insured())
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/fhir+json")
    parts = check_packet(r.json())
    assert len(parts["gfe"]) == 2 and not parts["missing"]

    claims = _claims_by_provider(parts)
    facility = claims["Bedford Regional Medical Center"]
    assert profile(facility) == c.P_GFE_INSTITUTIONAL
    lines = {(code(i["productOrService"]), code(i["revenue"])): i["net"]["value"] for i in facility["item"]}
    # plan-specific rate beats payer-wide; contrast is per-ml x 15
    assert lines == {("73722", "0614"): 1320.0, ("A9579", "0636"): 97.5, ("L1820", "0274"): 310.0}
    assert facility["total"]["value"] == 1727.5

    radiologist = claims["Nora Ologist, MD"]
    assert profile(radiologist) == c.P_GFE_PROFESSIONAL
    item = radiologist["item"][0]
    assert code(item["productOrService"]) == "73722" and code(item["modifier"][0]) == "26"
    assert code(item["locationCodeableConcept"]) == "22"
    assert item["net"]["value"] == 98.0
    assert "plan Premim Family Plus Plan" in ext(item, c.X_PROVIDER_EVENT_METHODOLOGY)["valueString"]

    comp = parts["composition"]
    assert code(ext(comp, c.X_REQUEST_ORIGINATION_TYPE)["valueCodeableConcept"]) == "scheduled-request"
    assert item["servicedDate"] == "2026-11-12"
    assert facility["diagnosis"][0]["diagnosisCodeableConcept"]["coding"][0]["code"] == "M25.561"


def test_self_pay_uses_cash_prices_and_technical_component(client):
    r = post(client, "/fhir/$gfe-estimate", ex.brain_mri_self_pay())
    assert r.status_code == 200, r.text
    packet = r.json()
    parts = check_packet(packet)
    coverage = resources(packet, "Coverage")[0]
    assert ext(coverage, c.X_COVERAGE_KIND)["valueCode"] == "self-pay"

    claims = _claims_by_provider(parts)
    imaging = claims["Lexington Imaging Center"]
    assert profile(imaging) == c.P_GFE_PROFESSIONAL  # freestanding center bills on a professional claim
    assert code(imaging["item"][0]["modifier"][0]) == "TC"
    assert imaging["item"][0]["net"]["value"] == 425.0
    assert "insurer" not in imaging
    assert claims["Nora Ologist, MD"]["item"][0]["net"]["value"] == 110.0
    assert code(ext(parts["composition"], c.X_REQUEST_ORIGINATION_TYPE)["valueCodeableConcept"]) == "nonscheduled-request"


def test_percentage_of_charges_contract(client):
    r = post(client, "/fhir/$gfe-estimate", ex.knee_arthroscopy())
    parts = check_packet(r.json())
    claims = _claims_by_provider(parts)
    assert claims["Bedford Regional Medical Center"]["total"]["value"] == 6660.0  # 45% x $14,800
    assert claims["Sam Cutter, MD"]["total"]["value"] == 910.0
    assert claims["Ana Sthesia, MD"]["total"]["value"] == 655.0


def test_ig_example_information_bundle_uses_default_provider(client):
    spec = json.loads((PROJECT_ROOT / "examples/spec/Bundle-PCT-GFE-Information-Bundle-1.json").read_text())
    r = post(client, "/fhir/$gfe-estimate", spec)
    assert r.status_code == 200, r.text
    claims = _claims_by_provider(check_packet(r.json()))
    assert set(claims) == {"Bedford Regional Medical Center", "Nora Ologist, MD"}


def test_parameters_wrapper_is_accepted(client):
    body = {"resourceType": "Parameters", "parameter": [{"name": "resource", "resource": ex.mri_knee_insured()}]}
    assert post(client, "/fhir/$gfe-estimate", body).status_code == 200


def test_unpriceable_component_becomes_missing_bundle(client, settings):
    import sqlite3

    conn = sqlite3.connect(settings.db_path)
    conn.execute("DELETE FROM negotiated_rates WHERE npi = '1430572606'")
    conn.execute("DELETE FROM standard_charges WHERE npi = '1430572606'")
    conn.commit()
    parts = check_packet(post(client, "/fhir/$gfe-estimate", ex.mri_knee_insured()).json())
    assert len(parts["gfe"]) == 1 and len(parts["missing"]) == 1
    assert resources(parts["missing"][0], "Practitioner")[0]["id"] == "npi-1430572606"


def test_invalid_inputs_return_operation_outcome(client):
    no_items = ex.mri_knee_insured()
    no_items["entry"] = [e for e in no_items["entry"] if e["resource"]["resourceType"] not in ("ServiceRequest", "DeviceRequest")]
    for body, status in [
        ({"resourceType": "Patient"}, 400),
        ({**ex.mri_knee_insured(), "type": "transaction"}, 400),
        (no_items, 400),
    ]:
        r = post(client, "/fhir/$gfe-estimate", body)
        assert r.status_code == status
        assert r.json()["resourceType"] == "OperationOutcome"
    r = client.post("/fhir/$gfe-estimate", content="{not json", headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 400 and r.json()["issue"][0]["code"] == "structure"
