"""Response formats.

FHIR allows a server to return resources in several representations; the PCT IG examples
are JSON, so JSON is the default. Formats are pluggable: register another ``ResponseFormat``
(e.g. a patient-facing PDF/HTML rendering of the GFE Packet, or Turtle) and it becomes
selectable through ``_format`` or the ``Accept`` header without touching the endpoints.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable

FHIR_NS = "http://hl7.org/fhir"
XHTML_NS = "http://www.w3.org/1999/xhtml"
ET.register_namespace("xhtml", XHTML_NS)


@dataclass(frozen=True)
class ResponseFormat:
    name: str
    media_type: str
    aliases: tuple[str, ...]
    serialize: Callable[[dict[str, Any], bool], str]


def _json(resource: dict[str, Any], pretty: bool) -> str:
    return json.dumps(resource, indent=2 if pretty else None, separators=None if pretty else (",", ":"), ensure_ascii=False)


# --- FHIR JSON -> FHIR XML ------------------------------------------------------------------
# Implements the R4 XML rules that matter for these resources: primitives as @value,
# extension @url, element @id, contained/inline resources wrapped in their type element,
# primitive extensions from "_field" siblings, and XHTML narrative. Element order follows
# the JSON, which our builders emit in FHIR definition order.


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _add_element(parent: ET.Element, name: str, value: Any, shadow: Any = None) -> None:
    if isinstance(value, list):
        shadows = shadow if isinstance(shadow, list) else [None] * len(value)
        for v, sh in zip(value, shadows + [None] * (len(value) - len(shadows))):
            _add_element(parent, name, v, sh)
        return
    el = ET.SubElement(parent, name)
    if isinstance(value, dict):
        if "resourceType" in value:
            _resource_into(el, value)
            return
        if name == "div":
            return
        _object_into(el, value)
    elif value is not None:
        el.set("value", _scalar(value))
    if isinstance(shadow, dict):
        if "id" in shadow:
            el.set("id", shadow["id"])
        for ext in shadow.get("extension", []):
            _add_element(el, "extension", ext)


def _object_into(el: ET.Element, obj: dict[str, Any]) -> None:
    for key, value in obj.items():
        if key.startswith("_"):
            continue
        if key in ("id", "url") and isinstance(value, str) and (key == "id" or el.tag in ("extension", "modifierExtension")):
            el.set(key, value)
            continue
        if key == "div" and isinstance(value, str):
            try:
                div = ET.fromstring(value)
            except ET.ParseError:
                div = ET.Element(f"{{{XHTML_NS}}}div")
                div.text = value
            el.append(div)
            continue
        _add_element(el, key, value, obj.get(f"_{key}"))


def _resource_into(parent: ET.Element, resource: dict[str, Any]) -> None:
    root = ET.SubElement(parent, resource["resourceType"])
    body = {k: v for k, v in resource.items() if k != "resourceType"}
    # On resources, "id" is an element (not an attribute).
    if "id" in body:
        ET.SubElement(root, "id").set("value", body.pop("id"))
    _object_into(root, body)


def _xml(resource: dict[str, Any], pretty: bool) -> str:
    holder = ET.Element("holder")
    _resource_into(holder, resource)
    root = holder[0]
    root.set("xmlns", FHIR_NS)
    if pretty:
        ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


FORMATS: dict[str, ResponseFormat] = {}


def register(fmt: ResponseFormat) -> None:
    FORMATS[fmt.name] = fmt


register(ResponseFormat("json", "application/fhir+json", ("json", "application/json", "application/fhir+json", "text/json"), _json))
register(ResponseFormat("xml", "application/fhir+xml", ("xml", "application/xml", "application/fhir+xml", "text/xml"), _xml))


class NotAcceptable(Exception):
    pass


def negotiate(format_param: str | None, accept: str | None) -> ResponseFormat:
    """Pick a format: ``_format`` wins, then ``Accept`` (in order), defaulting to JSON."""

    def lookup(token: str) -> ResponseFormat | None:
        token = token.split(";")[0].strip().lower()
        for fmt in FORMATS.values():
            if token in fmt.aliases:
                return fmt
        return None

    if format_param:
        fmt = lookup(format_param)
        if not fmt:
            raise NotAcceptable(f"Unsupported _format '{format_param}'. Supported: {', '.join(FORMATS)}")
        return fmt
    if accept:
        for token in accept.split(","):
            token = token.strip()
            if token in ("*/*", "") or token.startswith("application/*"):
                return FORMATS["json"]
            fmt = lookup(token)
            if fmt:
                return fmt
        raise NotAcceptable(f"None of Accept '{accept}' is supported. Supported: "
                            + ", ".join(f.media_type for f in FORMATS.values()))
    return FORMATS["json"]
