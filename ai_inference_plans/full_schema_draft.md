# Full Schema Draft for Phases 1–7

This schema family is designed for a system that evolves from:

- single prompt / single response
- indexed responses
- multi-branch reasoning
- synthesis and rescoring
- retrieval-augmented inference
- recursive question fan-out
- historical repo-backed knowledge reuse

The design principle is:

> Use one shared envelope, with specialized payloads and score profiles by object type.

---

# 1. Design Goals

## Requirements

The schema should support:

- stable indexing
- phase-aware evaluation
- rescoring without destructive overwrites
- provenance tracking
- retrieval eligibility
- question expansion control
- historical analysis across runs

## Core principle

Not every artifact is the same kind of object.

A:

- branch response
- synthesized response
- retrieval-informed response
- candidate follow-up question
- evidence item
- final research synthesis

should **not** all share identical score fields.

Instead:

- the **envelope** is shared
- the **metadata** is mostly shared where possible
- the **score profile** varies by object type

---

# 2. Shared Envelope Schema

Every stored artifact should use this top-level structure.

```json
{
  "schema_version": "1.0",
  "object_type": "response",
  "object_id": "resp_123",
  "phase": 2,
  "status": "complete",
  "created_at": "2026-03-26T20:17:25Z",
  "updated_at": "2026-03-26T20:18:10Z",
  "run_context": {},
  "content": {},
  "metadata": {},
  "scores": [],
  "judge": {},
  "provenance": {},
  "promotion": {},
  "links": {}
}
```

---

# 3. Shared Envelope Field Definitions

## Required Fields

### `schema_version`
```json
"schema_version": "1.0"
```

### `object_type`
Allowed values:

- `question`
- `response`
- `synthesized_response`
- `retrieval_response`
- `candidate_question`
- `evidence`
- `evaluation`
- `final_response`
- `research_summary`

### `object_id`
Unique ID for this artifact.

### `phase`
Which roadmap phase produced or first introduced this artifact.

### `status`
Allowed examples:

- `pending`
- `complete`
- `failed`
- `filtered_out`
- `archived`

### `created_at`
ISO 8601 timestamp.

### `updated_at`
ISO 8601 timestamp.

---

## Shared Supporting Fields

### `run_context`
Describes which question / run / branch / research tree this belongs to.

```json
{
  "job_id": "job_0c5adfb5aa47461a",
  "question_id": "q_20260326_001",
  "run_id": "run_001",
  "branch_id": "branch_03",
  "research_id": null,
  "parent_object_id": null,
  "root_object_id": "q_20260326_001"
}
```

### `content`
The core object payload. Varies by object type.

### `metadata`
Searchable descriptive fields. More stable than scores.

### `scores`
Array of score events. Never overwrite prior score events.

### `judge`
Information about the process that created or last evaluated the object.

### `provenance`
Tracks source inputs, upstream artifacts, or retrieved artifacts used.

### `promotion`
Controls whether this object is reusable memory.

### `links`
Paths or references to related repo artifacts.

---

# 4. Shared Metadata Schema

This is the shared base metadata schema. Not every object needs every field.

```json
{
  "title": null,
  "topic_short": null,
  "topic": null,
  "topics": [],
  "keywords": [],
  "summary": null,
  "query_variants": [],
  "language": "en",
  "domain": null,
  "subdomain": null,
  "sensitivity": "public",
  "recency_class": "stable"
}
```

## Field Notes

### `title`
Good for evidence items, research summaries, or named reports.

### `topic_short`
Short human-readable label, ideally 3–8 words.

### `topic`
More precise topic label.

### `topics`
Broad categories.

### `keywords`
Lexical search terms.

### `summary`
Short summary, usually one sentence.

### `query_variants`
Alternative ways a user might search for this.

### `language`
Usually `"en"`.

### `domain`
Examples:
- `software`
- `medicine`
- `law`
- `finance`

### `subdomain`
Examples:
- `oncology`
- `github-actions`
- `contracts`

### `sensitivity`
Examples:
- `public`
- `internal`
- `private`
- `restricted`

### `recency_class`
Examples:
- `stable`
- `slow_changing`
- `fast_changing`
- `time_sensitive`

---

# 5. Shared Score Event Schema

All scoring and rescoring should be stored as score events.

```json
{
  "score_event_id": "score_evt_001",
  "score_profile": "answer_score_profile",
  "judge_type": "branch_judge",
  "judge_model": "gpt-5.4-thinking",
  "phase": 3,
  "created_at": "2026-03-26T20:19:00Z",
  "dimensions": {},
  "overall_score": 0.0,
  "confidence_band": "medium",
  "notes": null
}
```

## Field Notes

### `score_event_id`
Unique score event ID.

### `score_profile`
Allowed values:

- `answer_score_profile`
- `synthesis_score_profile`
- `retrieval_score_profile`
- `question_score_profile`
- `evidence_score_profile`
- `final_score_profile`

### `judge_type`
Examples:

- `self_index`
- `branch_judge`
- `synthesis_judge`
- `retrieval_rescore`
- `question_judge`
- `evidence_judge`
- `final_judge`

### `dimensions`
Dictionary of numeric scoring dimensions.

### `overall_score`
Normalized aggregate score, typically 0–10.

### `confidence_band`
Examples:
- `low`
- `medium`
- `high`

### `notes`
Optional explanation.

---

# 6. Shared Promotion Schema

Use this to control whether an artifact becomes reusable memory.

```json
{
  "memory_eligible": false,
  "memory_tier": null,
  "promotion_reason": null,
  "blocked_reason": null,
  "effective_score": null
}
```

## Field Notes

### `memory_eligible`
Whether this can be reused in retrieval.

### `memory_tier`
Examples:
- `gold`
- `silver`
- `bronze`

### `promotion_reason`
Why it was promoted.

### `blocked_reason`
Why it was excluded.

### `effective_score`
Score used for memory promotion after recency and quality adjustments.

---

# 7. Shared Links Schema

```json
{
  "repo_path": null,
  "index_path": null,
  "source_paths": [],
  "related_object_ids": [],
  "evaluation_path": null,
  "final_path": null
}
```

---

# 8. Object Type: `question`

Represents the initial user question or a canonical question record.

## Example

```json
{
  "schema_version": "1.0",
  "object_type": "question",
  "object_id": "q_20260326_001",
  "phase": 1,
  "status": "complete",
  "created_at": "2026-03-26T20:10:00Z",
  "updated_at": "2026-03-26T20:10:00Z",
  "run_context": {
    "job_id": null,
    "question_id": "q_20260326_001",
    "run_id": null,
    "branch_id": null,
    "research_id": null,
    "parent_object_id": null,
    "root_object_id": "q_20260326_001"
  },
  "content": {
    "question_text": "What current technology and information is available to cure certain forms of cancer?"
  },
  "metadata": {
    "title": "Root question",
    "topic_short": "Cancer cure technologies",
    "topic": "Current technologies and information related to curative cancer treatment",
    "topics": ["medicine", "oncology"],
    "keywords": ["cancer", "cure", "treatment", "technology"],
    "summary": "Root question for oncology research planning.",
    "query_variants": [
      "what cancers are curable today",
      "latest curative cancer treatments"
    ],
    "language": "en",
    "domain": "medicine",
    "subdomain": "oncology",
    "sensitivity": "private",
    "recency_class": "fast_changing"
  },
  "scores": [],
  "judge": {},
  "provenance": {},
  "promotion": {
    "memory_eligible": false,
    "memory_tier": null,
    "promotion_reason": null,
    "blocked_reason": "Questions are not memory artifacts by default.",
    "effective_score": null
  },
  "links": {
    "repo_path": "questions/q_20260326_001.json",
    "index_path": "indexes/questions_index.jsonl",
    "source_paths": [],
    "related_object_ids": [],
    "evaluation_path": null,
    "final_path": null
  }
}
```

---

# 9. Object Type: `response`

Represents a standard branch or single-response answer.

## Response Content Schema

```json
{
  "response_text": "...",
  "response_role": "assistant",
  "branch_profile": "baseline"
}
```

## Recommended Metadata Schema

Use the shared metadata fields.

## Score Profile: `answer_score_profile`

### Dimensions
- `correctness`
- `completeness`
- `clarity`
- `usefulness`
- `groundedness`
- `specificity`

---

# 10. Object Type: `synthesized_response`

Represents a merged answer from top branches.

## Content Schema

```json
{
  "response_text": "...",
  "response_role": "assistant",
  "source_branch_ids": ["branch_01", "branch_02", "branch_04"]
}
```

## Score Profile: `synthesis_score_profile`

### Dimensions
- `correctness`
- `completeness`
- `clarity`
- `usefulness`
- `groundedness`
- `coherence`
- `redundancy_penalty`

---

# 11. Object Type: `retrieval_response`

Represents a response informed by prior repo memory.

## Score Profile: `retrieval_score_profile`

### Dimensions
- `correctness`
- `completeness`
- `clarity`
- `usefulness`
- `retrieval_relevance`
- `retrieval_faithfulness`
- `novelty`

---

# 12. Object Type: `candidate_question`

Represents a generated follow-up question waiting to be scored and gated.

## Score Profile: `question_score_profile`

### Dimensions
- `relevance`
- `information_gain`
- `specificity`
- `answerability`
- `evidence_potential`
- `anti_drift`

---

# 13. Object Type: `evidence`

Represents a normalized evidence unit from a source or structured prior artifact.

## Score Profile: `evidence_score_profile`

### Dimensions
- `relevance`
- `source_quality`
- `recency`
- `specificity`
- `support_strength`

---

# 14. Object Type: `evaluation`

Represents a ranking/evaluation artifact for a set of branches.

Use this to store:
- evaluated object IDs
- ranked order
- winner
- top candidates
- judge summary

---

# 15. Object Type: `final_response`

Represents the selected final artifact for a run after comparing winner vs synthesis.

## Score Profile: `final_score_profile`

### Dimensions
- `correctness`
- `completeness`
- `clarity`
- `usefulness`
- `groundedness`
- `final_selection_confidence`

---

# 16. Object Type: `research_summary`

Represents an interim or final summary in recursive research mode.

Use this to store:
- summary text
- claims
- open questions
- continue/stop decision

---

# 17. Score Profile Definitions

## `answer_score_profile`
- `correctness`
- `completeness`
- `clarity`
- `usefulness`
- `groundedness`
- `specificity`

## `synthesis_score_profile`
- `correctness`
- `completeness`
- `clarity`
- `usefulness`
- `groundedness`
- `coherence`
- `redundancy_penalty`

## `retrieval_score_profile`
- `correctness`
- `completeness`
- `clarity`
- `usefulness`
- `retrieval_relevance`
- `retrieval_faithfulness`
- `novelty`

## `question_score_profile`
- `relevance`
- `information_gain`
- `specificity`
- `answerability`
- `evidence_potential`
- `anti_drift`

## `evidence_score_profile`
- `relevance`
- `source_quality`
- `recency`
- `specificity`
- `support_strength`

## `final_score_profile`
- `correctness`
- `completeness`
- `clarity`
- `usefulness`
- `groundedness`
- `final_selection_confidence`

---

# 18. Index Files

Recommended repo-level indexes:

- `indexes/questions_index.jsonl`
- `indexes/responses_index.jsonl`
- `indexes/evidence_index.jsonl`
- `indexes/research_index.jsonl`
- `indexes/evaluations_index.jsonl`

---

# 19. Minimal Index Record Shape

Example compact record:

```json
{
  "object_id": "final_001",
  "object_type": "final_response",
  "question_id": "q_20260326_001",
  "run_id": "run_001",
  "topic_short": "GitHub Actions server locations",
  "topics": ["github", "cloud infrastructure", "data residency"],
  "keywords": ["GitHub Actions", "Azure", "regions"],
  "summary": "Final selected answer for the run.",
  "overall_score": 8.0,
  "effective_score": 8.2,
  "memory_eligible": true,
  "repo_path": "runs/q_20260326_001/run_001/final.json",
  "created_at": "2026-03-26T20:23:00Z"
}
```

---

# 20. Schema Rules

1. Do not overwrite scores. Append new score events.
2. Keep metadata stable; treat scores as revisable.
3. Only promote artifacts to retrieval memory if they pass a quality threshold.
4. Questions, evaluations, and frontier artifacts are not retrieval memory by default.
5. Use specialized score profiles.
6. Use the same envelope structure everywhere.

---

# 21. Recommended Threshold Strategy

Example defaults:

- memory promotion: `overall_score >= 7.5`
- gold tier: `effective_score >= 8.5`
- silver tier: `effective_score >= 7.5`
- candidate question execution: `overall_score >= 7.0`
- retrieval eligibility: `memory_eligible == true`

---

# 22. Recommended Repo Layout

```text
questions/
  q_20260326_001.json

runs/
  q_20260326_001/
    run_001/
      branches/
        branch_01.json
        branch_02.json
        branch_03.json
        branch_04.json
      evaluation.json
      synthesized.json
      final.json

research/
  r_001/
    root.json
    frontier/
      cq_001.json
    evidence/
      evid_001.json
    summaries/
      iteration_001.json
      iteration_002.json
    final.json

indexes/
  questions_index.jsonl
  responses_index.jsonl
  evidence_index.jsonl
  research_index.jsonl
  evaluations_index.jsonl
```

---

# 23. Final Recommendation

Use this schema family as your system contract:

- shared envelope
- stable metadata
- append-only score events
- specialized score profiles
- explicit provenance
- explicit memory promotion

That gives you:
- consistency
- flexibility
- auditability
- retrieval safety
- phase-by-phase extensibility
