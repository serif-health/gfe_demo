from __future__ import annotations

import json
import warnings

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from gfe_service.app import create_app
from gfe_service.config import Settings
from gfe_service.db import init_db

FHIR_JSON = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"}


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
