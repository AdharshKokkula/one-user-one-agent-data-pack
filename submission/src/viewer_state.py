"""
Viewer State Store – Step 2

One JSON file per viewer, storing:
  - facts: {fact_name: {value, source, observed_at, freshness}}
  - active_goal: current goal with deadline and success_signal
  - plan: current plan
  - decisions: list of past decisions (keyed by trigger_event_id for idempotency)
  - actions: list of published actions
  - outcomes: list of judged outcomes
  - learnings: list of learnings from judged goals
  - messages_sent_today: {ist_date_str: count} for daily cap
  - conflicts: recorded fact conflicts

Writes are idempotent: same trigger_event_id → same decision.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from . import config as cfg


class ViewerState:
    """In-memory representation of one viewer's state, persisted as JSON."""

    def __init__(self, user_id: str, state_dir: str | None = None):
        self.user_id = user_id
        self.state_dir = state_dir
        self._data: dict[str, Any] = {
            "user_id": user_id,
            "facts": {},
            "active_goal": None,
            "plan": None,
            "decisions": {},          # trigger_event_id → decision
            "actions": [],
            "outcomes": [],
            "learnings": [],
            "messages_sent_today": {},  # "YYYY-MM-DD" → count
            "conflicts": [],
            "goal_history": [],       # past goals
        }
        # Try to load existing state
        if state_dir:
            self._path = os.path.join(state_dir, f"{user_id}.json")
            if os.path.exists(self._path):
                with open(self._path, encoding="utf-8") as f:
                    self._data = json.load(f)
        else:
            self._path = None

    # ── Fact management ──────────────────────────────────────────────

    def set_fact(self, name: str, value: Any, source: str,
                 observed_at: str | None = None, freshness: str = "fresh") -> None:
        """Set a fact with provenance. Idempotent: same value+source = no-op."""
        existing = self._data["facts"].get(name)
        if existing and existing["value"] == value and existing["source"] == source:
            return
        self._data["facts"][name] = {
            "value": value,
            "source": source,
            "observed_at": observed_at,
            "freshness": freshness,
        }

    def get_fact(self, name: str) -> dict | None:
        return self._data["facts"].get(name)

    def get_fact_value(self, name: str, default: Any = None) -> Any:
        f = self._data["facts"].get(name)
        return f["value"] if f else default

    # ── Goal management ──────────────────────────────────────────────

    def set_goal(self, goal: dict | None) -> None:
        """Set the active goal. None clears it."""
        if self._data["active_goal"] is not None:
            self._data["goal_history"].append(self._data["active_goal"])
        self._data["active_goal"] = goal

    def get_goal(self) -> dict | None:
        return self._data["active_goal"]

    # ── Decision management (idempotent by trigger_event_id) ─────────

    def has_decision(self, trigger_event_id: str) -> bool:
        return trigger_event_id in self._data["decisions"]

    def add_decision(self, trigger_event_id: str, decision: dict) -> None:
        """Record a decision. Idempotent: duplicate trigger_event_id is a no-op."""
        if trigger_event_id in self._data["decisions"]:
            return
        self._data["decisions"][trigger_event_id] = decision

    def get_decision(self, trigger_event_id: str) -> dict | None:
        return self._data["decisions"].get(trigger_event_id)

    def get_all_decisions(self) -> dict:
        return self._data["decisions"]

    # ── Action management ────────────────────────────────────────────

    def add_action(self, action: dict) -> None:
        self._data["actions"].append(action)

    def get_actions(self) -> list[dict]:
        return self._data["actions"]

    def get_pending_actions(self) -> list[dict]:
        """Actions not yet delivered or cancelled."""
        return [a for a in self._data["actions"] if a.get("status") == "pending"]

    def cancel_action(self, action_id: str, reason: str) -> None:
        """Cancel a pending action."""
        for a in self._data["actions"]:
            if a.get("action_id") == action_id:
                a["status"] = "cancelled"
                a["cancel_reason"] = reason
                break

    # ── Outcome management ───────────────────────────────────────────

    def add_outcome(self, outcome: dict) -> None:
        self._data["outcomes"].append(outcome)

    def get_outcomes(self) -> list[dict]:
        return self._data["outcomes"]

    # ── Learnings ────────────────────────────────────────────────────

    def add_learning(self, learning: dict) -> None:
        self._data["learnings"].append(learning)

    # ── Messaging cap tracking ───────────────────────────────────────

    def get_messages_sent_on_date(self, ist_date_str: str) -> int:
        return self._data["messages_sent_today"].get(ist_date_str, 0)

    def record_message_sent(self, ist_date_str: str) -> None:
        cur = self._data["messages_sent_today"].get(ist_date_str, 0)
        self._data["messages_sent_today"][ist_date_str] = cur + 1

    # ── Conflict tracking ────────────────────────────────────────────

    def add_conflict(self, conflict: dict) -> None:
        self._data["conflicts"].append(conflict)

    def get_conflicts(self) -> list[dict]:
        return self._data["conflicts"]

    # ── Persistence ──────────────────────────────────────────────────

    def save(self) -> None:
        """Persist state to disk."""
        if self._path:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False, default=str)

    def to_dict(self) -> dict:
        return self._data

    # ── Raw access ───────────────────────────────────────────────────

    @property
    def data(self) -> dict:
        return self._data
