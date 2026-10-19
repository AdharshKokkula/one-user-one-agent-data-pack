# Architecture – One User, One Agent

## Overview

The system implements a **decide → verify → act → judge → measure** loop where each
viewer gets one deterministic agent. The core principle: rules-first, model-optional,
independently verified.

```
┌─────────────┐     ┌──────────────┐     ┌──────────────────┐
│   Loader    │────▶│  Fact        │────▶│  Goal Planner    │
│  (Step 1)   │     │  Resolver    │     │  (Step 4)        │
│             │     │  (Step 3)    │     │  deterministic    │
└─────────────┘     └──────────────┘     └────────┬─────────┘
       │                                          │
       ▼                                          ▼
┌─────────────┐     ┌──────────────┐     ┌──────────────────┐
│  Viewer     │◀───▶│ Model        │────▶│  Intervention    │
│  State      │     │ Provider     │     │  Planner         │
│  (Step 2)   │     │ (Step 6)     │     │  (Step 5)        │
└─────────────┘     └──────────────┘     └────────┬─────────┘
                                                  │
                                                  ▼
                                         ┌──────────────────┐
                                         │  Verifier        │
                                         │  (Step 7)        │
                                         │  INDEPENDENT     │
                                         └────────┬─────────┘
                                                  │
                    ┌─────────────────────────────┼──────────────────┐
                    ▼                             ▼                  ▼
           ┌──────────────┐            ┌──────────────┐   ┌──────────────┐
           │  Replay      │            │  Outcome     │   │  Impact      │
           │  Harness     │            │  Judge       │   │  Reporter    │
           │  (Step 8)    │            │  (Step 10)   │   │  (Step 11)   │
           └──────────────┘            └──────────────┘   └──────────────┘
```

## Module Descriptions

### 1. Loader (`src/loader.py`)

Loads and validates the entire data pack into a `DataPack` dataclass:

- **Events**: Deduped by `event_id`, ordered by `received_at` (the trustworthy
  timestamp). Computes `event_age_s = received_at - occurred_at`.
- **Profiles**: Dual rows resolved by latest `updated_at`; consent booleans parsed.
- **Catalogue**: Deduped by `content_slug` (keeping first variant); injected
  descriptions detected via keyword scan.
- **Triggers**: Parsed with `deliver_at` timestamps; stale triggers flagged when
  `event_age_s > 300s`.
- **Withdrawn titles**: Applied from control events to catalogue `_available_until`.
- Produces a `data_issues` list covering: duplicates, dual profiles, dialect
  conflicts, injections, stale triggers, missing profiles.

### 2. Viewer State Store (`src/viewer_state.py`)

One JSON file per viewer in `viewer_state/`. Stores:

```
{
  facts: { fact_name: { value, source, observed_at, freshness } },
  active_goal, plan,
  decisions: { trigger_event_id → decision },   // idempotent
  actions, outcomes, learnings,
  messages_sent_today: { "YYYY-MM-DD" → count },
  conflicts, goal_history
}
```

Writes are idempotent: adding a decision for the same `trigger_event_id` is a no-op.

### 3. Fact Resolver (`src/fact_resolver.py`)

Explicit, documented precedence per fact:

| Fact | Precedence |
|------|-----------|
| **subscription** | Newest purchase/subscription event by `received_at` beats profile snapshot |
| **dialect** | `app_dialect` from trigger → latest `app_dialect_changed` event → profile `preferred_dialect` |
| **signup_date** | Profile `signup_at` if present → `signup_completed` event for new viewers |
| **consent** | Profile value, overridden by later `notification_opt_out` events |
| **profile dups** | Latest `updated_at` wins; conflict recorded |

All conflicts are recorded in the viewer state.

### 4. Goal Planner (`src/goal_planner.py`)

Deterministic goal selection with priority ordering:
`G01 > G03 > G02 > G04 > G05 > G06 > G07 > G08 > G09 > G12`

- Evaluates eligibility from resolved facts and events only
- **Skips G10**: `success.event` is null — no attributable success metric
- **Skips G11**: `tv_app_open` event never appears in the data
- Supports corrected goal rules (version tracking)
- Returns `None` for NO_ACTION with a reason

**Documented assumptions:**
- **G07**: "3 or more days" = `received_at` of last play ≥ 3 calendar days before
  trigger
- **G09**: "at most 1 day in last 14" = counting distinct IST calendar days with
  `play_started` events

### 5. Intervention Planner (`src/intervention_planner.py`)

Rules-first intervention selection:

1. **HOME_PAGE**: Always attempted if viable in-dialect content exists.
   - 1 hero + up to 6 rows (3-10 titles each)
   - Row types: `next_episode`, `popular_in_dialect`, `new_releases`,
     `because_you_watched`, `genre`
   - Never publishes "Continue Watching"
   - Rows with <3 valid titles are dropped
   - Each `content_slug` appears once per page
   - Only available, in-dialect, non-injected titles

2. **Message** (one channel): Push → WhatsApp → In-app
   - Respects consent, quiet hours (22:00-08:00 IST), daily cap
   - Push: title ≤40 chars, body ≤120, deeplink `ott://title/<id>`, no
     forbidden phrases
   - WhatsApp: only 3 approved templates, variables from catalogue/pricing

### 6. Model Provider (`src/model_provider.py`)

- **MockProvider**: Default, deterministic, no API key
- Replays faults from `model_faults.jsonl`: timeout, malformed_output,
  unsupported_claim
- `CallBudget`: max 8 model calls, 20 tool calls (configurable)
- `call_with_retries()`: bounded retries, budget enforcement
- `safe_json_parse()`: extracts JSON from markdown code blocks

### 7. Independent Verifier (`src/verifier.py`)

**Cannot import planner internals.** Re-checks from raw state:

- Holdout → must be NO_ACTION
- Idempotency → duplicate trigger_event_id rejected
- Home page: dialect, row count, titles per row, slug uniqueness, availability,
  no Continue Watching
- Push: consent, quiet hours, daily cap, char limits, forbidden phrases, deeplink
- WhatsApp: consent, quiet hours, daily cap, approved templates
- Unsupported model claims → rejected

Can reject (`REJECTED_BY_VERIFIER`) or pass. Rejected decisions are downgraded
to NO_ACTION.

### 8. Replay Harness (`src/replay.py`)

Processes the unified timeline (triggers + live events + control events) in
`deliver_at` order:

- **Duplicate triggers**: Idempotent by `trigger_event_id`
- **Holdout**: Immediate NO_ACTION
- **Stale triggers**: NO_ACTION with reason
- **Live events**: Update facts, trigger re-planning if purchase/lapse
- **Control events**: Title withdrawal, goal rule corrections, dependency outages
- **published_at**: `deliver_at + measured decision time`

Post-replay: outcome judging → simulator → impact reporter → final report.

### 9. Outcome Judge (`src/outcome_judge.py`)

Judges each goal as `succeeded` / `failed` / `inconclusive` using only events
inside the goal window (`started_at` to `deadline`). Applies corrected goal rules.
Extracts learnings per viewer.

### 10. Impact Reporter (`src/impact_reporter.py`)

- **ITT**: Success rate of ALL agent-arm viewers vs ALL holdout viewers (including
  no-action viewers), with 95% CI
- **Pilot bias**: Recomputes uplift using randomized arms; explains why "served vs
  everyone else" is selection-biased
- Reports honestly even if uplift ≈ 0

## Data Flow

```
triggers.jsonl ─┐
live_events.jsonl ─┤──▶ Unified Timeline (sorted by deliver_at)
control_events.jsonl ─┘      │
                              ▼
                    ┌───── For each trigger ─────┐
                    │  1. Dedupe / holdout / stale │
                    │  2. Resolve facts             │
                    │  3. Select goal               │
                    │  4. Plan intervention          │
                    │  5. Verify                     │
                    │  6. Record decision            │
                    └──────────────────────────────┘
                              │
                    ┌─────────┼──────────┐
                    ▼         ▼          ▼
             decisions.jsonl  actions.jsonl  viewer_state/
                              │
                    ┌─────────┼──────────┐
                    ▼         ▼          ▼
             outcomes.jsonl  impact_report.md  final_report.md
```

## Configuration

All tunables live in `src/config.py`:
- Timezone: IST (UTC+5:30)
- Stale threshold: 300s
- Model budget: 8 model calls, 20 tool calls
- Quiet hours: 22:00-08:00 IST
- Daily message cap: 1 push+WhatsApp combined
- Push copy limits: title ≤40, body ≤120
- Home page: max 6 rows, 3-10 titles per row
- Valid prices: ₹1, ₹149, ₹399
