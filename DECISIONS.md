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
