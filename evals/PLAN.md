# ITBench-AA evaluation plan

Written 2026-11-11, before any agent or prompt exists. Changes after this date are listed at the bottom with a reason;
nothing here is changed after test-set results are seen.

## Data
- ITBench-AA SRE, public release (40 scenarios, CC-BY-4.0), ingested to Parquet by `evals/ingest.py` (lossless:
  row counts reconcile with raw line counts, multi-line values included).
- Agents read only `data/itbench-aa/snapshots/<id>/`; ground truth lives in `data/itbench-aa/truth/` and no tool can
  read it.

## Split (fixed by `evals/split.py`, seed 20261111, committed in `evals/split.json`)
- **Dev, 10 scenarios** (iterate freely): 4 chaos-mesh, 3 feature-flag, 3 Kubernetes-config faults.
- **Test, 30 scenarios** (scored once): 10 / 10 / 10.
- Known exposure of test scenarios, recorded for honesty: the public leaderboard page describes Scenario-3 (adHighCpu
  flag) and Scenario-16 (shipping env var); Scenario-3's ground truth was printed once while inspecting the schema;
  Scenario-27's raw rows were viewed to debug the file format. None of these is used to write prompts or tools.

## Setups (same model, same tools, same budgets)
| Setup | What | Test runs |
|---|---|---|
| A | Single agent with all read-only tools | 30 × 3 |
| B | Commander + investigators, no verifier | 10 × 3 (first 10 test ids by number) |
| C | Commander + investigators + verifier | 30 × 3 |

## Scoring
- ITBench-AA's metric: per run, precision of the submitted root-cause entities if every ground-truth root cause is
  found, else 0; averaged over runs and scenarios. Entity matching follows the ground truth's groups, filters and
  aliases (implemented in `evals/score.py`, unit-tested on hand-written submissions before any agent run).
- Headline: C vs A, paired by scenario, with a bootstrap 95% interval over scenarios.
- Also reported: recall-only rate, mean false positives, turns, tokens and API-equivalent cost per run, wall time.
- The public leaderboard (59 tasks incl. 19 private, Stirrup harness) is cited as context only.

## How runs execute
- Claude Agent SDK on the subscription, clean environment (no plugins, user settings, memory files or connectors),
  request captured and diffed against the intended prompt before scoring starts, model/effort/max tokens pinned.
- The runner refuses any API key, records each run atomically, and stops at 80% of the 5-hour or the weekly usage
  window; it resumes after the reset.
- Before the scored run: 3 dev scenarios measure quota use per run → number of nights, written here.

## Expansion, in this order, only if quota allows (decided before the scored run)
1. Setup B on all 30 test scenarios.
2. 5 repeats instead of 3.
3. ~~More scenarios from ITBench-Lite~~: not available (its SRE scenarios are the same IDs).
4. All 40 scenarios as a secondary figure, labeled "includes the 10 dev scenarios".

## Change log
- (none yet)
