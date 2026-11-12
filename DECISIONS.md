# Decisions

Each entry: what we chose, the alternatives, and why. Numbers are filled in as they're measured.

## D1. Orchestration: LangGraph, with each agent on the Claude Agent SDK
- **Chose:** LangGraph for the incident workflow (commander → parallel investigators → verifier → executor), with
  each agent's tool loop running on the Claude Agent SDK.
- **Alternatives:** Claude Agent SDK alone (sub-agents); LangGraph with LangChain agent wrappers.
- **Why:** the workflow is an explicit graph we can test (parallel fan-out, `interrupt()` before any write action,
  checkpoints); LangGraph is the most requested orchestration framework in agent-engineering postings after LangChain
  and the base of most open-source AI SRE agents. The Agent SDK gives each agent a production-grade tool loop and runs
  the same way locally and in the cloud (only the credentials change). Security is enforced by the gateway, outside
  the agent's process, not by in-process hooks.

## D2. Durable state: LangGraph Postgres checkpointer
- **Chose:** a checkpoint after every step, keyed by incident; a crashed run resumes from the last completed step.
  Write actions carry an idempotency key checked at the gateway, so a replayed step can't act twice.
- **Alternatives:** Temporal (with its LangGraph plugin).
- **Why:** runs last minutes and pause at most for one human approval, inside one service. Temporal fits workflows
  that wait for days or span services; here it would add a server without adding a guarantee. Proven by a test that
  kills the run before and during an approval and asserts no action ran twice.

## D3. ITBench-AA: 10 development tasks, 30 held-out test tasks
- **Chose:** the 40 public scenarios split 10/30 before any prompt work; the 30 are scored once, 3 repeats.
  Setups: A single agent (same model, same tools), C full team, both on 30 × 3; B team without verifier on 10 × 3.
- **Alternatives:** all 40 with no held-out set (tuning on the reported tasks); a 10–15 task sample.
- **Why:** scoring on tasks used for tuning overstates results. The paired A-vs-C comparison on the same tasks cancels
  most task-to-task noise. The public leaderboard covers 59 tasks (19 private), so it is cited as context only.
  IBM's ITBench-Lite has the same SRE scenario IDs, so it adds no extra test tasks. Any expansion is fixed in
  `evals/PLAN.md` before the scored run.
- **How it runs:** on a Claude subscription through the Agent SDK, in a clean environment (no plugins, user settings
  or memory files), with the request captured to prove the model sees only our prompt, parameters pinned, and the run
  stopping at 80% of the 5-hour or weekly usage window. Cost is reported as API-equivalent from token counts. The
  deployed version uses an API key; a 5-task parity check compares both paths.

## D4. Policy: Cedar at the gateway, Kubernetes admission as the second layer
- **Chose:** Cedar for every agent tool call (rules on arguments, schema-validated, unit-tested, and a property
  proved in CI with Cedar's symbolic analysis: the executor can never delete or roll back without human approval).
  Kubernetes RBAC + ValidatingAdmissionPolicy (CEL) limit what the executor's service account can do even if the
  gateway were bypassed.
- **Alternatives:** OPA/Rego (and Gatekeeper).
- **Why:** Cedar is built for per-request authorization, is deny-by-default with forbid-overrides-permit, can be
  analyzed formally, and is what AWS uses for AgentCore Policy, so these policies would move to the managed service
  unchanged. OPA is the better choice for a client that already runs Gatekeeper.

## D5. Gateway runtime: one container image, local for evals, Lambda in AWS
- **Chose:** the same image runs next to the local cluster (incidents, all evals, overhead measurements) and on AWS
  Lambda (Web Adapter + SnapStart) for the public demo in front of P5's MCP server. Stateless; kill switch, rate
  limits and the audit chain head live in DynamoDB, so a revocation reaches every instance.
- **Alternatives:** ECS Fargate behind a load balancer; AgentCore Gateway (managed).
- **Why:** each tool call is a short request, which fits a stateless function at ~$0 idle; Fargate costs ~$25–40 a
  month to stay warm. Overhead is reported in three rows (local, Lambda warm, Lambda cold start); provisioned
  concurrency only during demos. AgentCore Gateway would replace the part this project exists to build.

## D6. Live telemetry: record the cluster into the benchmark's snapshot layout
- **Chose:** a recorder polls Kubernetes (objects, events, pod status), Prometheus (service error rate and p95 from
  span metrics, container memory/CPU, firing alerts), Jaeger (spans; errors fetched separately) and OpenSearch
  (logs) every 15 s, and rewrites the incident folder in the same layout the ITBench-AA ingest produces. The telemetry
  tools read it unchanged; during an incident each tool call sees data at most one poll old.
- **Alternatives:** a second implementation of every tool against the live APIs.
- **Why:** one implementation means identical tool names, arguments and outputs on the benchmark and on live faults
  (no "it works on the benchmark tools only"); Kubernetes keeps no object history, so a recorder is needed anyway for
  `changes`/`object_diff`; every live incident becomes a frozen, replayable snapshot (same input for every setup and
  repeat, audit trail of what the agents saw). The recording ends while the fault is still active, as in ITBench-AA.
- **Alert parity:** Prometheus gets the two service alerts the benchmark's environment fires (RequestErrorRate,
  RequestLatency; same names, labels and description format) and a 15 s scrape.
- **Environment fixes found while recording (2026-11-12):** the load generator's browser users (headless Chrome) and
  Grafana used ~2 of the VM's 4 CPUs; probes timed out and the collector restarted. Both are off (HTTP users still
  drive the same service traffic). The chart's Prometheus has no config reloader, so `cluster.sh up` restarts it.

## D7. Faults on the live cluster: flags plus our own configuration faults, revert that survives crashes
- **Chose:** 19 faults: 12 of the demo's feature flags (root cause = the flagd-config ConfigMap, as in the
  benchmark) and 7 configuration faults a person could roll out (wrong dependency address in a Deployment's env x3,
  Service selector typo, scale to zero, deny-all NetworkPolicy, memory limit too low). Each has ground truth in
  ITBench-AA's format, so the same scorer runs on benchmark and live incidents.
- **Safety:** the objects a fault touches are saved (atomically) before it changes anything; revert restores them
  from that file. Every episode reverts leftovers first and reverts in `finally`, and nothing (not even a recorder
  that won't stop) stands between a fault and its revert: an early version let a stuck recorder skip the revert,
  caught on the second episode and fixed.
- **Settle time:** flag faults show alerts in ~80 s; configuration faults act only after a rollout and on real
  traffic (checkouts are a few per minute), so they wait up to 300 s. Episodes record which expected alerts fired;
  an episode with no symptom is reported, not scored as a miss.

## D8. Stop-safe runs: a usage stop costs at most the agent calls in flight
- **Constraint:** stop at 80% of the 5-hour or weekly window; one shared quota across projects. Lessons
  from P4 (exit 75 + reset time + a driver that waits and resumes) and P5 (append-only results, pinned config,
  interrupted episodes never counted).
- **Chose:**
  1. Before each episode: usage + the measured cost of one episode (`--reserve`) must fit below 80%, so stops land
     between episodes.
  2. Team episodes are checkpointed after every step in Postgres; a rerun resumes the same thread from the last
     finished step and the result records `resumed: true`.
  3. A stop inside one parallel investigator waits for the investigators already running to finish before failing
     the step (no new agent starts after a stop). Found by the crash-resume test: LangGraph cancels the other tasks of
     a step when one fails, so without the wait their work was thrown away and redone.
  4. Exit 75 with `EVAL_RETRY_AT`; `scripts/run_eval.sh` waits for the reset, restarts the checkpoint database if
     needed, and resumes; paid extra usage or an unclean environment exits 2 for good.
  5. `run_config.json` pins models, turn caps and a hash of the prompts per results folder; a resume with a different
     configuration is refused (new `--tag` instead).
- **Live episodes are not resumed mid-way** (the cluster keeps changing); they are discarded and re-recorded.

## D9. Team v1 on the dev scenarios (2026-11-12; dev only, test untouched)
- **Setup C (dev1), 10 dev scenarios × 1:** mean **0.65** vs setup A **0.35**; root cause found **8/10 vs 5/10**;
  mean false positives 0.6 vs 0.9; **$0.19 vs $0.09** API-equivalent per run; 110 s vs 26 s. Ten C runs used ~3
  points of the 5-hour window and ~1 point of the weekly one.
- **Where the gain comes from:** the drafts (commander + investigators, i.e. setup B's answer) already score 0.70.
  Parallel investigators with one signal each found chaos objects and flag changes the single agent missed (S21,
  S80, S83).
- **Verifier v1 hurt (0.70 → 0.65):** asked to "check and return the final diagnosis", it added causes (S11: a Kafka
  restart that was part of applying the flag). **v2:** it rules on every candidate (draft + every possible cause the
  investigators raised: cause / symptom / unrelated + explained_by) and code submits only causes no other cause
  explains. Replayed on the same team runs: **0.70** (fixes S9's symptom, no regressions). On dev the verifier is
  neutral-to-small; the test-set B vs C comparison measures it properly.
- **Not tuned further (overfitting risk on 10 scenarios):** S26's root cause (an HTTPChaos object) appears nowhere in
  its snapshot (no setup can name it); S33's second entity really had the same bad change; S1's object timestamps are
  snapshot capture times, which hides that the load generator came first.

## D10. Agent identity: a small OIDC-shaped issuer of short-lived tokens, delegation per RFC 8693 (2026-11-12)
- **Chose (option A of three):** the project issues its own tokens. Each agent role is an OAuth client that
  authenticates with a signed JWT assertion (`private_key_jwt`, RFC 7523) and gets a 10-minute ES256 access token
  (client-credentials shape). Acting for a person goes through RFC 8693 token exchange: `sub` = the user, nested
  `act` = the agent chain (commander → executor). The issuer publishes a standard OIDC discovery document and JWKS,
  so the gateway validates any OIDC issuer by configuration (a customer's Okta or Entra ID is a config change).
  On AWS the signing key is a KMS ECC_NIST_P256 key (the private key never leaves KMS).
- **Alternatives:** B, Keycloak locally (Standard Token Exchange since 26.2, but `act` delegation only in preview;
  on AWS Cognito has client credentials and no RFC 8693, so local and cloud would diverge; ~0.5–1 GB more RAM).
  C, SPIFFE/SPIRE plus exchange (our agents run as host processes and on Lambda, not pods, so attestation doesn't
  fit; ~15–25 h). AgentCore Identity (still needs an external IdP for inbound; ties local evals to AWS).
- **Why:** the project's value is the gateway's decisions (Cedar on arguments, screening, audit, kill switch); A
  produces exactly the claims they consume, identical locally and on Lambda, ~$1/month. Postings checked
  (Celonis, Revin and ~10 other FDE postings): none asks to operate an IdP; identity shows up as protocol literacy
  (OAuth, SSO, delegated access) plus least-privilege authorization. P3 already shows a real IdP integration
  (Entra ID SSO).
- **Invariants (same in every option):** tokens live in the agent's MCP connection headers, never in the model's
  context; human approval is a separate signed record checked by Cedar, never a token claim; the kill switch
  deny-lists agent ids and token ids; the orchestrator holding agent credentials is the trust root (documented in
  the threat model).
