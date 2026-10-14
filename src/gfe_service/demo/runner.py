"""In-process walkthrough of every scenario, rendered with the live trace."""

from __future__ import annotations

import json
import warnings
from dataclasses import replace
from typing import Any

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient
from rich.console import Console
from rich.panel import Panel

from ..app import create_app
from ..config import PROJECT_ROOT, Settings
from ..console import make_console_sink
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
        _run(TestClient(create_app(replace(settings, price_source="sqlite"))), settings, console, pause)
    finally:
        tracer.remove_sink(sink)


def _post(client: TestClient, path: str, body: dict[str, Any], accept: str = "application/fhir+json") -> Any:
    return client.post(path, content=json.dumps(body), headers={**FHIR_JSON, "Accept": accept})


def _run(client: TestClient, settings: Settings, console: Console, pause: bool) -> None:
    _banner(console, 1, "Hospital prices the services it was asked for (insured) → GFE Packet",
            "A scheduling system sends a [bold]GFE Information Bundle[/] naming Bedford Regional for an MRI with contrast\n"
            "and a knee brace to [cyan]POST /fhir/$gfe-estimate[/]. Bedford prices [bold]its own lines[/] (MRI, contrast,\n"
            "brace) from contracted rates. The radiologist's read is billed separately, so it is skipped rather than\n"
            "chased down. The answer is a [bold]GFE Packet[/] with one GFE Bundle.")
    _wait(console, pause)
    _post(client, "/fhir/$gfe-estimate", ex.mri_knee_insured())

    _banner(console, 2, "Self-pay estimate, XML response",
            "No Coverage in the request → priced from the imaging center's discounted-cash price. A freestanding center\n"
            "bills the technical component (-TC) of the MRI. Same packet shape, requested as\n"
            "[bold]application/fhir+xml[/] to show the pluggable response formats.")
    _wait(console, pause)
    _post(client, "/fhir/$gfe-estimate?_format=xml", ex.brain_mri_self_pay())

    _banner(console, 3, "Participating provider: GFE Contributor Task",
            "Surgeon Dr. Cutter was assigned a Contributor Task for a knee arthroscopy. The Task carries the GFE\n"
            "Information Bundle as a base64 attachment. The service prices [bold]only the lines Dr. Cutter bills[/] and\n"
            "returns the Task [bold]completed[/] with a GFE Bundle in Task.output, ready to PUT back to whoever assigned it.")
    _wait(console, pause)
    _post(client, "/fhir/Task/$gfe-contribute", ex.contributor_task_surgeon())

    n = 4
    spec = PROJECT_ROOT / "examples" / "spec" / "Bundle-PCT-GFE-Information-Bundle-1.json"
    if spec.exists():
        _banner(console, n, "The HL7 PCT IG's own GFE Information Bundle example",
                "The IG example names no provider, so the service's default provider (GFE_DEFAULT_PROVIDER_NPI) is used.")
        _wait(console, pause)
        _post(client, "/fhir/$gfe-estimate", json.loads(spec.read_text()))
        n += 1

    if settings.neuron_api_key:
        _banner(console, n, "Live prices from the Serif Health Neuron API",
                "Same request shape, but prices come from [bold]GET /rates/v1[/] instead of SQL: Emerson Hospital's\n"
                "BCBS MA Blue Care Elect rate for CPT 70551, plus its hospital-file gross charge. Only the price lookup\n"
                "changed; the FHIR output is identical in shape.")
        _wait(console, pause)
        neuron = TestClient(create_app(replace(settings, price_source="neuron")))
        _post(neuron, "/fhir/$gfe-estimate", ex.emerson_brain_mri_bcbsma())
    else:
        console.print("\n[dim]Set GFE_NEURON_API_KEY to add a live Serif Health Neuron scenario.[/]")
    console.print("\n[bold green]Demo complete.[/]")
