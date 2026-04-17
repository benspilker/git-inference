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


if __name__ == "__main__":
    unittest.main()
