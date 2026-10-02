"""Domain exception hierarchy."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for rule/state violations detected by the domain layer."""


class RuleViolation(DomainError):
    """An action or state breaks the active ruleset (squad, budget, chips, lineup…)."""

    def __init__(self, code: str, message: str, **context: object) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.context = context


class LeakageError(DomainError):
    """Information with availability after the decision cutoff was accessed (ADR-0004)."""


class StaleDataError(DomainError):
    """Required data is older than its freshness SLA and degraded mode was not allowed."""
