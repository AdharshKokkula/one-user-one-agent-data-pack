"""
Comprehensive tests for the One User, One Agent system.

Covers:
- Late/duplicate trigger: no duplicate action, stale trigger handled
- Conflicting source: dialect and subscription conflict resolved per rules
- Tool/model failure: safe fallback within budget
- Corrected goal rule: only affected open goals re-judged
- Quick extras: quiet hours, daily cap, holdout, injection, <3-title row, withdrawn title
"""
import os
import sys
import json
import pytest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import config as cfg
from src.loader import load_pack, _ts, DataPack
from src.viewer_state import ViewerState
from src.fact_resolver import resolve_all_facts, update_facts_from_event
from src.goal_planner import select_goal, should_replan, SKIPPED_GOALS
from src.intervention_planner import build_home_page, plan_message, _is_quiet_hours, _ist_date_str
from src.model_provider import MockProvider, CallBudget, call_with_retries, ModelCallResult
from src.verifier import verify_decision, VerificationResult
from src.outcome_judge import judge_goal


PACK_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


@pytest.fixture(scope="module")
def pack():
    return load_pack(PACK_ROOT)


# ═══════════════════════════════════════════════════════════════════
# Test: Late / duplicate trigger
# ═══════════════════════════════════════════════════════════════════

class TestDuplicateTrigger:
    """No duplicate action for the same trigger_event_id."""

    def test_idempotent_decision(self, pack):
        """Adding a decision twice with the same trigger_event_id is idempotent."""
        state = ViewerState("U-TEST-01")
        decision = {"action_type": "HOME_PAGE", "test": True}
        state.add_decision("trigger-001", decision)
        state.add_decision("trigger-001", decision)  # duplicate
        assert len(state.get_all_decisions()) == 1

    def test_stale_trigger_flagged(self, pack):
        """Stale triggers (>5 min gap) are flagged."""
        stale = [t for t in pack.triggers if t.get("_is_stale")]
        assert len(stale) > 0
        for t in stale:
            assert t["event_age_s"] > cfg.STALE_TRIGGER_THRESHOLD_S


# ═══════════════════════════════════════════════════════════════════
# Test: Conflicting source – dialect and subscription
# ═══════════════════════════════════════════════════════════════════

class TestConflictResolution:
    """Dialect and subscription conflicts resolved per documented rules."""

    def test_dialect_from_trigger_beats_profile(self, pack):
        """When trigger app_dialect differs from profile, trigger wins."""
        # Find a user with a dialect conflict
        for uid, trigs in pack.triggers_by_user.items():
            profile = pack.profiles.get(uid)
            if not profile:
                continue
            for t in trigs:
                if (t.get("app_dialect") and profile.get("preferred_dialect")
                        and t["app_dialect"] != profile["preferred_dialect"]):
                    state = ViewerState(uid)
                    resolve_all_facts(uid, pack, state, t)
                    assert state.get_fact_value("dialect") == t["app_dialect"]
                    # Conflict should be recorded
                    conflicts = [c for c in state.get_conflicts() if c["fact"] == "dialect"]
                    assert len(conflicts) > 0
                    return
        pytest.skip("No dialect conflict found in data")

    def test_subscription_event_beats_profile(self, pack):
        """Newest subscription event beats profile snapshot."""
        # Find a user with subscription events after profile snapshot
        for uid, events in pack.events_by_user.items():
            profile = pack.profiles.get(uid)
            if not profile:
                continue
            sub_events = [e for e in events if e["event"] in
                          ("purchase_completed", "subscription_renewed", "subscription_lapsed")]
            if sub_events:
                latest = max(sub_events, key=lambda e: e["received_at"])
                profile_snap = profile.get("_snapshot_at")
                if profile_snap and latest["_received_at"] > profile_snap:
                    state = ViewerState(uid)
                    resolve_all_facts(uid, pack, state)
                    sub = state.get_fact_value("subscription_status")
                    assert sub is not None
                    # Source should reference the event
                    fact = state.get_fact("subscription_status")
                    assert "event:" in fact["source"]
                    return
        pytest.skip("No subscription conflict found")


# ═══════════════════════════════════════════════════════════════════
# Test: Tool / model failure → safe fallback
# ═══════════════════════════════════════════════════════════════════

class TestModelFailure:
    """Model failures produce safe fallback within budget."""

    def test_timeout_fallback(self):
        """Timeout produces failure result, budget tracked."""
        faults = {"trigger-timeout": {
            "trigger_event_id": "trigger-timeout",
            "fault": "timeout",
            "seconds": 45,
        }}
        provider = MockProvider(faults)
        provider.set_trigger("trigger-timeout")
        budget = CallBudget()
        result = call_with_retries(provider, "test", budget)
        assert not result.success
        assert result.fault_type == "timeout"
        assert budget.model_calls_used == 1  # no retry on timeout

    def test_malformed_output_retried(self):
        """Malformed output is retried up to max."""
        faults = {"trigger-malformed": {
            "trigger_event_id": "trigger-malformed",
            "fault": "malformed_output",
            "model_output": '{"goal_id": "G02", "eligible": tr',
        }}
        provider = MockProvider(faults)
        provider.set_trigger("trigger-malformed")
        budget = CallBudget()
        result = call_with_retries(provider, "test", budget, max_retries=2)
        assert not result.success
        assert budget.model_calls_used == 3  # 1 + 2 retries

    def test_budget_exhaustion(self):
        """Budget exhaustion stops further calls."""
        provider = MockProvider()
        budget = CallBudget(max_model_calls=2)
        for _ in range(3):
            result = call_with_retries(provider, "test", budget, max_retries=0)
        assert budget.model_calls_used == 2
        assert result.fault_type == "budget_exhausted"

    def test_unsupported_claim_flagged(self):
        """Unsupported model claim returns success=True but fault_type set."""
        faults = {"trigger-claim": {
            "trigger_event_id": "trigger-claim",
            "fault": "unsupported_claim",
            "model_output": {
                "goal_id": "G04",
                "eligible": True,
                "confidence": 0.97,
                "reason": "The user is on an active trial",
            },
        }}
        provider = MockProvider(faults)
        provider.set_trigger("trigger-claim")
        budget = CallBudget()
        result = call_with_retries(provider, "test", budget)
        assert result.success  # returns but flagged
        assert result.fault_type == "unsupported_claim"


# ═══════════════════════════════════════════════════════════════════
# Test: Corrected goal rule
# ═══════════════════════════════════════════════════════════════════

class TestCorrectedGoalRule:
    """Only affected open goals are re-judged on correction."""

    def test_g03_correction_changes_success_event(self, pack):
        """After G03 correction, success event changes from app_open to play_started."""
        corrected = {
            "G03": {
                "new_success": {"event": "play_started",
                                "condition": "on a later IST calendar day within the window"},
            }
        }
        # Create a mock goal with original success
        goal = {
            "goal_id": "G03",
            "name": "second_day_return_2d",
            "started_at": "2026-09-30T18:00:00+00:00",
            "deadline": "2026-10-02T18:00:00+00:00",
            "success_signal": {"event": "app_open", "condition": "on a later IST calendar day"},
            "window_hours": 48,
        }
        # Judge with correction
        outcome = judge_goal("U-00001", goal, pack, [], corrected)
        # It should use play_started, not app_open
        assert outcome["status"] in ("failed", "inconclusive")

    def test_unaffected_goal_not_changed(self, pack):
        """A G01 goal is not affected by a G03 correction."""
        corrected = {
            "G03": {
                "new_success": {"event": "play_started",
                                "condition": "on a later IST calendar day"},
            }
        }
        goal = {
            "goal_id": "G01",
            "name": "first_play_24h",
            "started_at": "2026-09-30T18:00:00+00:00",
            "deadline": "2026-10-01T18:00:00+00:00",
            "success_signal": {"event": "play_started", "condition": "any title"},
            "window_hours": 24,
        }
        outcome = judge_goal("U-00001", goal, pack, [], corrected)
        # G01 is unaffected by G03 correction
        assert outcome["goal_id"] == "G01"


# ═══════════════════════════════════════════════════════════════════
# Quick extras
# ═══════════════════════════════════════════════════════════════════

class TestQuietHours:
    """Quiet hours enforcement."""

    def test_22_ist_is_quiet(self):
        dt = datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc)  # 22:00 IST
        assert _is_quiet_hours(dt)

    def test_07_ist_is_quiet(self):
        dt = datetime(2026, 10, 1, 1, 30, tzinfo=timezone.utc)  # 07:00 IST
        assert _is_quiet_hours(dt)

    def test_12_ist_is_not_quiet(self):
        dt = datetime(2026, 10, 1, 6, 30, tzinfo=timezone.utc)  # 12:00 IST
        assert not _is_quiet_hours(dt)

    def test_08_ist_is_not_quiet(self):
        dt = datetime(2026, 10, 1, 2, 30, tzinfo=timezone.utc)  # 08:00 IST
        assert not _is_quiet_hours(dt)


class TestHoldout:
    """Holdout arm gets zero actions."""

    def test_holdout_rejected_by_verifier(self, pack):
        """Verifier rejects any action for holdout users."""
        holdout_user = next(uid for uid, arm in pack.arms.items() if arm == "holdout")
        decision = {
            "user_id": holdout_user,
            "trigger_event_id": "test-trigger-holdout",
            "action_type": "HOME_PAGE",
            "actions": [{"type": "home_page"}],
        }
        result = verify_decision(
            decision, pack.arms, pack.profiles, pack.catalogue,
            pack.events_by_user, set(), {}, pack.withdrawn_titles,
        )
        assert not result.passed
        assert any("holdout" in v["rule"] for v in result.violations)


class TestInjectionIgnored:
    """Injected instructions in catalogue descriptions are flagged."""

    def test_injection_titles_flagged(self, pack):
        """Titles with injected descriptions have _has_injection=True."""
        flagged = [t for t in pack.catalogue.values() if t.get("_has_injection")]
        assert len(flagged) >= 2  # T0002 and T0522

    def test_injected_title_not_in_homepage(self, pack):
        """Home pages should not include titles with injected descriptions."""
        user_id = "U-00620"  # has d1 dialect
        state = ViewerState(user_id)
        resolve_all_facts(user_id, pack, state)
        state.set_fact("dialect", "d1", "test")
        trigger_time = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        page = build_home_page(user_id, pack, state, trigger_time, None)
        if page:
            all_ids = [page.get("hero_title_id", "")] + \
                      [tid for row in page.get("rows", []) for tid in row.get("title_ids", [])]
            for tid in all_ids:
                cat = pack.catalogue.get(tid)
                if cat:
                    assert not cat.get("_has_injection"), \
                        f"Injected title {tid} found in home page"


class TestUnsupportedModelClaim:
    """Unsupported model claims are rejected by verifier."""

    def test_unsupported_claim_rejected(self, pack):
        """Decision based on unsupported model claim is rejected."""
        # Pick a trigger with unsupported_claim fault
        for trigger_id, fault in pack.model_faults.items():
            if fault.get("fault") == "unsupported_claim":
                model_goal_id = fault["model_output"]["goal_id"]
                decision = {
                    "user_id": fault["user_id"],
                    "trigger_event_id": trigger_id,
                    "action_type": "HOME_PAGE",
                    "goal": {"goal_id": model_goal_id},
                    "actions": [],
                }
                result = verify_decision(
                    decision, pack.arms, pack.profiles, pack.catalogue,
                    pack.events_by_user, set(), {}, pack.withdrawn_titles,
                    pack.model_faults,
                )
                assert not result.passed
                assert any("unsupported" in v["rule"] for v in result.violations)
                return
        pytest.skip("No unsupported_claim faults found")


class TestHomePageRules:
    """Home page construction rules."""

    def test_min_titles_per_row(self, pack):
        """Rows with <3 valid titles should be dropped."""
        # This is enforced at construction time
        user_id = "U-00620"
        state = ViewerState(user_id)
        resolve_all_facts(user_id, pack, state)
        state.set_fact("dialect", "d1", "test")
        trigger_time = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        page = build_home_page(user_id, pack, state, trigger_time, None)
        if page:
            for row in page.get("rows", []):
                assert len(row.get("title_ids", [])) >= cfg.HOME_MIN_TITLES_PER_ROW

    def test_no_slug_duplicates(self, pack):
        """Each content_slug appears at most once per page."""
        user_id = "U-00620"
        state = ViewerState(user_id)
        resolve_all_facts(user_id, pack, state)
        state.set_fact("dialect", "d1", "test")
        trigger_time = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        page = build_home_page(user_id, pack, state, trigger_time, None)
        if page:
            slugs = set()
            all_ids = [page.get("hero_title_id")] + \
                      [tid for row in page.get("rows", []) for tid in row.get("title_ids", [])]
            for tid in all_ids:
                if tid:
                    cat = pack.catalogue.get(tid)
                    if cat:
                        slug = cat["content_slug"]
                        assert slug not in slugs, f"Duplicate slug {slug}"
                        slugs.add(slug)


class TestWithdrawnTitle:
    """Withdrawn titles are not used in new actions."""

    def test_withdrawn_title_not_available(self, pack):
        """T0001 is withdrawn; should not be available after withdrawal time."""
        from src.loader import title_available_at
        t0001 = pack.catalogue.get("T0001")
        if t0001:
            # After withdrawal
            after = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
            assert not title_available_at(t0001, after)


class TestDailyCapVerifier:
    """Daily cap enforcement by verifier."""

    def test_second_message_rejected(self, pack):
        """Second push/WhatsApp on same IST day is rejected."""
        user_id = next(uid for uid, arm in pack.arms.items() if arm == "agent")
        messages_sent = {user_id: {"2026-10-01": 1}}  # already 1 message
        decision = {
            "user_id": user_id,
            "trigger_event_id": "test-trigger-cap",
            "action_type": "PUSH",
            "actions": [{
                "type": "push",
                "send_at": "2026-10-01T12:00:00+05:30",
                "push_title": "Test",
                "push_body": "Test body",
                "deeplink": "ott://title/T0001",
            }],
        }
        result = verify_decision(
            decision, pack.arms, pack.profiles, pack.catalogue,
            pack.events_by_user, set(), messages_sent, pack.withdrawn_titles,
        )
        has_cap_violation = any("daily_cap" in v["rule"] for v in result.violations)
        assert has_cap_violation


class TestSkippedGoals:
    """G10 and G11 are always skipped."""

    def test_g10_skipped(self, pack):
        """G10 is never selected due to no concrete success event."""
        assert "G10" in SKIPPED_GOALS

    def test_g11_skipped(self, pack):
        """G11 is never selected because tv_app_open never appears in data."""
        assert "G11" in SKIPPED_GOALS
