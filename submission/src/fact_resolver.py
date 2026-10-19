"""
Fact Resolver – Step 3

Resolves viewer facts from multiple sources with explicit, documented precedence:

  subscription: newest purchase/subscription event by received_at beats profile snapshot.
  dialect:      app_setting (from app_open / app_dialect_changed) decides content dialect;
                profile and campaign deeplink are recorded as visible conflicts.
  signup_date:  profile signup_at if present, else signup_completed event.
  profile_dup:  latest snapshot (by updated_at), conflict recorded.
  consent:      profile value, overridden by later opt-out events.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .loader import DataPack, _ts
from .viewer_state import ViewerState
from . import config as cfg


# ── Subscription status resolution ───────────────────────────────────

_SUBSCRIPTION_EVENTS = frozenset([
    "purchase_completed",
    "subscription_renewed",
    "subscription_lapsed",
])


def _resolve_subscription(user_id: str, pack: DataPack, state: ViewerState) -> None:
    """Resolve subscription status.

    Precedence:
    1. Newest purchase/subscription/lapse event by received_at.
    2. Profile snapshot (fallback).
    """
    profile = pack.profiles.get(user_id)
    profile_status = profile.get("subscription_status", "never") if profile else "never"
    profile_snapshot_at = profile.get("_snapshot_at") if profile else None

    # Find newest subscription-related event
    user_events = pack.events_by_user.get(user_id, [])
    sub_events = [e for e in user_events if e["event"] in _SUBSCRIPTION_EVENTS]
    sub_events.sort(key=lambda e: e["received_at"], reverse=True)

    if sub_events:
        latest = sub_events[0]
        event_received = latest["_received_at"]
        ev_type = latest["event"]
        props = latest.get("properties", {})

        if ev_type == "purchase_completed":
            is_trial = props.get("is_trial", False)
            if isinstance(is_trial, str):
                is_trial = is_trial.lower() == "true"
            status = "trial_active" if is_trial else "active"
            plan_id = props.get("plan_id", "")
        elif ev_type == "subscription_renewed":
            status = "active"
            plan_id = props.get("plan_id", "")
        elif ev_type == "subscription_lapsed":
            status = "lapsed"
            plan_id = ""
        else:
            status = profile_status
            plan_id = ""

        # Does event beat profile?
        if profile_snapshot_at is None or event_received > profile_snapshot_at:
            source = f"event:{ev_type}:{latest['event_id']}"
        else:
            # Profile is newer – use profile
            status = profile_status
            source = "profile_snapshot"
            plan_id = ""

        state.set_fact("subscription_status", status, source,
                       observed_at=latest["received_at"])
        if plan_id:
            state.set_fact("current_plan_id", plan_id, source,
                           observed_at=latest["received_at"])

        # Record conflict if profile disagrees
        if profile and profile_status != status:
            state.add_conflict({
                "fact": "subscription_status",
                "profile_value": profile_status,
                "resolved_value": status,
                "source": source,
                "reason": "Newest subscription event by received_at beats profile snapshot",
            })
    else:
        # No subscription events → use profile
        state.set_fact("subscription_status", profile_status, "profile_snapshot",
                       observed_at=profile.get("snapshot_at") if profile else None)


def _resolve_dialect(user_id: str, pack: DataPack, state: ViewerState,
                     trigger: dict | None = None) -> None:
    """Resolve content dialect.

    Precedence:
    1. app_setting from the trigger's app_dialect field (most recent app state).
    2. Most recent app_dialect_changed event by received_at.
    3. Profile preferred_dialect (fallback).

    Campaign deeplink dialect and profile dialect conflicts are recorded.
    """
    profile = pack.profiles.get(user_id)
    profile_dialect = profile.get("preferred_dialect", "") if profile else ""

    # Start with trigger's app_dialect
    resolved = None
    source = ""

    if trigger and trigger.get("app_dialect"):
        resolved = trigger["app_dialect"]
        source = f"trigger_app_dialect:{trigger['event_id']}"
    else:
        # Check app_dialect_changed events
        user_events = pack.events_by_user.get(user_id, [])
        dialect_events = [e for e in user_events if e["event"] == "app_dialect_changed"]
        dialect_events.sort(key=lambda e: e["received_at"], reverse=True)
        if dialect_events:
            latest = dialect_events[0]
            resolved = latest.get("app_dialect") or latest.get("properties", {}).get("new_dialect")
            source = f"event:app_dialect_changed:{latest['event_id']}"
        elif profile_dialect:
            resolved = profile_dialect
            source = "profile_snapshot"

    if not resolved:
        resolved = profile_dialect or ""
        source = source or "profile_snapshot"

    state.set_fact("dialect", resolved, source)

    # Record conflicts
    if profile_dialect and resolved and profile_dialect != resolved:
        state.add_conflict({
            "fact": "dialect",
            "profile_value": profile_dialect,
            "resolved_value": resolved,
            "source": source,
            "reason": "app_setting decides content dialect; profile recorded as conflict",
        })

    # Check campaign deeplink dialect
    if trigger:
        deeplink = trigger.get("properties", {}).get("deeplink", "")
        if deeplink and "/d" in deeplink:
            # Try to extract dialect from deeplink like ott://c/cmp032/d5/title/T1901
            parts = deeplink.split("/")
            for p in parts:
                if p in cfg.VALID_DIALECTS and p != resolved:
                    state.add_conflict({
                        "fact": "dialect",
                        "campaign_deeplink_dialect": p,
                        "resolved_value": resolved,
                        "source": "campaign_deeplink",
                        "reason": "Campaign deeplink dialect differs from app_setting",
                    })
                    break


def _resolve_signup_date(user_id: str, pack: DataPack, state: ViewerState) -> None:
    """Resolve signup date.

    Precedence:
    1. Profile signup_at if present.
    2. signup_completed event (for new viewers without profile).
    """
    profile = pack.profiles.get(user_id)
    if profile and profile.get("signup_at"):
        state.set_fact("signup_date", profile["signup_at"], "profile_snapshot",
                       observed_at=profile.get("snapshot_at"))
        return

    # Check for signup_completed event
    user_events = pack.events_by_user.get(user_id, [])
    signup_events = [e for e in user_events if e["event"] == "signup_completed"]
    if signup_events:
        signup_events.sort(key=lambda e: e["received_at"])
        earliest = signup_events[0]
        state.set_fact("signup_date", earliest["received_at"], 
                       f"event:signup_completed:{earliest['event_id']}",
                       observed_at=earliest["received_at"])
    else:
        # Also check live events for new signups
        live = pack.live_events_by_user.get(user_id, [])
        signup_live = [e for e in live if e["event"] == "signup_completed"]
        if signup_live:
            signup_live.sort(key=lambda e: e["received_at"])
            earliest = signup_live[0]
            state.set_fact("signup_date", earliest["received_at"],
                           f"live_event:signup_completed:{earliest['event_id']}",
                           observed_at=earliest["received_at"])


def _resolve_consent(user_id: str, pack: DataPack, state: ViewerState) -> None:
    """Resolve consent flags.

    Precedence:
    1. Profile value.
    2. Overridden by any later notification_opt_out event.
    """
    profile = pack.profiles.get(user_id)

    # Push consent
    push_opt_in = profile.get("_notifications_opt_in", False) if profile else False
    wa_opt_in = profile.get("_whatsapp_opt_in", False) if profile else False

    # Check for opt-out events
    user_events = pack.events_by_user.get(user_id, [])
    opt_out_events = [e for e in user_events if e["event"] == "notification_opt_out"]
    opt_out_events.sort(key=lambda e: e["received_at"])

    profile_snapshot_at = profile.get("_snapshot_at") if profile else None

    for e in opt_out_events:
        channel = e.get("properties", {}).get("channel", "")
        event_at = e["_received_at"]
        # Opt-out overrides profile if it's after the snapshot
        if profile_snapshot_at is None or event_at > profile_snapshot_at:
            if channel == "push":
                push_opt_in = False
            elif channel == "whatsapp":
                wa_opt_in = False
            elif channel == "":
                # Generic opt-out = both
                push_opt_in = False
                wa_opt_in = False

    state.set_fact("push_consent", push_opt_in,
                   "profile_snapshot+opt_out_events",
                   observed_at=profile.get("snapshot_at") if profile else None)
    state.set_fact("whatsapp_consent", wa_opt_in,
                   "profile_snapshot+opt_out_events",
                   observed_at=profile.get("snapshot_at") if profile else None)


def _resolve_platform(user_id: str, pack: DataPack, state: ViewerState,
                      trigger: dict | None = None) -> None:
    """Resolve platform from trigger or profile."""
    if trigger and trigger.get("platform"):
        state.set_fact("platform", trigger["platform"], 
                       f"trigger:{trigger['event_id']}")
        return
    profile = pack.profiles.get(user_id)
    if profile and profile.get("platform"):
        state.set_fact("platform", profile["platform"], "profile_snapshot")


# ── Main resolver ────────────────────────────────────────────────────

def resolve_all_facts(user_id: str, pack: DataPack, state: ViewerState,
                      trigger: dict | None = None) -> ViewerState:
    """Resolve all facts for a viewer. Returns the updated state."""
    _resolve_subscription(user_id, pack, state)
    _resolve_dialect(user_id, pack, state, trigger)
    _resolve_signup_date(user_id, pack, state)
    _resolve_consent(user_id, pack, state)
    _resolve_platform(user_id, pack, state, trigger)
    return state


def update_facts_from_event(user_id: str, event: dict, pack: DataPack,
                            state: ViewerState) -> bool:
    """Update facts incrementally from a new event.
    
    Returns True if something changed that might affect the goal/plan.
    """
    ev_type = event["event"]
    changed = False

    if ev_type == "purchase_completed":
        props = event.get("properties", {})
        is_trial = props.get("is_trial", False)
        if isinstance(is_trial, str):
            is_trial = is_trial.lower() == "true"
        new_status = "trial_active" if is_trial else "active"
        old = state.get_fact_value("subscription_status")
        if old != new_status:
            state.set_fact("subscription_status", new_status,
                           f"event:purchase_completed:{event['event_id']}",
                           observed_at=event["received_at"])
            changed = True

    elif ev_type == "subscription_lapsed":
        old = state.get_fact_value("subscription_status")
        if old != "lapsed":
            state.set_fact("subscription_status", "lapsed",
                           f"event:subscription_lapsed:{event['event_id']}",
                           observed_at=event["received_at"])
            changed = True

    elif ev_type == "subscription_renewed":
        old = state.get_fact_value("subscription_status")
        if old != "active":
            state.set_fact("subscription_status", "active",
                           f"event:subscription_renewed:{event['event_id']}",
                           observed_at=event["received_at"])
            changed = True

    elif ev_type == "notification_opt_out":
        channel = event.get("properties", {}).get("channel", "")
        if channel == "push" or channel == "":
            state.set_fact("push_consent", False,
                           f"event:notification_opt_out:{event['event_id']}",
                           observed_at=event["received_at"])
        if channel == "whatsapp" or channel == "":
            state.set_fact("whatsapp_consent", False,
                           f"event:notification_opt_out:{event['event_id']}",
                           observed_at=event["received_at"])

    elif ev_type == "app_dialect_changed":
        new_dialect = event.get("app_dialect") or event.get("properties", {}).get("new_dialect")
        if new_dialect:
            old = state.get_fact_value("dialect")
            if old != new_dialect:
                state.set_fact("dialect", new_dialect,
                               f"event:app_dialect_changed:{event['event_id']}",
                               observed_at=event["received_at"])
                changed = True

    return changed
