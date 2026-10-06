"""FastAPI application exposing the GFE responder and a demo Coordination Platform."""

from __future__ import annotations

import json
import time
import traceback
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import __version__
from .config import Settings, get_settings
from .fhir import formats
from .fhir.builders import operation_outcome
from .fhir.parse import FhirInputError, information_bundle_from_task, unwrap_parameters
from .platform.store import CoordinationPlatform
from .pricing.repository import PricingRepository, SqlitePricingRepository
from .service import GfeService
from .trace import new_request_id, set_request_id, tracer

ACCEPTED_REQUEST_TYPES = ("application/fhir+json", "application/json", "")


async def inbound(request: Request) -> dict[str, Any] | None:
    """Assign a request id, parse the JSON body, and emit the ``http.in`` trace event."""
    rid = new_request_id()
    set_request_id(rid)
    request.state.t0 = time.perf_counter()
    raw = await request.body()
    body = None
    if raw:
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in ACCEPTED_REQUEST_TYPES:
            tracer.emit("http.in", method=request.method, path=request.url.path, query=str(request.url.query),
                        content_type=ctype, size=len(raw), body=None)
            raise FhirInputError(f"Unsupported request Content-Type '{ctype}'. Send application/fhir+json.",
                                 code="not-supported", status=415)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FhirInputError(f"Request body is not valid JSON: {exc}", code="structure") from exc
    tracer.emit("http.in", method=request.method, path=request.url.path, query=str(request.url.query),
                content_type=request.headers.get("content-type", ""), size=len(raw), body=body,
                accept=request.headers.get("accept", ""))
    return body


def fhir_response(request: Request, resource: dict[str, Any], status: int = 200,
                  headers: dict[str, str] | None = None) -> Response:
    try:
        fmt = formats.negotiate(request.query_params.get("_format"), request.headers.get("accept"))
    except formats.NotAcceptable as exc:
        fmt = formats.FORMATS["json"]
        resource, status = operation_outcome([("error", "not-supported", str(exc))]), 406
    pretty = request.query_params.get("_pretty", "true").lower() != "false"
    payload = fmt.serialize(resource, pretty)
    elapsed = (time.perf_counter() - getattr(request.state, "t0", time.perf_counter())) * 1000
    tracer.emit("http.out", status=status, media_type=fmt.media_type, size=len(payload.encode()),
                body=resource, serialized=payload, format=fmt.name, elapsed_ms=elapsed,
                method=request.method, path=request.url.path)
    return Response(payload, status_code=status, media_type=fmt.media_type, headers=headers)


def bundle_searchset(resources: list[dict[str, Any]]) -> dict[str, Any]:
    return {"resourceType": "Bundle", "type": "searchset", "total": len(resources),
            "entry": [{"resource": r, "search": {"mode": "match"}} for r in resources]}


def create_app(settings: Settings | None = None, repo: PricingRepository | None = None) -> FastAPI:
    settings = settings or get_settings()
    repo = repo or SqlitePricingRepository(settings.db_path)
    service = GfeService(repo, settings)
    platform = CoordinationPlatform(repo, settings)

    app = FastAPI(
        title="GFE Responder (Da Vinci PCT)",
        version=__version__,
        description="Turns pricing data into HL7 Da Vinci PCT GFE Bundles / GFE Packets.",
    )
    app.state.settings, app.state.service, app.state.platform = settings, service, platform

    # -- errors -> OperationOutcome ---------------------------------------------------------
    @app.exception_handler(FhirInputError)
    async def _fhir_error(request: Request, exc: FhirInputError) -> Response:
        tracer.emit("http.error", status=exc.status, message=str(exc))
        return fhir_response(request, operation_outcome([("error", exc.code, str(exc))], expression=exc.expression), exc.status)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> Response:
        tracer.emit("http.error", status=500, message=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        return fhir_response(request, operation_outcome([("fatal", "exception", f"{type(exc).__name__}: {exc}")]), 500)

    # -- discovery --------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse({"service": "gfe-demo", "fhir_base": settings.fhir_base, "docs": "/docs",
                             "capabilities": f"{settings.fhir_base}/metadata"})

    @app.get("/fhir/metadata")
    async def metadata(request: Request) -> Response:
        await inbound(request)
        return fhir_response(request, capability_statement(settings))

    # -- 1. Direct: GFE Information Bundle -> GFE Packet -----------------------------------
    @app.post("/fhir/$gfe-estimate")
    async def gfe_estimate(request: Request) -> Response:
        """Non-normative convenience operation: price a GFE Information Bundle, return a GFE Packet."""
        body = await inbound(request)
        if not body:
            raise FhirInputError("Request body required: a GFE Information Bundle (or Parameters.resource)")
        info = unwrap_parameters(body, "resource", "gfe-information-bundle")
        return fhir_response(request, service.packet_from_information_bundle(info))

    # -- 2. Contributor: Contributor Task -> completed Task with GFE Bundle ----------------
    @app.post("/fhir/Task/$gfe-contribute")
    async def gfe_contribute(request: Request) -> Response:
        """Non-normative: the response a GFE Contributor would PUT back to the Coordination Platform."""
        body = await inbound(request)
        if not body:
            raise FhirInputError("Request body required: a GFE Contributor Task")
        task = unwrap_parameters(body, "task", "resource")
        if task.get("resourceType") != "Task":
            raise FhirInputError("Expected a GFE Contributor Task", expression="resourceType")
        info = information_bundle_from_task(task, platform.resolve_reference)
        coordination_task = None
        parent = next((p.get("reference") for p in task.get("partOf", []) or []), None)
        if parent:
            coordination_task = platform.resolve_reference(parent)
            if info is None and coordination_task:
                info = information_bundle_from_task(coordination_task, platform.resolve_reference)
        if info is None:
            raise FhirInputError("Task.input SHALL carry the GFE Information Bundle (valueAttachment)",
                                 code="required", expression="Task.input")
        contributor = service.contributor_from_task(task, info, platform.resolve_reference,
                                                    request.query_params.get("npi"))
        return fhir_response(request, service.fulfill_contributor_task(task, info, contributor, coordination_task))

    # -- 3. Coordination Platform -----------------------------------------------------------
    @app.post("/fhir/$gfe-coordination-request")
    async def coordination_request(request: Request) -> Response:
        body = await inbound(request)
        bundle = unwrap_parameters(body or {}, "resource", "coordination-bundle")
        return fhir_response(request, platform.coordination_request(bundle), 200)

    @app.api_route("/fhir/Task/{task_id}/$gfe-retrieve", methods=["GET", "POST"])
    async def gfe_retrieve(task_id: str, request: Request) -> Response:
        await inbound(request)
        return fhir_response(request, platform.retrieve_packet(task_id))

    @app.get("/fhir/Task")
    async def search_tasks(request: Request) -> Response:
        await inbound(request)
        q = request.query_params
        tasks = platform.search_tasks(owner=q.get("owner"), part_of=q.get("part-of"),
                                      status=q.get("status"), code=q.get("code"))
        return fhir_response(request, bundle_searchset(tasks))

    @app.get("/fhir/Task/{task_id}")
    async def read_task(task_id: str, request: Request) -> Response:
        await inbound(request)
        task = platform.read("Task", task_id)
        if not task:
            raise FhirInputError(f"Task/{task_id} not found", code="not-found", status=404)
        return fhir_response(request, task)

    @app.put("/fhir/Task/{task_id}")
    async def update_task(task_id: str, request: Request) -> Response:
        body = await inbound(request)
        return fhir_response(request, platform.update_task(task_id, body or {}))

    @app.get("/fhir/{resource_type}/{rid}")
    async def read_other(resource_type: str, rid: str, request: Request) -> Response:
        await inbound(request)
        res = platform.read(resource_type, rid)
        if not res:
            raise FhirInputError(f"{resource_type}/{rid} not found", code="not-found", status=404)
        return fhir_response(request, res)

    @app.get("/fhir/{resource_type}")
    async def search_directory(resource_type: str, request: Request) -> Response:
        await inbound(request)
        if resource_type not in ("Organization", "Practitioner"):
            raise FhirInputError(f"Search on {resource_type} is not supported", code="not-supported", status=404)
        return fhir_response(request, bundle_searchset(
            platform.search_directory(resource_type, request.query_params.get("identifier"))))

    return app


def capability_statement(settings: Settings) -> dict[str, Any]:
    pct = "http://hl7.org/fhir/us/davinci-pct"
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": "2026-10-06",
        "kind": "instance",
        "software": {"name": "gfe-demo", "version": __version__},
        "implementation": {"description": "GFE responder + demo Coordination Platform", "url": settings.fhir_base},
        "fhirVersion": "4.0.1",
        "format": [f.media_type for f in formats.FORMATS.values()],
        "implementationGuide": [f"{pct}/ImplementationGuide/hl7.fhir.us.davinci-pct"],
        "rest": [{
            "mode": "server",
            "resource": [
                {"type": "Task", "supportedProfile": [f"{pct}/StructureDefinition/davinci-pct-gfe-coordination-task",
                                                      f"{pct}/StructureDefinition/davinci-pct-gfe-contributor-task"],
                 "interaction": [{"code": "read"}, {"code": "update"}, {"code": "search-type"}],
                 "searchParam": [{"name": n, "type": t} for n, t in
                                 [("owner", "reference"), ("part-of", "reference"), ("status", "token"), ("code", "token")]],
                 "operation": [{"name": "gfe-retrieve", "definition": f"{pct}/OperationDefinition/GFE-retrieve"},
                               {"name": "gfe-contribute", "definition": f"{settings.fhir_base}/OperationDefinition/gfe-contribute"}]},
                {"type": "Organization", "interaction": [{"code": "read"}, {"code": "search-type"}]},
                {"type": "Practitioner", "interaction": [{"code": "read"}, {"code": "search-type"}]},
                {"type": "Bundle", "interaction": [{"code": "read"}]},
            ],
            "operation": [
                {"name": "gfe-coordination-request", "definition": f"{pct}/OperationDefinition/GFE-coordination-request"},
                {"name": "gfe-estimate", "definition": f"{settings.fhir_base}/OperationDefinition/gfe-estimate"},
            ],
        }],
    }
