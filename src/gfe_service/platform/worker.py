"""GFE Contributor worker: the participating-provider side of the coordination workflow.

Talks to a Coordination Platform over plain FHIR REST (``httpx``), exactly as a provider's
billing system or a pricing-data vendor acting on its behalf would:

  1. GET  {platform}/Organization|Practitioner?identifier=<npi>   -> my FHIR id on the platform
  2. GET  {platform}/Task?owner=<me>&code=gfe-contributor-task&status=requested,received
  3. PUT  {platform}/Task/{id}            status=accepted
  4. read the GFE Information Bundle (Contributor Task input, else the Coordination Task's)
  5. price it from our database -> GFE Bundle
  6. PUT  {platform}/Task/{id}            status=completed, output = GFE Bundle attachment
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ..domain import ProviderHint
from ..fhir.parse import FhirInputError, information_bundle_from_task
from ..service import GfeService
from ..trace import tracer

FHIR_JSON = "application/fhir+json"


def _traced_client(base_url: str, trace: bool = True) -> httpx.Client:
    def on_request(request: httpx.Request) -> None:
        request.extensions["t0"] = time.perf_counter()
        body = json.loads(request.content) if request.content else None
        tracer.emit("client.request", method=request.method, url=str(request.url), body=body)

    def on_response(response: httpx.Response) -> None:
        response.read()
        elapsed = (time.perf_counter() - response.request.extensions.get("t0", time.perf_counter())) * 1000
        try:
            body = response.json()
        except ValueError:
            body = None
        tracer.emit("client.response", method=response.request.method, url=str(response.request.url),
                    status=response.status_code, body=body, elapsed_ms=elapsed)

    hooks = {"request": [on_request], "response": [on_response]} if trace else {}
    return httpx.Client(base_url=base_url.rstrip("/") + "/", headers={"Accept": FHIR_JSON}, event_hooks=hooks, timeout=30)


class ContributorWorker:
    def __init__(self, service: GfeService, npi: str, *, platform_url: str | None = None,
                 client: httpx.Client | None = None, path_prefix: str = "", trace_http: bool = True) -> None:
        if client is None and platform_url is None:
            raise ValueError("platform_url or client is required")
        self.service = service
        self.npi = npi
        self.client = client or _traced_client(platform_url, trace_http)
        # e.g. "fhir/" when handed an in-process TestClient rooted at the app, not the FHIR base
        self._prefix = path_prefix

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        r = self.client.get(self._prefix + path, params=params or None)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, resource: dict[str, Any]) -> dict[str, Any]:
        r = self.client.put(self._prefix + path, json=resource, headers={"Content-Type": FHIR_JSON})
        r.raise_for_status()
        return r.json()

    def whoami(self) -> dict[str, Any]:
        for rtype in ("Organization", "Practitioner"):
            bundle = self._get(rtype, identifier=f"http://hl7.org/fhir/sid/us-npi|{self.npi}")
            if bundle.get("entry"):
                return bundle["entry"][0]["resource"]
        raise RuntimeError(f"NPI {self.npi} is not registered on the coordination platform")

    def run_once(self) -> list[dict[str, Any]]:
        me = self.whoami()
        me_ref = f"{me['resourceType']}/{me['id']}"
        tracer.step(f"contributor {me_ref} (NPI {self.npi}) polling for assigned GFE Contributor Tasks")
        search = self._get("Task", owner=me_ref, code="gfe-contributor-task", status="requested,received,accepted")
        tasks = [e["resource"] for e in search.get("entry", []) or []]
        if not tasks:
            tracer.step(f"no open Tasks for {me_ref}")
        done = []
        for task in tasks:
            done.append(self.handle(task, me))
        return done

    def handle(self, task: dict[str, Any], me: dict[str, Any]) -> dict[str, Any]:
        if task.get("status") != "accepted":
            task = self._put(f"Task/{task['id']}", {**task, "status": "accepted"})

        coordination_task = None
        parent = next((p.get("reference") for p in task.get("partOf", []) or []), None)
        if parent:
            coordination_task = self._get(parent)
        resolver = lambda ref: self._get(ref) if ref else None  # noqa: E731
        info = information_bundle_from_task(task, resolver)
        if info is None and coordination_task:
            info = information_bundle_from_task(coordination_task, resolver)
        if info is None:
            raise FhirInputError(f"Task/{task['id']}: no GFE Information Bundle on Task or its Coordination Task")

        contributor = ProviderHint(npi=self.npi, kind=me["resourceType"].lower(), resource=me)
        updated = self.service.fulfill_contributor_task(task, info, contributor, coordination_task)
        return self._put(f"Task/{task['id']}", updated)
