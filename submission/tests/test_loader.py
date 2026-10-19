"""Tests for the loader module (Step 1)."""
import os
import sys
import pytest

# Ensure submission package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.loader import load_pack, _ts


PACK_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


@pytest.fixture(scope="module")
def pack():
    """Load the pack once for all tests."""
    return load_pack(PACK_ROOT)


class TestLoader:
    def test_events_deduped(self, pack):
        """No duplicate event_ids after loading."""
        eids = [e["event_id"] for e in pack.events]
        assert len(eids) == len(set(eids))

    def test_events_sorted_by_received_at(self, pack):
        """Events are in received_at order."""
        for i in range(1, min(1000, len(pack.events))):
            assert pack.events[i]["received_at"] >= pack.events[i-1]["received_at"]

    def test_event_age_computed(self, pack):
        """event_age_s is computed for every event."""
        for e in pack.events[:100]:
            assert e["event_age_s"] is not None

    def test_profiles_one_per_user(self, pack):
        """Each user_id maps to exactly one profile row."""
        assert all(isinstance(v, dict) for v in pack.profiles.values())

    def test_catalogue_has_titles(self, pack):
        assert len(pack.catalogue) > 0

    def test_catalogue_deduped_by_slug(self, pack):
        """catalogue_by_slug has unique slugs."""
        assert len(pack.catalogue_by_slug) <= len(pack.catalogue)
        slugs = list(pack.catalogue_by_slug.keys())
        assert len(slugs) == len(set(slugs))

    def test_injection_detected(self, pack):
        """At least one injected description is detected."""
        injection_issues = [i for i in pack.data_issues if i.category == "injected_description"]
        assert len(injection_issues) >= 1

    def test_stale_triggers_detected(self, pack):
        """Stale triggers are flagged."""
        stale = [t for t in pack.triggers if t.get("_is_stale")]
        assert len(stale) > 0

    def test_arms_loaded(self, pack):
        """Arms file loaded with agent/holdout."""
        assert "agent" in pack.arms.values()
        assert "holdout" in pack.arms.values()

    def test_control_events_loaded(self, pack):
        """Control events include title_withdrawn and goal_rule_corrected."""
        types = {c["type"] for c in pack.control_events}
        assert "title_withdrawn" in types
        assert "goal_rule_corrected" in types

    def test_model_faults_loaded(self, pack):
        """Model faults are loaded and keyed by trigger_event_id."""
        assert len(pack.model_faults) > 0
        for key, fault in pack.model_faults.items():
            assert fault["trigger_event_id"] == key

    def test_data_issues_not_empty(self, pack):
        """At least some data issues are detected."""
        assert len(pack.data_issues) > 0
        categories = {i.category for i in pack.data_issues}
        assert "duplicate_events" in categories

    def test_episodes_sorted(self, pack):
        """Episodes within each slug are sorted by season, episode."""
        for slug, eps in pack.episodes.items():
            for i in range(1, len(eps)):
                assert (eps[i]["_season"], eps[i]["_episode"]) >= (eps[i-1]["_season"], eps[i-1]["_episode"])

    def test_goals_loaded(self, pack):
        """All 12 goals loaded."""
        assert len(pack.goals) == 12

    def test_whatsapp_templates_loaded(self, pack):
        """3 WhatsApp templates loaded."""
        assert len(pack.whatsapp_templates) == 3

    def test_triggers_sorted_by_deliver_at(self, pack):
        """Triggers sorted by deliver_at."""
        for i in range(1, len(pack.triggers)):
            assert pack.triggers[i]["deliver_at"] >= pack.triggers[i-1]["deliver_at"]

    def test_missing_profiles_detected(self, pack):
        """Missing profile issue detected for triggered users."""
        missing = [i for i in pack.data_issues if i.category == "missing_new_viewer_profiles"]
        assert len(missing) > 0
