# AI Collaboration Log

This document records how AI tools were used during development, including
suggestions that were changed or rejected.

## Tools Used

- **AI Coding Assistant**: Used as a pair-programming partner for implementation,
  testing, and documentation. All code was reviewed and validated against the
  data pack before acceptance.

## AI Suggestions Accepted

1. **Goal priority ordering** (G01 > G03 > G02 > G04 > ...): AI suggested
   prioritizing new-user goals (G01, G03) over revenue goals (G04, G05) based
   on the time-sensitivity principle. Accepted as reasonable.

2. **Dual-profile resolution rule**: AI suggested picking the row with the latest
   `updated_at`. Accepted — documented as the conflict resolution strategy.

3. **G07 "3 or more days" interpretation**: AI proposed using `received_at` of the
   last `play_started` event for the content_slug, compared to trigger time.
   Accepted and documented as an assumption.

4. **G09 "at most 1 day in last 14"**: AI proposed counting distinct IST calendar
   days with `play_started` events in the 14 IST days ending on the trigger's
   IST date (inclusive). Accepted and documented.

5. **Pilot bias analysis**: AI correctly identified the selection bias in the pilot
   report's "served vs everyone else" comparison and recomputed the correct ITT
   using randomized arms.

## AI Suggestions Modified

1. **Home page row ordering**: Initial suggestion put `because_you_watched` before
   `popular_in_dialect`. Changed to: `next_episode` → `popular_in_dialect` →
   `new_releases` → `because_you_watched` → `genre`, since users returning to
   the app most likely want to continue watching or see what's popular.

2. **Message channel priority**: AI initially suggested WhatsApp before push.
   Changed to push → WhatsApp → in_app, since push has broader device coverage
   and lower friction for the user.

3. **Withdrawn title handling**: AI initially only tracked withdrawn titles in a
   separate dict. Modified to also update the catalogue entry's `_available_until`
   at load time so `title_available_at()` works correctly without needing the
   separate withdrawn dict.

## AI Suggestions Rejected

1. **Using `occurred_at` for event ordering**: AI initially suggested ordering by
   `occurred_at`. Rejected — the spec says `received_at` is trustworthy while
   `occurred_at` (phone clocks) is not.

2. **Model output as eligibility evidence**: AI suggested using model confidence
   scores to boost goal eligibility. Rejected — the spec explicitly says "model
   output is never evidence or approval."

3. **Caching profile lookups in memory**: AI suggested an LRU cache for profile
   reads. Rejected — profiles are already in a dict; unnecessary complexity.

## Key Design Decisions Made with AI

- The verifier module deliberately does NOT import goal_planner or
  intervention_planner. It uses its own `_ts()`, `_is_quiet_hours()`, and
  `_title_available()` implementations to maintain independence.

- `NO_ACTION` is always a valid, recorded decision with a reason. The system
  prefers NO_ACTION over a potentially incorrect action.

- Consent checking uses a two-phase approach: profile snapshot values are the
  baseline, then any `notification_opt_out` events with `received_at` after the
  snapshot override them.
