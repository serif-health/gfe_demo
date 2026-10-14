"""FastAPI application: answer a GFE request (or a Contributor Task) with priced PCT resources."""

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
from .pricing.repository import CompositeRepository, PricingRepository, SqlitePricingRepository
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


def build_repository(settings: Settings) -> PricingRepository:
    """SQLite for everything, or SQLite reference data + Serif Health Neuron prices."""
    local = SqlitePricingRepository(settings.db_path)
    if settings.price_source == "sqlite":
        return local
    if settings.price_source == "neuron":
        from .pricing.neuron import NeuronPriceSource

        prices = NeuronPriceSource(settings.neuron_api_key, base_url=settings.neuron_url, fallback=local)
        return CompositeRepository(local, prices)  # type: ignore[return-value]
    raise ValueError(f"unknown price source '{settings.price_source}' (use sqlite or neuron)")


def create_app(settings: Settings | None = None, repo: PricingRepository | None = None) -> FastAPI:
    settings = settings or get_settings()
    repo = repo or build_repository(settings)
    service = GfeService(repo, settings)

    app = FastAPI(
        title="GFE Responder (Da Vinci PCT)",
        version=__version__,
        description="Turns pricing data into HL7 Da Vinci PCT GFE Bundles / GFE Packets.",
    )
    app.state.settings, app.state.service = settings, service

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
        """Non-normative: price this provider's part of a GFE and return the completed Task.

        The response is the Task to return to whoever sent it (this demo does not act as a coordination platform).
        """
        body = await inbound(request)
        if not body:
            raise FhirInputError("Request body required: a GFE Contributor Task")
        task = unwrap_parameters(body, "task", "resource")
        if task.get("resourceType") != "Task":
            raise FhirInputError("Expected a GFE Contributor Task", expression="resourceType")
        info = information_bundle_from_task(task)
        if info is None:
            raise FhirInputError("Task.input SHALL carry the GFE Information Bundle as a base64 valueAttachment",
                                 code="required", expression="Task.input")
        contributor = service.contributor_from_task(task, info, request.query_params.get("npi"))
        return fhir_response(request, service.fulfill_contributor_task(task, info, contributor))

    return app


def capability_statement(settings: Settings) -> dict[str, Any]:
    pct = "http://hl7.org/fhir/us/davinci-pct"
    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "date": "2026-10-06",
        "kind": "instance",
        "software": {"name": "gfe-demo", "version": __version__},
        "implementation": {"description": "GFE responder: prices one provider's services into PCT GFE resources",
                           "url": settings.fhir_base},
        "fhirVersion": "4.0.1",
        "format": [f.media_type for f in formats.FORMATS.values()],
        "implementationGuide": [f"{pct}/ImplementationGuide/hl7.fhir.us.davinci-pct"],
        "rest": [{
            "mode": "server",
            "resource": [
                {"type": "Task", "supportedProfile": [f"{pct}/StructureDefinition/davinci-pct-gfe-contributor-task"],
                 "operation": [{"name": "gfe-contribute",
                                "definition": f"{settings.fhir_base}/OperationDefinition/gfe-contribute"}]},
            ],
            "operation": [
                {"name": "gfe-estimate", "definition": f"{settings.fhir_base}/OperationDefinition/gfe-estimate"},
            ],
        }],
    }
