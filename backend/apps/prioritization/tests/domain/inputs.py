"""One honest ``SignalInput`` builder for the database-free tests.

Defaults mirror what the seed actually contains: no target date, no next step, no activity and no
tasks. A builder that defaulted ``target_date`` to next week would hide ``NO_TARGET_DATE`` and the
suite would then prove nothing about the real portfolio.
"""

from datetime import UTC, datetime
from typing import Any

from apps.prioritization.domain.types import ProjectRiskInput, SignalInput

#: Fixed instant every test scores against, so no assertion depends on the day it runs.
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


# ``Any`` is the honest type here: the builder passes overrides straight through to the Pydantic
# model, which validates them. Narrowing it would restate every SignalInput field in the test helper.
def signal_input(**overrides: Any) -> SignalInput:
    """Build a ``SignalInput`` at :data:`NOW`, overriding only what the test is about."""
    return SignalInput(project_code="PRJ-01", now=NOW, **overrides)


# Same passthrough as ``signal_input``: ProjectRiskInput validates the overrides at construction.
def risk_input(**overrides: Any) -> ProjectRiskInput:
    """Build a ``ProjectRiskInput`` at :data:`NOW`, overriding only what the test is about."""
    return ProjectRiskInput(project_code="PRJ-01", now=NOW, **overrides)
