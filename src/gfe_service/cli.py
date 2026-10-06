"""``gfe-demo`` command line: run the service, send requests, play a contributor, run the demo."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import PROJECT_ROOT, Settings, get_settings, set_settings
from .console import describe, make_console_sink
from .db import init_db, load_csv
from .trace import tracer

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Da Vinci PCT Good Faith Estimate responder demo.")
db_app = typer.Typer(no_args_is_help=True, help="Manage the pricing database.")
app.add_typer(db_app, name="db")
console = Console()

EXAMPLES_DIR = PROJECT_ROOT / "examples"


class Level(str, Enum):
    off = "off"
    quiet = "quiet"
    summary = "summary"
    full = "full"


class Fmt(str, Enum):
    json = "json"
    xml = "xml"


def _settings(db: Optional[Path]) -> Settings:
    settings = get_settings()
    if db:
        settings.db_path = db
    set_settings(settings)
    if not settings.db_path.exists():
        init_db(settings.db_path)
        console.print(f"[dim]initialized pricing database at {settings.db_path}[/]")
    return settings


# --- db ---------------------------------------------------------------------------------------


@db_app.command("init")
def db_init(reset: bool = typer.Option(False, help="Delete and recreate the database."),
            db: Optional[Path] = typer.Option(None, help="SQLite path (default: data/pricing.db or $GFE_DB_PATH).")) -> None:
    """Create the schema and load the synthetic seed data."""
    path = init_db(db or get_settings().db_path, reset=reset)
    console.print(f"[green]✓[/] pricing database ready: {path}")


@db_app.command("load")
def db_load(table: str = typer.Argument(..., help="Target table, e.g. negotiated_rates or standard_charges."),
            csv_path: Path = typer.Argument(..., exists=True, help="CSV whose header matches the table's columns."),
            db: Optional[Path] = typer.Option(None)) -> None:
    """Append your own pricing data from CSV (header names = column names)."""
    settings = _settings(db)
    n = load_csv(settings.db_path, table, csv_path)
    console.print(f"[green]✓[/] loaded {n} row(s) into {table}")


@db_app.command("show")
def db_show(table: str = typer.Argument("negotiated_rates"), limit: int = 25, db: Optional[Path] = typer.Option(None)) -> None:
    """Print rows from a table."""
    settings = _settings(db)
    conn = sqlite3.connect(settings.db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if table not in tables:
        raise typer.BadParameter(f"unknown table; choose from {sorted(tables - {'sqlite_sequence'})}")
    cur = conn.execute(f"SELECT * FROM {table} LIMIT ?", (limit,))
    t = Table(title=table, box=box.SIMPLE_HEAVY)
    for col in [d[0] for d in cur.description]:
        t.add_column(col, overflow="fold")
    for row in cur.fetchall():
        t.add_row(*["" if v is None else str(v) for v in row])
    console.print(t)


# --- examples ---------------------------------------------------------------------------------


@app.command()
def examples(out: Path = typer.Option(EXAMPLES_DIR, help="Output directory.")) -> None:
    """(Re)generate the example request payloads."""
    from .demo.examples import write_examples

    for p in write_examples(out):
        console.print(f"[green]✓[/] {p}")


# --- serve ------------------------------------------------------------------------------------


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000,
          trace: Level = typer.Option(Level.summary, help="Terminal trace detail."),
          body_lines: int = typer.Option(80, help="Max lines of raw body to print at --trace full."),
          db: Optional[Path] = typer.Option(None)) -> None:
    """Run the FHIR API with a live terminal trace of requests, DB lookups and responses."""
    import uvicorn

    from .app import create_app

    settings = _settings(db)
    if "GFE_FHIR_BASE" not in os.environ:
        settings.fhir_base = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/fhir"
    tracer.add_sink(make_console_sink(trace.value, body_lines, console))
    console.print(Panel.fit(
        f"[bold]GFE responder[/]  FHIR base [cyan]{settings.fhir_base}[/]\n"
        f"pricing db [dim]{settings.db_path}[/] · default provider NPI [dim]{settings.default_provider_npi}[/] · trace [bold]{trace.value}[/]\n\n"
        f"POST {settings.fhir_base}/$gfe-estimate            GFE Information Bundle → GFE Packet\n"
        f"POST {settings.fhir_base}/Task/$gfe-contribute     Contributor Task → completed Task + GFE Bundle\n"
        f"POST {settings.fhir_base}/$gfe-coordination-request  Coordination Bundle → Tasks on platform\n"
        f"GET  {settings.fhir_base}/Task/{{id}}/$gfe-retrieve    → GFE Packet (incl. Missing Bundles)\n"
        f"[dim]add ?_format=xml (or Accept: application/fhir+xml) for XML · docs at /docs[/]",
        title="gfe-demo", border_style="cyan"))
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")


# --- client commands --------------------------------------------------------------------------


def _detect_endpoint(resource: dict[str, Any]) -> str:
    rt, btype = resource.get("resourceType"), resource.get("type")
    if rt == "Task":
        return "Task/$gfe-contribute"
    if rt == "Bundle" and btype == "transaction":
        return "$gfe-coordination-request"
    return "$gfe-estimate"


def _show_response(r: Any, fmt: str, raw: bool, save: Optional[Path]) -> None:
    color = "green" if r.status_code < 400 else "red"
    console.rule(f"[bold {color}]{r.status_code}[/] {r.headers.get('content-type', '')} · {len(r.content):,} B", align="left", style=color)
    if fmt == "json":
        try:
            body = r.json()
            console.print(describe(body, expand_outputs=True))
        except ValueError:
            raw = True
    if raw or fmt == "xml":
        from rich.syntax import Syntax

        console.print(Syntax(r.text, "xml" if fmt == "xml" else "json", theme="ansi_dark", background_color="default"))
    if save:
        save.write_text(r.text)
        console.print(f"[dim]saved → {save}[/]")


@app.command()
def send(file: Path = typer.Argument(..., exists=True, help="Request payload (JSON)."),
         url: str = typer.Option("http://localhost:8000/fhir", help="FHIR base URL of the running service."),
         endpoint: Optional[str] = typer.Option(None, help="Override: $gfe-estimate | Task/$gfe-contribute | $gfe-coordination-request"),
         fmt: Fmt = typer.Option(Fmt.json, "--format", help="Response format to request."),
         raw: bool = typer.Option(False, help="Also print the raw response body."),
         save: Optional[Path] = typer.Option(None, help="Write the response body to this file.")) -> None:
    """POST a payload to the running service and pretty-print the FHIR response."""
    import httpx

    resource = json.loads(file.read_text())
    ep = endpoint or _detect_endpoint(resource)
    console.rule(f"[bold cyan]POST {url}/{ep}[/] [dim]{file}[/]", align="left", style="cyan")
    console.print(describe(resource))
    r = httpx.post(f"{url.rstrip('/')}/{ep}", json=resource, timeout=60,
                   headers={"Content-Type": "application/fhir+json", "Accept": f"application/fhir+{fmt.value}"})
    _show_response(r, fmt.value, raw, save)
    if r.status_code >= 400:
        raise typer.Exit(1)


@app.command()
def retrieve(task_id: str = typer.Argument(..., help="Coordination Task id."),
             url: str = typer.Option("http://localhost:8000/fhir"),
             fmt: Fmt = typer.Option(Fmt.json, "--format"),
             raw: bool = False, save: Optional[Path] = typer.Option(None)) -> None:
    """Call $gfe-retrieve on a Coordination Task and print the GFE Packet."""
    import httpx

    r = httpx.get(f"{url.rstrip('/')}/Task/{task_id}/$gfe-retrieve", timeout=60,
                  headers={"Accept": f"application/fhir+{fmt.value}"})
    _show_response(r, fmt.value, raw, save)


@app.command()
def contribute(npi: str = typer.Option(..., help="NPI of the provider this worker acts for."),
               platform_url: str = typer.Option("http://localhost:8000/fhir", help="Coordination Platform FHIR base."),
               trace: Level = typer.Option(Level.summary),
               db: Optional[Path] = typer.Option(None)) -> None:
    """Act as a GFE Contributor: pick up assigned Tasks on a platform, price them, attach GFE Bundles."""
    from .platform.worker import ContributorWorker
    from .pricing.repository import SqlitePricingRepository
    from .service import GfeService

    settings = _settings(db)
    tracer.add_sink(make_console_sink(trace.value, console=console))
    service = GfeService(SqlitePricingRepository(settings.db_path), settings)
    done = ContributorWorker(service, npi, platform_url=platform_url).run_once()
    console.print(f"[green]✓[/] {len(done)} task(s) processed: " + ", ".join(f"Task/{t['id']}={t['status']}" for t in done))


# --- demo -------------------------------------------------------------------------------------


@app.command()
def demo(trace: Level = typer.Option(Level.summary, help="Terminal trace detail."),
         pause: bool = typer.Option(sys.stdin.isatty(), help="Wait for Enter between scenarios."),
         db: Optional[Path] = typer.Option(None)) -> None:
    """Run every scenario in-process (no server needed) with the live trace."""
    from .demo.runner import run_demo

    settings = _settings(db)
    run_demo(settings, trace.value, pause, console)


if __name__ == "__main__":  # pragma: no cover
    app()
