"""Squad version-pin + deprecation registry.

Iolaus the cauterizer reads from here on every `pre_dispatch`. A squad that
declares `deprecated_after: <ISO date>` in its `squad.yaml` is dispatchable
*before* that date and refused *on or after* it, unless the caller passes
`allow_archived=True` (reserved for replay / forensic flows).

The semver shape is intentionally loose — we want monotonic comparability
but not full PEP-440 ceremony. `parse_version("1.0.0") < parse_version("1.0.1")`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional


class SquadDeprecated(RuntimeError):
    """Raised when a deprecated squad is invoked without `allow_archived=True`."""

    def __init__(self, slug: str, version: str, deprecated_after: date, *, reason: str = ""):
        self.slug = slug
        self.version = version
        self.deprecated_after = deprecated_after
        self.reason = reason
        super().__init__(
            f"Squad {slug!r} (v{version}) was deprecated after {deprecated_after.isoformat()}"
            + (f": {reason}" if reason else "")
        )


class DoubleSpawnRefused(RuntimeError):
    """Raised when the same squad is dispatched twice for the same envelope in
    the same workflow. The Hydra's first failure mode (cut one, two grow) made
    explicit and refused at the gate.
    """

    def __init__(self, slug: str, envelope_id: str, workflow_id: str):
        self.slug = slug
        self.envelope_id = envelope_id
        self.workflow_id = workflow_id
        super().__init__(
            f"Squad {slug!r} already dispatched for envelope {envelope_id} "
            f"in workflow {workflow_id}. Refusing duplicate spawn."
        )


@dataclass(frozen=True, order=True)
class Version:
    """Loose semver. `Version.parse('1.2.3')` is the common path."""
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, s: str) -> "Version":
        parts = (s or "1.0.0").strip().split(".")
        while len(parts) < 3:
            parts.append("0")
        try:
            return cls(
                major=int(parts[0]),
                minor=int(parts[1]),
                patch=int(parts[2].split("-")[0].split("+")[0]),
            )
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid version {s!r}: {e}") from e

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def parse_deprecated_after(value: str | date | datetime | None) -> Optional[date]:
    """Accept ISO date string, date, or datetime; return a `date` or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def is_deprecated(
    deprecated_after: Optional[date],
    *,
    now: Optional[date] = None,
) -> bool:
    if deprecated_after is None:
        return False
    today = now or datetime.now(timezone.utc).date()
    return today >= deprecated_after
