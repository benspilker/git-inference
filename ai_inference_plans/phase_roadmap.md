# AI Inference System Roadmap (Phases 1–7)

## Overview

This document outlines a multi-phase architecture for building a **self-improving AI inference system** that evolves from simple prompt-response pipelines into a **retrieval-augmented, multi-branch, recursively researching knowledge system**.

The system progresses through:

- Single inference
- Indexed memory
- Multi-branch reasoning
- Retrieval-based synthesis
- Recursive research graphs
- Eventually, a scalable knowledge platform

---

# Phase 1 — Single Prompt Pipeline (Current State)

## Description

- User submits a question
- Playwright sends prompt to ChatGPT
- Response is captured and stored

## Characteristics

- No memory
- No evaluation
- No optimization

## Output

```json
{
  "job_id": "...",
  "response": "...",
  "timestamps": {}
}
```

---

# Phase 2 — Metadata Indexing + Scoring

## Description

After generating a response, a second prompt extracts structured metadata.

## Goals

- Enable search
- Enable ranking
- Prepare for retrieval

## Metadata Schema

```json
{
  "topic_short": "...",
  "topic": "...",
  "topics": ["..."],
  "keywords": ["..."],
  "summary": "...",
  "query_variants": ["..."],
  "usefulness_score": 0,
  "informativeness_score": 0,
  "confidence_score": 0
}
```

## Key Insight

> This phase is the **foundation of the entire system**. Poor metadata = poor retrieval later.

## Storage

- Append to:
  - `responses_index.jsonl`
  - `questions_index.jsonl`

---

# Phase 3 — Multi-Branch Fan-Out + Synthesis

## Description

- One question -> 4 (or 8) branches
- Each branch uses a different prompt strategy
- Responses are scored and ranked
- Top 3 are synthesized into one final answer

## Flow

1. Generate branches
2. Judge all branches
3. Select top 3
4. Synthesize
5. **Rescore synthesized output**
6. Compare vs best branch

## Branch Profiles (Example)

- baseline
- concise
- deep
- skeptical
- structured
- example-rich
- fact-first
- edge-case

## Scoring Dimensions

- correctness
- completeness
- clarity
- usefulness
- groundedness

## Critical Rule

> Do not assume synthesis is better than the best branch.

## Output

```json
{
  "winner_branch_id": "...",
  "winner_score": 8.7,
  "synthesized_score": 8.5,
  "selected_final_type": "winner_branch | synthesized",
  "final_response": "..."
}
```

---

# Phase 4 — Public vs Private Repo Split (Optional)

## Description

Separate execution from data:

- Public repo:
  - Job IDs
  - Pipeline execution
- Private repo:
  - Actual questions
  - Full responses
  - Metadata

## Purpose

- Keep system free (GitHub Actions)
- Maintain privacy of data

## Risks

- Data leakage
- Complexity
- Sync issues

## Recommendation

Prefer:

- Public repo -> demo / code
- Private storage -> actual data

---

# Phase 5 — Retrieval-Augmented Inference (Knowledge Base)

## Description

Before generating responses:

- Retrieve prior high-scoring answers
- Inject into prompt context

## Retrieval Sources

- topic match
- keyword match
- query_variants
- recency

## Strategy

Two branch types:

### Fresh branches
- No prior context

### Retrieval-informed branches
- Include prior responses

## Benefits

- Reuse high-quality answers
- Improve accuracy
- Enable knowledge accumulation

## Retrieval Scoring Additions

- retrieval relevance
- retrieval faithfulness
- novelty vs reuse

## Final Score Formula

```text
effective_score =
  base_score +
  recency_bonus +
  retrieval_match_bonus
```

---

# Phase 6 — Recursive Research Graph (Deep Research)

## Description

Move from single questions -> **question decomposition + exploration graph**

## Flow

1. Root question
2. Generate sub-questions
3. Execute in parallel
4. Evaluate results
5. Generate follow-up questions
6. Repeat until stop condition

---

## Node Types

### Question Node
```json
{
  "node_type": "question",
  "question": "..."
}
```

### Evidence Node
```json
{
  "node_type": "evidence",
  "summary": "...",
  "confidence": 0.8
}
```

### Synthesis Node
```json
{
  "node_type": "synthesis",
  "claims": [],
  "open_questions": []
}
```

### Critique Node
```json
{
  "node_type": "critique",
  "missing_areas": [],
  "conflicts": []
}
```

---

## Question Generation Control

### DO NOT auto-expand all questions

Instead:

1. Generate candidate questions
2. Score them
3. Only run high-value ones

---

## Question Scoring Dimensions

- relevance
- information gain
- specificity
- answerability
- evidence potential
- anti-drift

### Priority Formula

```text
priority_score =
  0.30 * relevance +
  0.25 * information_gain +
  0.15 * answerability +
  0.10 * specificity +
  0.10 * evidence_potential +
  0.10 * anti_drift
```

---

## Stop Conditions

### Hard Stops
- max depth
- max nodes
- time limit
- file size limit

### Soft Stops
- no new insights
- repeated conclusions
- diminishing returns

---

## Core Rule

> Expansion must be justified. No uncontrolled recursion.

---

# Phase 7 — Multi-Repo + Platform + Data Lake

## Description

System evolves into:

- multi-repo communication
- cross-run learning
- embedding-based retrieval (optional)
- knowledge marketplace

## Features

- vector search (optional)
- cross-domain datasets
- organization-level knowledge bases
- API layer

---

## System Evolution

| Phase | System Type |
|------|------------|
| 1–2 | Stateless inference |
| 3 | Ensemble reasoning |
| 5 | Knowledge base |
| 6 | Research engine |
| 7 | Platform |

---

# Core Scoring Architecture

## Three Gates

### 1. Answer Gate
- Is this response good?

### 2. Memory Gate
- Should this be reused later?

### 3. Expansion Gate
- Is this worth further computation?

---

## Score Types

### Answer Score
- correctness
- clarity
- completeness
- usefulness

### Retrieval Score
- relevance
- faithfulness
- novelty

### Question Score
- information gain
- relevance
- specificity

---

# Repo as Database

## Layers

### Raw Layer
- responses
- branches
- questions

### Index Layer
- `responses_index.jsonl`
- `questions_index.jsonl`
- `topic_buckets.json`

### Derived Layer
- top answers
- topic summaries
- best-of lists

---

## Key Rule

> Git is storage. Index files are the database.

---

# Monetization Opportunities

## 1. Best Answer Engine
- multi-branch + judge + synthesis
- sell quality, not generation

## 2. Domain Knowledge Systems
- oncology
- legal
- finance

## 3. Enterprise Knowledge Base
- internal company memory
- indexed reasoning

## 4. API Layer

```http
POST /best-answer
```

## 5. Research-as-a-Service
- time-bounded deep research
- structured outputs

---

# Biggest Risks

## 1. Memory contamination
Bad answers reused -> system degrades

## 2. Retrieval failure
Weak indexing -> poor results

## 3. Recursive hallucination
System reinforces incorrect conclusions

## 4. Cost explosion
Fan-out + recursion = expensive

---

# Key Success Factors

1. Strong metadata schema (Phase 2)
2. Reliable scoring system (Phase 3)
3. High-quality retrieval (Phase 5)
4. Controlled recursion (Phase 6)
5. Clear product surface (Phase 7)

---

# Final Summary

This system evolves into:

> **A self-improving, memory-backed, multi-branch reasoning engine with controlled exploration and synthesis**

Core architecture:

- Generate -> Evaluate -> Store -> Retrieve -> Expand -> Synthesize -> Repeat (bounded)

---

# Next Steps (Recommended)

1. Finalize metadata schema (Phase 2)
2. Implement judge + scoring system (Phase 3)
3. Build retrieval index (Phase 5)
4. Add question gating (Phase 6)
5. Define minimal product (Phase 3–5)
