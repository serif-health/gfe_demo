from __future__ import annotations

import json
import warnings

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from gfe_service.app import create_app
from gfe_service.config import Settings
from gfe_service.db import connect, init_db

FHIR_JSON = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"}
OLOGIST = "1430572606"
THERA = "1394820173"


@pytest.fixture()
def additional_test_providers(settings):
    conn = connect(settings.db_path)
    try:
        conn.executemany(
            "INSERT INTO providers (npi, entity_type, name, taxonomy_code, billing_class, place_of_service) "
            "VALUES (?, 'practitioner', ?, ?, 'professional', ?)",
            [(OLOGIST, "Nora Ologist, MD", "2085R0202X", "22"),
             (THERA, "Pat Thera, PT", "225100000X", "11")],
        )
        conn.execute(
            "INSERT INTO negotiated_rates "
            "(payer_id, plan_id, npi, billing_class, code_type, code, modifier, negotiated_type, "
            "negotiated_rate, effective_date, expiration_date, source) "
            "VALUES ('umbrella', 'umbrella-premim', ?, 'professional', 'CPT', '73722', '26', "
            "'fee schedule', 98, '2026-01-01', '2026-12-31', 'test fixture')",
            (OLOGIST,),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def settings(tmp_path):
    s = Settings(db_path=tmp_path / "pricing.db", fhir_base="http://test/fhir", default_provider_npi="1520397849")
    init_db(s.db_path)
    return s


@pytest.fixture()
def app(settings):
    return create_app(settings)


@pytest.fixture()
def client(app):
    return TestClient(app)


def post(client, path, body, **headers):
    return client.post(path, content=json.dumps(body), headers={**FHIR_JSON, **headers})
