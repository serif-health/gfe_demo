"""Structural checks derived from the Da Vinci PCT profiles' differential constraints.

Not a replacement for the HL7 FHIR Validator (see README), but it pins the
cardinalities / fixed values the IG requires on everything this service produces.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from gfe_service.fhir import constants as c


def profile(res: dict[str, Any]) -> str:
    return res.get("meta", {}).get("profile", [""])[0]


def code(concept: dict[str, Any] | None) -> str | None:
    return ((concept or {}).get("coding") or [{}])[0].get("code")


def resources(bundle: dict[str, Any], rtype: str | None = None) -> list[dict[str, Any]]:
    return [e["resource"] for e in bundle["entry"] if rtype is None or e["resource"]["resourceType"] == rtype]


def ext(res: dict[str, Any], url: str) -> dict[str, Any] | None:
    return next((e for e in res.get("extension", []) if e["url"] == url), None)


def check_entries(bundle: dict[str, Any]) -> None:
    for e in bundle["entry"]:
        assert e.get("fullUrl"), "Bundle.entry.fullUrl 1..1"
        assert "request" not in e and "response" not in e and "search" not in e


def check_gfe_claim(claim: dict[str, Any]) -> None:
    prof = profile(claim)
    institutional = prof == c.P_GFE_INSTITUTIONAL
    assert prof in (c.P_GFE_INSTITUTIONAL, c.P_GFE_PROFESSIONAL)
    assert claim["use"] == "predetermination" and claim["status"] == "active"
    assert code(claim["type"]) == ("institutional" if institutional else "professional")
    plac = [i for i in claim["identifier"] if code(i.get("type")) == "PLAC"]
    assert len(plac) == 1 and plac[0]["value"], "identifier:PLAC 1..1"
    assert ext(claim["provider"], c.X_PROVIDER_TAXONOMY), "provider.extension:providerTaxonomy 1..1"
    assert claim["provider"].get("reference")
    assert claim["priority"] and claim["insurance"] and claim["patient"] and claim["created"]
    if institutional:
        tob = [s for s in claim.get("supportingInfo", []) if code(s["category"]) == "typeofbill"]
        assert len(tob) == 1 and code(tob[0]["code"]), "supportingInfo:typeOfBill 1..1"
    assert 1 <= len(claim["item"]) <= (999 if institutional else 50)
    total = 0.0
    for item in claim["item"]:
        assert ext(item, c.X_SERVICE_DESCRIPTION), "item.extension:serviceDescription 1..1"
        assert "servicedDate" in item or "servicedPeriod" in item, "item.serviced[x] 1..1"
        assert item["quantity"]["value"] > 0 and item["net"]["currency"] == "USD"
        assert len(item.get("modifier", [])) <= 4
        if institutional:
            assert "revenue" in item, "item.revenue 1..1 (institutional)"
        else:
            assert "locationCodeableConcept" in item, "item.location[x] 1..1 (professional)"
            assert "unitPrice" in item, "item.unitPrice 1..1 (professional)"
        assert round(item["unitPrice"]["value"] * item["quantity"]["value"], 2) == item["net"]["value"]
        total += item["net"]["value"]
    assert round(total, 2) == claim["total"]["value"]


def check_summary_claim(claim: dict[str, Any]) -> None:
    assert profile(claim) == c.P_GFE_SUMMARY
    assert code(claim["type"]) == "estimate-summary"
    assert ext(claim["provider"], c.X_DATA_ABSENT_REASON), "summary provider uses data-absent-reason"
    assert "reference" not in claim["provider"]
    for forbidden in ("item", "supportingInfo", "payee", "referral", "facility"):
        assert forbidden not in claim, f"summary {forbidden} 0..0"
    assert "total" in claim


def check_gfe_bundle(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    assert profile(bundle) == c.P_GFE_BUNDLE
    assert bundle["type"] == "collection" and bundle["identifier"] and bundle["timestamp"]
    assert len(bundle["entry"]) >= 3
    check_entries(bundle)
    claims = resources(bundle, "Claim")
    assert len(claims) >= 2, "entry:gfe 2..*"
    summaries = [cl for cl in claims if profile(cl) == c.P_GFE_SUMMARY]
    details = [cl for cl in claims if profile(cl) != c.P_GFE_SUMMARY]
    assert len(summaries) == 1 and details
    check_summary_claim(summaries[0])
    for cl in details:
        check_gfe_claim(cl)
    assert summaries[0]["total"]["value"] == round(sum(cl["total"]["value"] for cl in details), 2)
    assert 1 <= len(resources(bundle, "Patient")) <= 2
    assert len(resources(bundle, "Coverage")) <= 1
    # every literal reference in the claims resolves inside this bundle
    refs = {f"{r['resourceType']}/{r['id']}" for r in resources(bundle)}
    for cl in claims:
        for ref in (cl["patient"], cl.get("insurer"), cl["provider"], *[i["coverage"] for i in cl["insurance"]]):
            if ref and ref.get("reference"):
                assert ref["reference"] in refs, f"unresolved {ref['reference']}"
    return details


def check_missing_bundle(bundle: dict[str, Any]) -> None:
    assert profile(bundle) == c.P_GFE_MISSING_BUNDLE
    assert bundle["type"] == "collection" and bundle["timestamp"]
    check_entries(bundle)
    assert len(resources(bundle, "Patient")) == 1
    assert len(resources(bundle, "Organization")) <= 2 and len(resources(bundle, "Practitioner")) <= 1


def check_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Validate a GFE Packet; returns {'gfe': [...bundles], 'missing': [...bundles], 'composition': {...}}."""
    assert profile(packet) == c.P_GFE_PACKET
    assert packet["type"] == "document" and packet["identifier"] and packet["timestamp"]
    check_entries(packet)
    comp = packet["entry"][0]["resource"]
    assert comp["resourceType"] == "Composition" and profile(comp) == c.P_GFE_COMPOSITION
    assert ext(comp, c.X_REQUEST_INITIATION_TIME) and ext(comp, c.X_REQUEST_ORIGINATION_TYPE)
    assert comp["identifier"]["system"] and comp["identifier"]["value"]
    assert code(comp["type"]) == "111480-0" and code(comp["category"][0]) == "estimate"
    assert comp["status"] == "final" and comp["subject"]["reference"].startswith("Patient/")
    assert len(resources(packet, "Organization")) <= 2 and len(resources(packet, "Practitioner")) <= 1
    assert 1 <= len(resources(packet, "Patient")) <= 2 and len(resources(packet, "Coverage")) <= 1

    top_refs = {f"{r['resourceType']}/{r['id']}" for r in resources(packet)}
    sections = comp["section"]
    cov = [s for s in sections if code(s["code"]) == "coverage-section"]
    gfe = [s for s in sections if code(s["code"]) == "gfe-section"]
    assert len(cov) == 1 and len(cov[0]["entry"]) == 1 and cov[0]["entry"][0]["reference"] in top_refs
    assert gfe, "section:gfeBundle 1..*"
    out = {"gfe": [], "missing": [], "composition": comp}
    for s in gfe:
        assert len(s["author"]) == 1 and len(s["entry"]) == 1
        author = s["author"][0]
        assert author.get("reference") in top_refs or author.get("identifier"), "section author resolvable"
        ref = s["entry"][0]["reference"]
        assert ref in top_refs, f"section entry {ref} must be contained in the document"
        bundle = next(r for r in resources(packet, "Bundle") if f"Bundle/{r['id']}" == ref)
        if profile(bundle) == c.P_GFE_MISSING_BUNDLE:
            check_missing_bundle(bundle)
            out["missing"].append(bundle)
        else:
            check_gfe_bundle(bundle)
            out["gfe"].append(bundle)
    return out


def decode_output(task: dict[str, Any]) -> dict[str, Any]:
    out = next(o for o in task["output"] if code(o["type"]) == "gfe-bundle")
    att = out["valueAttachment"]
    assert att["contentType"] == "application/fhir+json" and att["creation"]
    return json.loads(base64.b64decode(att["data"]))
