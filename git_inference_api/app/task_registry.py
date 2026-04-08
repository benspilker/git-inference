from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Any


@dataclass(frozen=True)
class TaskDefinition:
    task_type: str
    required_fields: tuple[str, ...]
    executor_name: str
    description: str


SUPPORTED_TASKS: dict[str, TaskDefinition] = {
    "system_command": TaskDefinition(
        task_type="system_command",
        required_fields=(),
        executor_name="system_noop",
        description="Meta/system command task. No external side effects; treated as no-op execution.",
    ),
    "system_message": TaskDefinition(
        task_type="system_message",
        required_fields=(),
        executor_name="system_noop",
        description="Meta/system message task. No external side effects; treated as no-op execution.",
    ),
    "system_response": TaskDefinition(
        task_type="system_response",
        required_fields=("response_text",),
        executor_name="system_response",
        description="No-op response task used when planner determines no external action is required.",
    ),
    "scheduled_weather_report": TaskDefinition(
        task_type="scheduled_weather_report",
        required_fields=("frequency", "time", "location"),
        executor_name="create_scheduled_weather_report",
        description="Create a recurring weather report job.",
    ),
    "reminder": TaskDefinition(
        task_type="reminder",
        required_fields=("time", "message"),
        executor_name="create_reminder",
        description="Create a reminder.",
    ),
    "file_write": TaskDefinition(
        task_type="file_write",
        required_fields=("path", "content"),
        executor_name="write_file",
        description="Write or overwrite a file.",
    ),
}


def get_task(task_type: str) -> TaskDefinition | None:
    return SUPPORTED_TASKS.get(task_type)


def validate_required_fields(task_type: str, parameters: dict[str, Any]) -> list[str]:
    task = get_task(task_type)
    if task is None:
        return ["task_type"]
    missing: list[str] = []
    for field in task.required_fields:
        value = parameters.get(field)
        if value is None or value == "":
            missing.append(field)
    return missing
