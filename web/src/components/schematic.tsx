"use client";

import { useEffect, useRef } from "react";
import { type AuditRecord, type RunDoc, type Step, hits } from "@/lib/run";

// Geometry of the board (SVG user units).
const W = 1000;
const H = 540;
const AGENTS: { id: string; label: string; sub: string; y: number }[] = [
  { id: "agent:commander", label: "Commander", sub: "plans, reads alerts", y: 96 },
  { id: "agent:investigator", label: "Investigators ×4", sub: "one signal each", y: 172 },
  { id: "agent:verifier", label: "Verifier", sub: "cause or symptom?", y: 248 },
  { id: "agent:executor", label: "Executor", sub: "proposes, never runs", y: 324 },
];
const AX = 196; // right edge of the agent boxes
const REQ_Y = 150; // request bus
const RES_Y = 282; // response bus
const G_ID = 318; // identity + kill switch gate
const G_CEDAR = 462; // Cedar gate
const G_SCREEN = 600; // output screening gate (on the way back)
const TOOL_X = 820;
const BAND_Y = 470;

const agentY = (id?: string) => AGENTS.find((a) => a.id === id)?.y ?? AGENTS[1].y;

function reqPath(y: number, toX: number) {
  return `M${AX},${y} C${AX + 30},${y} ${AX + 22},${REQ_Y} ${AX + 52},${REQ_Y} L${toX},${REQ_Y}`;
}
function roundPath(y: number) {
  return (
    `${reqPath(y, TOOL_X)} L${TOOL_X},${REQ_Y + 20} ` +
    `M${TOOL_X},${RES_Y} L${AX + 52},${RES_Y} C${AX + 22},${RES_Y} ${AX + 30},${y} ${AX},${y}`
  );
}

export type GateStats = { id: { pass: number; stop: number }; cedar: { pass: number; stop: number }; screen: { flagged: number } };

export function gateStats(records: AuditRecord[]): GateStats {
  const s: GateStats = { id: { pass: 0, stop: 0 }, cedar: { pass: 0, stop: 0 }, screen: { flagged: 0 } };
  for (const r of records) {
    const where = stopAt(r);
    if (r.event === "auth" && r.decision === "deny") s.id.stop++;
    else if (r.event !== "call") continue;
    else if (where === "id") s.id.stop++;
    else if (where === "cedar") {
      s.id.pass++;
      s.cedar.stop++;
    } else {
      s.id.pass++;
      s.cedar.pass++;
      if (hits(r) > 0) s.screen.flagged++;
    }
  }
  return s;
}

function stopAt(r: AuditRecord): "id" | "cedar" | null {
  if (r.decision !== "deny") return null;
  if (r.event === "auth") return "id";
  const p = r.policies || [];
  if (p.includes("kill-switch") || p.includes("rate-limit") || p.includes("unknown-tool")) return "id";
  return "cedar";
}

function Pulse({ r, onDone }: { r: AuditRecord; onDone: (seq: number) => void }) {
  const ref = useRef<SVGAnimateMotionElement>(null);
  const where = stopAt(r);
  const y = agentY(r.agent);
  const d = where === "id" ? reqPath(y, G_ID) : where === "cedar" ? reqPath(y, G_CEDAR) : roundPath(y);
  const flagged = hits(r) > 0;
  const dur = where ? 0.7 : 1.5;
  useEffect(() => {
    ref.current?.beginElement();
    const t = setTimeout(() => onDone(r.seq), dur * 1000 + 900);
    return () => clearTimeout(t);
  }, [r.seq, dur, onDone]);
  const gx = where === "id" ? G_ID : G_CEDAR;
  return (
    <g>
      <circle r={4.5} fill={where ? "var(--stop)" : "var(--pass)"} opacity={0.95}>
        <animateMotion ref={ref} dur={`${dur}s`} begin="indefinite" fill="freeze" path={d} keyPoints="0;1" keyTimes="0;1" calcMode="linear" />
        {flagged && !where && (
          <animate attributeName="fill" values="var(--pass);var(--pass);var(--stop)" keyTimes="0;0.55;0.62" dur={`${dur}s`} begin={`${0}s`} fill="freeze" />
        )}
      </circle>
      {where && (
        <circle cx={gx} cy={REQ_Y} r={6} fill="none" stroke="var(--stop)" strokeWidth={2} className="gate-burst" style={{ animationDelay: `${dur}s`, opacity: 0 }} />
      )}
    </g>
  );
}

function Gate({ x, y, label, sub, n, tone, h = 56 }: { x: number; y: number; label: string; sub: string; n: string; tone: "idle" | "pass" | "stop" | "wait"; h?: number }) {
  const color = tone === "stop" ? "var(--stop)" : tone === "wait" ? "var(--wait)" : tone === "pass" ? "var(--pass)" : "var(--line-strong)";
  return (
    <g>
      <rect x={x - 7} y={y - h / 2} width={14} height={h} fill="var(--bg)" stroke={color} strokeWidth={1.5} />
      <line x1={x} x2={x} y1={y - h / 2 + 6} y2={y + h / 2 - 6} stroke={color} strokeWidth={1} strokeDasharray="2 3" />
      <text x={x} y={y - h / 2 - 22} textAnchor="middle" className="fill-[var(--ink)]" fontSize={13} fontWeight={600}>{label}</text>
      <text x={x} y={y - h / 2 - 8} textAnchor="middle" className="fill-[var(--muted)]" fontSize={10.5}>{sub}</text>
      <text x={x} y={y + h / 2 + 16} textAnchor="middle" fontFamily="var(--font-jetbrains)" fontSize={10.5} fill={tone === "idle" ? "var(--faint)" : color}>{n}</text>
    </g>
  );
}

export function Schematic({ doc, records, pulses, steps, onPulseDone }: {
  doc: RunDoc;
  records: AuditRecord[];
  pulses: AuditRecord[];
  steps: Step[];
  onPulseDone: (seq: number) => void;
}) {
  const s = gateStats(records);
  const perAgent = (id: string) => records.filter((r) => r.event === "call" && r.agent === id).length;
  const verify = steps.find((x) => x.node === "verify");
  const planned = steps.find((x) => x.node === "plan_fix");
  const verdicts = verify?.verdicts || [];
  const vc = (k: string) => verdicts.filter((v) => v.verdict === k).length;
  const nCalls = planned?.fix_plan?.actions?.length ?? 0;
  const escalated = verify?.final?.escalate;
  const killed = doc.kill || doc.status === "killed";
  const carrier = doc.attack?.carrier_service;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full select-none" role="img"
      aria-label="Schematic of the security gateway: agents on the left, the gateway's layers in the middle, tools on the right; each tool call is drawn as a pulse that passes or stops at a layer.">
      <defs>
        <marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M0,0 L8,4 L0,8 z" fill="var(--line-strong)" />
        </marker>
      </defs>

      {/* agents */}
      {AGENTS.map((a) => (
        <g key={a.id}>
          <rect x={36} y={a.y - 24} width={AX - 36} height={48} fill="var(--panel)" stroke="var(--line-strong)" />
          <text x={48} y={a.y - 4} fontSize={13.5} fontWeight={600} className="fill-[var(--ink)]">{a.label}</text>
          <text x={48} y={a.y + 13} fontSize={10.5} className="fill-[var(--muted)]">{a.sub}</text>
          <text x={AX - 8} y={a.y - 4} textAnchor="end" fontSize={10.5} fontFamily="var(--font-jetbrains)" className="fill-[var(--faint)]">{perAgent(a.id) || ""}</text>
          <path d={reqPath(a.y, AX + 52)} fill="none" stroke="var(--line)" />
          <path d={`M${AX + 52},${RES_Y} C${AX + 22},${RES_Y} ${AX + 30},${a.y} ${AX},${a.y}`} fill="none" stroke="var(--line)" />
        </g>
      ))}
      <text x={36} y={54} className="tag fill-[var(--faint)]" fontSize={10}>AGENTS · OWN IDENTITY EACH</text>

      {/* gateway frame */}
      <rect x={AX + 44} y={46} width={TOOL_X - AX - 88} height={290} fill="none" stroke="var(--line-strong)" strokeDasharray="5 4" />
      <text x={AX + 56} y={64} className="tag fill-[var(--faint)]" fontSize={10}>MCP SECURITY GATEWAY · AWS LAMBDA</text>
      <line x1={AX + 52} x2={TOOL_X} y1={REQ_Y} y2={REQ_Y} stroke="var(--line-strong)" markerEnd="url(#arr)" />
      <line x1={TOOL_X} x2={AX + 52} y1={RES_Y} y2={RES_Y} stroke="var(--line-strong)" markerEnd="url(#arr)" />
      <text x={TOOL_X - 12} y={REQ_Y - 8} textAnchor="end" fontSize={10} className="fill-[var(--faint)]" fontFamily="var(--font-jetbrains)">tool call →</text>
      <text x={AX + 64} y={RES_Y + 18} fontSize={10} className="fill-[var(--faint)]" fontFamily="var(--font-jetbrains)">← result</text>

      <Gate x={G_ID} y={REQ_Y} label="Identity" sub="signed token · kill switch" tone={killed || s.id.stop ? "stop" : s.id.pass ? "pass" : "idle"}
        n={`${s.id.pass} in${s.id.stop ? ` · ${s.id.stop} refused` : ""}`} />
      <Gate x={G_CEDAR} y={REQ_Y} label="Cedar policy" sub="rules on the arguments" tone={s.cedar.stop ? "stop" : s.cedar.pass ? "pass" : "idle"}
        n={`${s.cedar.pass} allowed${s.cedar.stop ? ` · ${s.cedar.stop} denied` : ""}`} />
      <Gate x={G_SCREEN} y={RES_Y} label="Screening" sub="telemetry marked untrusted" tone={s.screen.flagged ? "stop" : s.cedar.pass ? "pass" : "idle"}
        n={s.screen.flagged ? `${s.screen.flagged} results flagged` : `${s.cedar.pass} results marked`} />

      {/* tools */}
      <rect x={TOOL_X} y={REQ_Y - 46} width={150} height={RES_Y - REQ_Y + 92} fill="var(--panel)" stroke="var(--line-strong)" />
      <text x={TOOL_X + 12} y={REQ_Y - 26} fontSize={13.5} fontWeight={600} className="fill-[var(--ink)]">Telemetry tools</text>
      <text x={TOOL_X + 12} y={REQ_Y - 10} fontSize={10.5} className="fill-[var(--muted)]">read-only MCP server</text>
      <text x={TOOL_X + 12} y={REQ_Y + 30} fontSize={10.5} fontFamily="var(--font-jetbrains)" className="fill-[var(--faint)]">logs · traces</text>
      <text x={TOOL_X + 12} y={REQ_Y + 46} fontSize={10.5} fontFamily="var(--font-jetbrains)" className="fill-[var(--faint)]">metrics · events</text>
      {doc.attack && (
        <g>
          <rect x={TOOL_X + 10} y={REQ_Y + 66} width={130} height={40} fill="var(--stop-soft)" stroke="var(--stop)" strokeDasharray="3 3" />
          <text x={TOOL_X + 18} y={REQ_Y + 82} fontSize={10.5} fontWeight={600} fill="var(--stop)">your line, planted ×{doc.attack.copies ?? 6}</text>
          <text x={TOOL_X + 18} y={REQ_Y + 97} fontSize={10} fontFamily="var(--font-jetbrains)" fill="var(--stop)">{carrier ? `${carrier} logs` : "in the logs"}</text>
        </g>
      )}

      {/* decisions band */}
      <text x={36} y={BAND_Y - 46} className="tag fill-[var(--faint)]" fontSize={10}>EVERY DECISION</text>
      <rect x={36} y={BAND_Y - 26} width={AX - 36} height={52} fill="var(--panel)" stroke="var(--line-strong)" />
      <text x={48} y={BAND_Y - 4} fontSize={13.5} fontWeight={600} className="fill-[var(--ink)]">Draft diagnosis</text>
      <text x={48} y={BAND_Y + 13} fontSize={10.5} className="fill-[var(--muted)]">{steps.some((x) => x.node === "commander_synthesize") ? "from the findings" : "not yet"}</text>
      <line x1={AX} x2={TOOL_X} y1={BAND_Y} y2={BAND_Y} stroke="var(--line-strong)" markerEnd="url(#arr)" />
      <Gate x={G_ID + 40} y={BAND_Y} h={46} label="Verifier" sub="tests every suspect" tone={verify ? (vc("cause") ? "pass" : "stop") : "idle"}
        n={verify ? `${vc("cause")} cause · ${vc("symptom")} symptom · ${vc("unrelated")} unrelated` : "waiting"} />
      <Gate x={G_SCREEN + 20} y={BAND_Y} h={46} label="A person's signature" sub="exact calls, signed" tone={escalated ? "wait" : nCalls ? "wait" : "idle"}
        n={escalated ? "escalated to a person" : nCalls ? `${nCalls} call${nCalls > 1 ? "s" : ""} unsigned` : "nothing proposed yet"} />
      <rect x={TOOL_X} y={BAND_Y - 26} width={150} height={52} fill="var(--panel)" stroke="var(--line-strong)" />
      <text x={TOOL_X + 12} y={BAND_Y - 4} fontSize={13.5} fontWeight={600} className="fill-[var(--ink)]">Cluster writes</text>
      <text x={TOOL_X + 12} y={BAND_Y + 13} fontSize={10.5} fill={nCalls ? "var(--wait)" : "var(--muted)"}>{nCalls ? "locked: no signature" : "no model holds a key"}</text>

      {pulses.map((r) => (
        <Pulse key={r.seq} r={r} onDone={onPulseDone} />
      ))}
    </svg>
  );
}
