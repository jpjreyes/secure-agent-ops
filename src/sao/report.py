"""Incident report and postmortem draft, built only from recorded facts (no model call, nothing invented).

Sources: the incident's telemetry (alerts, object changes), the team's state (diagnosis, verdicts, plan, approval,
action results, check), the gateway's audit log (every call, decision and policy) and the per-agent cost records.
Every statement in the report points back to one of them.

  build(values, snapshot_dir, audit_path) -> markdown
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sao.telemetry.snapshot import Snapshot


def _ts(x) -> str:
    if isinstance(x, (int, float)):
        return datetime.fromtimestamp(x, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(x)[:19].replace("T", " ") + " UTC"


def _first_alerts(snapshot: Path) -> list[tuple[str, str, str]]:
    """(first active, alert, service) for the service alerts, from the alert snapshots."""
    seen: dict[tuple, str] = {}
    for f in sorted((snapshot / "alerts").glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        for a in (d.get("data") or {}).get("alerts", []) if isinstance(d, dict) else d:
            lb = a.get("labels", {})
            if a.get("state") != "firing" or lb.get("alertname") not in ("RequestErrorRate", "RequestLatency"):
                continue
            k = (lb["alertname"], lb.get("service_name", ""))
            if a.get("activeAt") and (k not in seen or a["activeAt"] < seen[k]):
                seen[k] = a["activeAt"]
    return sorted((t, k[0], k[1]) for k, t in seen.items())


def build(values: dict, snapshot: Path, audit_path: Path | None = None, title: str = "Incident") -> str:
    final = (values.get("final") or values.get("draft") or {}).get("contributing_factors", [])
    plan, decision = values.get("fix_plan") or {}, values.get("decision") or {}
    results, outcome = values.get("action_results") or [], values.get("outcome") or {}
    calls = values.get("calls") or []
    cost = round(sum(c.get("cost_usd_api_equivalent") or 0 for c in calls), 3)
    audit = [json.loads(x) for x in audit_path.read_text().splitlines()] if audit_path and audit_path.exists() else []

    events: list[tuple[str, str]] = []
    for t, name, svc in _first_alerts(snapshot):
        events.append((_ts(t), f"alert **{name}** starts firing on `{svc}`"))
    snap = Snapshot(snapshot)
    # objects that carry configuration (pods, nodes, replica sets and endpoints change status all the time)
    kinds = ("Deployment", "StatefulSet", "DaemonSet", "ConfigMap", "Service", "NetworkPolicy",
             "HorizontalPodAutoscaler", "Schedule")
    for ts, kind, ns, name in snap._q(f"""SELECT max(ts), kind, namespace, name FROM obj
            WHERE kind IN {kinds} OR kind LIKE '%Chaos' GROUP BY 2,3,4 HAVING count(DISTINCT body) > 1"""):
        events.append((_ts(ts), f"configuration change on `{ns or 'cluster'}/{kind}/{name}` (last version seen)"))
    for r in results:
        shown = {k: v for k, v in r["args"].items() if k != "idempotency_key"}
        events.append(("~ after approval",
                       f"`{r['tool']}` {json.dumps(shown)} → **{r['status']}**: {r.get('detail', '')[:160]}"))
    events.sort(key=lambda e: e[0])

    lines = [f"# {title}: incident report", "",
             "_Generated from recorded facts only: telemetry, the team's state, the gateway audit log._", "",
             "## Summary", ""]
    if final:
        lines.append("**Root cause:** " + ", ".join(f"`{f['name']}`" for f in final))
    if plan.get("actions"):
        lines.append(f"**Remediation proposed:** {plan.get('summary', '')[:400]}")
    elif plan:
        lines.append(f"**No automatic remediation:** {plan.get('escalate') or plan.get('summary', '')}")
    if decision:
        lines.append(f"**Approval:** {decision.get('decision', '?')} by `{decision.get('approver', '?')}`"
                     f" ({decision.get('how', '')})")
    if outcome:
        lines.append(f"**Outcome:** {'resolved' if outcome.get('resolved') else 'not resolved'}"
                     + (f"; still firing: {outcome.get('still_firing')}" if outcome.get("still_firing") else ""))
    lines += ["", "## Timeline", "", "| when | what |", "|---|---|"]
    lines += [f"| {w} | {e} |" for w, e in events] or ["| | (no events recorded) |"]

    lines += ["", "## Root cause and evidence", ""]
    for f in final:
        lines += [f"### `{f['name']}`", "", f"**Why:** {f.get('reasoning', '')}", "",
                  f"**Evidence:** {f.get('evidence', '')}", ""]
    ruled_out = [v for v in values.get("verdicts") or [] if v.get("verdict") != "cause"]
    if ruled_out:
        lines += ["**Ruled out by the verifier:**", ""]
        lines += [f"- `{v['entity']}`: {v['verdict']}" + (f" (explained by `{v['explained_by']}`)"
                                                       if v.get("explained_by") else "") for v in ruled_out]
        lines.append("")

    if plan.get("actions"):
        lines += ["## Actions", "", "| # | action | why | precondition | undo | result |", "|---|---|---|---|---|---|"]
        for i, a in enumerate(plan["actions"], 1):
            r = results[i - 1] if i - 1 < len(results) else {}
            lines.append(f"| {i} | `{a['tool']}` {json.dumps(a.get('args', {}))} | {a.get('why', '')[:150]} | "
                         f"{json.dumps(a.get('precondition', {}))} | {a.get('undo', '')[:80]} | "
                         f"{r.get('status', 'not run')} |")
        lines.append("")

    if audit:
        calls_ = [r for r in audit if r.get("event") == "call"]
        dec = Counter((r.get("decision"), tuple(r.get("policies") or [])) for r in calls_)
        lines += ["## Gateway (every tool call)", "",
                  f"{len(calls_)} calls; audit chain records {audit[0].get('seq')}–{audit[-1].get('seq')}.", "",
                  "| decision | policy | calls |", "|---|---|---|"]
        lines += [f"| {d} | {', '.join(p)} | {n} |" for (d, p), n in dec.most_common()]
        hits = sum(len(r.get("screening_hits") or []) for r in calls_)
        lines += ["", f"Injection screening flagged {hits} line(s) in tool outputs.", ""]

    lines += ["## Cost and effort", "", "| agent | model | turns | tool calls | API-equivalent $ |",
              "|---|---|---|---|---|"]
    lines += [f"| {c.get('node')} | {c.get('model')} | {c.get('turns')} | {len(c.get('tool_calls') or [])} | "
              f"{c.get('cost_usd_api_equivalent') or 0:.3f} |" for c in calls]
    lines += [f"| **total** | | | | **{cost:.3f}** |", ""]

    lines += ["## Postmortem draft", "",
              "- **What happened:** " + (", ".join(f"`{f['name']}`" for f in final) or "(undiagnosed)")
              + " (see root cause).",
              "- **Detection:** the first service alert in the timeline above.",
              "- **Response:** the diagnosis, the plan and the approval above; every tool call is in the audit log.",
              "- **What went well:** _(for the team to fill in)_",
              "- **What went poorly:** _(for the team to fill in)_",
              "- **Action items:** _(owners and dates; e.g. a guard that blocks this kind of change at deploy time)_",
              ""]
    return "\n".join(lines)
