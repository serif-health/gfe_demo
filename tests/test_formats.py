import xml.etree.ElementTree as ET

import pytest
from conftest import post

from gfe_service.demo import examples as ex
from gfe_service.fhir.formats import FORMATS, NotAcceptable, negotiate

NS = {"f": "http://hl7.org/fhir"}


@pytest.mark.parametrize(
    ("fmt", "accept", "expected"),
    [
        (None, None, "json"),
        (None, "*/*", "json"),
        (None, "application/fhir+xml", "xml"),
        (None, "text/html, application/xml;q=0.9", "xml"),
        ("json", "application/fhir+xml", "json"),
        ("application/fhir+xml", None, "xml"),
    ],
)
def test_negotiation(fmt, accept, expected):
    assert negotiate(fmt, accept).name == expected


def test_negotiation_rejects_unknown():
    with pytest.raises(NotAcceptable):
        negotiate("turtle", None)
    with pytest.raises(NotAcceptable):
        negotiate(None, "text/html")


def test_xml_packet_follows_fhir_xml_rules(client):
    r = post(client, "/fhir/$gfe-estimate?_format=xml", ex.mri_knee_insured())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/fhir+xml")
    root = ET.fromstring(r.content)
    assert root.tag == "{http://hl7.org/fhir}Bundle"
    assert root.find("f:type", NS).get("value") == "document"
    # inline resources are wrapped: <entry><resource><Composition>...
    comp = root.find("f:entry/f:resource/f:Composition", NS)
    assert comp is not None and comp.find("f:id", NS).get("value")
    # extension url is an attribute, primitives are @value
    ext = comp.find("f:extension", NS)
    assert ext.get("url", "").endswith("requestInitiationTime")
    assert ext.find("f:valueInstant", NS).get("value")
    # nested GFE Bundles and Claim items survive the conversion
    claims = root.findall(".//f:Claim", NS)
    assert len(claims) == 2  # GFE Summary + Bedford's institutional claim
    nets = [float(n.get("value")) for n in root.findall(".//f:Claim/f:item/f:net/f:value", NS)]
    assert sorted(nets) == [97.5, 310.0, 1320.0]


def test_406_on_unsupported_format(client):
    r = post(client, "/fhir/$gfe-estimate?_format=turtle", ex.mri_knee_insured())
    assert r.status_code == 406 and r.json()["resourceType"] == "OperationOutcome"


def test_415_on_xml_request_body(client):
    r = client.post("/fhir/$gfe-estimate", content="<Bundle/>", headers={"Content-Type": "application/fhir+xml"})
    assert r.status_code == 415


def test_registered_formats_are_advertised(client):
    cs = client.get("/fhir/metadata").json()
    assert cs["format"] == [f.media_type for f in FORMATS.values()]
