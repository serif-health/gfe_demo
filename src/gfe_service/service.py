"""Use cases: Information Bundle -> GFE Packet, and Contributor Task -> completed Task."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from .config import Settings
from .domain import EstimateResult, ProviderHint
from .fhir import builders as b
from .fhir.parse import FhirInputError, index_bundle, npi_of, parse_information_bundle, resolve
from .pricing.engine import EstimateEngine
from .pricing.repository import PricingRepository
from .trace import tracer


def linking_identifier_for(coordination_task: dict[str, Any] | None, base: str) -> dict[str, Any] | None:
    """All GFEs for one coordinated period of care share this GFEServiceLinkingInfo identifier."""
    if not coordination_task:
        return None
    for ident in coordination_task.get("identifier", []) or []:
        if ident.get("value"):
            return {k: ident[k] for k in ("system", "value") if k in ident}
    return {"system": f"{base}/identifiers/gfe-coordination-task", "value": coordination_task.get("id")}


class GfeService:
    def __init__(self, repo: PricingRepository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings
        self.engine = EstimateEngine(repo, settings.default_provider_npi)

    # -- Direct: Information Bundle -> GFE Packet ------------------------------------------
    def packet_from_information_bundle(self, info_bundle: dict[str, Any]) -> dict[str, Any]:
        req = parse_information_bundle(info_bundle)
        result = self.engine.estimate(req)
        if not result.provider_estimates:
            raise FhirInputError(
                "No requested service could be priced: " + "; ".join(result.warnings or ["no pricing data"]),
                code="not-found", status=422,
            )
        ctx = b.PacketContext(self.settings.fhir_base, req, self.settings.disclaimer)
        sections: list[b.PacketSection] = []
        for est in result.provider_estimates:
            bundle, provider_res = b.build_gfe_bundle(ctx, est)
            sections.append(b.PacketSection(bundle, provider_res))
        sections.extend(self._missing_sections(ctx, result))
        initiating = next((s.author for s in sections if s.author["resourceType"] == "Organization"), sections[0].author)
        packet = b.build_packet(ctx, sections, initiating)
        tracer.emit("fhir.built", resource=packet, label="GFE Packet")
        return packet

    def _missing_sections(self, ctx: b.PacketContext, result: EstimateResult) -> list[b.PacketSection]:
        """Components we could not price become GFE Missing Bundles so the gap is explicit."""
        grouped: OrderedDict[str, list] = OrderedDict()
        for u in result.unpriced:
            grouped.setdefault(u.provider.npi if u.provider else "", []).append(u)
        sections = []
        for unpriced in grouped.values():
            provider = unpriced[0].provider
            if provider is None:
                continue  # nobody to attribute the section to (author is 1..1); surfaced as a warning
            provider_res = b.provider_resource(provider)
            missing = b.build_missing_bundle(ctx, provider_res, [u.item.resource for u in unpriced])
            sections.append(b.PacketSection(missing, provider_res, missing=True))
            tracer.warn(f"Missing GFE Bundle added for {provider.name}: " + ", ".join(u.component.code for u in unpriced))
        return sections

    # -- Contributor: Task -> completed Task with GFE Bundle -------------------------------
    def fulfill_contributor_task(
        self,
        task: dict[str, Any],
        info_bundle: dict[str, Any],
        contributor: ProviderHint,
        coordination_task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        req = parse_information_bundle(
            info_bundle,
            tasks=[task, coordination_task] if coordination_task else [task],
            contributor=contributor,
            linking_identifier=linking_identifier_for(coordination_task, self.settings.fhir_base),
        )
        result = self.engine.estimate(req)
        if not result.provider_estimates:
            codes = ", ".join(f"{i.code_type} {i.code}" for i in req.items)
            detail = result.warnings or [f"none of the requested services ({codes}) include components billed by "
                                         f"NPI {contributor.npi} in our pricing data"]
            reason = "Unable to produce an estimate: " + "; ".join(detail)
            tracer.warn(f"Task/{task.get('id')} -> failed: {reason}")
            return b.fail_contributor_task(task, reason)
        ctx = b.PacketContext(self.settings.fhir_base, req, self.settings.disclaimer)
        # A contributor bills under one billing class; merge defensively if more than one came back.
        est = result.provider_estimates[0]
        for extra in result.provider_estimates[1:]:
            est.lines.extend(extra.lines)
        bundle, _ = b.build_gfe_bundle(ctx, est)
        tracer.emit("fhir.built", resource=bundle, label=f"GFE Bundle for Task/{task.get('id')}")
        return b.complete_contributor_task(task, bundle)

    def contributor_from_task(
        self, task: dict[str, Any], info_bundle: dict[str, Any] | None,
        directory_lookup: Any = None, npi_override: str | None = None,
    ) -> ProviderHint:
        """Work out which provider (NPI) a Contributor Task is assigned to."""
        owner = task.get("owner") or {}
        npi = npi_override or npi_of(owner)
        resource = None
        if not npi and info_bundle:
            resource = resolve(owner, index_bundle(info_bundle))
            npi = npi_of(resource)
        if not npi and directory_lookup and owner.get("reference"):
            resource = directory_lookup(owner["reference"])
            npi = npi_of(resource)
        if not npi and owner.get("reference", "").split("/")[-1].startswith("npi-"):
            npi = owner["reference"].split("/")[-1][4:]
        if not npi:
            raise FhirInputError(
                "Cannot determine the contributor's NPI: put it on Task.owner.identifier, include the owner resource "
                "in the GFE Information Bundle, or pass ?npi=", code="required", expression="Task.owner", status=422,
            )
        kind = (resource or {}).get("resourceType") or owner.get("reference", "Organization/").split("/")[0]
        return ProviderHint(npi=npi, kind=kind.lower(), resource=resource)
