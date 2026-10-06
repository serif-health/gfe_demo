"""Rich terminal renderer for trace events.

Levels:
  quiet   - one line per inbound request / outbound response
  summary - + parsed request, every DB lookup (with returned rows), engine decisions,
            priced lines, and a tree view of the FHIR resources received and sent
  full    - + SQL text / parameters and the raw serialized request & response bodies
"""

from __future__ import annotations

import base64
import json
import threading
from datetime import datetime
from typing import Any

from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from .domain import EstimateRequest, EstimateResult
from .fhir.constants import CODE_SYSTEM_TO_TYPE, CS_POS, CS_REVENUE, CS_TAXONOMY
from .trace import TraceEvent

LEVELS = ("off", "quiet", "summary", "full")


def _short(url: str | None) -> str:
    return (url or "").rstrip("/").split("/")[-1]


_SYSTEM_LABELS = {**CODE_SYSTEM_TO_TYPE, CS_REVENUE: "REV", CS_POS: "POS", CS_TAXONOMY: "NUCC"}


def _sys(system: str | None) -> str:
    return _SYSTEM_LABELS.get(system or "", _short(system))


def _money(v: Any) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _codings(concept: dict[str, Any] | None) -> str:
    out = []
    for cd in (concept or {}).get("coding", []) or []:
        sys = _sys(cd.get("system"))
        out.append(f"{sys}:{cd.get('code')}" + (f" {cd['display']}" if cd.get("display") else ""))
    return ", ".join(out) or (concept or {}).get("text", "")


def _name(res: dict[str, Any]) -> str:
    n = res.get("name")
    if isinstance(n, str):
        return n
    if isinstance(n, list) and n:
        return n[0].get("text") or " ".join([*n[0].get("given", []), n[0].get("family", "")]).strip()
    return ""


def _npi(res: dict[str, Any]) -> str:
    for i in res.get("identifier", []) or []:
        if i.get("system") == "http://hl7.org/fhir/sid/us-npi":
            return f"NPI {i.get('value')}"
    return ""


def _decode(att: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return json.loads(base64.b64decode(att.get("data", "")))
    except Exception:
        return None


def describe(res: dict[str, Any], tree: Tree | None = None, depth: int = 0, expand_outputs: bool = False) -> Tree:
    """Human summary of a FHIR resource as a Rich tree."""
    rt = res.get("resourceType", "?")
    rid = res.get("id", "")
    profile = _short(((res.get("meta") or {}).get("profile") or [""])[0])
    prof = f" [dim]{profile}[/]" if profile else ""

    if rt == "Bundle":
        total = ""
        claims = [e["resource"] for e in res.get("entry", []) or [] if (e.get("resource") or {}).get("resourceType") == "Claim"]
        summary = next((c for c in claims if "summary" in _short(((c.get("meta") or {}).get("profile") or [""])[0])), None)
        if summary:
            total = f"  total [bold green]{_money((summary.get('total') or {}).get('value'))}[/]"
        elif res.get("type") == "document":
            nested = [e["resource"] for e in res.get("entry", []) or [] if (e.get("resource") or {}).get("resourceType") == "Bundle"]
            amounts = [
                (c.get("total") or {}).get("value", 0)
                for nb in nested for c in (x["resource"] for x in nb.get("entry", []) or [])
                if c.get("resourceType") == "Claim" and "summary" in _short(((c.get("meta") or {}).get("profile") or [""])[0])
            ]
            if amounts:
                total = f"  packet total [bold green]{_money(sum(amounts))}[/]"
        style = "red" if "missing" in profile else "bold"
        label = f"[{style}]Bundle[/] {res.get('type')}{prof} [dim]{rid if len(rid) < 30 else rid[:8]}[/] · {len(res.get('entry', []) or [])} entries{total}"
    elif rt == "Patient":
        label = f"[cyan]Patient[/] {_name(res)} · {res.get('gender', '')} · DOB {res.get('birthDate', '?')}"
    elif rt == "Coverage":
        kind = next((e.get("valueCode") for e in res.get("extension", []) or [] if "Coverage.kind" in e.get("url", "")), "insurance")
        plan = ", ".join(c.get("value", "") for c in res.get("class", []) or [])
        label = f"[cyan]Coverage[/] {kind}" + (f" · plan {plan}" if plan else "") + (f" · subscriber {res['subscriberId']}" if res.get("subscriberId") else "")
    elif rt in ("Organization", "Practitioner"):
        label = f"[cyan]{rt}[/] {_name(res)} [dim]{_npi(res) or rid}[/]"
    elif rt in ("ServiceRequest", "DeviceRequest", "MedicationRequest"):
        concept = res.get("code") or res.get("codeCodeableConcept") or res.get("medicationCodeableConcept")
        label = f"[magenta]{rt}[/] {_codings(concept)}"
    elif rt == "Location":
        label = f"[cyan]Location[/] {res.get('name', '')}"
    elif rt == "Claim":
        ctype = ((res.get("type") or {}).get("coding") or [{}])[0].get("code")
        prov = (res.get("provider") or {}).get("display", "")
        label = f"[yellow]Claim[/] {ctype}{prof} {prov} · total [green]{_money((res.get('total') or {}).get('value'))}[/]"
    elif rt == "Composition":
        label = f"[bold]Composition[/] {res.get('title', '')} · {len(res.get('section', []))} sections"
    elif rt == "Task":
        code = ((res.get("code") or {}).get("coding") or [{}])[0].get("code")
        owner = (res.get("owner") or {}).get("reference", "-")
        label = f"[bold blue]Task[/] {rid} · {code} · status [bold]{res.get('status')}[/] · owner {owner}"
        if res.get("statusReason"):
            label += f" · [red]{res['statusReason'].get('text', '')}[/]"
    elif rt == "OperationOutcome":
        label = "[red]OperationOutcome[/]"
    else:
        label = f"{rt} {rid}"

    node = tree.add(label) if tree is not None else Tree(label)

    if depth > 3:
        return node
    if rt == "Bundle":
        for e in res.get("entry", []) or []:
            if e.get("resource"):
                describe(e["resource"], node, depth + 1, expand_outputs)
            if e.get("response"):
                node.children[-1].label = Text.from_markup(f"{node.children[-1].label} [dim]→ {e['response'].get('status')}[/]")  # type: ignore[arg-type]
    elif rt == "Claim":
        for it in res.get("item", []) or []:
            mod = ",".join(cd.get("code") for m in it.get("modifier", []) or [] for cd in m.get("coding", []))
            rev = ((it.get("revenue") or {}).get("coding") or [{}])[0].get("code")
            code = ((it.get("productOrService") or {}).get("coding") or [{}])[0]
            node.add(
                f"#{it.get('sequence')} {_sys(code.get('system'))} {code.get('code')}{'-' + mod if mod else ''}"
                + (f" rev {rev}" if rev else "")
                + f" × {(it.get('quantity') or {}).get('value', 1):g} @ {_money((it.get('unitPrice') or {}).get('value'))}"
                + f" = [green]{_money((it.get('net') or {}).get('value'))}[/]"
            )
    elif rt == "Composition":
        for s in res.get("section", []) or []:
            code = ((s.get("code") or {}).get("coding") or [{}])[0].get("code")
            author = ", ".join(a.get("display", a.get("reference", "")) for a in s.get("author", []) or [])
            node.add(f"[dim]section[/] {code}" + (f" · author {author}" if author else ""))
    elif rt == "Task":
        for inp in res.get("input", []) or []:
            kind = ((inp.get("type") or {}).get("coding") or [{}])[0].get("code")
            if "valueAttachment" in inp:
                inner = _decode(inp["valueAttachment"])
                sub = node.add(f"input {kind} [dim](base64 attachment)[/]")
                if inner:
                    describe(inner, sub, depth + 1)
            elif "valueReference" in inp:
                node.add(f"input {kind} → {inp['valueReference'].get('reference')}")
        for out in res.get("output", []) or []:
            kind = ((out.get("type") or {}).get("coding") or [{}])[0].get("code")
            inner = _decode(out.get("valueAttachment") or {})
            sub = node.add(f"[green]output {kind}[/] [dim](base64 attachment, {len(out.get('valueAttachment', {}).get('data', ''))} chars)[/]")
            if inner:
                if expand_outputs:
                    describe(inner, sub, depth + 1)
                else:
                    sub.add(f"Bundle {inner.get('type')} {_short(((inner.get('meta') or {}).get('profile') or [''])[0])} · {len(inner.get('entry', []))} entries")
    elif rt == "OperationOutcome":
        for issue in res.get("issue", []) or []:
            node.add(f"[red]{issue.get('severity')}[/] {issue.get('code')}: {issue.get('diagnostics', '')}")
    return node


class ConsoleRenderer:
    def __init__(self, console: Console | None = None, level: str = "summary", body_lines: int = 80) -> None:
        if level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}")
        self.console = console or Console()
        self.level = level
        self.body_lines = body_lines
        self._lock = threading.Lock()
        self._last_built: dict[str | None, str | None] = {}

    def __call__(self, event: TraceEvent) -> None:
        if self.level == "off":
            return
        handler = getattr(self, "_on_" + event.kind.replace(".", "_"), None)
        if handler is None:
            return
        if self.level == "quiet" and event.kind not in ("http.in", "http.out", "http.error"):
            return
        with self._lock:
            handler(event)

    # -- helpers ---------------------------------------------------------------------------
    @property
    def full(self) -> bool:
        return self.level == "full"

    def _prefix(self, e: TraceEvent) -> str:
        ts = datetime.fromtimestamp(e.ts).strftime("%H:%M:%S.%f")[:-3]
        return f"[dim]{ts}[/] [bold]{e.request_id or ''}[/]"

    def _body(self, text: str, lexer: str) -> Syntax:
        lines = text.splitlines()
        if len(lines) > self.body_lines:
            text = "\n".join(lines[: self.body_lines]) + f"\n… ({len(lines) - self.body_lines} more lines)"
        return Syntax(text, lexer, theme="ansi_dark", word_wrap=True, background_color="default")

    # -- HTTP ------------------------------------------------------------------------------
    def _on_http_in(self, e: TraceEvent) -> None:
        d = e.data
        q = f"?{d['query']}" if d.get("query") else ""
        self.console.print()
        self.console.rule(f"{self._prefix(e)} [bold cyan]▶ INBOUND {d['method']} {d['path']}{q}[/] "
                          f"[dim]{d.get('content_type') or ''} {d['size']:,} B[/]", align="left", style="cyan")
        if self.level == "quiet" or not d.get("body"):
            return
        self.console.print(Panel(describe(d["body"]), title="request", title_align="left", border_style="cyan", box=box.ROUNDED))
        if self.full:
            self.console.print(self._body(json.dumps(d["body"], indent=2), "json"))

    def _on_http_out(self, e: TraceEvent) -> None:
        d = e.data
        ok = d["status"] < 400
        color = "green" if ok else "red"
        already_shown = d.get("body") and self._last_built.get(e.request_id) == d["body"].get("id")
        if already_shown and self.level != "quiet":
            self.console.print(f"  [dim]response body = {d['body']['resourceType']}/{d['body'].get('id')} (tree above)[/]")
        elif self.level != "quiet" and d.get("body") and d["body"].get("resourceType") != "CapabilityStatement":
            self.console.print(Panel(describe(d["body"]), title=f"response ({d['format']})", title_align="left",
                                     border_style=color, box=box.ROUNDED))
        if self.full and d.get("serialized"):
            self.console.print(self._body(d["serialized"], "xml" if d["format"] == "xml" else "json"))
        self.console.rule(f"{self._prefix(e)} [bold {color}]◀ OUTBOUND {d['status']}[/] {d['method']} {d['path']} "
                          f"[dim]{d['media_type']} {d['size']:,} B · {d['elapsed_ms']:.1f} ms[/]", align="left", style=color)

    def _on_http_error(self, e: TraceEvent) -> None:
        self.console.print(f"  [bold red]✖ {e.data['status']}[/] {e.data['message']}")
        if self.full and e.data.get("traceback"):
            self.console.print(Text(e.data["traceback"], style="red dim"))

    def _on_client_request(self, e: TraceEvent) -> None:
        d = e.data
        self.console.print()
        self.console.rule(f"{self._prefix(e)} [bold magenta]→ CONTRIBUTOR {d['method']} {d['url']}[/]", align="left", style="magenta")
        if d.get("body") and self.level != "quiet":
            self.console.print(Panel(describe(d["body"]), border_style="magenta", box=box.ROUNDED, title="sent", title_align="left"))

    def _on_client_response(self, e: TraceEvent) -> None:
        d = e.data
        color = "magenta" if d["status"] < 400 else "red"
        body = d.get("body") or {}
        extra = ""
        if body.get("resourceType") == "Bundle" and body.get("type") == "searchset":
            extra = f" · {body.get('total', len(body.get('entry', [])))} match(es)"
        elif body.get("resourceType"):
            extra = f" · {body['resourceType']}/{body.get('id', '')}" + (f" status={body['status']}" if "status" in body else "")
        self.console.print(f"  [{color}]← {d['status']}[/] [dim]{d['elapsed_ms']:.1f} ms{extra}[/]")

    # -- Parsing / engine ------------------------------------------------------------------
    def _on_fhir_parsed(self, e: TraceEvent) -> None:
        r: EstimateRequest = e.data["request"]
        t = Table(box=box.SIMPLE_HEAD, show_header=False, padding=(0, 1), title="[bold]Estimate request (parsed)[/]", title_justify="left")
        t.add_column(style="dim", no_wrap=True)
        t.add_column()
        payer = (r.payer_org or {}).get("name") or ("self-pay" if r.self_pay else "-")
        plan = ", ".join(c.get("value", "") for c in (r.coverage or {}).get("class", []) or [])
        t.add_row("patient", f"{_name(r.patient)} ({r.patient.get('birthDate', '?')})")
        t.add_row("coverage", f"{payer}" + (f" · {plan}" if plan else ""))
        t.add_row("service date", f"{r.planned_start or 'unscheduled'}" + (f" → {r.planned_end}" if r.planned_end and r.planned_end != r.planned_start else ""))
        t.add_row("items", "\n".join(f"{i.resource_type}/{i.resource_id}: {i.code_type} {i.code} × {i.quantity:g}" for i in r.items))
        if r.diagnoses:
            t.add_row("diagnoses", ", ".join(f"{d.code} {d.display or ''}".strip() for d in r.diagnoses))
        if r.provider_hints:
            t.add_row("providers named", ", ".join(f"{h.kind} NPI {h.npi}" for h in r.provider_hints))
        if r.contributor_npi:
            t.add_row("acting as", f"[bold]GFE Contributor[/] NPI {r.contributor_npi}")
        if r.linking_identifier:
            t.add_row("linking id", str(r.linking_identifier.get("value")))
        self.console.print(t)

    def _on_db_query(self, e: TraceEvent) -> None:
        d = e.data
        n = len(d["rows"])
        color = "blue" if n else "yellow"
        self.console.print(f"  [{color}]⛁[/] [dim]{d['elapsed_ms']:.2f} ms[/] {d['purpose']} [dim]→ {n} row(s)[/]")
        if self.full:
            self.console.print(Syntax(d["sql"], "sql", theme="ansi_dark", background_color="default", padding=(0, 4)))
            self.console.print(f"    [dim]params: {d['params']}[/]")
        if not n:
            return
        rows = d["rows"][:5]
        if len(d["columns"]) > 7:
            # wide rows (e.g. provider records): one compact key=value line each
            for row in rows:
                pairs = " · ".join(f"{k}={v}" for k, v in zip(d["columns"], row) if v not in (None, ""))
                self.console.print(Padding(Text(pairs, style="dim", overflow="ellipsis", no_wrap=True), (0, 0, 0, 6)))
        else:
            t = Table(box=box.SIMPLE_HEAD, padding=(0, 1), header_style="dim", show_edge=False)
            for col in d["columns"]:
                t.add_column(col, overflow="fold", max_width=48)
            for row in rows:
                t.add_row(*["" if v is None else str(v) for v in row])
            self.console.print(Padding(t, (0, 0, 0, 4)))
        if n > 5:
            self.console.print(f"      [dim]… {n - 5} more row(s)[/]")

    def _on_engine_step(self, e: TraceEvent) -> None:
        self.console.print(f"  [bold white]⚙[/] {e.data['message']}")

    def _on_engine_warn(self, e: TraceEvent) -> None:
        self.console.print(f"  [bold yellow]⚠[/] [yellow]{e.data['message']}[/]")

    def _on_engine_priced(self, e: TraceEvent) -> None:
        line, prov = e.data["line"], e.data["provider"]
        code = f"{line.component.code_type} {line.component.code}{'-' + line.modifier if line.modifier else ''}"
        self.console.print(f"  [green]$[/] {code} × {line.quantity:g} @ {_money(line.unit_price)} = [bold green]{_money(line.net)}[/]"
                           f" [dim]· {prov.name} · {line.methodology}[/]")

    def _on_engine_result(self, e: TraceEvent) -> None:
        res: EstimateResult = e.data["result"]
        t = Table(title="[bold]Estimate[/]", title_justify="left", box=box.SIMPLE_HEAVY, show_footer=True)
        t.add_column("Billing provider", footer="[bold]Total[/]")
        t.add_column("Claim")
        t.add_column("Code")
        t.add_column("Rev")
        t.add_column("Qty", justify="right")
        t.add_column("Unit", justify="right")
        t.add_column("Net", justify="right", footer=f"[bold green]{_money(res.total)}[/]")
        t.add_column("Basis", style="dim", max_width=46)
        for est in res.provider_estimates:
            for i, line in enumerate(est.lines):
                t.add_row(
                    f"{est.provider.name}\n[dim]NPI {est.provider.npi}[/]" if i == 0 else "",
                    est.billing_class if i == 0 else "",
                    f"{line.component.code}{'-' + line.modifier if line.modifier else ''}",
                    line.component.revenue_code or "", f"{line.quantity:g}", _money(line.unit_price),
                    _money(line.net), line.methodology,
                )
        for u in res.unpriced:
            t.add_row(u.provider.name if u.provider else "[red]unassigned[/]", "-", u.component.code, "", "", "", "[red]missing[/]", u.reason)
        self.console.print(t)

    def _on_fhir_built(self, e: TraceEvent) -> None:
        self._last_built[e.request_id] = e.data["resource"].get("id")
        self.console.print(Panel(describe(e.data["resource"]), title=f"built {e.data.get('label', '')}",
                                 title_align="left", border_style="green", box=box.ROUNDED))


def make_console_sink(level: str = "summary", body_lines: int = 80, console: Console | None = None) -> ConsoleRenderer:
    return ConsoleRenderer(console=console, level=level, body_lines=body_lines)


__all__ = ["ConsoleRenderer", "make_console_sink", "describe", "LEVELS"]
