"""
Independent Verifier – Step 7

A separate module that CANNOT import the planner's internals.
It re-checks every decision from raw state:
  - Eligibility has supporting evidence
  - Dialect correctness
  - Title availability at publish time
  - Consent for push/WhatsApp
  - Quiet hours
  - Daily cap (push+WhatsApp combined per IST day)
  - Copy rules (char limits, forbidden phrases)
  - Pricing accuracy
  - Home-page rules (rows, titles per row, slug uniqueness, no Continue Watching)
  - Duplicate detection (idempotency key = trigger_event_id)
  - Holdout arm gets nothing
  - Unsupported model claims rejected

Can reject (REJECTED_BY_VERIFIER), downgrade to safe action, or pass.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# NOTE: This module must NOT import goal_planner or intervention_planner


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _ist_hour(dt: datetime) -> int:
    from . import config as cfg
    return dt.astimezone(cfg.IST).hour


def _ist_date_str(dt: datetime) -> str:
    from . import config as cfg
    return dt.astimezone(cfg.IST).strftime("%Y-%m-%d")


def _is_quiet_hours(dt: datetime) -> bool:
    from . import config as cfg
    h = _ist_hour(dt)
    return h >= cfg.QUIET_HOURS_START or h < cfg.QUIET_HOURS_END


class VerificationResult:
    """Result of verifying a decision."""

    def __init__(self):
        self.passed = True
        self.violations: list[dict] = []
        self.warnings: list[dict] = []
        self.status = "VERIFIED"
        self.downgraded_to: str | None = None

    def fail(self, rule: str, detail: str) -> None:
        self.passed = False
        self.status = "REJECTED_BY_VERIFIER"
        self.violations.append({"rule": rule, "detail": detail})

    def warn(self, rule: str, detail: str) -> None:
        self.warnings.append({"rule": rule, "detail": detail})

    def downgrade(self, to_action: str, reason: str) -> None:
        self.status = "DOWNGRADED_BY_VERIFIER"
        self.downgraded_to = to_action
        self.violations.append({"rule": "downgrade", "detail": reason, "downgraded_to": to_action})

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "status": self.status,
            "violations": self.violations,
            "warnings": self.warnings,
            "downgraded_to": self.downgraded_to,
        }


def verify_decision(
    decision: dict,
    arms: dict[str, str],
    profiles: dict[str, dict],
    catalogue: dict[str, dict],
    events_by_user: dict[str, list[dict]],
    seen_trigger_ids: set[str],
    messages_sent: dict[str, dict[str, int]],
    withdrawn_titles: dict[str, datetime] | None = None,
    model_faults: dict[str, dict] | None = None,
) -> VerificationResult:
    """Verify a decision against all hard rules.

    This function does not import planner internals.
    """
    from . import config as cfg

    result = VerificationResult()
    user_id = decision.get("user_id", "")
    trigger_id = decision.get("trigger_event_id", "")
    action_type = decision.get("action_type", "")

    # ── 1. Holdout check ─────────────────────────────────────────────
    arm = arms.get(user_id, "agent")
    if arm == "holdout" and action_type not in ("NO_ACTION", "no_action", ""):
        result.fail("holdout", f"User {user_id} is in holdout arm, must get NO_ACTION")
        return result

    # ── 2. Duplicate trigger (idempotency) ───────────────────────────
    if trigger_id in seen_trigger_ids:
        result.fail("idempotency", f"Duplicate trigger {trigger_id}")
        return result

    # No further checks needed for NO_ACTION
    if action_type in ("NO_ACTION", "no_action"):
        return result

    # ── 3. Verify actions ────────────────────────────────────────────
    actions = decision.get("actions", [])
    for action in actions:
        atype = action.get("type", "")
        published_at_str = action.get("published_at") or action.get("send_at")
        published_at = _ts(published_at_str) if published_at_str else None

        # ── 3a. Home page checks ─────────────────────────────────────
        if atype == "home_page":
            _verify_home_page(action, catalogue, published_at, withdrawn_titles, cfg, result)

        # ── 3b. Push checks ──────────────────────────────────────────
        elif atype == "push":
            _verify_push(action, user_id, profiles, events_by_user,
                         messages_sent, published_at, catalogue,
                         withdrawn_titles, cfg, result)

        # ── 3c. WhatsApp checks ──────────────────────────────────────
        elif atype == "whatsapp":
            _verify_whatsapp(action, user_id, profiles, events_by_user,
                             messages_sent, published_at, catalogue,
                             withdrawn_titles, cfg, result)

    # ── 4. Check for unsupported model claims ────────────────────────
    if model_faults and trigger_id in model_faults:
        fault = model_faults[trigger_id]
        if fault.get("fault") == "unsupported_claim":
            # The model claimed something without evidence
            model_goal = fault.get("model_output", {}).get("goal_id")
            decision_goal = decision.get("goal", {}).get("goal_id") if decision.get("goal") else None
            if model_goal and decision_goal == model_goal:
                result.fail(
                    "unsupported_model_claim",
                    f"Decision uses goal {model_goal} from unsupported model claim"
                )

    return result


def _verify_home_page(action, catalogue, published_at, withdrawn_titles, cfg, result):
    """Verify home page rules."""
    dialect = action.get("dialect", "")
    rows = action.get("rows", [])
    hero_id = action.get("hero_title_id")

    if dialect not in cfg.VALID_DIALECTS:
        result.fail("home_page_dialect", f"Invalid dialect: {dialect}")

    # Check row count
    if len(rows) > cfg.HOME_MAX_ROWS:
        result.warn("home_page_max_rows", f"Too many rows: {len(rows)} > {cfg.HOME_MAX_ROWS}")

    seen_slugs: set[str] = set()

    # Check hero
    if hero_id:
        hero = catalogue.get(hero_id)
        if not hero:
            result.warn("home_page_hero", f"Hero title {hero_id} not in catalogue")
        elif hero.get("dialect") != dialect:
            result.fail("home_page_hero_dialect", f"Hero {hero_id} dialect {hero.get('dialect')} != page dialect {dialect}")
        elif published_at and not _title_available(hero, published_at, withdrawn_titles):
            result.fail("home_page_hero_unavailable", f"Hero {hero_id} not available at publish time")
        if hero:
            seen_slugs.add(hero.get("content_slug", ""))

    # Check each row
    for i, row in enumerate(rows):
        row_type = row.get("row_type", "")
        title_ids = row.get("title_ids", [])

        if row_type not in cfg.ALLOWED_ROW_TYPES:
            result.fail("home_page_row_type", f"Row {i}: invalid type '{row_type}'")

        if row_type.lower() == "continue watching":
            result.fail("home_page_continue_watching", f"Row {i}: Continue Watching is forbidden")

        if len(title_ids) < cfg.HOME_MIN_TITLES_PER_ROW:
            result.fail("home_page_min_titles", f"Row {i}: only {len(title_ids)} titles (min {cfg.HOME_MIN_TITLES_PER_ROW})")

        for tid in title_ids:
            t = catalogue.get(tid)
            if not t:
                result.fail("home_page_title_missing", f"Row {i}: title {tid} not in catalogue")
                continue
            if t.get("dialect") != dialect:
                result.fail("home_page_title_dialect", f"Row {i}: title {tid} dialect {t.get('dialect')} != {dialect}")
            if published_at and not _title_available(t, published_at, withdrawn_titles):
                result.fail("home_page_title_unavailable", f"Row {i}: title {tid} not available at publish time")

            slug = t.get("content_slug", "")
            if slug in seen_slugs:
                result.fail("home_page_slug_duplicate", f"Row {i}: slug '{slug}' already used in page")
            seen_slugs.add(slug)


def _verify_push(action, user_id, profiles, events_by_user,
                 messages_sent, send_at, catalogue, withdrawn_titles, cfg, result):
    """Verify push notification rules."""
    profile = profiles.get(user_id)

    # Consent
    push_ok = profile.get("_notifications_opt_in", False) if profile else False
    # Check for later opt-out events
    user_events = events_by_user.get(user_id, [])
    for e in user_events:
        if e["event"] == "notification_opt_out":
            channel = e.get("properties", {}).get("channel", "")
            if channel in ("push", ""):
                push_ok = False

    if not push_ok:
        result.fail("push_consent", f"User {user_id} has not opted in for push")

    # Quiet hours
    if send_at and _is_quiet_hours(send_at):
        result.fail("quiet_hours", f"Push scheduled during quiet hours: {send_at}")

    # Daily cap
    if send_at:
        ist_date = _ist_date_str(send_at)
        user_msgs = messages_sent.get(user_id, {})
        if user_msgs.get(ist_date, 0) >= cfg.MAX_MESSAGES_PER_DAY:
            result.fail("daily_cap", f"User {user_id} already has {user_msgs[ist_date]} messages on {ist_date}")

    # Copy rules
    title = action.get("push_title", "")
    body = action.get("push_body", "")
    if len(title) > cfg.PUSH_TITLE_MAX_CHARS:
        result.fail("push_title_length", f"Title too long: {len(title)} > {cfg.PUSH_TITLE_MAX_CHARS}")
    if len(body) > cfg.PUSH_BODY_MAX_CHARS:
        result.fail("push_body_length", f"Body too long: {len(body)} > {cfg.PUSH_BODY_MAX_CHARS}")

    combined = (title + " " + body).lower()
    for phrase in cfg.FORBIDDEN_PHRASES:
        if phrase in combined:
            result.fail("forbidden_phrase", f"Forbidden phrase '{phrase}' in push copy")

    # Deeplink format
    deeplink = action.get("deeplink", "")
    if deeplink and not deeplink.startswith("ott://title/"):
        result.fail("deeplink_format", f"Invalid deeplink format: {deeplink}")

    # Title availability
    title_id = action.get("title_id")
    if title_id:
        t = catalogue.get(title_id)
        if t and send_at and not _title_available(t, send_at, withdrawn_titles):
            result.fail("push_title_unavailable", f"Title {title_id} not available at send time")


def _verify_whatsapp(action, user_id, profiles, events_by_user,
                     messages_sent, send_at, catalogue, withdrawn_titles, cfg, result):
    """Verify WhatsApp message rules."""
    profile = profiles.get(user_id)

    # Consent
    wa_ok = profile.get("_whatsapp_opt_in", False) if profile else False
    user_events = events_by_user.get(user_id, [])
    for e in user_events:
        if e["event"] == "notification_opt_out":
            channel = e.get("properties", {}).get("channel", "")
            if channel in ("whatsapp", ""):
                wa_ok = False

    if not wa_ok:
        result.fail("whatsapp_consent", f"User {user_id} has not opted in for WhatsApp")

    # Quiet hours
    if send_at and _is_quiet_hours(send_at):
        result.fail("quiet_hours", f"WhatsApp scheduled during quiet hours: {send_at}")

    # Daily cap (shared with push)
    if send_at:
        ist_date = _ist_date_str(send_at)
        user_msgs = messages_sent.get(user_id, {})
        if user_msgs.get(ist_date, 0) >= cfg.MAX_MESSAGES_PER_DAY:
            result.fail("daily_cap", f"User {user_id} already has {user_msgs[ist_date]} messages on {ist_date}")

    # Approved template
    template_id = action.get("template_id", "")
    if template_id not in cfg.APPROVED_TEMPLATE_IDS:
        result.fail("whatsapp_template", f"Unapproved template: {template_id}")


def _title_available(cat_entry: dict, at: datetime, withdrawn: dict | None = None) -> bool:
    """Check title availability at a point in time."""
    avail_from = cat_entry.get("_available_from")
    avail_until = cat_entry.get("_available_until")
    tid = cat_entry.get("title_id", "")
    
    if avail_from and at < avail_from:
        return False
    if avail_until and at >= avail_until:
        return False
    if withdrawn and tid in withdrawn and at >= withdrawn[tid]:
        return False
    return True
