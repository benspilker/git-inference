from __future__ import annotations

import json
from dataclasses import dataclass

from app.intents import IntentEnvelope, PlannerEnvelope, IntentType
from app.task_registry import validate_required_fields


@dataclass
class ExecutionResult:
    execution_status: str
    task_type: str
    verified: bool
    details: dict


def parse_json_payload(text: str) -> dict:
    return json.loads(text)


def orchestrate_request(
    user_text: str,
    router_call,
    planner_call,
    answerer_call,
    final_phraser_call,
    execute_local_task,
) -> str:
    # Turn 1: router
    router_raw = router_call(user_text)
    router = IntentEnvelope.model_validate(parse_json_payload(router_raw))

    if router.intent_type == IntentType.QUESTION:
        return answerer_call(user_text)

    if router.intent_type == IntentType.RESEARCH:
        # placeholder: route into a dedicated research pipeline
        return answerer_call(
            "This request should be routed to the research pipeline, which is not implemented in this scaffold yet."
        )

    if router.intent_type == IntentType.NEEDS_CLARIFICATION:
        return "I need a bit more information before I can help with that."

    # Turn 2: planner
    planner_raw = planner_call(user_text)
    plan = PlannerEnvelope.model_validate(parse_json_payload(planner_raw))

    if plan.intent_type == IntentType.NEEDS_CLARIFICATION:
        return plan.question or "I need a bit more information before I can do that."

    missing_fields = validate_required_fields(plan.task_type, plan.parameters)
    if missing_fields:
        fields = ", ".join(missing_fields)
        return f"I need a bit more information before I can do that. Missing: {fields}."

    # Local execution + verification
    result = execute_local_task(plan.task_type, plan.parameters)

    if not isinstance(result, ExecutionResult):
        raise TypeError("execute_local_task must return ExecutionResult")

    # Optional final phrasing
    final_input = {
        "execution_status": result.execution_status,
        "task_type": result.task_type,
        "verified": result.verified,
        "details": result.details,
    }
    return final_phraser_call(json.dumps(final_input))
