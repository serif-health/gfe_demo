"""In-process walkthrough of every scenario, rendered with the live trace."""

from __future__ import annotations

import json
import warnings
from typing import Any

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient
from rich.console import Console
from rich.panel import Panel

from ..app import create_app
from ..config import PROJECT_ROOT, Settings
from ..console import make_console_sink
from ..db import seed_data as s
from ..platform.worker import ContributorWorker
from ..trace import tracer
from . import examples as ex

FHIR_JSON = {"Content-Type": "application/fhir+json"}


def _banner(console: Console, n: int, title: str, body: str) -> None:
    console.print()
    console.print(Panel(body, title=f"[bold]Scenario {n}: {title}[/]", border_style="bright_white", padding=(1, 2)))


def _wait(console: Console, pause: bool) -> None:
    if pause:
        console.input("[dim]↵ press Enter to continue…[/]")


def run_demo(settings: Settings, level: str, pause: bool, console: Console) -> None:
    sink = make_console_sink(level, settings.trace_body_lines, console)
    tracer.add_sink(sink)
    try:
        app = create_app(settings)
        client = TestClient(app)
        _run(client, app, console, pause)
    finally:
        tracer.remove_sink(sink)


def _post(client: TestClient, path: str, body: dict[str, Any], accept: str = "application/fhir+json") -> Any:
    return client.post(path, content=json.dumps(body), headers={**FHIR_JSON, "Accept": accept})


def _run(client: TestClient, app: Any, console: Console, pause: bool) -> None:
    _banner(console, 1, "Direct estimate → GFE Packet (insured)",
            "A scheduling system sends a [bold]GFE Information Bundle[/] (patient, Coverage, payer, requested MRI + knee brace)\n"
            "to [cyan]POST /fhir/$gfe-estimate[/]. The service resolves payer/plan, expands the CPT code into facility,\n"
            "contrast and radiologist components, looks up contracted rates, and answers with a [bold]GFE Packet[/]\n"
            "(document Bundle: Composition + one GFE Bundle per billing provider) instead of a proprietary JSON.")
    _wait(console, pause)
    _post(client, "/fhir/$gfe-estimate", ex.mri_knee_insured())

    _banner(console, 2, "Self-pay estimate, XML response",
            "No Coverage in the request → priced from published discounted-cash prices. A freestanding imaging center\n"
            "bills the technical component (-TC) and the radiologist the read (-26). Same packet, but requested as\n"
            "[bold]application/fhir+xml[/] to show the pluggable response formats.")
    _wait(console, pause)
    _post(client, "/fhir/$gfe-estimate?_format=xml", ex.brain_mri_self_pay())

    _banner(console, 3, "Participating provider: GFE Contributor Task",
            "A convening facility asked surgeon Dr. Cutter for his part of a knee arthroscopy estimate. The Contributor\n"
            "Task carries the GFE Information Bundle as a base64 attachment. The service prices [bold]only the lines this\n"
            "provider bills[/] and returns the Task [bold]completed[/] with a GFE Bundle in Task.output — exactly what a\n"
            "contributor PUTs back to the Coordination Platform.")
    _wait(console, pause)
    _post(client, "/fhir/Task/$gfe-contribute", ex.contributor_task_surgeon())

    _banner(console, 4, "Coordinated multi-provider GFE (platform + contributors + $gfe-retrieve)",
            "Bedford (convening provider) submits a [bold]GFE Coordination Bundle[/]: 1 Coordination Task + 4 Contributor\n"
            "Tasks (facility, surgeon, anesthesiologist, physical therapist). Three contributors run the worker and\n"
            "attach GFE Bundles; the PT never responds. [cyan]$gfe-retrieve[/] assembles the GFE Packet with a\n"
            "[red]GFE Missing Bundle[/] for the PT.")
    _wait(console, pause)
    resp = _post(client, "/fhir/$gfe-coordination-request", ex.coordination_bundle()).json()
    coord_id = next(e["resource"]["id"] for e in resp["entry"]
                    if e["resource"]["code"]["coding"][0]["code"] == "gfe-coordination-task")
    service = app.state.service
    for npi in (s.BEDFORD, s.CUTTER, s.STHESIA):
        console.print()
        console.print(Panel(f"contributor worker for NPI {npi}", border_style="magenta"))
        _wait(console, pause)
        ContributorWorker(service, npi, client=client, path_prefix="fhir/", trace_http=False).run_once()
    _wait(console, pause)
    client.get(f"/fhir/Task/{coord_id}/$gfe-retrieve")

    spec = PROJECT_ROOT / "examples" / "spec" / "Bundle-PCT-GFE-Information-Bundle-1.json"
    if spec.exists():
        _banner(console, 5, "The HL7 PCT IG's own GFE Information Bundle example",
                "The IG example names no provider, so the service's default provider (GFE_DEFAULT_PROVIDER_NPI) is used.")
        _wait(console, pause)
        _post(client, "/fhir/$gfe-estimate", json.loads(spec.read_text()))
    console.print("\n[bold green]Demo complete.[/]")
