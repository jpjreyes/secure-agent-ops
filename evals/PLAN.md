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
- ITBench-AA's metric and Artificial Analysis' published grading rules (`evals/score.py`, unit-tested): entities as
  `namespace/Kind/name`; Deployment ≡ Pod for the same workload; chaos-mesh kinds interchangeable for the same
  experiment; alias groups merged into scoring groups (counted once); 0 if any root-cause group is missed, else
  TP / (TP + FP). AA normalizes names with an LLM judge; ours is rule-based (possibly stricter) and identical for
  every setup.
- Task statement: AA's generation prompt (`evals/aa_prompt.md`) for every setup, shell instructions replaced by
  our read-only tools.
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

## Live incidents (our own environment; written 2026-11-12, before any agent saw a live episode)
- **Set:** the 19 faults in `src/sao/faults/catalog.py` (12 demo flags, 7 configuration faults), one recorded
  episode each (`runs/live/<fault>-r1`), recorded on the 6-CPU VM. An episode is scored only if at least one of the
  benchmark's service alerts fired after injection (`symptom_observed`); episodes without symptoms are listed, not
  scored as misses.
- **What runs:** setups A and C with the configuration frozen at commit c48e258 (team prompts and verifier v2, A's
  prompt), 1 repeat first; 3 repeats if the measured quota allows (decided before scoring, written here).
- **No tuning on live episodes.** Prompts and tools were developed on the 10 ITBench-AA dev scenarios only. If a live
  result leads to a change, the changed version is reported separately as "after one fix", never mixed with the
  first-contact numbers (same protocol as P4's held-out portal).
- **Scoring:** the same scorer; ground truth from the fault catalog (root = the object that was changed: the
  flagd-config ConfigMap for flags, the Deployment / Service / NetworkPolicy for configuration faults; the services
  expected to show symptoms are listed as non-root groups).
- **Reported:** mean score, root cause found, false positives, cost and time per run, per family (flag vs config).

## Change log
- 2026-11-12: verifier v2 (rules on every candidate; submission computed in code), decided on dev only (repo
  DECISIONS.md D9). Live-incident section added before any live result.
