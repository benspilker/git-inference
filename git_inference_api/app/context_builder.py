from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ContextPacket:
    system_prompt: str
    relevant_memory: list[str] = field(default_factory=list)
    recent_context: list[str] = field(default_factory=list)
    user_input: str = ""

    def render(self) -> str:
        parts: list[str] = [self.system_prompt.strip()]
        if self.relevant_memory:
            parts.append("Relevant memory:")
            parts.extend(f"- {item}" for item in self.relevant_memory)
        if self.recent_context:
            parts.append("Recent context:")
            parts.extend(f"- {item}" for item in self.recent_context[-2:])
        parts.append("Current request:")
        parts.append(self.user_input.strip())
        return "\n".join(parts).strip() + "\n"


def build_minimal_question_context(
    system_prompt: str,
    user_input: str,
    relevant_memory: list[str] | None = None,
    recent_context: list[str] | None = None,
) -> ContextPacket:
    return ContextPacket(
        system_prompt=system_prompt,
        relevant_memory=relevant_memory or [],
        recent_context=recent_context or [],
        user_input=user_input,
    )
