# Scoring and Rescoring Strategy Across Phases

## Core Rule

Use this principle:

> **Anything that can enter memory, influence retrieval, or spawn more work must pass a gate.**

This means:
- Phase 3 synthesized outputs should be rescored
- Phase 5 retrieval-informed outputs should be rescored
- Phase 6 candidate follow-up questions should be scored before execution

---

# Phase 3 — Rescore the Synthesized Answer

## Recommended flow

1. Generate 4 branch responses
2. Judge all branch responses
3. Select the top 3
4. Synthesize a merged answer
5. Run a second judge on the synthesized answer
6. Compare the synthesized answer against the best branch

## Why this matters

Synthesis may improve:
- completeness
- structure
- practical usefulness

But it can also degrade:
- precision
- support
- coherence

## Store

- `branch_scores`
- `winner_branch_id`
- `top_branch_ids`
- `synthesized_score`
- `winner_vs_synth_comparison`

## Best decision rule

- if synthesis clearly beats the winner, keep synthesis
- if synthesis is worse, keep the winner
- if close, keep synthesis but mark as low-margin

---

# Phase 5 — Rescore Retrieval-Informed Outputs

## Why this matters

Retrieval introduces new failure modes:

- stale answers
- irrelevant prior matches
- topic drift
- self-reinforcement of weak answers
- over-reliance on memory

## Add retrieval-aware scoring dimensions

- correctness
- usefulness
- completeness
- clarity
- retrieval relevance
- retrieval faithfulness
- novel contribution vs copied prior content

## Important metric

Compare:
- fresh branch performance
- retrieval-informed branch performance

---

# Phase 6 — Score Candidate Sub-Questions Before Asking Them

## Core idea

Generated sub-questions should not execute automatically.

Instead:

1. Generate candidate follow-up questions
2. Score them
3. Only enqueue high-value questions

## Recommended scoring dimensions

- relevance to root question
- expected information gain
- non-duplication
- answerability
- specificity
- evidence potential
- risk of drift

## Example priority formula

```text
question_priority =
  0.30 * relevance +
  0.25 * information_gain +
  0.15 * answerability +
  0.10 * specificity +
  0.10 * evidence_potential +
  0.10 * anti_drift
```

## Most important dimension

Information gain.

The key question is:

> If one more branch is spent on this, how much closer does it get the system to the root answer?

---

# Different Score Types for Different Objects

Use separate score profiles for:

## Answer score
For responses and branch outputs:
- correctness
- completeness
- clarity
- usefulness
- groundedness

## Retrieval score
For retrieval-informed outputs:
- relevance
- faithfulness
- novelty
- usefulness

## Question score
For candidate follow-up questions:
- relevance
- information gain
- specificity
- answerability
- evidence potential
- anti-drift

---

# Recommended Gating Points

## Phase 2
Metadata quality gate:
- topic present
- summary present
- keywords present
- valid schema

## Phase 3
Answer gate:
- judge branches
- synthesize top 3
- rescore synthesis
- compare with best branch

## Phase 5
Memory/retrieval gate:
- retrieve only sufficiently strong prior answers
- rescore retrieval-informed outputs
- compare retrieval vs fresh branches

## Phase 6
Expansion gate:
- score candidate questions
- filter by threshold
- enqueue only highest-value items

---

# Score Decay Over Time

For time-sensitive domains, use effective score rather than static score.

Example:

```text
effective_score = quality_score + recency_bonus + retrieval_match_bonus
```

---

# Promotion Thresholds

Not every artifact should become reusable memory.

Promote to memory only if:
- score >= threshold
- confidence band is not low
- no major contradictions flagged
- artifact passes retrieval quality criteria

---

# Confidence Bands

Attach a confidence band to scores:
- low
- medium
- high

This helps later retrieval prioritize stable high-confidence artifacts.

---

# Best Architecture Summary

Think of the system as having three gates:

## Gate 1 — Answer Gate
Is this answer good enough to keep?

## Gate 2 — Memory Gate
Is this artifact good enough to reuse later?

## Gate 3 — Expansion Gate
Is this candidate question worth more compute?

---

# Bottom Line

Yes to all three:

- rescore synthesis in Phase 3
- rescore retrieval-assisted outputs in Phase 5
- score and gate follow-up questions in Phase 6

That turns the overall system into a **quality-controlled search-and-reasoning system** rather than a simple generative pipeline.
