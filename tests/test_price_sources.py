"""Swapping the price source: Serif Health Neuron (HTTP JSON) and a FHIR-native Claim.item source."""

from io import StringIO
import os
import warnings
from dataclasses import replace

import httpx
import pytest
from rich.console import Console
from conformance import check_packet, code, ext, resources
from conftest import post

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from gfe_service.app import create_app
from gfe_service.db import seed_data as s
from gfe_service.demo import examples as ex
from gfe_service.fhir import constants as c
from gfe_service.console import make_console_sink
from gfe_service.pricing.neuron import NeuronPriceSource
from gfe_service.pricing.repository import CompositeRepository, PriceQuote, SqlitePricingRepository
from gfe_service.trace import tracer


def _row(**kw):
    """A synthetic Neuron rateV1 row (only the fields the adapter reads)."""
    row = {"code": "70551", "code_type": "CPT", "npi_list": s.EMERSON, "entity_name": "EMERSON HOSPITAL",
           "billing_class": "institutional", "modifier_list": None, "place_of_service": "22", "arrangement": "ffs",
           "negotiation_type": "fee schedule", "rate": 1000.0, "hpt_charge_gross": 2000.0, "expiration": "9999-12-31",
           "payer_name": "Blue Cross Blue Shield of Massachusetts", "network_name": "Blue Care Elect",
           "network_year_month": "202609"}
    row.update(kw)
    return row


def _neuron_client(settings, rows, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if rows is None:
            return httpx.Response(503, json={"errors": ["unavailable"]})
        code_ = request.url.params["codes"]
        return httpx.Response(200, json={code_: {"code": code_, "rates": rows}})

    local = SqlitePricingRepository(settings.db_path)
    http = httpx.Client(transport=httpx.MockTransport(handler), headers={"X-API-KEY": "test-key"})
    repo = CompositeRepository(local, NeuronPriceSource(client=http, base_url="https://neuron.test/api", fallback=local))
    return TestClient(create_app(settings, repo))


def _only_claim(packet):
    parts = check_packet(packet)
    return next(cl for b in parts["gfe"] for cl in resources(b, "Claim") if "summary" not in cl["meta"]["profile"][0])


def test_neuron_rate_becomes_the_gfe_line(settings):
    calls = []
    rows = [_row(modifier_list="52", rate=500.0),           # different modifier: ignored
            _row(place_of_service="11", rate=1500.0),       # other place of service: lower preference
            _row()]
    client = _neuron_client(settings, rows, calls)
    r = post(client, "/fhir/$gfe-estimate", ex.emerson_brain_mri_bcbsma())
    assert r.status_code == 200, r.text
    claim = _only_claim(r.json())
    item = claim["item"][0]
    assert code(item["productOrService"]) == "70551" and code(item["revenue"]) == "0611"
    assert item["net"]["value"] == 1000.0
    assert "Serif Health Neuron" in ext(item, c.X_PROVIDER_EVENT_METHODOLOGY)["valueString"]

    q = calls[0].url.params
    assert calls[0].url.path == "/api/rates/v1" and calls[0].headers["X-API-KEY"] == "test-key"
    assert (q["codes"], q["npis"], q["network_template_ids"], q["billing_classes"]) == (
        "70551", s.EMERSON, s.BCBSMA_BLUE_CARE_ELECT, "institutional")


def test_summary_trace_shows_neuron_url_and_response_rows(settings):
    calls = []
    rows = [_row(rate=1283.5), _row(modifier_list="52", rate=641.75)]
    client = _neuron_client(settings, rows, calls)
    output = StringIO()
    sink = make_console_sink("summary", console=Console(file=output, width=300, color_system=None))
    tracer.add_sink(sink)
    try:
        response = post(client, "/fhir/$gfe-estimate", ex.emerson_brain_mri_bcbsma())
    finally:
        tracer.remove_sink(sink)

    assert response.status_code == 200, response.text
    rendered = output.getvalue()
    assert f"GET {calls[0].url}" in rendered
    assert "EMERSON HOSPITAL" in rendered
    assert "1283.5" in rendered
    assert "641.75" in rendered
    assert "test-key" not in rendered


def test_neuron_percentage_rate_uses_hospital_gross_charge(settings):
    client = _neuron_client(settings, [_row(negotiation_type="percentage", rate=50.0, hpt_charge_gross=2203.0)], [])
    item = _only_claim(post(client, "/fhir/$gfe-estimate", ex.emerson_brain_mri_bcbsma()).json())["item"][0]
    assert item["net"]["value"] == 1101.5
    assert "50% of gross charge $2,203.00" in ext(item, c.X_PROVIDER_EVENT_METHODOLOGY)["valueString"]


def test_neuron_self_pay_and_unmapped_plans_use_local_prices(settings):
    calls = []
    client = _neuron_client(settings, [_row()], calls)
    claim = _only_claim(post(client, "/fhir/$gfe-estimate", ex.brain_mri_self_pay()).json())
    assert claim["item"][0]["net"]["value"] == 425.0
    claim = _only_claim(post(client, "/fhir/$gfe-estimate", ex.knee_arthroscopy()).json())  # Acme: no Neuron network
    assert claim["total"]["value"] == 6660.0
    assert calls == []


def test_neuron_outage_falls_back_and_reports_unpriced(settings):
    client = _neuron_client(settings, None, [])
    assert post(client, "/fhir/$gfe-estimate", ex.emerson_brain_mri_bcbsma()).status_code == 422


def test_cleveland_clinic_no_match_queries_neuron_for_each_ct_code(settings):
    calls = []
    client = _neuron_client(settings, [], calls)
    response = post(client, "/fhir/$gfe-estimate", ex.cleveland_clinic_ct_aetna())
    assert response.status_code == 422
    assert response.json()["resourceType"] == "OperationOutcome"
    assert {call.url.params["codes"] for call in calls} == {"70450", "71250", "74176"}
    assert len(calls) == 3
    for call in calls:
        assert call.url.params["npis"] == s.CLEVELAND_CLINIC
        assert call.url.params["network_template_ids"] == s.AETNA_OAMC
        assert call.url.params["billing_classes"] == "institutional"


def test_cleveland_clinic_no_match_returns_failed_contributor_task(settings):
    calls = []
    client = _neuron_client(settings, [], calls)
    task = ex.contributor_task_cleveland_clinic_ct()
    response = post(client, "/fhir/Task/$gfe-contribute", task)
    assert response.status_code == 200
    failed = response.json()
    assert failed["resourceType"] == "Task"
    assert failed["status"] == "failed"
    assert "GFE cannot be priced completely" in failed["statusReason"]["text"]
    assert failed["partOf"] == task["partOf"]
    assert "output" not in failed
    assert {call.url.params["codes"] for call in calls} == {"70450", "71250", "74176"}
    assert len(calls) == 3
    for call in calls:
        assert call.url.params["npis"] == s.CLEVELAND_CLINIC
        assert call.url.params["network_template_ids"] == s.AETNA_OAMC


class _FhirNativeSource:
    """A price source whose API already speaks PCT: it returns a GFE Claim.item per line."""

    def find_price(self, *, provider, payer, plan, code_type, code, modifier, quantity, service_date):
        unit = {"73722": 1111.0, "A9579": 5.0, "L1820": 300.0}[code]
        return PriceQuote(claim_item={
            "extension": [{"url": c.X_PROVIDER_EVENT_METHODOLOGY, "valueString": "Contracted rate from pricing API"}],
            "revenue": {"coding": [{"system": c.CS_REVENUE, "code": "0614"}]},
            "productOrService": {"coding": [{"system": c.TYPE_TO_CODE_SYSTEM[code_type], "code": code}]},
            "quantity": {"value": quantity},
            "unitPrice": {"value": unit, "currency": "USD"},
            "net": {"value": round(unit * quantity, 2), "currency": "USD"},
        })


def test_fhir_native_claim_items_are_used_verbatim(settings):
    repo = CompositeRepository(SqlitePricingRepository(settings.db_path), _FhirNativeSource())
    client = TestClient(create_app(settings, repo))
    r = post(client, "/fhir/$gfe-estimate", ex.mri_knee_insured())
    assert r.status_code == 200, r.text
    claim = _only_claim(r.json())  # passes the PCT structural checks
    assert [i["net"]["value"] for i in claim["item"]] == [1111.0, 75.0, 300.0]
    for item in claim["item"]:
        assert ext(item, c.X_PROVIDER_EVENT_METHODOLOGY)["valueString"] == "Contracted rate from pricing API"
        assert ext(item, c.X_SERVICE_DESCRIPTION) and item["servicedDate"] == "2026-11-12"
    assert claim["total"]["value"] == 1486.0


@pytest.mark.skipif(not os.getenv("GFE_NEURON_API_KEY"), reason="set GFE_NEURON_API_KEY for the live Neuron test")
def test_live_neuron_lookup(settings):
    client = TestClient(create_app(replace(settings, price_source="neuron", neuron_api_key=os.environ["GFE_NEURON_API_KEY"])))
    r = post(client, "/fhir/$gfe-estimate", ex.emerson_brain_mri_bcbsma())
    assert r.status_code == 200, r.text
    item = _only_claim(r.json())["item"][0]
    assert item["net"]["value"] > 0
    assert "Serif Health Neuron" in ext(item, c.X_PROVIDER_EVENT_METHODOLOGY)["valueString"]
