from __future__ import annotations

import unittest

from git_inference_api.app.config import settings
from git_inference_api.app.worker import JobWorker


class WorkerRoutingTests(unittest.TestCase):
    def test_coerce_routing_metadata_applies_defaults(self) -> None:
        worker = JobWorker()
        payload = worker._coerce_routing_metadata(
            {"model": "git-chatgpt", "user_prompt": "hello"},
            default_intent_type="question",
            default_task_type="general_question",
        )
        routing = payload.get("routing_metadata")
        self.assertIsInstance(routing, dict)
        self.assertEqual(routing.get("intent_type"), "question")
        self.assertEqual(routing.get("task_type"), "general_question")
        self.assertEqual(routing.get("route_state"), "received")
        self.assertEqual(routing.get("schema_version"), "1.0")
        self.assertIs(routing.get("requires_local_execution"), False)

    def test_build_allsequential_child_payload_inherits_parent_routing(self) -> None:
        worker = JobWorker()
        parent_payload = {
            "model": "git-allsequential",
            "user_prompt": "research this topic",
            "routing_metadata": {
                "schema_version": "1.0",
                "intent_type": "research",
                "task_type": "root_topic_research",
                "route_state": "routed",
                "requires_local_execution": False,
            },
        }

        child = worker._build_allsequential_child_payload(
            parent_payload,
            target_model="git-chatgpt",
            parent_job_id="job123",
            index=2,
            total=5,
        )
        routing = child.get("routing_metadata")
        self.assertIsInstance(routing, dict)
        self.assertEqual(routing.get("intent_type"), "research")
        self.assertEqual(routing.get("task_type"), "root_topic_research")
        self.assertEqual(routing.get("route_state"), "received")
        self.assertIs(routing.get("requires_local_execution"), False)
        self.assertEqual(child.get("model"), "git-chatgpt")
        self.assertEqual(child.get("allsequential_parent_job_id"), "job123")
        self.assertEqual(child.get("allsequential_index"), 2)
        self.assertEqual(child.get("allsequential_total"), 5)

    def test_build_allparallel_child_payload_inherits_parent_routing(self) -> None:
        worker = JobWorker()
        parent_payload = {
            "model": "git-parallel",
            "user_prompt": "research this topic",
            "routing_metadata": {
                "schema_version": "1.0",
                "intent_type": "question",
                "task_type": "information",
                "route_state": "routed",
                "requires_local_execution": False,
            },
        }

        child = worker._build_allparallel_child_payload(
            parent_payload,
            target_model="git-grok",
            parent_job_id="job456",
            index=3,
            total=5,
        )
        routing = child.get("routing_metadata")
        self.assertIsInstance(routing, dict)
        self.assertEqual(routing.get("intent_type"), "question")
        self.assertEqual(routing.get("task_type"), "information")
        self.assertEqual(routing.get("route_state"), "received")
        self.assertIs(routing.get("requires_local_execution"), False)
        self.assertEqual(child.get("model"), "git-grok")
        self.assertEqual(child.get("allparallel_parent_job_id"), "job456")
        self.assertEqual(child.get("allparallel_index"), 3)
        self.assertEqual(child.get("allparallel_total"), 5)

    def test_resolve_branch_for_model_uses_model_tail(self) -> None:
        worker = JobWorker()
        self.assertEqual(worker._resolve_branch_for_model("git-chatgpt"), "git-chatgpt")
        self.assertEqual(worker._resolve_branch_for_model("providers/custom/git-grok"), "git-grok")

    def test_resolve_branch_for_aggregate_models_falls_back_to_default(self) -> None:
        worker = JobWorker()
        self.assertEqual(worker._resolve_branch_for_model("git-allsequential"), settings.branch)
        self.assertEqual(worker._resolve_branch_for_model("git-parallel"), settings.branch)
        self.assertEqual(worker._resolve_branch_for_model("git-synth"), settings.branch)

    def test_is_synthesis_model_detects_model_tail(self) -> None:
        worker = JobWorker()
        self.assertTrue(worker._is_synthesis_model("git-synth"))
        self.assertTrue(worker._is_synthesis_model("providers/custom/git-synthesis"))
        self.assertFalse(worker._is_synthesis_model("git-chatgpt"))

    def test_is_inceptionlabs_model_detects_model_tail(self) -> None:
        worker = JobWorker()
        self.assertTrue(worker._is_inceptionlabs_model("git-inceptionlabs"))
        self.assertTrue(worker._is_inceptionlabs_model("providers/x/git-inceptionlabs"))
        self.assertFalse(worker._is_inceptionlabs_model("git-chatgpt"))

    def test_extract_weather_location_from_prompt(self) -> None:
        worker = JobWorker()
        prompt = "What is the weather today in Indianapolis, Indiana?"
        self.assertEqual(worker._extract_weather_location(prompt), "Indianapolis, Indiana")

    def test_iter_geocode_candidates_includes_city_fallback(self) -> None:
        worker = JobWorker()
        candidates = worker._iter_geocode_candidates("Indianapolis, IN, USA")
        self.assertIn("Indianapolis", candidates)

    def test_inception_weather_grounding_keeps_non_weather_content(self) -> None:
        worker = JobWorker()
        content = "Normal model response."
        grounded = worker._maybe_ground_inception_weather_response(
            target_model="git-inceptionlabs",
            request_payload={"user_prompt": "Tell me about BemenderferLLC"},
            content=content,
        )
        self.assertEqual(grounded, content)

    def test_inception_weather_grounding_uses_live_summary_when_available(self) -> None:
        worker = JobWorker()

        def fake_fetch(_location: str) -> str:
            return "Weather for Indianapolis, Indiana, United States: Current 22.0C (71.6F), Clear sky."

        worker._fetch_openmeteo_weather_summary = fake_fetch  # type: ignore[method-assign]
        grounded = worker._maybe_ground_inception_weather_response(
            target_model="git-inceptionlabs",
            request_payload={"user_prompt": "What is the weather today in Indianapolis, Indiana?"},
            content="stale output",
        )
        self.assertIn("Weather for Indianapolis", grounded)

    def test_build_synthesis_aggregate_prefers_qwen_and_grok_weights(self) -> None:
        worker = JobWorker()
        source_entries = [
            {"source": "git-grok", "status": "completed", "content": "Current 80F. Wind 20 mph. Thunderstorm risk 80%."},
            {"source": "git-qwen", "status": "completed", "content": "Current 78F. Wind 18 mph. Rain risk 70%."},
            {"source": "git-chatgpt", "status": "completed", "content": "Current 60F. Wind 5 mph. Low rain risk."},
        ]
        aggregate = worker._build_synthesis_aggregate(source_entries, prefer_best_sources=True)
        avg_temp = aggregate.get("temperature", {}).get("weighted_avg_f")
        self.assertIsInstance(avg_temp, float)
        self.assertGreater(float(avg_temp), 73.0)
        self.assertEqual(aggregate.get("rain", {}).get("consensus_risk"), "high")
        self.assertIs(aggregate.get("prefer_best_sources"), True)

    def test_build_synthesis_aggregate_general_mode_uses_equal_weights(self) -> None:
        worker = JobWorker()
        source_entries = [
            {"source": "git-grok", "status": "completed", "content": "Current 80F."},
            {"source": "git-qwen", "status": "completed", "content": "Current 78F."},
            {"source": "git-chatgpt", "status": "completed", "content": "Current 60F."},
        ]
        aggregate = worker._build_synthesis_aggregate(source_entries, prefer_best_sources=False)
        avg_temp = aggregate.get("temperature", {}).get("weighted_avg_f")
        self.assertEqual(avg_temp, 72.7)
        self.assertIs(aggregate.get("prefer_best_sources"), False)

    def test_extract_current_temperature_handles_fahrenheit_ranges(self) -> None:
        value = JobWorker._extract_current_temperature_f("Today high 72-78°F with storms later.")
        self.assertIsNotNone(value)
        self.assertGreater(float(value), 70.0)

    def test_chunk_synthesis_entries_splits_large_inputs(self) -> None:
        worker = JobWorker()
        source_entries = [
            {"source": "git-chatgpt", "status": "completed", "content": "alpha " * 400},
            {"source": "git-grok", "status": "completed", "content": "beta " * 400},
            {"source": "git-qwen", "status": "completed", "content": "gamma " * 400},
        ]
        chunks = worker._chunk_synthesis_entries(source_entries, max_words_per_chunk=500)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(sum(len(chunk) for chunk in chunks), len(source_entries))

    def test_build_synthesis_prompt_uses_question_only_tell_me_prefix(self) -> None:
        worker = JobWorker()
        system_prompt, user_prompt = worker._build_synthesis_prompt(
            base_prompt="Summarize this.",
            source_job_ids=["job_abc"],
            synthesis_mode="general",
            aggregate={"completed_sources": 1},
            source_entries=[{"source": "git-chatgpt", "source_job_id": "job_abc", "status": "completed", "content": "text"}],
        )
        self.assertIn("question-answer synthesis task", system_prompt)
        self.assertTrue(user_prompt.startswith("Tell me one final response"))
        self.assertIn("Do not schedule tasks", user_prompt)

    def test_detect_unusable_synthesis_content_flags_debug_payload_leak(self) -> None:
        issue = JobWorker._detect_unusable_synthesis_content(
            'LIVE_WEB_UNAVAILABLE\\nRelevant memory:\\n{"source": "git-qwen"}'
        )
        self.assertIsNotNone(issue)

    def test_detect_unusable_synthesis_content_accepts_normal_answer(self) -> None:
        issue = JobWorker._detect_unusable_synthesis_content(
            "Preheat oven to 475F, stretch dough, add sauce and cheese, and bake 12 minutes."
        )
        self.assertIsNone(issue)

    def test_extract_synthesis_source_job_ids_supports_lists_and_prompt_mentions(self) -> None:
        worker = JobWorker()
        payload = {
            "options": {
                "source_job_ids": ["job_alpha", "job_beta"],
                "synth_source_job_id": "job_gamma",
            }
        }
        prompt = "please combine job_delta and job_alpha"
        source_ids = worker._extract_synthesis_source_job_ids(payload, prompt)
        self.assertEqual(source_ids, ["job_alpha", "job_beta", "job_gamma", "job_delta"])

    def test_local_synthesis_fallback_renders_core_metrics(self) -> None:
        text = JobWorker._build_local_synthesis_fallback(
            instruction="Summarize this weather fanout.",
            aggregate={
                "temperature": {"weighted_avg_f": 70.0, "weighted_avg_c": 21.1, "min_f": 60.0, "max_f": 80.0},
                "wind": {"weighted_avg_mph": 12.0, "min_mph": 5.0, "max_mph": 20.0},
                "rain": {"consensus_risk": "moderate", "max_precip_chance_pct": 65.0},
                "conditions": {"consensus": "cloudy"},
                "completed_sources": 5,
                "failed_sources": [],
            },
            child_error="timeout",
            synthesis_mode="weather",
            source_entries=[],
        )
        self.assertIn("Weighted average temperature", text)
        self.assertIn("Rain risk consensus: moderate", text)
        self.assertIn("fallback synthesis used", text)

    def test_local_synthesis_fallback_general_includes_source_excerpt(self) -> None:
        text = JobWorker._build_local_synthesis_fallback(
            instruction="Tell me how to make pizza.",
            aggregate={"completed_sources": 2, "failed_sources": []},
            child_error="timeout",
            synthesis_mode="general",
            source_entries=[
                {"source": "git-chatgpt", "status": "completed", "content": "Use high-hydration dough and preheat the oven."},
                {"source": "git-qwen", "status": "completed", "content": "Rest dough, shape gently, and bake on a hot stone."},
            ],
        )
        self.assertIn("Cross-source synthesis (deterministic fallback)", text)
        self.assertIn("git-chatgpt:", text)

    def test_build_synthesis_entries_from_fanout_results_orders_and_keeps_errors(self) -> None:
        entries = JobWorker._build_synthesis_entries_from_fanout_results(
            source_job_id="job_parent",
            results=[
                {"index": 2, "model": "git-chatgpt", "status": "failed", "error": "timeout"},
                {"index": 1, "model": "git-grok", "status": "completed", "content": "answer"},
            ],
        )
        self.assertEqual([entry.get("source") for entry in entries], ["git-grok", "git-chatgpt"])
        self.assertEqual(entries[0].get("content"), "answer")
        self.assertEqual(entries[1].get("content"), "Error: timeout")
        self.assertEqual(entries[1].get("source_job_id"), "job_parent")

    def test_build_synthesis_entries_from_fanout_results_marks_policy_echo_as_failed(self) -> None:
        entries = JobWorker._build_synthesis_entries_from_fanout_results(
            source_job_id="job_parent",
            results=[
                {
                    "index": 1,
                    "model": "git-grok",
                    "status": "completed",
                    "content": "You are Juniper. Execution constraints: ... If and only if live web lookup is truly unavailable, reply exactly: LIVE_WEB_UNAVAILABLE",
                },
            ],
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].get("source"), "git-grok")
        self.assertEqual(entries[0].get("status"), "failed")
        self.assertIn("unusable source content (policy_echo)", str(entries[0].get("content")))

    def test_normalize_synthesis_source_entry_marks_live_web_unavailable_as_failed(self) -> None:
        normalized = JobWorker._normalize_synthesis_source_entry(
            {
                "source": "git-chatgpt",
                "status": "completed",
                "content": "LIVE_WEB_UNAVAILABLE",
            }
        )
        self.assertEqual(normalized.get("status"), "failed")
        self.assertIn("live_web_unavailable", str(normalized.get("error")))

    def test_has_last_chat_context_trigger_detects_keyword_prefix(self) -> None:
        self.assertTrue(JobWorker._has_last_chat_context_trigger("Based on the last chat, what should I do next?"))
        self.assertFalse(JobWorker._has_last_chat_context_trigger("Tell me about pizza dough techniques."))

    def test_maybe_apply_last_chat_context_for_parallel_injects_effective_prompt(self) -> None:
        worker = JobWorker()
        worker._resolve_latest_auto_synthesis_content = lambda skip_job_id="": (  # type: ignore[method-assign]
            "job_prev123",
            "Synthesized answer about topic X.",
        )
        payload = {
            "model": "git-parallel",
            "user_prompt": "Based on the last chat, summarize next steps.",
            "messages": [{"role": "user", "content": "Based on the last chat, summarize next steps."}],
        }
        enriched, meta = worker._maybe_apply_last_chat_context_for_parallel(
            job_id="job_now",
            request_payload=payload,
            base_prompt="Based on the last chat, summarize next steps.",
        )
        self.assertIsInstance(meta, dict)
        self.assertTrue(bool(meta.get("applied")))
        self.assertEqual(meta.get("source_job_id"), "job_prev123")
        self.assertIn("Previous synthesized context from job_prev123", str(enriched.get("user_prompt")))
        messages = enriched.get("messages")
        self.assertIsInstance(messages, list)
        self.assertIn("Previous synthesized context from job_prev123", str(messages[-1].get("content")))

    def test_maybe_apply_last_chat_context_for_parallel_handles_missing_previous_synth(self) -> None:
        worker = JobWorker()
        worker._resolve_latest_auto_synthesis_content = lambda skip_job_id="": None  # type: ignore[method-assign]
        payload = {
            "model": "git-parallel",
            "user_prompt": "Based on the last chat, continue this.",
            "messages": [{"role": "user", "content": "Based on the last chat, continue this."}],
        }
        enriched, meta = worker._maybe_apply_last_chat_context_for_parallel(
            job_id="job_now",
            request_payload=payload,
            base_prompt="Based on the last chat, continue this.",
        )
        self.assertEqual(enriched.get("user_prompt"), "Based on the last chat, continue this.")
        self.assertIsInstance(meta, dict)
        self.assertFalse(bool(meta.get("applied")))
        self.assertEqual(meta.get("reason"), "no_completed_auto_synthesis_found")

    def test_fanout_auto_synthesis_request_flag_can_disable(self) -> None:
        worker = JobWorker()
        worker._allsequential_followup_delivery_enabled = lambda: True  # type: ignore[method-assign]
        enabled = worker._fanout_auto_synthesis_enabled_for_request(
            {"options": {"auto_synthesis": False}},
        )
        self.assertFalse(enabled)

    def test_kickoff_content_mentions_auto_synthesis_when_enabled(self) -> None:
        content = JobWorker._build_virtual_turns_kickoff_content(
            5,
            send_followups=True,
            include_auto_synthesis_note=True,
        )
        self.assertIn("each source result as a separate follow-up", content)
        self.assertIn("synthesized follow-up", content)


if __name__ == "__main__":
    unittest.main()
