"""Estimate engine: one billing provider's price for each requested service.

Steps (each emits trace events so the terminal shows the reasoning):

1. Resolve payer + plan from Coverage (or treat as self-pay).
2. Pick the billing provider for each requested item: the Contributor Task owner, else the
   item's performer, else a provider named in the request, else the configured default.
3. Take the lines that provider bills for the service (``service_components`` rows whose role
   it fills). Lines other providers bill separately, such as a radiologist's read, are skipped:
   each provider answers only for its own piece of the GFE.
4. Price each line with one ``PriceSource.find_price`` call. A FHIR-native ``Claim.item`` is used
   as-is; otherwise: negotiated rate (plan-specific > payer-wide) -> % of gross charge ->
   self-pay discounted cash -> gross charge.
"""

from __future__ import annotations

from datetime import date

from ..domain import (
    Component, EstimateRequest, EstimateResult, Payer, Plan, PricedLine, Provider,
    ProviderEstimate, RequestedItem, UnpricedComponent,
)
from ..fhir.parse import FhirInputError, claim_item_terms
from ..trace import tracer
from .repository import PriceQuote, PricingRepository, line_label


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
        known = self._known_providers(req, warnings)
        hints = {h.npi: h for h in req.provider_hints}

        estimates: dict[tuple[str, str], ProviderEstimate] = {}
        unpriced: list[UnpricedComponent] = []
        for item in req.items:
            provider = self._billing_provider(item, req, known)
            components = self._components(item, provider)
            split_codes = {cp.code for cp in components if cp.modifier == "26"}
            for comp in components:
                if not self._bills(provider, comp):
                    tracer.step(f"skip {line_label(comp.code_type, comp.code, comp.modifier)} ({comp.role}) - "
                                f"billed separately by the {comp.role}, not part of {provider.name}'s GFE")
                    continue
                line = self._price(item, comp, provider, payer, plan, req.self_pay, service_date, warnings, split_codes)
                if line is None:
                    unpriced.append(UnpricedComponent(item, comp, provider, "no rate or standard charge on file"))
                    continue
                key = (provider.npi, provider.billing_class)
                if key not in estimates:
                    hint = hints.get(provider.npi)
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

    def _known_providers(self, req: EstimateRequest, warnings: list[str]) -> dict[str, Provider]:
        """Providers named in the request that exist in our reference data, plus the contributor / default."""
        known: dict[str, Provider] = {}
        for hint in req.provider_hints:
            prov = self.repo.get_provider(hint.npi)
            if prov:
                known[prov.npi] = prov
            elif hint.npi != req.contributor_npi:
                msg = f"provider NPI {hint.npi} from request is not in pricing data"
                warnings.append(msg)
                tracer.warn(msg)
        if req.contributor_npi:
            if req.contributor_npi not in known:
                raise FhirInputError(f"Contributor NPI {req.contributor_npi} is not a provider in this pricing database",
                                     code="not-found", status=422)
            tracer.step(f"Contributor Task owner -> pricing only lines billed by [bold]{known[req.contributor_npi].name}[/]")
        elif not known and self.default_provider_npi:
            prov = self.repo.get_provider(self.default_provider_npi)
            if prov:
                known[prov.npi] = prov
                tracer.step(f"No provider named in request -> using service default {prov.name} (NPI {prov.npi})")
        if not known:
            raise FhirInputError("Request does not identify a provider (NPI) and no default provider is configured",
                                 code="required", status=422)
        return known

    def _billing_provider(self, item: RequestedItem, req: EstimateRequest, known: dict[str, Provider]) -> Provider:
        if req.contributor_npi:
            return known[req.contributor_npi]
        if item.performer_npi in known:
            provider, why = known[item.performer_npi], "performer"
        else:
            provider, why = next(iter(known.values())), "provider named in request"
        tracer.step(f"{item.code_type} {item.code} -> billing provider [bold]{provider.name}[/] NPI {provider.npi} "
                    f"({why}, {provider.billing_class} billing)")
        return provider

    def _components(self, item: RequestedItem, provider: Provider) -> list[Component]:
        components = self.repo.service_components(item.code_type, item.code)
        if components:
            return components
        role = "facility" if provider.entity_type == "organization" else "rendering"
        tracer.step(f"{item.code_type} {item.code} has no line definition; pricing it as a single {role} line")
        return [Component(role=role, code_type=item.code_type, code=item.code, modifier="",
                          revenue_code=None, quantity=1, service_category="Requested service")]

    def _bills(self, provider: Provider, comp: Component) -> bool:
        if comp.role == "rendering":
            return True
        if comp.role == "facility":
            return provider.entity_type == "organization"
        return provider.entity_type == "practitioner" and provider.taxonomy_code in self.repo.role_taxonomies(comp.role)

    def _price(self, item: RequestedItem, comp: Component, provider: Provider, payer: Payer | None,
               plan: Plan | None, self_pay: bool, service_date: date, warnings: list[str],
               split_codes: set[str]) -> PricedLine | None:
        modifier = comp.modifier
        # A freestanding facility bills the technical component (-TC) on a professional claim
        # when the professional read (-26) of the same code is billed separately.
        if comp.role == "facility" and provider.billing_class == "professional" and not modifier \
                and comp.code in split_codes:
            modifier = "TC"
        qty = comp.quantity * (item.quantity if comp.code == item.code else 1)
        label = line_label(comp.code_type, comp.code, modifier)

        quote = self.repo.find_price(provider=provider, payer=payer, plan=plan, code_type=comp.code_type,
                                     code=comp.code, modifier=modifier, quantity=qty, service_date=service_date)
        if quote.claim_item:
            return self._native_line(item, comp, modifier, qty, quote, provider)

        unit_price: float | None = None
        methodology = rate_source = ""
        rate, charge = quote.rate, quote.charge

        if payer and not self_pay:
            if rate:
                scope = f"plan {plan.plan_name}" if rate.plan_specific and plan else f"{payer.name} (all plans)"
                if not rate.in_effect:
                    msg = f"{label}: rate effective {rate.effective_date}..{rate.expiration_date} does not cover {service_date}; using it anyway"
                    warnings.append(msg)
                    tracer.warn(msg)
                if rate.negotiated_type == "percentage":
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

        if unit_price is None and charge:
            if self_pay and charge.discounted_cash is not None:
                unit_price, methodology = charge.discounted_cash, "Self-pay discounted cash price"
                rate_source = charge.source
            elif charge.gross_charge is not None:
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

    def _native_line(self, item: RequestedItem, comp: Component, modifier: str, qty: float,
                     quote: PriceQuote, provider: Provider) -> PricedLine:
        unit_price, quantity, methodology = claim_item_terms(quote.claim_item or {}, qty)
        line = PricedLine(
            item=item, component=comp, code_display=None, revenue_display=None, modifier=modifier,
            quantity=quantity, unit_price=unit_price,
            methodology=methodology or "Priced by FHIR-native price source", rate_source="",
            claim_item=quote.claim_item,
        )
        tracer.step(f"{line_label(comp.code_type, comp.code, modifier)}: price source returned a GFE Claim.item; used as-is")
        tracer.emit("engine.priced", line=line, provider=provider)
        return line
