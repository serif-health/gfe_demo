"""Minimal in-memory GFE Coordination Platform (PCT "GFE Coordination" workflow).

Implements just enough of the platform role for the demo:
  * $gfe-coordination-request  - accept a Coordination Bundle (1 Coordination Task + N Contributor Tasks)
  * Task read / search / update - contributors find their Tasks and attach GFE Bundles
  * Organization / Practitioner directory - participants' FHIR ids (seeded from the providers table)
  * $gfe-retrieve              - assemble a GFE Packet from completed Tasks + Missing Bundles

State lives in memory (restart = empty). A real platform would persist and send
Subscription notifications; see README.
"""

from __future__ import annotations

import copy
import threading
import uuid
from typing import Any

from ..config import Settings
from ..fhir import builders as b
from ..fhir import constants as c
from ..fhir.parse import (
    FhirInputError, decode_attachment, first_coding, information_bundle_from_task, npi_of,
    parse_information_bundle, ref_of,
)
from ..pricing.repository import PricingRepository
from ..service import linking_identifier_for
from ..trace import tracer

OPEN_STATUSES = {"draft", "requested", "received", "accepted", "ready", "in-progress", "on-hold"}


def task_code(task: dict[str, Any]) -> str | None:
    return (first_coding(task.get("code")) or {}).get("code")


class CoordinationPlatform:
    def __init__(self, repo: PricingRepository, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self.tasks: dict[str, dict[str, Any]] = {}
        self.bundles: dict[str, dict[str, Any]] = {}
        self.directory: dict[str, dict[str, Any]] = {}
        for provider in repo.list_providers():
            res = b.provider_resource(provider)
            self.directory[ref_of(res)] = res

    # -- reads ----------------------------------------------------------------------------
    def read(self, resource_type: str, rid: str) -> dict[str, Any] | None:
        if resource_type == "Task":
            return self.tasks.get(rid)
        if resource_type == "Bundle":
            return self.bundles.get(rid)
        return self.directory.get(f"{resource_type}/{rid}")

    def resolve_reference(self, reference: str) -> dict[str, Any] | None:
        parts = reference.rstrip("/").split("/")
        return self.read(parts[-2], parts[-1]) if len(parts) >= 2 else None

    def search_directory(self, resource_type: str, identifier: str | None) -> list[dict[str, Any]]:
        out = [r for r in self.directory.values() if r["resourceType"] == resource_type]
        if identifier:
            value = identifier.split("|")[-1]
            out = [r for r in out if npi_of(r) == value]
        return out

    def search_tasks(self, *, owner: str | None = None, part_of: str | None = None,
                     status: str | None = None, code: str | None = None) -> list[dict[str, Any]]:
        statuses = set(status.split(",")) if status else None
        codes = {x.split("|")[-1] for x in code.split(",")} if code else None
        out = []
        for task in self.tasks.values():
            if owner and (task.get("owner") or {}).get("reference") != owner and npi_of(task.get("owner")) != owner.split("|")[-1]:
                continue
            if part_of and part_of not in [p.get("reference") for p in task.get("partOf", []) or []]:
                continue
            if statuses and task.get("status") not in statuses:
                continue
            if codes and task_code(task) not in codes:
                continue
            out.append(task)
        return out

    # -- $gfe-coordination-request --------------------------------------------------------
    def coordination_request(self, bundle: dict[str, Any]) -> dict[str, Any]:
        if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "transaction":
            raise FhirInputError("$gfe-coordination-request expects a GFE Coordination Bundle (Bundle.type = transaction)",
                                 expression="Bundle.type")
        entries = bundle.get("entry", []) or []
        tasks = [e for e in entries if (e.get("resource") or {}).get("resourceType") == "Task"]
        coord = [e for e in tasks if task_code(e["resource"]) == "gfe-coordination-task"]
        contrib = [e for e in tasks if task_code(e["resource"]) == "gfe-contributor-task"]
        if len(coord) != 1:
            raise FhirInputError("Coordination Bundle SHALL contain exactly one GFE Coordination Task", code="required")
        if not contrib:
            raise FhirInputError("Coordination Bundle SHALL contain at least one GFE Contributor Task", code="required")

        with self._lock:
            # Optional: info bundles POSTed in the same transaction (so valueReference can resolve)
            id_map: dict[str, str] = {}
            for e in entries:
                res = e.get("resource") or {}
                if res.get("resourceType") == "Bundle":
                    res = copy.deepcopy(res)
                    res.setdefault("id", str(uuid.uuid4()))
                    self.bundles[res["id"]] = res
                    if e.get("fullUrl"):
                        id_map[e["fullUrl"]] = ref_of(res)

            coord_entry = coord[0]
            coord_task = copy.deepcopy(coord_entry["resource"])
            coord_task["id"] = coord_task.get("id") or str(uuid.uuid4())
            coord_ref = f"Task/{coord_task['id']}"
            for key in filter(None, [coord_entry.get("fullUrl"), coord_ref]):
                id_map[key] = coord_ref
            info = information_bundle_from_task(coord_task, lambda r: self.resolve_reference(id_map.get(r, r)))
            if info is None:
                raise FhirInputError("Coordination Task SHALL carry a GFE Information Bundle in Task.input "
                                     "(valueAttachment, or valueReference to a Bundle in this transaction)",
                                     code="required", expression="Task.input")
            coord_task["status"] = coord_task.get("status") or "requested"
            coord_task["lastModified"] = b.now_instant()
            self.tasks[coord_task["id"]] = coord_task
            created = [coord_task]

            for e in contrib:
                task = copy.deepcopy(e["resource"])
                task["id"] = task.get("id") or str(uuid.uuid4())
                task["partOf"] = [{"reference": coord_ref}]
                task["status"] = task.get("status") or "requested"
                task["lastModified"] = b.now_instant()
                owner_ref = (task.get("owner") or {}).get("reference", "")
                if owner_ref and owner_ref not in self.directory:
                    tracer.warn(f"Contributor Task/{task['id']} owner {owner_ref} is not registered on this platform")
                self.tasks[task["id"]] = task
                created.append(task)

        for t in created:
            tracer.step(f"platform stored Task/{t['id']} ({task_code(t)}) owner={(t.get('owner') or {}).get('reference', '-')} status={t['status']}")
        return {
            "resourceType": "Bundle", "id": str(uuid.uuid4()), "type": "transaction-response",
            "entry": [
                {"fullUrl": f"{self.settings.fhir_base}/Task/{t['id']}", "resource": t,
                 "response": {"status": "201 Created", "location": f"Task/{t['id']}/_history/1"}}
                for t in created
            ],
        }

    # -- Task update (contributors) -------------------------------------------------------
    def update_task(self, rid: str, task: dict[str, Any]) -> dict[str, Any]:
        if task.get("resourceType") != "Task":
            raise FhirInputError("Body must be a Task", expression="resourceType")
        if task.get("id") and task["id"] != rid:
            raise FhirInputError(f"Task.id '{task['id']}' does not match URL id '{rid}'", expression="Task.id")
        if rid not in self.tasks:
            raise FhirInputError(f"Task/{rid} not found", code="not-found", status=404)
        if task_code(task) == "gfe-contributor-task" and task.get("status") == "completed":
            has_bundle = any((first_coding(o.get("type")) or {}).get("code") == "gfe-bundle"
                             for o in task.get("output", []) or [])
            if not has_bundle:
                raise FhirInputError("A completed GFE Contributor Task SHALL have a gfe-bundle output",
                                     code="required", expression="Task.output", status=422)
        task = copy.deepcopy(task)
        task["id"] = rid
        task["lastModified"] = b.now_instant()
        with self._lock:
            previous = self.tasks[rid].get("status")
            self.tasks[rid] = task
        tracer.step(f"platform Task/{rid}: {previous} -> {task.get('status')}")
        return task

    # -- $gfe-retrieve --------------------------------------------------------------------
    def coordination_information_bundle(self, coord_task: dict[str, Any]) -> dict[str, Any]:
        info = information_bundle_from_task(coord_task, self.resolve_reference)
        if info is None:
            raise FhirInputError("Coordination Task has no resolvable GFE Information Bundle", code="not-found", status=422)
        return info

    def retrieve_packet(self, coord_id: str) -> dict[str, Any]:
        coord_task = self.tasks.get(coord_id)
        if not coord_task or task_code(coord_task) != "gfe-coordination-task":
            raise FhirInputError(f"Task/{coord_id} is not a GFE Coordination Task on this platform", code="not-found", status=404)

        info = self.coordination_information_bundle(coord_task)
        req = parse_information_bundle(info, tasks=[coord_task],
                                       linking_identifier=linking_identifier_for(coord_task, self.settings.fhir_base))
        ctx = b.PacketContext(self.settings.fhir_base, req, self.settings.disclaimer)

        sections: list[b.PacketSection] = []
        for task in self.search_tasks(part_of=f"Task/{coord_id}", code="gfe-contributor-task"):
            owner = self.resolve_reference((task.get("owner") or {}).get("reference", "")) or {}
            gfe_output = next((o for o in task.get("output", []) or []
                               if (first_coding(o.get("type")) or {}).get("code") == "gfe-bundle"), None)
            if task.get("status") == "completed" and gfe_output:
                gfe_bundle = decode_attachment(gfe_output["valueAttachment"], "Task.output.valueAttachment")
                author = owner or self._author_from_bundle(gfe_bundle)
                sections.append(b.PacketSection(gfe_bundle, author))
                tracer.step(f"$gfe-retrieve: Task/{task['id']} completed -> GFE Bundle from {b.display_name(author) if author else '?'}")
            else:
                own_info = information_bundle_from_task(task, self.resolve_reference) or info
                items = [e["resource"] for e in own_info.get("entry", [])
                         if e.get("resource", {}).get("resourceType") in ("ServiceRequest", "DeviceRequest", "MedicationRequest")]
                if not owner:
                    owner_ref = (task.get("owner") or {}).get("reference", "Organization/unknown")
                    rtype, _, rid = owner_ref.rpartition("/")
                    owner = {"resourceType": rtype or "Organization", "id": rid,
                             "name": (task.get("owner") or {}).get("display") or owner_ref}
                missing = b.build_missing_bundle(ctx, owner, items)
                sections.append(b.PacketSection(missing, owner, missing=True))
                tracer.warn(f"$gfe-retrieve: Task/{task['id']} is '{task.get('status')}' -> GFE Missing Bundle "
                            f"for {b.display_name(owner)}")

        requester = self.resolve_reference((coord_task.get("requester") or {}).get("reference", ""))
        packet = b.build_packet(ctx, sections, requester)
        tracer.emit("fhir.built", resource=packet, label=f"GFE Packet for Task/{coord_id}")
        return packet

    @staticmethod
    def _author_from_bundle(gfe_bundle: dict[str, Any]) -> dict[str, Any]:
        for e in gfe_bundle.get("entry", []):
            res = e.get("resource", {})
            if res.get("resourceType") == "Claim" and c.P_GFE_SUMMARY not in res.get("meta", {}).get("profile", []):
                ref = (res.get("provider") or {}).get("reference")
                for e2 in gfe_bundle["entry"]:
                    if ref and ref_of(e2["resource"]) == ref:
                        return e2["resource"]
        return {}
