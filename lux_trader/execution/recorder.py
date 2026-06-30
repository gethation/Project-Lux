from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from .intent import (
    ExecutionPlanStatus,
    PairExecutionPlan,
    validate_pair_execution_plan,
)

if TYPE_CHECKING:
    # Import only for type checking to break the runtime import cycle
    # store -> persistence -> execution.intent -> execution.__init__ ->
    # outcome -> recorder -> store. `from __future__ import annotations`
    # keeps the SQLiteStore annotation below valid as a string.
    from ..store import SQLiteStore


class DryRunExecutionRecorder:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        allow_live_order: bool = False,
    ) -> None:
        self.store = store
        self.allow_live_order = bool(allow_live_order)

    def record_plan(self, plan: PairExecutionPlan) -> PairExecutionPlan:
        validated = validate_pair_execution_plan(
            plan,
            allow_live_order=self.allow_live_order,
        )
        recorded = (
            replace(validated, status=ExecutionPlanStatus.RECORDED)
            if validated.status == ExecutionPlanStatus.VALIDATED
            else validated
        )
        self.store.record_execution_plan(recorded)
        return recorded
