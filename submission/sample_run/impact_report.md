## Impact Report

### Intention-to-Treat (ITT) Analysis

The ITT comparison includes ALL users assigned to each arm, regardless of
whether the agent actually took an action for them. This is the correct
causal comparison because it avoids selection bias.

| Group | N | Successes | Rate |
|---|---|---|---|
| Agent arm (all) | 2513 | 7 | 0.0028 (0.3%) |
| Holdout arm (all) | 2487 | 0 | 0.0000 (0.0%) |

**ITT Uplift**: 0.0028 (0.28 percentage points)

**95% Confidence Interval**: [0.0007, 0.0048]
(0.07 to 0.48 pp)

**Statistically significant (95% CI excludes 0)**

### Breakdown by Goal (Agent Arm Only)

| Goal | N | Successes | Rate |
|---|---|---|---|
| G01 | 59 | 0 | 0.0% |
| G02 | 220 | 0 | 0.0% |
| G03 | 1 | 0 | 0.0% |
| G05 | 152 | 3 | 2.0% |
| G06 | 52 | 3 | 5.8% |
| G07 | 243 | 1 | 0.4% |
| G08 | 24 | 0 | 0.0% |

> Note: Holdout users receive no agent action and no goal assignment,
> so per-goal breakdown is only available for the agent arm.

### Pilot Bias Analysis

The pilot summary report claimed a 2.3× uplift (+22.4 points) by comparing
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
| Treatment arm (all) | 1912 | 349 | 18.3% |
| Holdout arm (all) | 1979 | 387 | 19.6% |

**ITT Uplift**: -1.30 percentage points
**95% CI**: [-3.76, 1.16] pp

The corrected ITT estimate is **not** statistically significant – the 95% CI includes zero.

This is dramatically smaller than the biased 22.4pp claim, because the selection
bias inflated the apparent effect by comparing the most-engaged treatment users
against everyone else.

### Simulator Results

- Triggered users: 1746
- Events written: 31659
- actions_total: 1083
- home_page_accepted: 1083
- home_page_published_but_not_shown: 22
- home_page_shown_personalised: 827
- organic_purchases_scripted: 36
