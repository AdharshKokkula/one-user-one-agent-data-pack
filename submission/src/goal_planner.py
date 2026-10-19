"""
Goal Planner – Step 4

Deterministic goal selection:
  - Evaluate eligibility for each goal in goals.json from resolved facts and events only.
  - Select exactly one goal with a deadline and success signal.
  - Skip G10 (meaningful_engagement_7d) – no concrete success event.
  - Skip G11 (tv_activation_14d) – tv_app_open event never appears in events.jsonl.
  - On state changes (e.g., purchase), re-plan: cancel goals no longer relevant.

Assumptions for underspecified rules:
  G07 (resume_series_3d): "has not played it for 3 or more days" →
      We interpret this as: the user's most recent play_started for the content_slug
      has received_at ≥ 3 days before the trigger time.
  G09 (weekly_habit_14d): "plays on at most 1 day in the last 14 days" →
      We interpret "last 14 days" as the 14 IST calendar days ending on the trigger's
      IST date (inclusive), counting distinct IST dates with play_started events.

Goal priority order: G01 > G03 > G02 > G04 > G05 > G06 > G07 > G08 > G09 > G12
(higher-specificity goals first; payment goals before engagement goals)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from . import config as cfg
from .loader import DataPack, _ts
from .viewer_state import ViewerState


# Priority order (index = priority, lower index = higher priority)
GOAL_PRIORITY = [
    "G01",  # first_play_24h (new user, most urgent)
    "G03",  # second_day_return_2d (new user, time-sensitive)
    "G02",  # first_full_episode_3d
    "G04",  # trial_to_paid_7d (revenue, time-sensitive)
    "G05",  # first_paid_7d (revenue)
    "G06",  # reactivate_lapsed_14d (revenue)
    "G07",  # resume_series_3d (engagement)
    "G08",  # try_new_release_7d
    "G09",  # weekly_habit_14d
    "G12",  # return_after_absence_7d
]

# Goals we always skip (with reason)
SKIPPED_GOALS = {
    "G10": cfg.G10_SKIP_REASON,
    "G11": (
        "G11 (tv_activation_14d) has success.event='tv_app_open' but this event "
        "type never appears in data/events.jsonl. There is no way to observe success."
    ),
}

# Assumptions for underspecified rules
ASSUMPTIONS = {
    "G07": (
        "ASSUMPTION: 'has not played it for 3 or more days' is interpreted as: "
        "the user's most recent play_started event for the content_slug has "
        "received_at >= 3 calendar days before the trigger's received_at."
    ),
    "G09": (
        "ASSUMPTION: 'plays on at most 1 day in the last 14 days' is interpreted as: "
        "counting distinct IST calendar days with at least one play_started event "
        "in the 14 IST days ending on (and including) the trigger's IST date."
    ),
}


def _ist_date(dt: datetime) -> Any:
    """Convert a UTC datetime to an IST date."""
    return dt.astimezone(cfg.IST).date()


def _check_eligibility(
    goal: dict,
    user_id: str,
    pack: DataPack,
    state: ViewerState,
    trigger_time: datetime,
) -> tuple[bool, str]:
    """Check if a user is eligible for a goal.
    
    Returns (eligible: bool, reason: str).
    """
    gid = goal["goal_id"]
    eligibility = goal.get("eligibility", {})
    sub_status = state.get_fact_value("subscription_status", "never")
    
    user_events = pack.events_by_user.get(user_id, [])

    # ── G01: first_play_24h ──────────────────────────────────────────
    if gid == "G01":
        signup_str = state.get_fact_value("signup_date")
        if not signup_str:
            return False, "No signup date found"
        signup_dt = _ts(signup_str)
        days_since = (trigger_time - signup_dt).total_seconds() / 86400
        if days_since > eligibility.get("signed_up_within_days", 7):
            return False, f"Signed up {days_since:.1f} days ago (>7)"
        has_play = any(e["event"] == "play_started" for e in user_events)
        if has_play:
            return False, "Already has play_started event"
        return True, "New user within 7 days, no prior play"

    # ── G02: first_full_episode_3d ───────────────────────────────────
    if gid == "G02":
        has_complete = any(e["event"] == "episode_completed" for e in user_events)
        if has_complete:
            return False, "Already has episode_completed event"
        return True, "No prior episode_completed"

    # ── G03: second_day_return_2d ────────────────────────────────────
    if gid == "G03":
        signup_str = state.get_fact_value("signup_date")
        if not signup_str:
            return False, "No signup date found"
        signup_dt = _ts(signup_str)
        days_since = (trigger_time - signup_dt).total_seconds() / 86400
        if days_since > eligibility.get("signed_up_within_days", 2):
            return False, f"Signed up {days_since:.1f} days ago (>2)"
        return True, f"Signed up {days_since:.1f} days ago (<=2)"

    # ── G04: trial_to_paid_7d ────────────────────────────────────────
    if gid == "G04":
        required = eligibility.get("subscription_status", [])
        if sub_status not in required:
            return False, f"Subscription is '{sub_status}', need one of {required}"
        return True, f"Subscription is '{sub_status}'"

    # ── G05: first_paid_7d ───────────────────────────────────────────
    if gid == "G05":
        required = eligibility.get("subscription_status", [])
        if sub_status not in required:
            return False, f"Subscription is '{sub_status}', need one of {required}"
        # Also check no prior paid purchase
        has_paid = any(
            e["event"] == "purchase_completed"
            and not _is_trial_purchase(e)
            for e in user_events
        )
        if has_paid:
            return False, "Already has a non-trial purchase"
        return True, "Never paid subscriber"

    # ── G06: reactivate_lapsed_14d ───────────────────────────────────
    if gid == "G06":
        required = eligibility.get("subscription_status", [])
        if sub_status not in required:
            return False, f"Subscription is '{sub_status}', need one of {required}"
        return True, f"Subscription is '{sub_status}'"

    # ── G07: resume_series_3d ────────────────────────────────────────
    if gid == "G07":
        # Find series with unwatched episodes that user hasn't played for 3+ days
        play_events = [e for e in user_events if e["event"] == "play_started"]
        if not play_events:
            return False, "No play_started events"
        
        # Group by content_slug
        slug_last_play: dict[str, datetime] = {}
        for e in play_events:
            tid = e.get("properties", {}).get("title_id", "")
            cat_entry = pack.catalogue.get(tid)
            if not cat_entry:
                continue
            slug = cat_entry["content_slug"]
            rcv = e["_received_at"]
            if slug not in slug_last_play or rcv > slug_last_play[slug]:
                slug_last_play[slug] = rcv
        
        # Check each slug for unwatched episodes and 3+ day gap
        for slug, last_play in slug_last_play.items():
            days_since = (trigger_time - last_play).total_seconds() / 86400
            if days_since < 3:
                continue
            # Check if there are unwatched episodes
            eps = pack.episodes.get(slug, [])
            completed_eps = set()
            for e in user_events:
                if e["event"] == "episode_completed":
                    ep_id = e.get("properties", {}).get("episode_id", "")
                    if ep_id and pack.episodes_flat.get(ep_id, {}).get("content_slug") == slug:
                        completed_eps.add(ep_id)
            unwatched = [ep for ep in eps if ep["episode_id"] not in completed_eps]
            if unwatched:
                # Store the target slug in state for intervention planning
                state.set_fact("resume_target_slug", slug,
                               "goal_planner", observed_at=str(trigger_time))
                return True, f"Series '{slug}' not played for {days_since:.1f} days, {len(unwatched)} unwatched episodes"
        
        return False, "No unfinished series idle for 3+ days"

    # ── G08: try_new_release_7d ──────────────────────────────────────
    if gid == "G08":
        required = eligibility.get("subscription_status", [])
        if sub_status not in required:
            return False, f"Subscription is '{sub_status}', need one of {required}"
        # Check if there are new releases in the last 30 days
        dialect = state.get_fact_value("dialect", "")
        cutoff = trigger_time - timedelta(days=30)
        new_releases = [
            t for t in pack.catalogue.values()
            if t.get("dialect") == dialect
            and t.get("_available_from") and t["_available_from"] >= cutoff
            and (t.get("_available_until") is None or t["_available_until"] > trigger_time)
        ]
        if not new_releases:
            return False, "No new releases in dialect in last 30 days"
        return True, f"Active subscriber, {len(new_releases)} new releases available"

    # ── G09: weekly_habit_14d ────────────────────────────────────────
    if gid == "G09":
        required = eligibility.get("subscription_status", [])
        if sub_status not in required:
            return False, f"Subscription is '{sub_status}', need one of {required}"
        # Count distinct IST play days in last 14 days
        trigger_ist_date = _ist_date(trigger_time)
        cutoff_date = trigger_ist_date - timedelta(days=14)
        play_days = set()
        for e in user_events:
            if e["event"] == "play_started":
                d = _ist_date(e["_received_at"])
                if cutoff_date <= d <= trigger_ist_date:
                    play_days.add(d)
        if len(play_days) > 1:
            return False, f"Plays on {len(play_days)} days in last 14 (>1)"
        return True, f"Plays on {len(play_days)} day(s) in last 14 days (<=1)"

    # ── G12: return_after_absence_7d ─────────────────────────────────
    if gid == "G12":
        # No app_open in 14 days before trigger
        cutoff = trigger_time - timedelta(days=14)
        recent_opens = [
            e for e in user_events
            if e["event"] == "app_open"
            and e["_received_at"] >= cutoff
        ]
        if recent_opens:
            return False, f"{len(recent_opens)} app_open(s) in last 14 days"
        return True, "No app_open in 14 days before trigger"

    return False, f"Unknown goal {gid}"


def _is_trial_purchase(event: dict) -> bool:
    """Check if a purchase event is a trial."""
    is_trial = event.get("properties", {}).get("is_trial", False)
    if isinstance(is_trial, str):
        return is_trial.lower() == "true"
    return bool(is_trial)


def select_goal(
    user_id: str,
    pack: DataPack,
    state: ViewerState,
    trigger_time: datetime,
    goal_rules_version: str = "2026-09-30",
    corrected_goals: dict | None = None,
) -> dict | None:
    """Select exactly one goal for the viewer.

    Returns a goal dict with:
      goal_id, name, deadline, success_signal, success_event,
      eligibility_reason, assumptions, skipped_goals, rule_version

    Returns None if no goal is eligible → caller should produce NO_ACTION.
    """
    goals = pack.goals
    skipped = {}
    eligible_goals = []
    assumptions_used = []

    for goal in goals:
        gid = goal["goal_id"]

        # Skip permanently-excluded goals
        if gid in SKIPPED_GOALS:
            skipped[gid] = SKIPPED_GOALS[gid]
            continue

        # Apply corrected rules if any
        active_goal = dict(goal)
        if corrected_goals and gid in corrected_goals:
            active_goal["success"] = corrected_goals[gid]["new_success"]

        # Check eligibility
        is_eligible, reason = _check_eligibility(
            active_goal, user_id, pack, state, trigger_time
        )

        if is_eligible:
            eligible_goals.append((gid, active_goal, reason))
            if gid in ASSUMPTIONS:
                assumptions_used.append(ASSUMPTIONS[gid])

    if not eligible_goals:
        return None

    # Select by priority
    for priority_gid in GOAL_PRIORITY:
        for gid, goal, reason in eligible_goals:
            if gid == priority_gid:
                success = goal.get("success", {})
                window_hours = success.get("window_hours", 168)
                deadline = trigger_time + timedelta(hours=window_hours)

                return {
                    "goal_id": gid,
                    "name": goal.get("name", ""),
                    "description": goal.get("description", ""),
                    "deadline": deadline.isoformat(),
                    "window_hours": window_hours,
                    "success_signal": {
                        "event": success.get("event"),
                        "condition": success.get("condition"),
                    },
                    "eligibility_reason": reason,
                    "assumptions": assumptions_used,
                    "skipped_goals": skipped,
                    "rule_version": goal_rules_version,
                    "started_at": trigger_time.isoformat(),
                }

    # Fallback: first eligible
    gid, goal, reason = eligible_goals[0]
    success = goal.get("success", {})
    window_hours = success.get("window_hours", 168)
    deadline = trigger_time + timedelta(hours=window_hours)
    return {
        "goal_id": gid,
        "name": goal.get("name", ""),
        "description": goal.get("description", ""),
        "deadline": deadline.isoformat(),
        "window_hours": window_hours,
        "success_signal": {
            "event": success.get("event"),
            "condition": success.get("condition"),
        },
        "eligibility_reason": reason,
        "assumptions": assumptions_used,
        "skipped_goals": skipped,
        "rule_version": goal_rules_version,
        "started_at": trigger_time.isoformat(),
    }


def should_replan(
    current_goal: dict | None,
    event: dict,
    state: ViewerState,
) -> bool:
    """Check if a new event should trigger re-planning.

    Re-plan when:
    - A purchase cancels a first_paid (G05) or trial_to_paid (G04) goal.
    - Subscription lapses during an active goal.
    """
    if not current_goal:
        return False

    gid = current_goal.get("goal_id")
    ev_type = event["event"]

    if ev_type == "purchase_completed" and gid in ("G04", "G05"):
        # Goal succeeded → need to re-evaluate
        return True

    if ev_type == "subscription_lapsed" and gid in ("G04", "G08", "G09"):
        # No longer eligible
        return True

    return False
