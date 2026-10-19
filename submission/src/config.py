"""Central configuration – single source of truth for all tunables."""
from datetime import timezone, timedelta

# ── Timezone ─────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

# ── Staleness / freshness ────────────────────────────────────────────
STALE_TRIGGER_THRESHOLD_S = 300          # >5 min = flag as offline-buffered
OFFLINE_BUFFERED_THRESHOLD_S = 300       # same as stale, configurable separately

# ── Budgets ──────────────────────────────────────────────────────────
MAX_MODEL_CALLS = 8
MAX_TOOL_CALLS = 20
DECISION_DEADLINE_S = 60                 # must publish within 60 s
DECISION_HARD_FAIL_S = 300               # >5 min = failure

# ── Home-page limits ─────────────────────────────────────────────────
HOME_MAX_ROWS = 6
HOME_MIN_TITLES_PER_ROW = 3
HOME_MAX_TITLES_PER_ROW = 10
HOME_PAGE_TTL_MINUTES = 120

# ── Messaging limits ─────────────────────────────────────────────────
PUSH_TITLE_MAX_CHARS = 40
PUSH_BODY_MAX_CHARS = 120
QUIET_HOURS_START = 22   # IST hour
QUIET_HOURS_END = 8      # IST hour
MAX_MESSAGES_PER_DAY = 1  # push + whatsapp combined per IST day

FORBIDDEN_PHRASES = [
    "last chance", "hurry", "only today", "free forever", "guaranteed",
]

# ── Pricing (valid prices only) ──────────────────────────────────────
VALID_PRICES_INR = {1, 149, 399}

# ── Allowed row types ────────────────────────────────────────────────
ALLOWED_ROW_TYPES = frozenset([
    "next_episode", "because_you_watched", "popular_in_dialect",
    "new_releases", "genre",
])

# ── WhatsApp approved template IDs ───────────────────────────────────
APPROVED_TEMPLATE_IDS = frozenset([
    "wa_new_episode_v2", "wa_recommend_title_v1", "wa_trial_ending_v1",
])

# ── Goal G10 skip reason ─────────────────────────────────────────────
G10_SKIP_REASON = (
    "G10 (meaningful_engagement_7d) has success.event=null and "
    "condition='engages meaningfully' – there is no concrete success "
    "event in the data that can be attributed. Skipped."
)

# ── Injection-detection keywords ─────────────────────────────────────
INJECTION_KEYWORDS = [
    "system note", "ignore", "previous rules", "ai agent",
    "instruction", "[assistant", "SYSTEM:", "ignore your",
]

# ── Known valid dialects (d1-d6 per brief) ───────────────────────────
VALID_DIALECTS = frozenset(["d1", "d2", "d3", "d4", "d5", "d6"])

# ── Model retry budget ──────────────────────────────────────────────
MODEL_MAX_RETRIES = 2
MODEL_TIMEOUT_S = 10
