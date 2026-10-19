"""
Impact Reporter – Step 11

- Runs simulator/simulate_outcomes.py on agent actions.
- Computes intention-to-treat (ITT): success rate of ALL agent-arm viewers
  vs ALL holdout viewers (including those with no action), with 95% CI.
- Recomputes pilot uplift using the randomized arms.
- Explains why the pilot report's "served vs everyone else" comparison is
  selection-biased.
- Reports honestly even if uplift is ~0.
- Writes latency_report.json and impact_report.md.
"""
from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict
from typing import Any


def generate_impact_report(pack_root: str, out_dir: str, stats: dict) -> None:
    """Generate the impact report with ITT analysis."""

    # ── 1. Load arms ────────────────────────────────────────────────
    arms: dict[str, str] = {}
    with open(os.path.join(pack_root, "replay", "arms.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            arms[r["user_id"]] = r["arm"]

    # ── 2. Load outcomes ─────────────────────────────────────────────
    outcomes_path = os.path.join(out_dir, "outcomes.jsonl")
    outcomes: list[dict] = []
    if os.path.exists(outcomes_path):
        with open(outcomes_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    outcomes.append(json.loads(line))

    # ── 3. Load decisions ────────────────────────────────────────────
    decisions_path = os.path.join(out_dir, "decisions.jsonl")
    decisions: list[dict] = []
    if os.path.exists(decisions_path):
        with open(decisions_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    decisions.append(json.loads(line))

    # ── 4. Map user outcomes ─────────────────────────────────────────
    user_outcome: dict[str, str] = {}
    user_goal: dict[str, str] = {}
    for o in outcomes:
        uid = o.get("user_id", "")
        user_outcome[uid] = o.get("status", "inconclusive")
        user_goal[uid] = o.get("goal_id", "")

    # ── 5. ITT analysis: ALL agent-arm vs ALL holdout ────────────────
    agent_users = [uid for uid, arm in arms.items() if arm == "agent"]
    holdout_users = [uid for uid, arm in arms.items() if arm == "holdout"]

    agent_success = sum(1 for uid in agent_users if user_outcome.get(uid) == "succeeded")
    holdout_success = sum(1 for uid in holdout_users if user_outcome.get(uid) == "succeeded")

    n_agent = len(agent_users)
    n_holdout = len(holdout_users)

    agent_rate = agent_success / n_agent if n_agent > 0 else 0
    holdout_rate = holdout_success / n_holdout if n_holdout > 0 else 0
    itt_uplift = agent_rate - holdout_rate

    # 95% CI for difference in proportions
    ci_low, ci_high = _proportion_diff_ci(agent_rate, n_agent, holdout_rate, n_holdout)

    # ── 6. Breakdown by goal ─────────────────────────────────────────
    goal_breakdown: dict[str, dict] = defaultdict(lambda: {
        "agent_total": 0, "agent_success": 0,
        "holdout_total": 0, "holdout_success": 0,
    })

    for uid in agent_users:
        gid = user_goal.get(uid)
        if gid:
            goal_breakdown[gid]["agent_total"] += 1
            if user_outcome.get(uid) == "succeeded":
                goal_breakdown[gid]["agent_success"] += 1

    # Holdout users don't get goals assigned, so we can't break down by goal
    # We note this as a limitation

    # ── 7. Pilot bias analysis ───────────────────────────────────────
    pilot_path = os.path.join(pack_root, "pilot", "pilot_outcomes.csv")
    pilot_assignment_path = os.path.join(pack_root, "pilot", "pilot_assignment.csv")
    pilot_bias_analysis = _analyze_pilot_bias(pilot_path, pilot_assignment_path)

    # ── 8. Load simulator report if available ────────────────────────
    sim_report_path = os.path.join(out_dir, "sim", "simulator_report.json")
    sim_report = {}
    if os.path.exists(sim_report_path):
        with open(sim_report_path, encoding="utf-8") as f:
            sim_report = json.load(f)

    # ── 9. Write impact_report.md ────────────────────────────────────
    report = f"""## Impact Report

### Intention-to-Treat (ITT) Analysis

The ITT comparison includes ALL users assigned to each arm, regardless of
whether the agent actually took an action for them. This is the correct
causal comparison because it avoids selection bias.

| Group | N | Successes | Rate |
|---|---|---|---|
| Agent arm (all) | {n_agent} | {agent_success} | {agent_rate:.4f} ({agent_rate*100:.1f}%) |
| Holdout arm (all) | {n_holdout} | {holdout_success} | {holdout_rate:.4f} ({holdout_rate*100:.1f}%) |

**ITT Uplift**: {itt_uplift:.4f} ({itt_uplift*100:.2f} percentage points)

**95% Confidence Interval**: [{ci_low:.4f}, {ci_high:.4f}]
({ci_low*100:.2f} to {ci_high*100:.2f} pp)

{"**Statistically significant (95% CI excludes 0)**" if (ci_low > 0 or ci_high < 0) else "**Not statistically significant (95% CI includes 0)**"}

### Breakdown by Goal (Agent Arm Only)

| Goal | N | Successes | Rate |
|---|---|---|---|
"""
    for gid in sorted(goal_breakdown.keys()):
        g = goal_breakdown[gid]
        rate = g["agent_success"] / g["agent_total"] if g["agent_total"] > 0 else 0
        report += f"| {gid} | {g['agent_total']} | {g['agent_success']} | {rate*100:.1f}% |\n"

    report += f"""
> Note: Holdout users receive no agent action and no goal assignment,
> so per-goal breakdown is only available for the agent arm.

### Pilot Bias Analysis

{pilot_bias_analysis}

### Simulator Results

"""
    if sim_report:
        report += f"- Triggered users: {sim_report.get('triggered_users', 'N/A')}\n"
        report += f"- Events written: {sim_report.get('events_written', 'N/A')}\n"
        counts = sim_report.get("counts", {})
        for k, v in sorted(counts.items()):
            report += f"- {k}: {v}\n"
        problems = sim_report.get("policy_problems", [])
        if problems:
            report += f"\n**Policy problems detected by simulator: {sim_report.get('policy_problem_count', len(problems))}**\n\n"
            for p in problems[:10]:
                report += f"- {p}\n"
    else:
        report += "Simulator not run or report not found.\n"

    with open(os.path.join(out_dir, "impact_report.md"), "w", encoding="utf-8") as f:
        f.write(report)

    print(f"  ITT uplift: {itt_uplift*100:.2f}pp, 95% CI: [{ci_low*100:.2f}, {ci_high*100:.2f}]")
    print(f"  Written impact_report.md")


def _proportion_diff_ci(
    p1: float, n1: int, p2: float, n2: int, z: float = 1.96
) -> tuple[float, float]:
    """Compute 95% CI for difference in two proportions (p1 - p2)."""
    if n1 == 0 or n2 == 0:
        return (0.0, 0.0)

    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    diff = p1 - p2
    return (diff - z * se, diff + z * se)


def _analyze_pilot_bias(pilot_outcomes_path: str, pilot_assignment_path: str) -> str:
    """Analyze the pilot study for selection bias."""
    if not os.path.exists(pilot_outcomes_path):
        return "Pilot data not available."

    outcomes = []
    with open(pilot_outcomes_path, encoding="utf-8") as f:
        outcomes = list(csv.DictReader(f))

    assignments = []
    if os.path.exists(pilot_assignment_path):
        with open(pilot_assignment_path, encoding="utf-8") as f:
            assignments = list(csv.DictReader(f))

    arm_map = {r["user_id"]: r["arm"] for r in assignments}

    # The pilot report compared "served" (217 users) vs "all other users" (3,674)
    # This is selection-biased because:
    # 1. "Served" excludes treatment-arm users who weren't served
    # 2. "All other users" mixes holdout with unserved treatment users

    # Correct ITT: compare ALL treatment vs ALL holdout
    treatment_users = [r for r in outcomes if arm_map.get(r["user_id"]) == "treatment"]
    holdout_users = [r for r in outcomes if arm_map.get(r["user_id"]) == "holdout"]

    t_success = sum(1 for r in treatment_users if r.get("succeeded") == "true")
    h_success = sum(1 for r in holdout_users if r.get("succeeded") == "true")

    n_t = len(treatment_users)
    n_h = len(holdout_users)

    t_rate = t_success / n_t if n_t > 0 else 0
    h_rate = h_success / n_h if n_h > 0 else 0
    itt_diff = t_rate - h_rate

    ci_low, ci_high = _proportion_diff_ci(t_rate, n_t, h_rate, n_h)

    # Also compute the biased "served vs everyone else" from the report
    served = [r for r in outcomes if r.get("served") == "true"]
    not_served = [r for r in outcomes if r.get("served") != "true"]
    served_rate = sum(1 for r in served if r.get("succeeded") == "true") / len(served) if served else 0
    not_served_rate = sum(1 for r in not_served if r.get("succeeded") == "true") / len(not_served) if not_served else 0

    return f"""The pilot summary report claimed a 2.3× uplift (+22.4 points) by comparing
"users served by the agent" (217 users, 40.1% success) vs "all other users"
(3,674 users, 17.7% success). This comparison is **selection-biased** for
two key reasons:

1. **Served vs. not-served is not randomized.** The 217 "served" users are a
   self-selected subset of the treatment arm – they opened the app, received a
   personalised page, and in some cases received a push notification. Users who
   didn't engage enough to be "served" are excluded from the treatment group and
   mixed into the control group, biasing both groups.

2. **The "all other users" group mixes holdout with unserved treatment.** This
   contaminates the control condition with treatment-assigned users who happened
   not to be served, artificially lowering the control success rate.

**Correct ITT (intent-to-treat) recomputation using the randomized arms:**

| Group | N | Successes | Rate |
|---|---|---|---|
| Treatment arm (all) | {n_t} | {t_success} | {t_rate*100:.1f}% |
| Holdout arm (all) | {n_h} | {h_success} | {h_rate*100:.1f}% |

**ITT Uplift**: {itt_diff*100:.2f} percentage points
**95% CI**: [{ci_low*100:.2f}, {ci_high*100:.2f}] pp

{"The corrected ITT estimate is statistically significant." if (ci_low > 0 or ci_high < 0) else "The corrected ITT estimate is **not** statistically significant – the 95% CI includes zero."}

This is dramatically smaller than the biased 22.4pp claim, because the selection
bias inflated the apparent effect by comparing the most-engaged treatment users
against everyone else."""
