# Known Limitations

This document lists acknowledged gaps, simplifications, and areas for improvement.

## Functional Limitations

### 1. No Real Model Provider
Only the `MockProvider` is fully implemented. The architecture supports plugging in
a real provider (e.g., OpenAI, Gemini) via the `ModelProvider` ABC, but no real
implementation is included. The mock always returns `{"status": "ok", "mock": true}`
for non-faulted triggers.

### 2. Model Ranking Not Used
The model call in the replay loop is made but its output is not used for ranking or
selecting among candidates. The rules-first approach handles all decisions
deterministically. The model call exists to demonstrate fault replay and budget
tracking.

### 3. In-App Message Daily Cap
The spec mentions `max_per_day: 1` for in-app messages, but the hard rules focus
the combined daily cap on push + WhatsApp only. In-app messages are not currently
counted toward any cap. This could be tightened.

### 4. Home Page Hero Selection
The hero is selected as the most popular available title not already used in rows.
A more sophisticated approach could consider the goal (e.g., for G07, the hero
could be the series the user should resume).

### 5. WhatsApp Template Variable Validation
Template variables are filled from catalogue and pricing data, but we don't
exhaustively validate that every variable value comes strictly from those sources.
The approved template IDs are validated.

### 6. No Thread/Async Pool Under Load
The spec mentions using a "thread/async pool and skip optional model calls under
load." The current implementation is single-threaded and sequential. Under real
production load this would need async processing with load shedding.

### 7. Dependency Degradation Handling
The `dependency_degraded` control event (user_data_store p95=8s, 20% timeouts)
is acknowledged but not fully simulated. In practice, this would trigger fallback
reads or cached state.

## Data Limitations

### 8. Event Ordering Ties
When multiple events share the same `received_at` timestamp, ordering among them
is arbitrary (Python's `sort` is stable, so they keep their JSONL file order).
This shouldn't affect correctness but could affect reproducibility if the input
order changes.

### 9. Dialect Validation Against Catalogue
We validate that home page titles match the viewer's dialect, but we don't verify
that the dialect itself is semantically correct for the user's location — we trust
the `app_setting` as the spec directs.

### 10. G07 Content Slug Matching
For G07 (resume_series), we match by `content_slug` using the catalogue lookup.
If a title_id in a play_started event is not in the catalogue, it won't be
considered for the resume check.

## Measurement Limitations

### 11. Holdout Outcome Comparison
Holdout users get no goal assignment, so we can't compare per-goal success rates
between arms. The ITT comparison is at the aggregate level only. This is noted
in the impact report.

### 12. Outcome Judge Timing
The outcome judge runs after the full replay, not in real-time. It uses
`datetime.now(UTC)` to determine if a goal window is still open, which means
results depend on when the replay is run relative to the goal deadlines.

### 13. Pilot Bias Correction
The pilot bias analysis recomputes ITT from `pilot_outcomes.csv` and
`pilot_assignment.csv`. The column names in the pilot data use "treatment"/"holdout"
while the replay data uses "agent"/"holdout". This mapping is handled correctly
but is worth noting.

## Code Quality

### 14. Verifier Independence
The verifier reimplements `_ts()`, `_ist_hour()`, `_is_quiet_hours()`, and
`_title_available()` to avoid importing planner internals. This creates some
code duplication but is intentional per the spec requirement that the verifier
be independent.

### 15. Single Config File
All configuration is in `src/config.py` as module-level constants. A more
production-ready approach would use a YAML/JSON config file with environment
variable overrides.

### 16. No Logging Framework
The system uses `print()` for progress output. A production system would use
Python's `logging` module with structured log output.
