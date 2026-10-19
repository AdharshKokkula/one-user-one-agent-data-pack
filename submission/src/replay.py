"""
Replay Harness + CLI – Step 8

Processes triggers in deliver_at order, interleaves live and control events,
handles duplicate triggers idempotently, skips/downgrades stale triggers,
and produces the full sample_run output.

CLI: python -m src.cli replay --pack <path> --out sample_run/ --mock
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from . import config as cfg
from .loader import load_pack, _ts, DataPack, title_available_at
from .viewer_state import ViewerState
from .fact_resolver import resolve_all_facts, update_facts_from_event
from .goal_planner import select_goal, should_replan, SKIPPED_GOALS, ASSUMPTIONS
from .intervention_planner import build_home_page, plan_message, _ist_date_str, _is_quiet_hours
from .model_provider import MockProvider, CallBudget, call_with_retries
from .verifier import verify_decision
from .outcome_judge import judge_goal


def _make_no_action_decision(
    user_id: str,
    trigger: dict,
    reason: str,
    goal: dict | None = None,
    budget: CallBudget | None = None,
    fallback_used: bool = False,
    rule_version: str = "2026-09-30",
    conflicts: list | None = None,
    decided_in_ms: int = 0,
) -> dict:
    """Create a NO_ACTION decision."""
    return {
        "user_id": user_id,
        "trigger_event_id": trigger["event_id"],
        "action_type": "NO_ACTION",
        "goal": goal,
        "actions": [],
        "no_action_reason": reason,
        "confidence_reason": "Deterministic rule-based decision",
        "fallback_used": fallback_used,
        "model_calls": budget.model_calls_used if budget else 0,
        "tool_calls": budget.tool_calls_used if budget else 0,
        "rule_version": rule_version,
        "success_signal": goal.get("success_signal") if goal else None,
        "conflicts": conflicts or [],
        "decided_in_ms": decided_in_ms,
        "published_at": trigger.get("deliver_at"),
        "verification": {"passed": True, "status": "VERIFIED", "violations": [], "warnings": []},
    }


def _make_decision(
    user_id: str,
    trigger: dict,
    goal: dict | None,
    actions: list[dict],
    budget: CallBudget,
    fallback_used: bool,
    rule_version: str,
    conflicts: list,
    decided_in_ms: int,
    verification: dict,
) -> dict:
    """Create a full decision dict."""
    action_type = "NO_ACTION"
    if actions:
        types = [a["type"] for a in actions]
        if "home_page" in types:
            action_type = "HOME_PAGE"
        if any(t in types for t in ("push", "whatsapp", "in_app_message")):
            msg_type = next(t for t in types if t in ("push", "whatsapp", "in_app_message"))
            action_type = f"HOME_PAGE+{msg_type.upper()}" if "home_page" in types else msg_type.upper()

    return {
        "user_id": user_id,
        "trigger_event_id": trigger["event_id"],
        "action_type": action_type,
        "goal": goal,
        "actions": actions,
        "no_action_reason": None if actions else "No viable intervention",
        "confidence_reason": "Deterministic rule-based decision",
        "fallback_used": fallback_used,
        "model_calls": budget.model_calls_used,
        "tool_calls": budget.tool_calls_used,
        "rule_version": rule_version,
        "success_signal": goal.get("success_signal") if goal else None,
        "conflicts": conflicts,
        "decided_in_ms": decided_in_ms,
        "published_at": trigger.get("deliver_at"),
        "verification": verification,
    }


def run_replay(pack_root: str, out_dir: str, use_mock: bool = True) -> dict:
    """Run the full replay and produce all outputs.

    Returns a summary dict.
    """
    start_time = time.monotonic()
    print(f"Loading data pack from {pack_root}...")
    pack = load_pack(pack_root)
    print(f"  Loaded {len(pack.events)} events, {len(pack.triggers)} triggers, "
          f"{len(pack.profiles)} profiles, {len(pack.catalogue)} titles")
    print(f"  Data issues found: {len(pack.data_issues)}")

    os.makedirs(out_dir, exist_ok=True)
    state_dir = os.path.join(out_dir, "viewer_state")
    os.makedirs(state_dir, exist_ok=True)

    # Initialize model provider
    provider = MockProvider(pack.model_faults) if use_mock else MockProvider(pack.model_faults)
    provider.set_control_events(pack.control_events)

    # Build the unified timeline: triggers + live_events + control_events
    timeline: list[dict] = []
    for t in pack.triggers:
        timeline.append({"_sort_key": t["deliver_at"], "_type": "trigger", "_data": t})
    for e in pack.live_events:
        timeline.append({"_sort_key": e.get("deliver_at", e["received_at"]), "_type": "live_event", "_data": e})
    for c in pack.control_events:
        timeline.append({"_sort_key": c["at"], "_type": "control_event", "_data": c})

    timeline.sort(key=lambda x: x["_sort_key"])

    # Track state
    viewer_states: dict[str, ViewerState] = {}
    decisions: list[dict] = []
    all_actions: list[dict] = []
    seen_trigger_ids: set[str] = set()
    messages_sent: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    latencies: list[float] = []
    corrected_goals: dict[str, dict] = {}  # goal_id → correction
    goal_rule_version = "2026-09-30"
    withdrawn_at_time: dict[str, datetime] = dict(pack.withdrawn_titles)  # start with known withdrawals

    stats = {
        "total_triggers": 0,
        "decisions_made": 0,
        "holdout_skipped": 0,
        "stale_skipped": 0,
        "duplicate_skipped": 0,
        "no_action": 0,
        "home_page": 0,
        "push": 0,
        "whatsapp": 0,
        "in_app": 0,
        "verified_pass": 0,
        "verified_reject": 0,
        "fallback_used": 0,
        "model_faults_replayed": 0,
        "goals_assigned": 0,
        "control_events_processed": 0,
        "live_events_processed": 0,
    }

    print(f"\nProcessing {len(timeline)} timeline events...")

    for idx, entry in enumerate(timeline):
        entry_type = entry["_type"]
        data = entry["_data"]

        # ── Control events ───────────────────────────────────────────
        if entry_type == "control_event":
            stats["control_events_processed"] += 1
            _handle_control_event(data, corrected_goals, withdrawn_at_time,
                                  viewer_states, pack, all_actions)
            if data.get("type") == "goal_rule_corrected":
                goal_rule_version = f"corrected-{data.get('goal_id')}-{data['at']}"
            continue

        # ── Live events (interleave) ─────────────────────────────────
        if entry_type == "live_event":
            stats["live_events_processed"] += 1
            user_id = data["user_id"]
            state = viewer_states.get(user_id)
            if state:
                changed = update_facts_from_event(user_id, data, pack, state)
                if changed and should_replan(state.get_goal(), data, state):
                    # Cancel pending message actions
                    for a in state.get_pending_actions():
                        if a.get("type") in ("push", "whatsapp"):
                            state.cancel_action(a["action_id"],
                                                f"Re-plan triggered by {data['event']}")
            continue

        # ── Trigger processing ───────────────────────────────────────
        trigger = data
        stats["total_triggers"] += 1
        user_id = trigger["user_id"]
        trigger_id = trigger["event_id"]
        trigger_time = trigger["_deliver_at"] or trigger["_received_at"]

        # Set simulation time for model
        provider.set_current_time(trigger_time)
        provider.set_trigger(trigger_id)

        decision_start = time.monotonic()

        # ── Duplicate check ──────────────────────────────────────────
        if trigger_id in seen_trigger_ids:
            stats["duplicate_skipped"] += 1
            continue

        # ── Holdout check ────────────────────────────────────────────
        arm = pack.arms.get(user_id, "agent")
        if arm == "holdout":
            stats["holdout_skipped"] += 1
            seen_trigger_ids.add(trigger_id)
            decision = _make_no_action_decision(
                user_id, trigger, "Holdout arm: no action allowed",
                decided_in_ms=int((time.monotonic() - decision_start) * 1000),
            )
            decisions.append(decision)
            continue

        # ── Stale trigger check ──────────────────────────────────────
        if trigger.get("_is_stale"):
            stats["stale_skipped"] += 1
            seen_trigger_ids.add(trigger_id)
            age = trigger.get("event_age_s", 0)
            decision = _make_no_action_decision(
                user_id, trigger,
                f"Stale trigger: received_at - occurred_at = {age:.0f}s > {cfg.STALE_TRIGGER_THRESHOLD_S}s",
                decided_in_ms=int((time.monotonic() - decision_start) * 1000),
            )
            decisions.append(decision)
            continue

        seen_trigger_ids.add(trigger_id)

        # ── Get or create viewer state ───────────────────────────────
        if user_id not in viewer_states:
            viewer_states[user_id] = ViewerState(user_id, state_dir)
        state = viewer_states[user_id]

        # ── Resolve facts ────────────────────────────────────────────
        resolve_all_facts(user_id, pack, state, trigger)

        # ── Initialize call budget ───────────────────────────────────
        budget = CallBudget()
        fallback_used = False

        # ── Try model call (optional, for ranking) ───────────────────
        model_result = None
        if budget.can_call_model():
            model_result = call_with_retries(
                provider,
                f"Select goal and intervention for {user_id}",
                budget,
                context={"user_id": user_id, "trigger": trigger_id},
            )
            if model_result and model_result.fault_type:
                stats["model_faults_replayed"] += 1
                if model_result.fault_type == "unsupported_claim":
                    # Model claim is not evidence → fall back to rules
                    fallback_used = True

        # ── Select goal ──────────────────────────────────────────────
        goal = select_goal(
            user_id, pack, state, trigger_time,
            goal_rules_version=goal_rule_version,
            corrected_goals=corrected_goals if corrected_goals else None,
        )

        if goal is None:
            stats["no_action"] += 1
            decided_ms = int((time.monotonic() - decision_start) * 1000)
            latencies.append(decided_ms)
            decision = _make_no_action_decision(
                user_id, trigger,
                "No eligible goal found",
                budget=budget,
                fallback_used=fallback_used,
                rule_version=goal_rule_version,
                conflicts=state.get_conflicts(),
                decided_in_ms=decided_ms,
            )
            decisions.append(decision)
            state.add_decision(trigger_id, decision)
            continue

        state.set_goal(goal)
        stats["goals_assigned"] += 1

        # ── Plan interventions ───────────────────────────────────────
        actions: list[dict] = []

        # Home page
        home_page = build_home_page(user_id, pack, state, trigger_time, goal)
        if home_page:
            actions.append(home_page)
            stats["home_page"] += 1

        # Message
        message = plan_message(user_id, pack, state, trigger_time, goal)
        if message:
            msg_type = message.get("type", "")
            actions.append(message)
            if msg_type == "push":
                stats["push"] += 1
            elif msg_type == "whatsapp":
                stats["whatsapp"] += 1
            elif msg_type == "in_app_message":
                stats["in_app"] += 1

        if not actions:
            stats["no_action"] += 1
            fallback_used = True

        # ── Verify decision ──────────────────────────────────────────
        decided_ms = int((time.monotonic() - decision_start) * 1000)
        latencies.append(decided_ms)

        # published_at = deliver_at + measured decision time
        published_at_dt = trigger_time + timedelta(milliseconds=decided_ms) if trigger_time else None
        published_at_str = published_at_dt.isoformat() if published_at_dt else trigger.get("deliver_at")

        decision = _make_decision(
            user_id, trigger, goal, actions, budget, fallback_used,
            goal_rule_version, state.get_conflicts(), decided_ms,
            verification={"passed": True, "status": "VERIFIED", "violations": [], "warnings": []},
        )
        decision["published_at"] = published_at_str

        # Run verifier
        verify_result = verify_decision(
            decision,
            pack.arms,
            pack.profiles,
            pack.catalogue,
            pack.events_by_user,
            seen_trigger_ids - {trigger_id},  # exclude current
            dict(messages_sent),
            withdrawn_at_time,
            pack.model_faults,
        )

        decision["verification"] = verify_result.to_dict()

        if not verify_result.passed:
            stats["verified_reject"] += 1
            # Downgrade to NO_ACTION
            decision["action_type"] = "NO_ACTION"
            decision["actions"] = []
            decision["no_action_reason"] = f"Rejected by verifier: {verify_result.violations}"
            decision["verification"]["status"] = "REJECTED_BY_VERIFIER"
            stats["no_action"] += 1
            # Reverse action counts
            if home_page:
                stats["home_page"] -= 1
            if message:
                msg_type = message.get("type", "")
                if msg_type in stats:
                    stats[msg_type] -= 1
        else:
            stats["verified_pass"] += 1
            # Record actions and track message cap AFTER verification passes
            for a in actions:
                state.add_action(a)
                all_actions.append(a)
            # Track daily cap for push/WhatsApp (only after verified)
            if message:
                msg_type = message.get("type", "")
                if msg_type in ("push", "whatsapp"):
                    send_at_str = message.get("send_at") or message.get("published_at")
                    if send_at_str:
                        send_at_dt = _ts(send_at_str)
                        ist_date = _ist_date_str(send_at_dt)
                        messages_sent[user_id][ist_date] += 1
                        state.record_message_sent(ist_date)

        decisions.append(decision)
        state.add_decision(trigger_id, decision)
        stats["decisions_made"] += 1

        if idx % 500 == 0 and idx > 0:
            print(f"  Processed {idx}/{len(timeline)} events...")

    # ── Save viewer states ───────────────────────────────────────────
    print(f"\nSaving {len(viewer_states)} viewer states...")
    for state in viewer_states.values():
        state.save()

    # ── Write outputs ────────────────────────────────────────────────
    print("Writing output files...")

    # decisions.jsonl
    with open(os.path.join(out_dir, "decisions.jsonl"), "w", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")

    # actions.jsonl (for simulator)
    with open(os.path.join(out_dir, "actions.jsonl"), "w", encoding="utf-8") as f:
        for a in all_actions:
            f.write(json.dumps(a, ensure_ascii=False, default=str) + "\n")

    # latency_report.json
    latency_report = _compute_latency_report(latencies)
    with open(os.path.join(out_dir, "latency_report.json"), "w", encoding="utf-8") as f:
        json.dump(latency_report, f, indent=2)

    # data_issues report
    data_issues_report = []
    for issue in pack.data_issues:
        data_issues_report.append({
            "category": issue.category,
            "detail": issue.detail,
            "count": issue.count,
            "examples": issue.examples[:5],
        })
    with open(os.path.join(out_dir, "data_issues.json"), "w", encoding="utf-8") as f:
        json.dump(data_issues_report, f, indent=2, ensure_ascii=False)

    elapsed = time.monotonic() - start_time
    stats["total_time_s"] = round(elapsed, 2)

    print(f"\nReplay complete in {elapsed:.1f}s")
    print(f"  Decisions: {stats['decisions_made']}")
    print(f"  Holdout skipped: {stats['holdout_skipped']}")
    print(f"  Stale skipped: {stats['stale_skipped']}")
    print(f"  Duplicate skipped: {stats['duplicate_skipped']}")
    print(f"  Home pages: {stats['home_page']}")
    print(f"  Push: {stats['push']}, WhatsApp: {stats['whatsapp']}, In-app: {stats['in_app']}")
    print(f"  NO_ACTION: {stats['no_action']}")
    print(f"  Verified pass: {stats['verified_pass']}, reject: {stats['verified_reject']}")

    return stats


def _handle_control_event(
    event: dict,
    corrected_goals: dict,
    withdrawn_at_time: dict,
    viewer_states: dict[str, ViewerState],
    pack: DataPack,
    all_actions: list[dict],
) -> None:
    """Handle a control event (title_withdrawn, goal_rule_corrected, etc.)."""
    etype = event.get("type")

    if etype == "title_withdrawn":
        tid = event.get("title_id")
        at = event.get("_at")
        if tid and at:
            withdrawn_at_time[tid] = at
            # Check pending actions that use this title
            for a in all_actions:
                if a.get("status") == "pending" and a.get("title_id") == tid:
                    a["status"] = "cancelled"
                    a["cancel_reason"] = f"Title {tid} withdrawn at {at}"
            # Also update the catalogue entry
            cat = pack.catalogue.get(tid)
            if cat:
                if cat.get("_available_until") is None or at < cat["_available_until"]:
                    cat["_available_until"] = at

    elif etype == "goal_rule_corrected":
        gid = event.get("goal_id")
        if gid:
            corrected_goals[gid] = {
                "new_success": event.get("new_success", {}),
                "old_success": event.get("old_success", {}),
                "corrected_at": event.get("at"),
            }
            # Re-judge affected open goals
            for uid, state in viewer_states.items():
                goal = state.get_goal()
                if goal and goal.get("goal_id") == gid:
                    goal["success_signal"] = event.get("new_success", {})
                    goal["rule_version"] = f"corrected-{gid}-{event['at']}"


def _compute_latency_report(latencies: list[float]) -> dict:
    """Compute p50, p95, max latency report."""
    if not latencies:
        return {"p50_ms": 0, "p95_ms": 0, "max_ms": 0, "count": 0, "over_60s": 0}

    sorted_lat = sorted(latencies)
    n = len(sorted_lat)
    p50 = sorted_lat[int(n * 0.5)]
    p95 = sorted_lat[int(n * 0.95)]
    max_lat = sorted_lat[-1]
    over_60s = sum(1 for l in latencies if l > 60000)

    return {
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
        "max_ms": round(max_lat, 1),
        "count": n,
        "over_60s": over_60s,
    }


def inspect_decisions(decisions_path: str, user_id: str = None, raw: bool = False) -> None:
    """Inspect decisions from replay output for a specific user or show summary."""
    if not os.path.exists(decisions_path):
        print(f"Error: File not found: {decisions_path}")
        return

    decisions = []
    with open(decisions_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    decisions.append(json.loads(line))
                except Exception:
                    continue

    if not user_id:
        # Show overview
        unique_users = set(d.get("user_id") for d in decisions)
        action_counts = defaultdict(int)
        verif_counts = defaultdict(int)
        for d in decisions:
            action_counts[d.get("action_type", "UNKNOWN")] += 1
            v = d.get("verification", {}).get("status", "UNKNOWN")
            verif_counts[v] += 1

        print(f"\n--- Decisions File Overview ({decisions_path}) ---")
        print(f"Total decisions: {len(decisions)}")
        print(f"Unique users:    {len(unique_users)}")
        print(f"Action types:    {dict(action_counts)}")
        print(f"Verifications:   {dict(verif_counts)}")
        print("\nPass --user <user_id> to inspect specific user decisions.")
        return

    # Filter for user_id
    user_decs = [d for d in decisions if d.get("user_id") == user_id]
    if not user_decs:
        print(f"No decisions found for user '{user_id}' in {decisions_path}.")
        return

    if raw:
        for d in user_decs:
            print(json.dumps(d, indent=2, ensure_ascii=False))
        return

    print(f"\n=======================================================")
    print(f" Decisions for User: {user_id} (Total: {len(user_decs)})")
    print(f"=======================================================")

    for i, d in enumerate(user_decs, 1):
        print(f"\n--- [Decision {i}/{len(user_decs)}] ---")
        print(f"Trigger Event ID: {d.get('trigger_event_id')}")
        print(f"Action Type:      {d.get('action_type')}")
        print(f"Published At:     {d.get('published_at')}")
        print(f"Latency:          {d.get('decided_in_ms')} ms")
        print(f"Fallback Used:    {d.get('fallback_used')}")

        # Goal
        goal = d.get("goal")
        if goal:
            print(f"Goal:             [{goal.get('goal_id')}] {goal.get('name')} (Deadline: {goal.get('deadline')})")
            if goal.get("eligibility_reason"):
                print(f"  Eligibility:    {goal.get('eligibility_reason')}")
        else:
            print(f"Goal:             None")

        # Actions
        actions = d.get("actions", [])
        if actions:
            print(f"Actions ({len(actions)}):")
            for a in actions:
                atype = a.get("type")
                if atype == "home_page":
                    hero = a.get("hero_title_id")
                    dialect = a.get("dialect")
                    rows = a.get("rows", [])
                    print(f"  * HOME_PAGE (Dialect: {dialect}, Hero: {hero}, Rows: {len(rows)})")
                    for r in rows:
                        rtype = r.get("row_type")
                        tids = r.get("title_ids", [])
                        extra = f" [{r.get('genre')}]" if r.get("genre") else ""
                        print(f"      - {rtype}{extra}: {len(tids)} titles ({', '.join(tids[:5])}...)")
                elif atype in ("push", "whatsapp", "in_app"):
                    print(f"  * {atype.upper()} -> Title: '{a.get('title')}' | Body: '{a.get('body')}'")
        else:
            print(f"Actions:          None (Reason: {d.get('no_action_reason')})")

        # Verification
        verif = d.get("verification", {})
        vstatus = verif.get("status")
        vpassed = verif.get("passed")
        print(f"Verification:     {vstatus} (Passed: {vpassed})")
        if verif.get("violations"):
            print(f"  Violations:     {verif.get('violations')}")
        if verif.get("warnings"):
            print(f"  Warnings:       {verif.get('warnings')}")

        # Conflicts
        confs = d.get("conflicts", [])
        if confs:
            print(f"Fact Conflicts:   {len(confs)} conflict(s) resolved")
            for c in confs[:3]:
                print(f"  * Fact '{c.get('fact')}': resolved='{c.get('resolved_value')}' (Source: {c.get('source')})")
            if len(confs) > 3:
                print(f"    ... and {len(confs) - 3} more")


def main():
    parser = argparse.ArgumentParser(
        description="One User, One Agent – Replay Harness",
        prog="python -m src.cli",
    )
    sub = parser.add_subparsers(dest="command")

    replay_parser = sub.add_parser("replay", help="Run the replay")
    replay_parser.add_argument("--pack", required=True, help="Path to data pack root")
    replay_parser.add_argument("--out", required=True, help="Output directory")
    replay_parser.add_argument("--mock", action="store_true", default=True,
                               help="Use mock model provider (default)")

    inspect_parser = sub.add_parser("inspect", help="Inspect decisions from replay output")
    inspect_parser.add_argument("--decisions", required=True, help="Path to decisions.jsonl")
    inspect_parser.add_argument("--user", help="User ID to filter by (e.g. U-02344)")
    inspect_parser.add_argument("--raw", action="store_true", help="Print raw JSON format")

    args = parser.parse_args()

    if args.command == "replay":
        stats = run_replay(args.pack, args.out, use_mock=args.mock)
        # After replay, run outcomes + impact
        _run_post_replay(args.pack, args.out, stats)
    elif args.command == "inspect":
        inspect_decisions(args.decisions, user_id=args.user, raw=args.raw)
    else:
        parser.print_help()


def _run_post_replay(pack_root: str, out_dir: str, stats: dict) -> None:
    """Run outcome judging, simulator, and impact reporter after replay."""
    print("\n--- Post-replay: Running outcome judge ---")
    _run_outcome_judge(pack_root, out_dir)

    print("\n--- Post-replay: Running simulator ---")
    _run_simulator(pack_root, out_dir)

    print("\n--- Post-replay: Running impact reporter ---")
    _run_impact_reporter(pack_root, out_dir, stats)

    print("\n--- Post-replay: Writing final report ---")
    _write_final_report(pack_root, out_dir, stats)


def _run_outcome_judge(pack_root: str, out_dir: str) -> None:
    """Judge outcomes for all decisions."""
    pack = load_pack(pack_root)

    decisions_path = os.path.join(out_dir, "decisions.jsonl")
    if not os.path.exists(decisions_path):
        print("  No decisions.jsonl found, skipping")
        return

    decisions = []
    with open(decisions_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                decisions.append(json.loads(line))

    # Build corrected goals from control events
    corrected_goals = {}
    for c in pack.control_events:
        if c.get("type") == "goal_rule_corrected":
            gid = c.get("goal_id")
            if gid:
                corrected_goals[gid] = {"new_success": c.get("new_success", {})}

    outcomes = []
    for d in decisions:
        goal = d.get("goal")
        if not goal or not goal.get("goal_id"):
            continue
        user_id = d.get("user_id")

        # Combine historical + live events for this user
        user_events = pack.events_by_user.get(user_id, [])
        live = pack.live_events_by_user.get(user_id, [])
        all_events = user_events + live

        outcome = judge_goal(user_id, goal, pack, all_events, corrected_goals)
        outcomes.append(outcome)

    with open(os.path.join(out_dir, "outcomes.jsonl"), "w", encoding="utf-8") as f:
        for o in outcomes:
            f.write(json.dumps(o, ensure_ascii=False, default=str) + "\n")

    status_counts = defaultdict(int)
    for o in outcomes:
        status_counts[o["status"]] += 1
    print(f"  Judged {len(outcomes)} outcomes: {dict(status_counts)}")


def _run_simulator(pack_root: str, out_dir: str) -> None:
    """Run the simulator on our actions."""
    actions_path = os.path.join(out_dir, "actions.jsonl")
    if not os.path.exists(actions_path):
        print("  No actions.jsonl found, skipping")
        return

    sim_out = os.path.join(out_dir, "sim")
    sim_script = os.path.join(pack_root, "simulator", "simulate_outcomes.py")

    if not os.path.exists(sim_script):
        print(f"  Simulator script not found at {sim_script}, skipping")
        return

    import subprocess
    cmd = [
        sys.executable, sim_script,
        "--pack", pack_root,
        "--actions", actions_path,
        "--out", sim_out,
    ]
    print(f"  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  Simulator error: {result.stderr}")
    else:
        print(f"  Simulator output: {result.stderr.strip()}")


def _run_impact_reporter(pack_root: str, out_dir: str, stats: dict) -> None:
    """Compute ITT impact analysis and pilot bias explanation."""
    from .impact_reporter import generate_impact_report
    generate_impact_report(pack_root, out_dir, stats)


def _write_final_report(pack_root: str, out_dir: str, stats: dict) -> None:
    """Write the final markdown report."""
    # Load data issues
    issues_path = os.path.join(out_dir, "data_issues.json")
    issues = []
    if os.path.exists(issues_path):
        with open(issues_path, encoding="utf-8") as f:
            issues = json.load(f)

    # Load latency report
    latency_path = os.path.join(out_dir, "latency_report.json")
    latency = {}
    if os.path.exists(latency_path):
        with open(latency_path, encoding="utf-8") as f:
            latency = json.load(f)

    # Load outcomes
    outcomes_path = os.path.join(out_dir, "outcomes.jsonl")
    outcomes = []
    if os.path.exists(outcomes_path):
        with open(outcomes_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    outcomes.append(json.loads(line))

    status_counts = defaultdict(int)
    for o in outcomes:
        status_counts[o["status"]] += 1

    # Load impact report if exists
    impact_path = os.path.join(out_dir, "impact_report.md")
    impact_content = ""
    if os.path.exists(impact_path):
        with open(impact_path, encoding="utf-8") as f:
            impact_content = f.read()

    report = f"""# Final Report – One User, One Agent Replay

## Summary

| Metric | Value |
|---|---|
| Total triggers | {stats.get('total_triggers', 0)} |
| Decisions made | {stats.get('decisions_made', 0)} |
| Holdout skipped | {stats.get('holdout_skipped', 0)} |
| Stale skipped | {stats.get('stale_skipped', 0)} |
| Duplicate skipped | {stats.get('duplicate_skipped', 0)} |
| Home pages published | {stats.get('home_page', 0)} |
| Push notifications | {stats.get('push', 0)} |
| WhatsApp messages | {stats.get('whatsapp', 0)} |
| In-app messages | {stats.get('in_app', 0)} |
| NO_ACTION decisions | {stats.get('no_action', 0)} |
| Verified pass | {stats.get('verified_pass', 0)} |
| Verified reject | {stats.get('verified_reject', 0)} |
| Goals assigned | {stats.get('goals_assigned', 0)} |
| Total time (s) | {stats.get('total_time_s', 0)} |

## Latency

| Metric | Value |
|---|---|
| p50 (ms) | {latency.get('p50_ms', 0)} |
| p95 (ms) | {latency.get('p95_ms', 0)} |
| Max (ms) | {latency.get('max_ms', 0)} |
| Count | {latency.get('count', 0)} |
| Over 60s | {latency.get('over_60s', 0)} |

## Outcomes

| Status | Count |
|---|---|
| Succeeded | {status_counts.get('succeeded', 0)} |
| Failed | {status_counts.get('failed', 0)} |
| Inconclusive | {status_counts.get('inconclusive', 0)} |

## Data Issues → How Handled

| Issue | Count | How Handled |
|---|---|---|
"""
    issue_handling = {
        "duplicate_events": "Deduped by event_id, keeping first occurrence",
        "dual_profile_rows": "Picked latest by updated_at, conflict recorded",
        "dual_profile_rows_summary": "All dual-row users resolved by latest updated_at",
        "invalid_profile_dialect": "Used app_setting dialect from trigger; profile dialect noted as conflict",
        "duplicate_content_slugs": "Deduped by content_slug, keeping first variant",
        "injected_description": "Flagged and logged; title excluded from recommendations",
        "stale_triggers": "Flagged as offline-buffered; NO_ACTION with recorded reason",
        "missing_new_viewer_profiles": "Used signup_completed event for signup date; facts resolved from events",
        "dialect_conflict_profile_vs_app": "app_setting decides content dialect; profile and deeplink recorded as conflicts",
    }

    # Aggregate issues by category for cleaner table
    from collections import Counter as _Counter
    issue_counts: dict[str, int] = {}
    for issue in issues:
        cat = issue["category"]
        issue_counts[cat] = issue_counts.get(cat, 0) + issue.get("count", 1)

    for cat, count in issue_counts.items():
        handling = issue_handling.get(cat, "Logged and noted")
        report += f"| {cat} | {count} | {handling} |\n"

    report += f"""
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

{impact_content}
"""

    with open(os.path.join(out_dir, "final_report.md"), "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  Written final_report.md")


if __name__ == "__main__":
    main()
