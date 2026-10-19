# One User, One Agent – OTT Platform

A deterministic, rule-based agent system where each viewer gets one agent that owns
**one measurable goal, one active plan**, and the viewer's history. On each `app_open`
trigger, the agent decides among: `HOME_PAGE`, `PUSH`, `WHATSAPP`, `IN_APP`, or
`NO_ACTION` — always with a recorded reason.

## Quick Start

```bash
# From a fresh clone, ensure Python 3.11+ is installed
cd submission

# Run the full replay (one command)
python -m src.cli replay --pack .. --out sample_run/ --mock

# Inspect decisions for a specific user (or overall summary)
python -m src.cli inspect --decisions sample_run/decisions.jsonl --user U-02344
python -m src.cli inspect --decisions sample_run/decisions.jsonl

# Run tests
python -m pytest tests/ -v
```

**No API keys needed** — the mock provider is used by default.

## Project Structure

```
submission/
├── src/
│   ├── __init__.py
│   ├── __main__.py           # python -m src.cli entrypoint
│   ├── cli.py                # CLI wrapper
│   ├── config.py             # Central configuration (single source of truth)
│   ├── loader.py             # Step 1: Load & validate data pack
│   ├── viewer_state.py       # Step 2: Per-viewer JSON state store
│   ├── fact_resolver.py      # Step 3: Multi-source fact resolution
│   ├── goal_planner.py       # Step 4: Deterministic goal selection
│   ├── intervention_planner.py # Step 5: Rules-first intervention planning
│   ├── model_provider.py     # Step 6: Mock/real model abstraction
│   ├── verifier.py           # Step 7: Independent decision verifier
│   ├── replay.py             # Step 8: Replay harness + post-replay pipeline
│   ├── outcome_judge.py      # Step 10: Goal outcome judging
│   └── impact_reporter.py    # Step 11: ITT impact analysis
├── tests/
│   ├── test_loader.py        # 17 loader tests
│   └── test_system.py        # 24 system-wide tests
├── sample_run/               # Pre-generated output
│   ├── decisions.jsonl
│   ├── actions.jsonl
│   ├── outcomes.jsonl
│   ├── latency_report.json
│   ├── impact_report.md
│   ├── final_report.md
│   ├── data_issues.json
│   └── viewer_state/         # Per-viewer JSON state files
├── README.md                 # ← This file
├── ARCHITECTURE.md           # System design
├── AI_COLLABORATION.md       # AI tool usage log
└── KNOWN_LIMITATIONS.md      # Acknowledged gaps
```

## Dependencies

Only Python standard library + `pytest` for testing. No external packages required.

## How It Works

1. **Load** the data pack (events, profiles, catalogue, config, replay data)
2. **Build a timeline** of triggers + live events + control events, sorted by `deliver_at`
3. For each trigger:
   - Skip duplicates (idempotency by `trigger_event_id`)
   - Skip holdout-arm viewers (zero actions)
   - Skip stale triggers (`received_at - occurred_at > 5min`)
   - Resolve facts from multiple sources with documented precedence
   - Select the highest-priority eligible goal
   - Plan interventions (home page + optional message)
   - **Verify** the decision independently against all hard rules
   - Record the decision, or downgrade to NO_ACTION if rejected
4. **Judge outcomes** after the replay using events in the goal window
5. **Run the simulator** to generate simulated viewer behaviour
6. **Compute ITT impact** comparing agent-arm vs holdout-arm success rates
7. **Write the final report** with data issues, metrics, and honest impact analysis

## Key Design Decisions

- **Rules-first, model-optional**: The core pipeline is fully deterministic. The mock
  model provider is used by default; model output is never treated as evidence.
- **Independent verifier**: Cannot import planner internals. Re-checks every hard rule
  and can reject or downgrade decisions.
- **Idempotent writes**: Same trigger → same decision. ViewerState deduplicates by
  `trigger_event_id`.
- **Honest reporting**: Impact is computed as intention-to-treat on randomized arms.
  The pilot's selection bias is explicitly explained.
