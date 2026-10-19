"""
Loader – Step 1

Loads the full data pack:
  - data/events.jsonl      → dedupe by event_id, order by received_at
  - data/profiles_snapshot.csv → detect dual rows
  - data/catalogue.csv     → dedupe by content_slug
  - data/episodes.csv
  - config/*.json + config/messaging_policy.md
  - replay/ (triggers, live_events, control_events, model_faults, arms)
  - pilot/

Produces a DataPack object and a data_issues list.
"""
from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

from . import config as cfg


# ── Helpers ──────────────────────────────────────────────────────────

def _ts(s: str | None) -> datetime | None:
    """Parse ISO timestamp (with Z or offset) to aware datetime."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _read_jsonl(path: str) -> list[dict]:
    """Read a JSONL file, returning a list of dicts."""
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _read_csv(path: str) -> list[dict]:
    """Read a CSV file, returning a list of dicts."""
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── Data-issue entry ─────────────────────────────────────────────────

@dataclass
class DataIssue:
    category: str
    detail: str
    count: int = 1
    examples: list[str] = field(default_factory=list)


# ── DataPack ─────────────────────────────────────────────────────────

@dataclass
class DataPack:
    """All loaded & cleaned data for the replay."""

    # Core data
    events: list[dict]                          # deduped, sorted by received_at
    events_by_user: dict[str, list[dict]]       # user_id → [events]
    profiles: dict[str, dict]                   # user_id → latest profile row
    catalogue: dict[str, dict]                  # title_id → catalogue entry
    catalogue_by_slug: dict[str, dict]          # content_slug → first (kept) catalogue entry
    episodes: dict[str, list[dict]]             # content_slug → [episode rows] sorted
    episodes_flat: dict[str, dict]              # episode_id → episode row

    # Config
    goals: list[dict]
    interventions: dict
    pricing: dict
    messaging_policy: str
    whatsapp_templates: dict[str, dict]         # template_id → template

    # Replay data
    triggers: list[dict]                        # sorted by deliver_at
    triggers_by_user: dict[str, list[dict]]
    live_events: list[dict]                     # sorted by deliver_at
    live_events_by_user: dict[str, list[dict]]
    control_events: list[dict]                  # sorted by at
    model_faults: dict[str, dict]               # trigger_event_id → fault
    arms: dict[str, str]                        # user_id → "agent"/"holdout"
    withdrawn_titles: dict[str, datetime]       # title_id → withdrawn_at

    # Pilot
    pilot_assignment: list[dict]
    pilot_outcomes: list[dict]

    # Quality
    data_issues: list[DataIssue]


# ── Main loader ──────────────────────────────────────────────────────

def load_pack(pack_root: str) -> DataPack:
    """Load and validate the entire data pack."""
    issues: list[DataIssue] = []

    # ── 1. Events: dedupe by event_id, order by received_at ──────────
    raw_events = _read_jsonl(os.path.join(pack_root, "data", "events.jsonl"))
    seen_ids: set[str] = set()
    events: list[dict] = []
    dup_count = 0
    for e in raw_events:
        eid = e["event_id"]
        if eid in seen_ids:
            dup_count += 1
            continue
        seen_ids.add(eid)
        # Compute event_age_s = received_at - occurred_at
        rcv = _ts(e["received_at"])
        occ = _ts(e.get("occurred_at"))
        e["_received_at"] = rcv
        e["_occurred_at"] = occ
        e["event_age_s"] = (rcv - occ).total_seconds() if rcv and occ else None
        events.append(e)
    events.sort(key=lambda e: e["received_at"])
    if dup_count:
        issues.append(DataIssue("duplicate_events", f"{dup_count} duplicate event_ids removed", dup_count))

    events_by_user: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        events_by_user[e["user_id"]].append(e)

    # ── 2. Profiles: detect dual rows, pick latest by updated_at ─────
    raw_profiles = _read_csv(os.path.join(pack_root, "data", "profiles_snapshot.csv"))
    uid_rows: dict[str, list[dict]] = defaultdict(list)
    for r in raw_profiles:
        uid_rows[r["user_id"]].append(r)

    profiles: dict[str, dict] = {}
    dual_profile_count = 0
    for uid, rows in uid_rows.items():
        if len(rows) > 1:
            dual_profile_count += 1
            # pick latest by updated_at
            rows.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
            issues.append(DataIssue(
                "dual_profile_rows",
                f"User {uid} has {len(rows)} profile rows; keeping latest updated_at={rows[0].get('updated_at')}",
                examples=[uid],
            ))
        chosen = rows[0]
        # Parse consent booleans
        chosen["_notifications_opt_in"] = chosen.get("notifications_opt_in", "").lower() == "true"
        chosen["_whatsapp_opt_in"] = chosen.get("whatsapp_opt_in", "").lower() == "true"
        chosen["_signup_at"] = _ts(chosen.get("signup_at"))
        chosen["_snapshot_at"] = _ts(chosen.get("snapshot_at"))
        chosen["_updated_at"] = _ts(chosen.get("updated_at"))
        profiles[uid] = chosen

    if dual_profile_count:
        issues.append(DataIssue("dual_profile_rows_summary", f"{dual_profile_count} users with multiple profile rows", dual_profile_count))

    # Detect empty/invalid dialect
    bad_dialect_count = 0
    for uid, p in profiles.items():
        d = p.get("preferred_dialect", "")
        if d not in cfg.VALID_DIALECTS:
            bad_dialect_count += 1
    if bad_dialect_count:
        issues.append(DataIssue("invalid_profile_dialect", f"{bad_dialect_count} profiles with dialect not in d1-d6", bad_dialect_count))

    # ── 3. Catalogue: dedupe by content_slug ─────────────────────────
    raw_catalogue = _read_csv(os.path.join(pack_root, "data", "catalogue.csv"))
    catalogue: dict[str, dict] = {}
    catalogue_by_slug: dict[str, dict] = {}
    slug_seen: dict[str, str] = {}  # slug → first title_id
    slug_dup_count = 0
    injection_count = 0
    for r in raw_catalogue:
        tid = r["title_id"]
        slug = r["content_slug"]
        # Parse availability
        r["_available_from"] = _ts(r.get("available_from"))
        r["_available_until"] = _ts(r.get("available_until"))
        r["_is_premium"] = r.get("is_premium", "").lower() == "true"
        r["_genres"] = [g.strip() for g in r.get("genres", "").split("|") if g.strip()]

        # Check for injected instructions
        desc = r.get("description", "")
        desc_lower = desc.lower()
        has_injection = any(kw in desc_lower for kw in cfg.INJECTION_KEYWORDS)
        r["_has_injection"] = has_injection
        if has_injection:
            injection_count += 1
            issues.append(DataIssue(
                "injected_description",
                f"Title {tid} ({slug}) contains injected instructions in description",
                examples=[tid],
            ))

        catalogue[tid] = r

        if slug in slug_seen:
            slug_dup_count += 1
            # Keep the first variant encountered (could refine)
        else:
            slug_seen[slug] = tid
            catalogue_by_slug[slug] = r

    if slug_dup_count:
        issues.append(DataIssue("duplicate_content_slugs", f"{slug_dup_count} duplicate content_slug variants in catalogue", slug_dup_count))

    # ── 4. Episodes ──────────────────────────────────────────────────
    raw_episodes = _read_csv(os.path.join(pack_root, "data", "episodes.csv"))
    episodes: dict[str, list[dict]] = defaultdict(list)
    episodes_flat: dict[str, dict] = {}
    for r in raw_episodes:
        r["_season"] = int(r.get("season", 1))
        r["_episode"] = int(r.get("episode", 1))
        r["_minutes"] = int(r.get("minutes", 0))
        episodes[r["content_slug"]].append(r)
        episodes_flat[r["episode_id"]] = r
    # Sort episodes within each slug
    for slug in episodes:
        episodes[slug].sort(key=lambda r: (r["_season"], r["_episode"]))

    # ── 5. Config ────────────────────────────────────────────────────
    with open(os.path.join(pack_root, "config", "goals.json"), encoding="utf-8") as f:
        goals_config = json.load(f)
    goals = goals_config["goals"]

    with open(os.path.join(pack_root, "config", "interventions.json"), encoding="utf-8") as f:
        interventions = json.load(f)

    with open(os.path.join(pack_root, "config", "pricing.json"), encoding="utf-8") as f:
        pricing = json.load(f)

    with open(os.path.join(pack_root, "config", "messaging_policy.md"), encoding="utf-8") as f:
        messaging_policy = f.read()

    whatsapp_templates: dict[str, dict] = {}
    for t in interventions.get("whatsapp", {}).get("approved_templates", []):
        whatsapp_templates[t["template_id"]] = t

    # ── 6. Replay data ───────────────────────────────────────────────
    raw_triggers = _read_jsonl(os.path.join(pack_root, "replay", "triggers.jsonl"))
    # Parse trigger timestamps and flag stale
    stale_trigger_count = 0
    for t in raw_triggers:
        t["_received_at"] = _ts(t["received_at"])
        t["_occurred_at"] = _ts(t.get("occurred_at"))
        t["_deliver_at"] = _ts(t.get("deliver_at"))
        t["_sent_at"] = _ts(t.get("sent_at"))
        age = (t["_received_at"] - t["_occurred_at"]).total_seconds() if t["_received_at"] and t["_occurred_at"] else 0
        t["event_age_s"] = age
        t["_is_stale"] = age > cfg.STALE_TRIGGER_THRESHOLD_S
        if t["_is_stale"]:
            stale_trigger_count += 1

    raw_triggers.sort(key=lambda t: t.get("deliver_at", ""))
    if stale_trigger_count:
        issues.append(DataIssue("stale_triggers", f"{stale_trigger_count} triggers with received_at - occurred_at > {cfg.STALE_TRIGGER_THRESHOLD_S}s", stale_trigger_count))

    triggers_by_user: dict[str, list[dict]] = defaultdict(list)
    for t in raw_triggers:
        triggers_by_user[t["user_id"]].append(t)

    live_events = _read_jsonl(os.path.join(pack_root, "replay", "live_events.jsonl"))
    for e in live_events:
        e["_received_at"] = _ts(e["received_at"])
        e["_deliver_at"] = _ts(e.get("deliver_at"))
    live_events.sort(key=lambda e: e.get("deliver_at", ""))
    live_events_by_user: dict[str, list[dict]] = defaultdict(list)
    for e in live_events:
        live_events_by_user[e["user_id"]].append(e)

    control_events = _read_jsonl(os.path.join(pack_root, "replay", "control_events.jsonl"))
    for c in control_events:
        c["_at"] = _ts(c["at"])
    control_events.sort(key=lambda c: c["at"])

    # Model faults: keyed by trigger_event_id
    raw_faults = _read_jsonl(os.path.join(pack_root, "replay", "model_faults.jsonl"))
    model_faults: dict[str, dict] = {}
    for f in raw_faults:
        model_faults[f["trigger_event_id"]] = f

    # Arms
    raw_arms = _read_csv(os.path.join(pack_root, "replay", "arms.csv"))
    arms: dict[str, str] = {}
    for r in raw_arms:
        arms[r["user_id"]] = r["arm"]

    # Withdrawn titles from control_events
    withdrawn_titles: dict[str, datetime] = {}
    for c in control_events:
        if c["type"] == "title_withdrawn":
            withdrawn_titles[c["title_id"]] = c["_at"]
            # Also apply withdrawal to catalogue entry
            cat_entry = catalogue.get(c["title_id"])
            if cat_entry:
                existing = cat_entry.get("_available_until")
                if existing is None or c["_at"] < existing:
                    cat_entry["_available_until"] = c["_at"]

    # ── 7. Pilot ─────────────────────────────────────────────────────
    pilot_assignment = _read_csv(os.path.join(pack_root, "pilot", "pilot_assignment.csv"))
    pilot_outcomes = _read_csv(os.path.join(pack_root, "pilot", "pilot_outcomes.csv"))

    # ── 8. Detect missing profiles for triggered users ───────────────
    trigger_users = set(t["user_id"] for t in raw_triggers)
    profile_users = set(profiles.keys())
    missing = trigger_users - profile_users
    if missing:
        issues.append(DataIssue(
            "missing_new_viewer_profiles",
            f"{len(missing)} triggered users have no profile row (likely new sign-ups)",
            len(missing),
            examples=sorted(missing)[:10],
        ))

    # ── 9. Detect dialect conflicts ──────────────────────────────────
    dialect_conflict_count = 0
    for uid, trigs in triggers_by_user.items():
        profile = profiles.get(uid)
        if not profile:
            continue
        profile_dialect = profile.get("preferred_dialect", "")
        for t in trigs:
            app_dialect = t.get("app_dialect", "")
            if app_dialect and profile_dialect and app_dialect != profile_dialect:
                dialect_conflict_count += 1
                break  # count per user, not per trigger
    if dialect_conflict_count:
        issues.append(DataIssue(
            "dialect_conflict_profile_vs_app",
            f"{dialect_conflict_count} users have app_dialect in trigger != preferred_dialect in profile",
            dialect_conflict_count,
        ))

    return DataPack(
        events=events,
        events_by_user=dict(events_by_user),
        profiles=profiles,
        catalogue=catalogue,
        catalogue_by_slug=dict(catalogue_by_slug),
        episodes=dict(episodes),
        episodes_flat=episodes_flat,
        goals=goals,
        interventions=interventions,
        pricing=pricing,
        messaging_policy=messaging_policy,
        whatsapp_templates=whatsapp_templates,
        triggers=raw_triggers,
        triggers_by_user=dict(triggers_by_user),
        live_events=live_events,
        live_events_by_user=dict(live_events_by_user),
        control_events=control_events,
        model_faults=model_faults,
        arms=arms,
        withdrawn_titles=withdrawn_titles,
        pilot_assignment=pilot_assignment,
        pilot_outcomes=pilot_outcomes,
        data_issues=issues,
    )


def title_available_at(cat_entry: dict, at: datetime) -> bool:
    """Check if a catalogue title is available at a given time."""
    avail_from = cat_entry.get("_available_from")
    avail_until = cat_entry.get("_available_until")
    if avail_from and at < avail_from:
        return False
    if avail_until and at >= avail_until:
        return False
    return True
