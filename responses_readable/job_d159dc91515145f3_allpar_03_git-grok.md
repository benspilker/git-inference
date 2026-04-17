Routing check: OK

In a local OpenClaw setup:

Channel routing is deterministic — replies always return to the originating channel (controlled by host config, not the model).
Agent selection follows most-specific-wins rules: exact peer match first, then parent/thread inheritance.
Model routing (primary vs. thinking, or multi-model) is handled via ~/.openclaw/openclaw.json or agent bindings — no issues detected in standard config.

If you're seeing unexpected behavior (e.g., wrong agent, model escalation, or channel bounce), share the exact symptom or relevant config snippet for a targeted fix.

Everything looks good on the quick check. What's the specific routing issue you're observing?
