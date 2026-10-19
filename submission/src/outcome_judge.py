"""
Outcome Judge – Step 10

After the goal deadline or on new events, judge each goal as:
  succeeded / failed / inconclusive
using only events inside the goal window.

Writes learnings to viewer state and outcomes.jsonl.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from . import config as cfg
from .loader import DataPack, _ts


def _ist_date(dt: datetime) -> Any:
    """Convert UTC datetime to IST date."""
    return dt.astimezone(cfg.IST).date()


def judge_goal(
    user_id: str,
    goal: dict,
    pack: DataPack,
    all_events: list[dict],
    corrected_goals: dict | None = None,
) -> dict:
    """Judge whether a goal succeeded, failed, or is inconclusive.

    Args:
        user_id: The viewer's ID
        goal: The goal dict (from goal_planner.select_goal)
        pack: The loaded data pack
        all_events: All events for this user (historical + live + simulated)
        corrected_goals: Any corrected goal rules from control events

    Returns:
        outcome dict with: user_id, goal_id, status, evidence, judged_at, learning
    """
    gid = goal.get("goal_id", "")
    started_at = _ts(goal.get("started_at"))
    deadline = _ts(goal.get("deadline"))

    if not started_at or not deadline:
        return _make_outcome(user_id, goal, "inconclusive",
                             "Missing started_at or deadline", [])

    # Get the success signal (apply corrections if any)
    success = goal.get("success_signal", {})
    if corrected_goals and gid in corrected_goals:
        success = corrected_goals[gid].get("new_success", success)

    success_event = success.get("event")
    success_condition = success.get("condition", "")

    if not success_event:
        return _make_outcome(user_id, goal, "inconclusive",
                             f"Goal {gid} has no concrete success event", [])

    # Filter events inside the goal window
    window_events = [
        e for e in all_events
        if e.get("user_id") == user_id
        and _ts(e.get("received_at")) is not None
        and started_at <= _ts(e["received_at"]) <= deadline
    ]

    # Check for success based on goal type
    evidence = []

    if gid == "G01":  # first_play_24h
        plays = [e for e in window_events if e["event"] == "play_started"]
        if plays:
            evidence = [{"event_id": plays[0]["event_id"], "event": "play_started",
                         "at": plays[0]["received_at"]}]
            return _make_outcome(user_id, goal, "succeeded",
                                 "User started a play within 24h", evidence)

    elif gid == "G02":  # first_full_episode_3d
        completions = [e for e in window_events if e["event"] == "episode_completed"]
        if completions:
            evidence = [{"event_id": completions[0]["event_id"], "event": "episode_completed",
                         "at": completions[0]["received_at"]}]
            return _make_outcome(user_id, goal, "succeeded",
                                 "User completed an episode within 72h", evidence)

    elif gid == "G03":  # second_day_return_2d
        # Check for play_started (corrected rule) or app_open (original) on a later IST day
        check_event = success_event  # may be corrected to play_started
        goal_start_date = _ist_date(started_at)
        matching = [
            e for e in window_events
            if e["event"] == check_event
            and _ist_date(_ts(e["received_at"])) > goal_start_date
        ]
        if matching:
            evidence = [{"event_id": matching[0]["event_id"], "event": check_event,
                         "at": matching[0]["received_at"]}]
            return _make_outcome(user_id, goal, "succeeded",
                                 f"User had {check_event} on a later IST day", evidence)

    elif gid == "G04":  # trial_to_paid_7d
        purchases = [
            e for e in window_events
            if e["event"] == "purchase_completed"
            and not _is_trial(e)
        ]
        if purchases:
            evidence = [{"event_id": purchases[0]["event_id"], "event": "purchase_completed",
                         "at": purchases[0]["received_at"]}]
            return _make_outcome(user_id, goal, "succeeded",
                                 "User converted trial to paid plan", evidence)

    elif gid == "G05":  # first_paid_7d
        purchases = [
            e for e in window_events
            if e["event"] == "purchase_completed"
            and not _is_trial(e)
        ]
        if purchases:
            evidence = [{"event_id": purchases[0]["event_id"], "event": "purchase_completed",
                         "at": purchases[0]["received_at"]}]
            return _make_outcome(user_id, goal, "succeeded",
                                 "User made first paid purchase", evidence)

    elif gid == "G06":  # reactivate_lapsed_14d
        purchases = [
            e for e in window_events
            if e["event"] == "purchase_completed"
        ]
        if purchases:
            evidence = [{"event_id": purchases[0]["event_id"], "event": "purchase_completed",
                         "at": purchases[0]["received_at"]}]
            return _make_outcome(user_id, goal, "succeeded",
                                 "Lapsed user purchased again", evidence)

    elif gid == "G07":  # resume_series_3d
        target_slug = goal.get("_resume_target_slug")
        plays = [e for e in window_events if e["event"] == "play_started"]
        for p in plays:
            tid = p.get("properties", {}).get("title_id", "")
            cat = pack.catalogue.get(tid)
            if cat and (not target_slug or cat["content_slug"] == target_slug):
                evidence = [{"event_id": p["event_id"], "event": "play_started",
                             "title_id": tid, "at": p["received_at"]}]
                return _make_outcome(user_id, goal, "succeeded",
                                     "User resumed the series", evidence)

    elif gid == "G08":  # try_new_release_7d
        cutoff = started_at - timedelta(days=30)
        plays = [e for e in window_events if e["event"] == "play_started"]
        for p in plays:
            tid = p.get("properties", {}).get("title_id", "")
            cat = pack.catalogue.get(tid)
            if cat and cat.get("_available_from") and cat["_available_from"] >= cutoff:
                evidence = [{"event_id": p["event_id"], "event": "play_started",
                             "title_id": tid, "at": p["received_at"]}]
                return _make_outcome(user_id, goal, "succeeded",
                                     "User played a new release", evidence)

    elif gid == "G09":  # weekly_habit_14d
        plays = [e for e in window_events if e["event"] == "play_started"]
        play_days = set()
        for p in plays:
            play_days.add(_ist_date(_ts(p["received_at"])))
        if len(play_days) >= 3:
            evidence = [{"distinct_play_days": len(play_days),
                         "dates": sorted([str(d) for d in play_days])}]
            return _make_outcome(user_id, goal, "succeeded",
                                 f"User played on {len(play_days)} distinct IST days (>=3)", evidence)

    elif gid == "G12":  # return_after_absence_7d
        plays = [e for e in window_events if e["event"] == "play_started"]
        if plays:
            evidence = [{"event_id": plays[0]["event_id"], "event": "play_started",
                         "at": plays[0]["received_at"]}]
            return _make_outcome(user_id, goal, "succeeded",
                                 "Absent user played again", evidence)

    # No success event found → failed (if past deadline) or inconclusive
    now = datetime.now(cfg.UTC)
    if now >= deadline or (window_events and _ts(window_events[-1]["received_at"]) >= deadline):
        return _make_outcome(user_id, goal, "failed",
                             f"No {success_event} event found in goal window", [])
    else:
        return _make_outcome(user_id, goal, "inconclusive",
                             "Goal window still open", [])


def _is_trial(event: dict) -> bool:
    is_trial = event.get("properties", {}).get("is_trial", False)
    if isinstance(is_trial, str):
        return is_trial.lower() == "true"
    return bool(is_trial)


def _make_outcome(user_id: str, goal: dict, status: str,
                  reason: str, evidence: list) -> dict:
    return {
        "user_id": user_id,
        "goal_id": goal.get("goal_id", ""),
        "goal_name": goal.get("name", ""),
        "status": status,
        "reason": reason,
        "evidence": evidence,
        "goal_started_at": goal.get("started_at"),
        "goal_deadline": goal.get("deadline"),
        "judged_at": datetime.now(cfg.UTC).isoformat(),
        "learning": _extract_learning(goal, status, reason),
    }


def _extract_learning(goal: dict, status: str, reason: str) -> dict:
    """Extract a learning from the outcome for the viewer state."""
    return {
        "goal_id": goal.get("goal_id", ""),
        "status": status,
        "reason": reason,
        "window_hours": goal.get("window_hours"),
    }
