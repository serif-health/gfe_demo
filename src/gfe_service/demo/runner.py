"""Curated in-process walkthrough, rendered with the live trace."""

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
from ..config import Settings
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
        with TestClient(create_app(replace(settings, price_source="sqlite"))) as client:
            _run(client, settings, console, pause)
    finally:
        tracer.remove_sink(sink)


def _post(client: TestClient, path: str, body: dict[str, Any], accept: str = "application/fhir+json") -> Any:
    return client.post(path, content=json.dumps(body), headers={**FHIR_JSON, "Accept": accept})


def _run(client: TestClient, settings: Settings, console: Console, pause: bool) -> None:
    _banner(console, 1, "Self-pay estimate, XML response",
            "No Coverage in the request → priced from the imaging center's discounted-cash price. A freestanding center\n"
            "bills the technical component (-TC) of the MRI. Same packet shape, requested as\n"
            "[bold]application/fhir+xml[/] to show the pluggable response formats.")
    _wait(console, pause)
    _post(client, "/fhir/$gfe-estimate?_format=xml", ex.brain_mri_self_pay())

    _banner(console, 2, "Participating provider: completed GFE Contributor Task",
            "Surgeon Dr. Cutter was assigned a Contributor Task for a knee arthroscopy. The Task carries the GFE\n"
            "Information Bundle as a base64 attachment. The service prices [bold]only the lines Dr. Cutter bills[/] and\n"
            "returns the Task [bold]completed[/] with a GFE Bundle in Task.output, ready to PUT back to whoever assigned it.")
    _wait(console, pause)
    _post(client, "/fhir/Task/$gfe-contribute", ex.contributor_task_surgeon())

    if settings.neuron_api_key:
        with TestClient(create_app(replace(settings, price_source="neuron"))) as neuron:
            _banner(console, 3, "Live hospital brain MRI",
                    "Emerson Hospital, CPT 70551, BCBS MA Blue Care Elect. Neuron supplies the institutional price;\n"
                    "the radiologist's separately billed interpretation is excluded.")
            _wait(console, pause)
            _post(neuron, "/fhir/$gfe-estimate", ex.emerson_brain_mri_bcbsma())

            _banner(console, 4, "Live physician office visit",
                    "Brian D. Hoffman, MD (TriHealth), CPT 99213, Aetna OAMC. Neuron supplies the professional price.\n"
                    "Patient and coverage details are illustrative.")
            _wait(console, pause)
            _post(neuron, "/fhir/$gfe-estimate", ex.hoffman_office_visit_aetna())

            _banner(console, 5, "Live CT pricing attempt: Contributor Task outcome",
                    "Cleveland Clinic, CT head/chest/abdomen-pelvis, Aetna OAMC. Simulated accepted participation.\n"
                    "If any billed CT line is unpriced, return [bold]Task failed[/] with an explicit reason and no GFE.\n"
                    "No matching rate does not establish out-of-network status. Live results may change.")
            _wait(console, pause)
            _post(neuron, "/fhir/Task/$gfe-contribute", ex.contributor_task_cleveland_clinic_ct())
    else:
        console.print("\n[dim]Set GFE_NEURON_API_KEY to add the three live Neuron scenarios (MRI, office visit, CT Task).[/]")
    console.print("\n[bold green]Demo complete.[/]")
