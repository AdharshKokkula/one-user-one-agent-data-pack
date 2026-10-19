# Final Report – One User, One Agent Replay

## Summary

| Metric | Value |
|---|---|
| Total triggers | 2769 |
| Decisions made | 1154 |
| Holdout skipped | 1325 |
| Stale skipped | 200 |
| Duplicate skipped | 90 |
| Home pages published | 1083 |
| Push notifications | 0 |
| WhatsApp messages | 0 |
| In-app messages | 0 |
| NO_ACTION decisions | 71 |
| Verified pass | 1154 |
| Verified reject | 0 |
| Goals assigned | 1154 |
| Total time (s) | 82.97 |

## Latency

| Metric | Value |
|---|---|
| p50 (ms) | 70 |
| p95 (ms) | 88 |
| Max (ms) | 167 |
| Count | 1154 |
| Over 60s | 0 |

## Outcomes

| Status | Count |
|---|---|
| Succeeded | 10 |
| Failed | 1144 |
| Inconclusive | 0 |

## Data Issues → How Handled

| Issue | Count | How Handled |
|---|---|---|
| duplicate_events | 2176 | Deduped by event_id, keeping first occurrence |
| dual_profile_rows | 30 | Picked latest by updated_at, conflict recorded |
| dual_profile_rows_summary | 30 | All dual-row users resolved by latest updated_at |
| invalid_profile_dialect | 446 | Used app_setting dialect from trigger; profile dialect noted as conflict |
| injected_description | 2 | Flagged and logged; title excluded from recommendations |
| duplicate_content_slugs | 514 | Deduped by content_slug, keeping first variant |
| stale_triggers | 480 | Flagged as offline-buffered; NO_ACTION with recorded reason |
| missing_new_viewer_profiles | 103 | Used signup_completed event for signup date; facts resolved from events |
| dialect_conflict_profile_vs_app | 555 | app_setting decides content dialect; profile and deeplink recorded as conflicts |

## Goal Assumptions

### G07 (resume_series_3d)
"Has not played it for 3 or more days" is interpreted as: the user's most recent
play_started event for the content_slug has received_at ≥ 3 calendar days before
the trigger's received_at.

### G09 (weekly_habit_14d)
"Plays on at most 1 day in the last 14 days" is interpreted as: counting distinct
IST calendar days with at least one play_started event in the 14 IST days ending on
(and including) the trigger's IST date.

### Skipped Goals

- **G10 (meaningful_engagement_7d)**: success.event is null and condition is
  "engages meaningfully" – no concrete success event can be attributed.
- **G11 (tv_activation_14d)**: success.event is "tv_app_open" but this event type
  never appears in data/events.jsonl. No way to observe success.

## Impact Report

### Intention-to-Treat (ITT) Analysis

The ITT comparison includes ALL users assigned to each arm, regardless of
whether the agent actually took an action for them. This is the correct
causal comparison because it avoids selection bias.

| Group | N | Successes | Rate |
|---|---|---|---|
| Agent arm (all) | 2513 | 7 | 0.0028 (0.3%) |
| Holdout arm (all) | 2487 | 0 | 0.0000 (0.0%) |

**ITT Uplift**: 0.0028 (0.28 percentage points)

**95% Confidence Interval**: [0.0007, 0.0048]
(0.07 to 0.48 pp)

**Statistically significant (95% CI excludes 0)**

### Breakdown by Goal (Agent Arm Only)

| Goal | N | Successes | Rate |
|---|---|---|---|
| G01 | 59 | 0 | 0.0% |
| G02 | 220 | 0 | 0.0% |
| G03 | 1 | 0 | 0.0% |
| G05 | 152 | 3 | 2.0% |
| G06 | 52 | 3 | 5.8% |
| G07 | 243 | 1 | 0.4% |
| G08 | 24 | 0 | 0.0% |

> Note: Holdout users receive no agent action and no goal assignment,
> so per-goal breakdown is only available for the agent arm.

### Pilot Bias Analysis

The pilot summary report claimed a 2.3× uplift (+22.4 points) by comparing
"users served by the agent" (217 users, 40.1% success) vs "all other users"
(3,674 users, 17.7% success). This comparison is **selection-biased** for
two key reasons:

1. **Served vs. not-served is not randomized.** The 217 "served" users are a
   self-selected subset of the treatment arm – they opened the app, received a
   personalised page, and in some cases received a push notification. Users who
   didn't engage enough to be "served" are excluded from the treatment group and
   mixed into the control group, biasing both groups.

2. **The "all other users" group mixes holdout with unserved treatment.** This
   contaminates the control condition with treatment-assigned users who happened
   not to be served, artificially lowering the control success rate.

**Correct ITT (intent-to-treat) recomputation using the randomized arms:**

| Group | N | Successes | Rate |
|---|---|---|---|
| Treatment arm (all) | 1912 | 349 | 18.3% |
| Holdout arm (all) | 1979 | 387 | 19.6% |

**ITT Uplift**: -1.30 percentage points
**95% CI**: [-3.76, 1.16] pp

The corrected ITT estimate is **not** statistically significant – the 95% CI includes zero.

This is dramatically smaller than the biased 22.4pp claim, because the selection
bias inflated the apparent effect by comparing the most-engaged treatment users
against everyone else.

### Simulator Results

- Triggered users: 1746
- Events written: 31659
- actions_total: 1083
- home_page_accepted: 1083
- home_page_published_but_not_shown: 22
- home_page_shown_personalised: 827
- organic_purchases_scripted: 36

