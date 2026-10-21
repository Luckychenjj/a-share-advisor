from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from xiao_a.models import DataStatus, Quote


@dataclass
class ProviderResult:
    quote: Quote | None
    status: DataStatus
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class QuoteProvider(Protocol):
    name: str
    priority: int

    def fetch_quote(self, symbol: str) -> ProviderResult: ...


class QuoteRouter:
    """Explicit, observable provider fallback.

    A stale result is retained as a last resort while later providers are
    tried. Provider errors are never silently erased.
    """

    def __init__(self, providers: list[QuoteProvider]):
        self.providers = sorted(providers, key=lambda item: item.priority)

    def fetch_quote(self, symbol: str) -> ProviderResult:
        errors: list[str] = []
        stale_candidate: ProviderResult | None = None
        for provider in self.providers:
            try:
                result = provider.fetch_quote(symbol)
            except Exception as exc:  # one source must not hide later sources
                errors.append(f"{provider.name}: {type(exc).__name__}: {exc}")
                continue
            result.errors = [*errors, *result.errors]
            if result.quote is not None and result.status == DataStatus.FRESH:
                return result
            if result.quote is not None and result.status in {DataStatus.STALE, DataStatus.PARTIAL}:
                stale_candidate = result
            errors = result.errors

        if stale_candidate is not None:
            stale_candidate.errors = errors
            stale_candidate.warnings.append("all fresh providers failed; using degraded quote")
            return stale_candidate
        return ProviderResult(
            quote=None,
            status=DataStatus.UNAVAILABLE,
            warnings=["no provider returned a usable quote"],
            errors=errors,
        )

