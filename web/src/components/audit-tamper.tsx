"use client";

import { useState } from "react";
import { API, type AuditRecord, type RunDoc } from "@/lib/run";
import { Panel } from "./run-panels";

type Check = { ok: boolean; message: string; broken_index: number | null };

export function AuditTamper({ doc }: { doc: RunDoc }) {
  const [recs, setRecs] = useState<AuditRecord[]>(doc.audit);
  const [edited, setEdited] = useState<Set<number>>(new Set());
  const [check, setCheck] = useState<Check | null>(null);
  const [busy, setBusy] = useState(false);

  const flip = (i: number) => {
    setRecs((rs) => rs.map((r, j) => (j === i ? { ...r, decision: r.decision === "deny" ? "allow" : "deny" } : r)));
    setEdited((e) => new Set(e).add(recs[i].seq));
    setCheck(null);
  };
  const drop = (i: number) => {
    setEdited((e) => new Set(e).add(recs[i].seq));
    setRecs((rs) => rs.filter((_, j) => j !== i));
    setCheck(null);
  };
  const verify = async () => {
    setBusy(true);
    const r = await fetch(`${API}/api/audit/verify`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ records: recs }),
    }).catch(() => null);
    setCheck(r?.ok ? await r.json() : { ok: false, message: "could not reach the verifier", broken_index: null });
    setBusy(false);
  };

  const rows = recs.map((r, i) => ({ r, i })).filter(({ r }) => r.event !== "list");

  return (
    <Panel title="Audit log: try to cover your tracks" kicker={`${doc.audit.length} records · hash chain`}>
      <p className="mb-3 text-[13px] text-muted">
        Every gateway decision is one record carrying the hash of the one before it. Flip a decision or delete a row,
        then run the gateway&apos;s own verifier on your edited copy.
        {doc.audit_locked_version && " The original is also stored in S3 with Object Lock, where not even the account owner can change it."}
      </p>
      <div className="max-h-72 overflow-auto border border-line">
        <table className="w-full font-mono text-[11px]">
          <thead className="sticky top-0 bg-panel-2 text-left text-faint">
            <tr>
              <th className="px-2 py-1.5 font-normal">#</th>
              <th className="px-2 py-1.5 font-normal">agent</th>
              <th className="px-2 py-1.5 font-normal">call</th>
              <th className="px-2 py-1.5 font-normal">decision</th>
              <th className="hidden px-2 py-1.5 font-normal sm:table-cell">hash</th>
              <th className="px-2 py-1.5" />
            </tr>
          </thead>
          <tbody>
            {rows.map(({ r, i }) => {
              const broken = check && !check.ok && check.broken_index != null && i >= check.broken_index;
              const first = check?.broken_index === i;
              return (
                <tr key={`${r.seq}-${i}`} className={`border-t border-line ${first ? "bg-stop-soft" : broken ? "opacity-40" : ""} ${edited.has(r.seq) ? "text-wait" : ""}`}>
                  <td className="px-2 py-1 text-faint">{r.seq}</td>
                  <td className="px-2 py-1">{(r.agent || "").replace("agent:", "") || "–"}</td>
                  <td className="max-w-[12rem] truncate px-2 py-1" title={r.tool}>{r.event === "auth" ? "token check" : r.tool}</td>
                  <td className="px-2 py-1">
                    <button onClick={() => flip(i)} title="flip this decision"
                      className={`underline decoration-dotted underline-offset-2 ${r.decision === "deny" ? "text-stop" : "text-pass"}`}>
                      {r.decision}
                    </button>
                  </td>
                  <td className="hidden px-2 py-1 text-faint sm:table-cell">{r.hash.slice(0, 10)}…</td>
                  <td className="px-2 py-1 text-right">
                    <button onClick={() => drop(i)} title="delete this record" className="text-faint hover:text-stop">✕</button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button onClick={verify} disabled={busy}
          className="border border-pass bg-pass-soft px-4 py-2 font-mono text-[12px] font-semibold text-pass hover:bg-pass hover:text-bg disabled:opacity-50">
          {busy ? "VERIFYING…" : "VERIFY THE CHAIN"}
        </button>
        {edited.size > 0 && (
          <button onClick={() => { setRecs(doc.audit); setEdited(new Set()); setCheck(null); }} className="font-mono text-[12px] text-faint hover:text-ink">
            reset
          </button>
        )}
        {check && (
          <span className={`font-mono text-[12px] ${check.ok ? "text-ok" : "text-stop"}`}>
            {check.ok ? `✓ ${check.message}` : `✗ ${check.message}`}
          </span>
        )}
      </div>
    </Panel>
  );
}
