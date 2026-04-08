from __future__ import annotations

from enum import Enum


class JobState(str, Enum):
    RECEIVED = "received"
    ROUTING = "routing"
    ROUTED = "routed"
    PLANNING = "planning"
    PLANNED = "planned"
    NEEDS_CLARIFICATION = "needs_clarification"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
