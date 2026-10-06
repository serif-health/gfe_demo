"""Estimate engine: request in, priced lines grouped by billing provider out.

Steps (each emits trace events so the terminal shows the reasoning):

1. Resolve payer + plan from Coverage (or treat as self-pay).
2. Resolve the anchor facility / provider (request hints -> contributor -> default).
3. Expand each requested code into billable components (facility, professional, supplies).
4. Assign every component to a provider (facility, or an affiliated practitioner whose
   taxonomy fits the component's role).
5. When acting as a GFE *Contributor*, keep only the components this provider bills.
6. Price each component: negotiated rate (plan-specific > payer-wide) -> % of gross charge
   -> self-pay discounted cash -> gross charge.
"""

from __future__ import annotations

from datetime import date

from ..domain import (
    Component, EstimateRequest, EstimateResult, Payer, Plan, PricedLine, Provider,
    ProviderEstimate, ProviderHint, RequestedItem, UnpricedComponent,
)
from ..fhir.parse import FhirInputError
from ..trace import tracer
from .repository import PricingRepository


class _MemoRepo:
    """Per-request memo for repeat reference lookups (keeps the trace free of duplicate queries)."""

    _MEMOIZED = {"get_provider", "role_taxonomies", "code_display", "service_components"}

    def __init__(self, repo: PricingRepository) -> None:
        self._repo = repo
        self._memo: dict[tuple, object] = {}

    def __getattr__(self, name: str):
        fn = getattr(self._repo, name)
        if name not in self._MEMOIZED:
            return fn

        def memoized(*args):
            key = (name, *args)
            if key not in self._memo:
                self._memo[key] = fn(*args)
            return self._memo[key]

        return memoized


class EstimateEngine:
    def __init__(self, repo: PricingRepository, default_provider_npi: str | None = None) -> None:
        self.repo = repo
        self.default_provider_npi = default_provider_npi

    # ------------------------------------------------------------------------------------
    def estimate(self, req: EstimateRequest) -> EstimateResult:
        if not isinstance(self.repo, _MemoRepo):
            return EstimateEngine(_MemoRepo(self.repo), self.default_provider_npi).estimate(req)  # type: ignore[arg-type]
        warnings: list[str] = []
        service_date = req.planned_start or date.today()

        payer, plan = self._resolve_payer_plan(req, warnings)
        facility, hinted = self._resolve_providers(req, warnings)

        estimates: dict[tuple[str, str], ProviderEstimate] = {}
        unpriced: list[UnpricedComponent] = []

        for item in req.items:
            components = self.repo.service_components(item.code_type, item.code)
            if components:
                tracer.step(
                    f"{item.code_type} {item.code} expands to {len(components)} billable component(s): "
                    + ", ".join(f"{cp.role}:{cp.code}{'-' + cp.modifier if cp.modifier else ''}" for cp in components)
                )
            else:
                role = "facility" if facility and facility.entity_type == "organization" else "rendering"
                components = [Component(role=role, code_type=item.code_type, code=item.code, modifier="",
                                        revenue_code=None, quantity=1, service_category="Requested service")]
                tracer.step(f"{item.code_type} {item.code} has no package definition; pricing it as a single {role} line")

            split_codes = {cp.code for cp in components if cp.modifier == "26"}
            for comp in components:
                provider = self._assign(comp, facility, hinted, req.contributor_npi)
                if provider is None:
                    reason = f"no provider on file can fill role '{comp.role}'"
                    warnings.append(f"{comp.code}: {reason}")
                    tracer.warn(f"{comp.code_type} {comp.code}: {reason}")
                    unpriced.append(UnpricedComponent(item, comp, None, reason))
                    continue
                if req.contributor_npi and provider.npi != req.contributor_npi:
                    tracer.step(
                        f"skip {comp.code}{'-' + comp.modifier if comp.modifier else ''} ({comp.role}) - billed by "
                        f"{provider.name}, not this contributor"
                    )
                    continue

                line = self._price(item, comp, provider, payer, plan, req.self_pay, service_date, warnings, split_codes)
                if line is None:
                    unpriced.append(UnpricedComponent(item, comp, provider, "no rate or standard charge on file"))
                    continue
                key = (provider.npi, provider.billing_class)
                if key not in estimates:
                    hint = hinted.get(provider.npi)
                    estimates[key] = ProviderEstimate(provider=provider, billing_class=provider.billing_class,
                                                      resource=hint.resource if hint else None)
                estimates[key].lines.append(line)

        result = EstimateResult(request=req, payer=payer, plan=plan, provider_estimates=list(estimates.values()),
                                unpriced=unpriced, warnings=warnings)
        tracer.emit("engine.result", result=result)
        return result

    # ------------------------------------------------------------------------------------
    def _resolve_payer_plan(self, req: EstimateRequest, warnings: list[str]) -> tuple[Payer | None, Plan | None]:
        if req.self_pay:
            tracer.step("No insurance coverage (self-pay) -> price from discounted cash / standard charges")
            return None, None
        org = req.payer_org or {}
        etins = [i.get("value") for i in org.get("identifier", []) or [] if i.get("value")]
        payer = self.repo.find_payer(etins, org.get("name"))
        if not payer:
            msg = f"payer '{org.get('name') or etins}' not found in pricing data; falling back to self-pay pricing"
            warnings.append(msg)
            tracer.warn(msg)
            return None, None
        class_values = [
            v for cls in (req.coverage or {}).get("class", []) or []
            for v in (cls.get("value"), cls.get("name")) if v
        ]
        plan = self.repo.find_plan(payer.payer_id, class_values)
        tracer.step(
            f"Coverage -> payer [bold]{payer.name}[/] ({payer.payer_id})"
            + (f", plan [bold]{plan.plan_name}[/] / network {plan.network_name}" if plan else ", plan not matched (payer-wide rates only)")
        )
        return payer, plan

    def _resolve_providers(self, req: EstimateRequest, warnings: list[str]) -> tuple[Provider | None, dict[str, ProviderHint]]:
        hinted: dict[str, ProviderHint] = {}
        known: list[Provider] = []
        for hint in req.provider_hints:
            prov = self.repo.get_provider(hint.npi)
            if prov:
                hinted[prov.npi] = hint
                known.append(prov)
            else:
                msg = f"provider NPI {hint.npi} from request is not in pricing data"
                warnings.append(msg)
                tracer.warn(msg)

        contributor = next((p for p in known if p.npi == req.contributor_npi), None)
        if req.contributor_npi and contributor is None:
            raise FhirInputError(f"Contributor NPI {req.contributor_npi} is not a provider in this pricing database",
                                 code="not-found", status=422)

        facility: Provider | None = None
        if contributor and contributor.entity_type == "organization":
            facility = contributor
        if facility is None:
            facility = next((p for p in known if p.entity_type == "organization"), None)
        if facility is None:
            practitioner = contributor or next((p for p in known if p.entity_type == "practitioner"), None)
            if practitioner:
                facility = next(iter(self.repo.affiliated_facilities(practitioner.npi)), None)
        if facility is None and self.default_provider_npi:
            facility = self.repo.get_provider(self.default_provider_npi)
            if facility:
                tracer.step(f"No provider named in request -> using service default {facility.name} (NPI {facility.npi})")
        if facility is None and not known:
            raise FhirInputError("Request does not identify a provider (NPI) and no default provider is configured",
                                 code="required", status=422)
        if facility:
            tracer.step(f"Anchor facility: [bold]{facility.name}[/] NPI {facility.npi} ({facility.billing_class} billing)")
        return facility, hinted

    def _assign(self, comp: Component, facility: Provider | None, hinted: dict[str, ProviderHint],
                contributor_npi: str | None) -> Provider | None:
        if comp.role == "facility":
            return facility
        if comp.role == "rendering":
            npi = contributor_npi or next(iter(hinted), None)
            return self.repo.get_provider(npi) if npi else facility
        taxonomies = self.repo.role_taxonomies(comp.role)
        # Prefer the contributor, then providers named in the request, then facility affiliations.
        candidates = [contributor_npi] if contributor_npi else []
        candidates += [n for n in hinted if n != contributor_npi]
        for npi in candidates:
            prov = self.repo.get_provider(npi)
            if prov and prov.entity_type == "practitioner" and prov.taxonomy_code in taxonomies:
                tracer.step(f"{comp.role} for {comp.code}: {prov.name} (named in request)")
                return prov
        if facility:
            affiliated = self.repo.affiliated_practitioners(facility.npi, taxonomies)
            if affiliated:
                tracer.step(f"{comp.role} for {comp.code}: {affiliated[0].name} (affiliated with {facility.name})")
                return affiliated[0]
        return None

    def _price(self, item: RequestedItem, comp: Component, provider: Provider, payer: Payer | None,
               plan: Plan | None, self_pay: bool, service_date: date, warnings: list[str],
               split_codes: set[str]) -> PricedLine | None:
        modifier = comp.modifier
        # A freestanding facility bills the technical component (-TC) on a professional claim
        # when the package splits out a separate professional read (-26) for the same code.
        if comp.role == "facility" and provider.billing_class == "professional" and not modifier \
                and comp.code in split_codes:
            modifier = "TC"
        qty = comp.quantity * (item.quantity if comp.code == item.code else 1)
        label = f"{comp.code_type} {comp.code}{'-' + modifier if modifier else ''}"

        unit_price: float | None = None
        methodology = rate_source = ""

        if payer and not self_pay:
            rate = self.repo.find_rate(
                npi=provider.npi, payer_id=payer.payer_id, plan_id=plan.plan_id if plan else None,
                billing_class=provider.billing_class, code_type=comp.code_type, code=comp.code,
                modifier=modifier, service_date=service_date,
            )
            if rate:
                scope = f"plan {plan.plan_name}" if rate.plan_id and plan else f"{payer.name} (all plans)"
                if not rate.in_effect:
                    msg = f"{label}: rate effective {rate.effective_date}..{rate.expiration_date} does not cover {service_date}; using it anyway"
                    warnings.append(msg)
                    tracer.warn(msg)
                if rate.negotiated_type == "percentage":
                    charge = self.repo.find_standard_charge(provider.npi, comp.code_type, comp.code, modifier)
                    if charge and charge.gross_charge:
                        unit_price = round(charge.gross_charge * rate.negotiated_rate / 100, 2)
                        methodology = (f"In-network {rate.negotiated_rate:g}% of gross charge "
                                       f"${charge.gross_charge:,.2f} - {scope}")
                        rate_source = f"{rate.source}; {charge.source}"
                    else:
                        tracer.warn(f"{label}: percentage rate but no gross charge on file")
                else:
                    unit_price = rate.negotiated_rate
                    methodology = f"In-network {rate.negotiated_type} rate - {scope}"
                    rate_source = rate.source
            else:
                tracer.warn(f"{label}: no in-network rate for {payer.name} at {provider.name}")

        if unit_price is None:
            charge = self.repo.find_standard_charge(provider.npi, comp.code_type, comp.code, modifier)
            if charge and self_pay and charge.discounted_cash is not None:
                unit_price, methodology = charge.discounted_cash, "Self-pay discounted cash price"
                rate_source = charge.source
            elif charge and charge.gross_charge is not None:
                unit_price = charge.gross_charge
                methodology = "Gross charge (no in-network rate on file)" if not self_pay else "Gross charge (no cash price on file)"
                rate_source = charge.source
                if not self_pay:
                    warnings.append(f"{label} at {provider.name}: priced at gross charge (out-of-network / no rate)")

        if unit_price is None:
            msg = f"{label} at {provider.name}: no rate or standard charge on file"
            warnings.append(msg)
            tracer.warn(msg)
            return None

        line = PricedLine(
            item=item, component=comp, code_display=self.repo.code_display(comp.code_type, comp.code),
            revenue_display=self.repo.code_display("RC", comp.revenue_code) if comp.revenue_code else None,
            modifier=modifier, quantity=qty, unit_price=unit_price, methodology=methodology, rate_source=rate_source,
        )
        tracer.emit("engine.priced", line=line, provider=provider)
        return line
