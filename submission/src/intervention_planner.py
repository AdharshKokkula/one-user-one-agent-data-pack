"""
Intervention Planner – Step 5

Rules-first intervention selection:
  1. HOME_PAGE is always produced (if viable content exists in dialect).
  2. One message channel (PUSH / WHATSAPP / IN_APP) may be added if consent and
     cap allow.
  3. NO_ACTION if nothing is viable.

Home page construction:
  - 1 hero
  - Up to 6 rows, each with 3–10 titles
  - Row types: next_episode, popular_in_dialect, new_releases, because_you_watched, genre
  - All titles in the page dialect and available at publish time
  - Each content_slug appears only once per page
  - Never publish "Continue Watching"
  - Rows with <3 valid titles are dropped
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from . import config as cfg
from .loader import DataPack, _ts, title_available_at
from .viewer_state import ViewerState


def _ist_date_str(dt: datetime) -> str:
    """Convert UTC datetime to IST date string."""
    return dt.astimezone(cfg.IST).strftime("%Y-%m-%d")


def _ist_hour(dt: datetime) -> int:
    return dt.astimezone(cfg.IST).hour


def _is_quiet_hours(dt: datetime) -> bool:
    h = _ist_hour(dt)
    return h >= cfg.QUIET_HOURS_START or h < cfg.QUIET_HOURS_END


def _get_available_titles(pack: DataPack, dialect: str, at: datetime,
                          withdrawn: dict[str, datetime] | None = None) -> list[dict]:
    """Get all titles available in the given dialect at the given time."""
    titles = []
    for tid, t in pack.catalogue.items():
        if t.get("dialect") != dialect:
            continue
        if not title_available_at(t, at):
            continue
        # Check if withdrawn
        if withdrawn and tid in withdrawn and at >= withdrawn[tid]:
            continue
        # Skip titles with injected descriptions
        if t.get("_has_injection"):
            continue
        titles.append(t)
    return titles


def build_home_page(
    user_id: str,
    pack: DataPack,
    state: ViewerState,
    trigger_time: datetime,
    goal: dict | None,
) -> dict | None:
    """Build a home page for the viewer.

    Returns a home_page dict or None if not enough content.
    """
    dialect = state.get_fact_value("dialect", "")
    if dialect not in cfg.VALID_DIALECTS:
        return None

    available = _get_available_titles(pack, dialect, trigger_time, pack.withdrawn_titles)
    if len(available) < cfg.HOME_MIN_TITLES_PER_ROW:
        return None

    used_slugs: set[str] = set()
    rows: list[dict] = []

    # Helper: pick titles not yet used
    def pick_titles(candidates: list[dict], max_count: int = cfg.HOME_MAX_TITLES_PER_ROW) -> list[str]:
        selected = []
        for t in candidates:
            slug = t["content_slug"]
            if slug in used_slugs:
                continue
            selected.append(t["title_id"])
            used_slugs.add(slug)
            if len(selected) >= max_count:
                break
        return selected

    # ── Row 1: next_episode (if user has unfinished series) ──────────
    user_events = pack.events_by_user.get(user_id, [])
    play_events = [e for e in user_events if e["event"] == "play_started"]
    
    # Find series user has watched with unwatched episodes
    slug_played: dict[str, datetime] = {}
    for e in play_events:
        tid = e.get("properties", {}).get("title_id", "")
        cat = pack.catalogue.get(tid)
        if cat and cat.get("dialect") == dialect:
            slug = cat["content_slug"]
            rcv = e["_received_at"]
            if slug not in slug_played or rcv > slug_played[slug]:
                slug_played[slug] = rcv

    # Sort by recency, filter to available with unwatched eps
    next_ep_candidates = []
    for slug, last_play in sorted(slug_played.items(), key=lambda x: x[1], reverse=True):
        eps = pack.episodes.get(slug, [])
        completed_eps = set()
        for e in user_events:
            if e["event"] == "episode_completed":
                ep_id = e.get("properties", {}).get("episode_id", "")
                ep_entry = pack.episodes_flat.get(ep_id)
                if ep_entry and ep_entry.get("content_slug") == slug:
                    completed_eps.add(ep_id)
        unwatched = [ep for ep in eps if ep["episode_id"] not in completed_eps]
        if unwatched:
            cat = pack.catalogue_by_slug.get(slug)
            if cat and title_available_at(cat, trigger_time):
                next_ep_candidates.append(cat)

    if next_ep_candidates:
        titles = pick_titles(next_ep_candidates)
        if len(titles) >= cfg.HOME_MIN_TITLES_PER_ROW:
            rows.append({"row_type": "next_episode", "title_ids": titles})

    # ── Row 2: popular_in_dialect ────────────────────────────────────
    # Sort by play count in events
    title_play_counts: dict[str, int] = {}
    for e in pack.events:
        if e["event"] == "play_started":
            tid = e.get("properties", {}).get("title_id", "")
            cat = pack.catalogue.get(tid)
            if cat and cat.get("dialect") == dialect:
                title_play_counts[tid] = title_play_counts.get(tid, 0) + 1
    
    popular = sorted(
        [t for t in available if t["title_id"] in title_play_counts],
        key=lambda t: title_play_counts.get(t["title_id"], 0),
        reverse=True,
    )
    if not popular:
        popular = available  # fallback: any available
    titles = pick_titles(popular)
    if len(titles) >= cfg.HOME_MIN_TITLES_PER_ROW:
        rows.append({"row_type": "popular_in_dialect", "title_ids": titles})

    # ── Row 3: new_releases ──────────────────────────────────────────
    cutoff = trigger_time - timedelta(days=30)
    new_releases = sorted(
        [t for t in available if t.get("_available_from") and t["_available_from"] >= cutoff],
        key=lambda t: t["_available_from"],
        reverse=True,
    )
    titles = pick_titles(new_releases)
    if len(titles) >= cfg.HOME_MIN_TITLES_PER_ROW:
        rows.append({"row_type": "new_releases", "title_ids": titles})

    # ── Row 4: because_you_watched (based on user genre preferences) ─
    user_genres: dict[str, int] = {}
    for e in play_events:
        tid = e.get("properties", {}).get("title_id", "")
        cat = pack.catalogue.get(tid)
        if cat:
            for g in cat.get("_genres", []):
                user_genres[g] = user_genres.get(g, 0) + 1

    if user_genres:
        top_genre = max(user_genres, key=user_genres.get)
        genre_matches = [t for t in available if top_genre in t.get("_genres", [])]
        titles = pick_titles(genre_matches)
        if len(titles) >= cfg.HOME_MIN_TITLES_PER_ROW:
            rows.append({"row_type": "because_you_watched", "title_ids": titles})

    # ── Row 5-6: genre rows (fill remaining slots) ───────────────────
    genre_buckets: dict[str, list[dict]] = {}
    for t in available:
        for g in t.get("_genres", []):
            genre_buckets.setdefault(g, []).append(t)

    for genre, titles_in_genre in sorted(genre_buckets.items(), key=lambda x: len(x[1]), reverse=True):
        if len(rows) >= cfg.HOME_MAX_ROWS:
            break
        titles = pick_titles(titles_in_genre)
        if len(titles) >= cfg.HOME_MIN_TITLES_PER_ROW:
            rows.append({"row_type": "genre", "title_ids": titles, "genre": genre})

    if not rows:
        return None

    # ── Hero: pick most popular available title not yet used ──────────
    hero_id = None
    for t in sorted(available, key=lambda t: title_play_counts.get(t["title_id"], 0), reverse=True):
        if t["content_slug"] not in used_slugs:
            hero_id = t["title_id"]
            used_slugs.add(t["content_slug"])
            break

    action_id = f"hp-{user_id}-{trigger_time.strftime('%Y%m%dT%H%M%S')}"

    return {
        "action_id": action_id,
        "type": "home_page",
        "user_id": user_id,
        "dialect": dialect,
        "hero_title_id": hero_id,
        "rows": rows,
        "published_at": trigger_time.isoformat(),
        "ttl_minutes": cfg.HOME_PAGE_TTL_MINUTES,
    }


def plan_message(
    user_id: str,
    pack: DataPack,
    state: ViewerState,
    trigger_time: datetime,
    goal: dict | None,
) -> dict | None:
    """Plan a push/WhatsApp/in_app message if allowed.

    Returns an action dict or None.
    """
    ist_date = _ist_date_str(trigger_time)
    messages_today = state.get_messages_sent_on_date(ist_date)
    if messages_today >= cfg.MAX_MESSAGES_PER_DAY:
        return None

    push_ok = state.get_fact_value("push_consent", False)
    wa_ok = state.get_fact_value("whatsapp_consent", False)
    dialect = state.get_fact_value("dialect", "")

    # Determine send time (schedule outside quiet hours)
    send_at = trigger_time
    if _is_quiet_hours(send_at):
        # Schedule for 08:00 IST next day
        ist_now = send_at.astimezone(cfg.IST)
        next_day_8am = ist_now.replace(hour=8, minute=0, second=0, microsecond=0)
        if ist_now.hour >= cfg.QUIET_HOURS_START:
            next_day_8am += timedelta(days=1)
        send_at = next_day_8am.astimezone(cfg.UTC)

    # Check messages for the scheduled date too
    scheduled_ist_date = _ist_date_str(send_at)
    if scheduled_ist_date != ist_date:
        if state.get_messages_sent_on_date(scheduled_ist_date) >= cfg.MAX_MESSAGES_PER_DAY:
            return None

    # Try push first, then WhatsApp, then in_app
    if push_ok and not _is_quiet_hours(send_at):
        return _build_push(user_id, pack, state, send_at, goal, dialect)
    elif wa_ok and not _is_quiet_hours(send_at):
        return _build_whatsapp(user_id, pack, state, send_at, goal, dialect)
    else:
        # In-app message (no consent required)
        return _build_in_app(user_id, pack, state, trigger_time, goal, dialect)


def _build_push(
    user_id: str, pack: DataPack, state: ViewerState,
    send_at: datetime, goal: dict | None, dialect: str,
) -> dict | None:
    """Build a push notification action."""
    # Find a good title to recommend
    available = _get_available_titles(pack, dialect, send_at, pack.withdrawn_titles)
    if not available:
        return None

    # Pick best title based on goal
    target = _pick_target_title(available, pack, state, goal)
    if not target:
        return None

    title_name = target.get("title", "")
    content_type = target.get("content_type", "")

    # Build copy
    push_title = f"New on OTT: {title_name}"
    if len(push_title) > cfg.PUSH_TITLE_MAX_CHARS:
        push_title = title_name[:cfg.PUSH_TITLE_MAX_CHARS]

    push_body = f"Watch {title_name} now"
    if content_type == "micro_drama":
        push_body = f"New episodes of {title_name} available"
    elif content_type == "film":
        push_body = f"Watch the {target.get('_genres', [''])[0].lower()} film {title_name}"

    if len(push_body) > cfg.PUSH_BODY_MAX_CHARS:
        push_body = push_body[:cfg.PUSH_BODY_MAX_CHARS]

    # Check forbidden phrases
    combined = (push_title + " " + push_body).lower()
    for phrase in cfg.FORBIDDEN_PHRASES:
        if phrase in combined:
            return None  # safety: skip

    action_id = f"push-{user_id}-{send_at.strftime('%Y%m%dT%H%M%S')}"

    return {
        "action_id": action_id,
        "type": "push",
        "user_id": user_id,
        "title_id": target["title_id"],
        "send_at": send_at.isoformat(),
        "published_at": send_at.isoformat(),
        "push_title": push_title,
        "push_body": push_body,
        "deeplink": f"ott://title/{target['title_id']}",
        "status": "pending",
    }


def _build_whatsapp(
    user_id: str, pack: DataPack, state: ViewerState,
    send_at: datetime, goal: dict | None, dialect: str,
) -> dict | None:
    """Build a WhatsApp message action using approved templates only."""
    available = _get_available_titles(pack, dialect, send_at, pack.withdrawn_titles)
    if not available:
        return None

    sub_status = state.get_fact_value("subscription_status", "never")
    action_id = f"wa-{user_id}-{send_at.strftime('%Y%m%dT%H%M%S')}"

    # Choose template based on goal
    if goal and goal.get("goal_id") == "G04" and sub_status == "trial_active":
        # Trial ending template
        return {
            "action_id": action_id,
            "type": "whatsapp",
            "user_id": user_id,
            "template_id": "wa_trial_ending_v1",
            "variables": {"plan_price": "₹149/month"},
            "send_at": send_at.isoformat(),
            "published_at": send_at.isoformat(),
            "status": "pending",
        }

    # Default: recommend a title
    target = _pick_target_title(available, pack, state, goal)
    if not target:
        return None

    # Dialect name mapping
    dialect_names = {
        "d1": "Odia", "d2": "Bhojpuri", "d3": "Chhattisgarhi",
        "d4": "Rajasthani", "d5": "Haryanvi", "d6": "Assamese",
    }
    dialect_name = dialect_names.get(dialect, dialect)

    # Check if there's a new episode
    slug = target["content_slug"]
    eps = pack.episodes.get(slug, [])
    if eps and target.get("content_type") in ("micro_drama", "show"):
        ep = eps[-1]  # latest episode
        return {
            "action_id": action_id,
            "type": "whatsapp",
            "user_id": user_id,
            "title_id": target["title_id"],
            "template_id": "wa_new_episode_v2",
            "variables": {
                "title_name": target["title"],
                "episode_number": str(ep["_episode"]),
            },
            "send_at": send_at.isoformat(),
            "published_at": send_at.isoformat(),
            "status": "pending",
        }

    return {
        "action_id": action_id,
        "type": "whatsapp",
        "user_id": user_id,
        "title_id": target["title_id"],
        "template_id": "wa_recommend_title_v1",
        "variables": {
            "title_name": target["title"],
            "dialect_name": dialect_name,
        },
        "send_at": send_at.isoformat(),
        "published_at": send_at.isoformat(),
        "status": "pending",
    }


def _build_in_app(
    user_id: str, pack: DataPack, state: ViewerState,
    trigger_time: datetime, goal: dict | None, dialect: str,
) -> dict | None:
    """Build an in-app message (no consent required)."""
    available = _get_available_titles(pack, dialect, trigger_time, pack.withdrawn_titles)
    if not available:
        return None

    target = _pick_target_title(available, pack, state, goal)
    if not target:
        return None

    action_id = f"inapp-{user_id}-{trigger_time.strftime('%Y%m%dT%H%M%S')}"

    return {
        "action_id": action_id,
        "type": "in_app_message",
        "user_id": user_id,
        "title_id": target["title_id"],
        "published_at": trigger_time.isoformat(),
        "message": f"Check out {target['title']}",
        "status": "pending",
    }


def _pick_target_title(
    available: list[dict], pack: DataPack, state: ViewerState,
    goal: dict | None,
) -> dict | None:
    """Pick the best title to recommend based on the goal."""
    if not available:
        return None

    # If goal is resume_series, pick the target title
    if goal and goal.get("goal_id") == "G07":
        target_slug = state.get_fact_value("resume_target_slug")
        if target_slug:
            for t in available:
                if t["content_slug"] == target_slug:
                    return t

    # If goal is try_new_release, pick a new release
    if goal and goal.get("goal_id") == "G08":
        from datetime import timezone
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=30)
        new = [t for t in available if t.get("_available_from") and t["_available_from"] >= cutoff]
        if new:
            return new[0]

    # Default: first available
    return available[0]
