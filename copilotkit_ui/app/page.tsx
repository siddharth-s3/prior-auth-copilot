"use client";
import { useEffect, useMemo, useState } from "react";
import { CopilotKitProvider, CopilotChat, useAgent } from "@copilotkit/react-core/v2";

const API = process.env.NEXT_PUBLIC_API || "http://localhost:8002";

function Table({ rows }: { rows: any[] }) {
  if (!rows?.length) return <div className="k">none</div>;
  const cols = Object.keys(rows[0]);
  return (<table><thead><tr>{cols.map(c => <th key={c}>{c}</th>)}</tr></thead>
    <tbody>{rows.map((r, i) => <tr key={i}>{cols.map(c => <td key={c}>{typeof r[c] === "object" ? JSON.stringify(r[c]) : String(r[c] ?? "")}</td>)}</tr>)}</tbody></table>);
}

// Long strings (encrypted reasoning signatures, giant tool args) make the raw-event view unreadable.
const clip = (_k: string, v: any) => (typeof v === "string" && v.length > 400 ? v.slice(0, 400) + `… (+${v.length - 400} chars)` : v);
const pretty = (o: any) => { try { return JSON.stringify(o, clip, 2); } catch { return String(o); } };
const full = (o: any) => { try { return JSON.stringify(o, null, 2); } catch { return String(o); } };
const copy = (text: string) => { try { navigator.clipboard.writeText(text); } catch {} };

type Ev = { t: number; event: any };
type Tab = "events" | "state" | "a2ui" | "result";

// A2UI surface = what the agent's generate_a2ui tool streamed (render_a2ui args) / returned (a2ui_operations).
function a2uiFromEvents(events: Ev[]) {
  const args: Record<string, string> = {}; const names: Record<string, string> = {}; let ops: any = null;
  for (const { event: e } of events) {
    if (e.type === "TOOL_CALL_START") names[e.toolCallId] = e.toolCallName;
    if (e.type === "TOOL_CALL_ARGS") args[e.toolCallId] = (args[e.toolCallId] || "") + e.delta;
    if (e.type === "TOOL_CALL_RESULT" && typeof e.content === "string" && e.content.includes("a2ui_operations")) {
      try { ops = JSON.parse(e.content); } catch {}
    }
    if (e.type === "ACTIVITY_SNAPSHOT" || e.type === "ACTIVITY_DELTA") ops = ops ?? e;
  }
  const render = Object.keys(names).filter(id => names[id] === "render_a2ui").map(id => { try { return JSON.parse(args[id]); } catch { return args[id]; } });
  const gen = Object.keys(names).filter(id => names[id] === "generate_a2ui").map(id => { try { return JSON.parse(args[id]); } catch { return args[id]; } });
  return { generate_a2ui_call: gen.at(-1) ?? null, render_a2ui_surface: render.at(-1) ?? null, a2ui_operations: ops };
}

function Inspector({ events, state, result, tab, setTab, onClose, onClear }: { events: Ev[]; state: any; result: any; tab: Tab; setTab: (t: Tab) => void; onClose: () => void; onClear: () => void }) {
  const [hideNoise, setHideNoise] = useState(true);
  const t0 = events[0]?.t ?? 0;
  const shown = events.filter(x => !(hideNoise && x.event.type === "REASONING_ENCRYPTED_VALUE"));
  const a2ui = useMemo(() => a2uiFromEvents(events), [events]);
  const body = tab === "state" ? state : tab === "a2ui" ? a2ui : tab === "result" ? result : null;
  const tabs: [Tab, string][] = [["events", `AG-UI events (${events.length})`], ["state", "AG-UI shared state"], ["a2ui", "A2UI surface"], ["result", "Adjudication result"]];
  return (
    <div className="modal" onClick={onClose}><div className="sheet" onClick={e => e.stopPropagation()}>
      <div className="sheet-h">
        <b>Protocol inspector</b>
        <span className="k">AG-UI = transport (events + shared state) · A2UI = declarative UI payload carried inside it</span>
        <span style={{ flex: 1 }} />
        <button className="btn sm" onClick={() => copy(tab === "events" ? full(events.map(x => x.event)) : full(body))}>Copy JSON</button>
        <button className="btn sm ghost" onClick={onClose}>Close</button>
      </div>
      <div className="tabs">{tabs.map(([k, l]) => <button key={k} className={"tab" + (tab === k ? " on" : "")} onClick={() => setTab(k)}>{l}</button>)}</div>
      <div className="sheet-b">
        {tab === "events" ? (<>
          <div className="k" style={{ marginBottom: 8 }}>
            <label><input type="checkbox" checked={hideNoise} onChange={e => setHideNoise(e.target.checked)} /> hide REASONING_ENCRYPTED_VALUE</label>
            <button className="btn sm ghost" style={{ marginLeft: 12 }} onClick={onClear}>Clear</button>
          </div>
          {!shown.length && <div className="k">No events yet. Press Adjudicate (or chat) and they stream in live.</div>}
          {shown.map((x, i) => {
            const e = x.event; const label = e.toolCallName || (e.type === "TEXT_MESSAGE_CONTENT" ? String(e.delta).slice(0, 60) : "") || (e.type === "STATE_DELTA" ? e.delta?.map((d: any) => d.path).join(", ") : "");
            return (<details key={i} className="ev"><summary><span className="k">+{((x.t - t0) / 1000).toFixed(1)}s</span> <b>{e.type}</b> <span className="k">{label}</span></summary><pre>{pretty(e)}</pre></details>);
          })}
        </>) : (body == null || (typeof body === "object" && !Object.values(body).some(v => v != null) && tab === "a2ui")
          ? <div className="k">{tab === "a2ui" ? "No A2UI surface in this session yet. It is emitted when the supervisor calls generate_a2ui (after an adjudication, or when you ask the chat to show cases as cards)." : "Nothing yet. Run an adjudication first."}</div>
          : <pre>{pretty(body)}</pre>)}
      </div>
    </div></div>
  );
}

const RECS = ["MEETS_CRITERIA", "MORE_INFORMATION_NEEDED", "DOES_NOT_APPEAR_TO_MEET_CRITERIA", "SPECIALIST_REVIEW"];

async function post(path: string, body: any) {
  const r = await fetch(`${API}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}

// Human review: the reviewer (a person) records the disposition; every action is timestamped and audited server-side.
function ReviewActions({ caseId, rec, onChanged }: { caseId: string; rec: string; onChanged: () => void }) {
  const [mode, setMode] = useState<null | "override" | "escalate" | "clarify">(null);
  const [final, setFinal] = useState(rec);
  const [text, setText] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; t: string } | null>(null);
  const [state, setState] = useState<any>(null);
  const [audit, setAudit] = useState<any[] | null>(null);
  const load = async () => { try { setState(await (await fetch(`${API}/ui/cases/${caseId}/review`)).json()); } catch { setState(null); } };
  const loadAudit = async () => { try { setAudit(await (await fetch(`${API}/ui/cases/${caseId}/audit`)).json()); } catch { setAudit([]); } };
  useEffect(() => { load(); setMode(null); setMsg(null); setAudit(null); setFinal(rec); }, [caseId, rec]);
  const act = async (fn: () => Promise<any>) => {
    try { const r = await fn(); setMsg({ ok: true, t: `Saved: ${r.status} · ${r.ts}` }); setMode(null); setText(""); await load(); if (audit) await loadAudit(); onChanged(); }
    catch (e: any) { setMsg({ ok: false, t: String(e.message || e) }); }
  };
  const disp = (action: string, finalRec: string, reason: string) => post(`/ui/cases/${caseId}/disposition`, { action, final_recommendation: finalRec, ai_recommendation: rec, reason, reviewer: "reviewer" });
  const d = state?.latest_disposition;
  return (
    <div className="actions">
      <div className="k">Reviewer decision · status <b>{state?.status ?? "…"}</b>{d && <> · last: {d.action} → {d.final_recommendation} ({d.ts})</>}</div>
      <div className="row">
        <button className="btn sm" onClick={() => act(() => disp("ACCEPT", rec, ""))}>Accept recommendation</button>
        <button className="btn sm ghost" onClick={() => setMode(mode === "override" ? null : "override")}>Override…</button>
        <button className="btn sm ghost" onClick={() => setMode(mode === "escalate" ? null : "escalate")}>Escalate…</button>
        <button className="btn sm ghost" onClick={() => setMode(mode === "clarify" ? null : "clarify")}>Request clarification…</button>
        <button className="btn sm ghost" onClick={() => (audit ? setAudit(null) : loadAudit())}>{audit ? "Hide audit" : "Audit trail"}</button>
      </div>
      {mode && (
        <div className="form">
          {mode === "override" && <select value={final} onChange={e => setFinal(e.target.value)}>{RECS.map(r => <option key={r}>{r}</option>)}</select>}
          <textarea rows={2} value={text} onChange={e => setText(e.target.value)} placeholder={mode === "clarify" ? "What do you need clarified?" : "Reason (required)"} />
          <button className="btn sm" disabled={!text.trim()} onClick={() => act(() =>
            mode === "clarify" ? post(`/ui/cases/${caseId}/clarification`, { question: text, reviewer: "reviewer" })
              : disp(mode === "override" ? "OVERRIDE" : "ESCALATE", mode === "override" ? final : "SPECIALIST_REVIEW", text))}>Confirm</button>
        </div>)}
      {msg && <div className={msg.ok ? "ok" : "err"}>{msg.t}</div>}
      {audit && (<table><thead><tr><th>time (UTC)</th><th>actor</th><th>action</th><th>detail</th></tr></thead>
        <tbody>{audit.length ? audit.map(a => <tr key={a.id}><td>{a.ts}</td><td>{a.actor}</td><td>{a.action}</td><td>{JSON.stringify(a.detail)}</td></tr>) : <tr><td colSpan={4}>no actions yet</td></tr>}</tbody></table>)}
    </div>
  );
}

function Workspace() {
  const [cases, setCases] = useState<any[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<Record<string, any>>({});
  const { agent } = useAgent({ agentId: "preauth" });

  const refreshCases = () => fetch(`${API}/ui/cases`).then(r => r.json()).then(setCases).catch(() => setCases([]));
  useEffect(() => { refreshCases(); }, []);
  useEffect(() => { if (!sel) return setDetail(null); setDetail(null); fetch(`${API}/ui/cases/${sel}`).then(r => r.json()).then(setDetail).catch(() => setDetail({ error: true })); }, [sel]);

  // The review result arrives as AG-UI shared state (STATE_DELTA/SNAPSHOT: state.adjudication), not as chat text.
  const adj = (agent as any)?.state?.adjudication;
  useEffect(() => { if (adj?.case_id) { setResults(p => ({ ...p, [adj.case_id]: adj })); setBusy(false); } }, [adj]);

  // Live capture of every AG-UI event for the inspector.
  const [events, setEvents] = useState<Ev[]>([]);
  const [insp, setInsp] = useState<Tab | null>(null);
  useEffect(() => {
    if (!agent) return;
    const sub = (agent as any).subscribe?.({ onEvent: ({ event }: any) => setEvents(p => [...p.slice(-600), { t: Date.now(), event }]) });
    return () => sub?.unsubscribe?.();
  }, [agent]);

  const adjudicate = async () => {
    if (!sel || !agent) return;
    setBusy(true); setEvents([]);
    (agent as any).addMessage({ id: crypto.randomUUID(), role: "user", content: `Review case ${sel}` });
    try { await (agent as any).runAgent?.(); } catch (e) { console.error(e); }
    setBusy(false);
  };

  const res = sel ? results[sel] : null;
  const c = detail?.case;
  return (
    <div className="app">
      <div className="panel"><h2>My cases ({cases.length})</h2>
        {cases.map(x => (
          <div key={x.case_id} className={"case" + (sel === x.case_id ? " sel" : "")} onClick={() => setSel(x.case_id)}>
            <div><span className="id">{x.case_id}</span><span className="badge">{x.status}</span></div>
            <div>{x.patient_name} · {x.service_name}</div><div className="sub">{x.plan_name} · {x.request_date}</div>
          </div>))}
      </div>
      <div className="panel">
        {!sel ? <div className="empty">Select a case to open it</div> : (
          <div className="cols">
            <div className="body">
              <h3 style={{ margin: "0 0 8px" }}>{sel} {c && <span className="badge">{c.status}</span>}</h3>
              {!detail ? <div className="k">Loading…</div> : (<>
                <div className="grid">{c && Object.entries(c).slice(0, 12).map(([k, v]) => <div key={k}><div className="k">{k}</div>{String(v)}</div>)}</div>
                <p><button className="btn" disabled={busy} onClick={adjudicate}>{busy ? "Adjudicating…" : "Adjudicate"}</button></p>
                {Object.entries(detail).filter(([k]) => k !== "case").map(([k, v]) => (
                  <details key={k}><summary>{k.replace(/_/g, " ")}</summary><div>
                    {Array.isArray(v) ? <Table rows={v as any[]} /> : <Table rows={[v as any]} />}</div></details>))}
              </>)}
            </div>
            <div className="body" style={{ borderLeft: "1px solid var(--line)" }}>
              <h3 style={{ margin: "0 0 8px", display: "flex", gap: 8, alignItems: "center" }}>Adjudication <span style={{ flex: 1 }} />
                <button className="btn sm ghost" onClick={() => setInsp("result")}>{"{ }"} Show JSON</button></h3>
              {busy && <div className="k">The supervisor is running the review…</div>}
              {!busy && !res && <div className="k">Press Adjudicate to run the review. The result appears here.</div>}
              {res && (<>
                <div className="rec">{res.result?.recommendation}</div>
                <p className="k">Policy: {res.result?.applicable_policy_id ?? "none"} · human review required · pipeline: {(res.steps || []).join(" → ")}</p>
                <p>{res.result?.reviewer_summary}</p>
                {res.result?.verification && <p className="k">Citations verified {res.result.verification.citations_valid}/{res.result.verification.citations_total} · schema {res.result.verification.schema_valid ? "valid" : "INVALID"}{res.result.verification.recommendation_corrected ? ` · recommendation corrected (model said ${res.result.recommendation_model})` : ""}</p>}
                {!!res.result?.quality_flags?.length && <details><summary>Quality flags ({res.result.quality_flags.length})</summary><div><ul>{res.result.quality_flags.map((f: any, i: number) => <li key={i}>{String(f)}</li>)}</ul></div></details>}
                <ReviewActions caseId={res.case_id} rec={res.result?.recommendation} onChanged={refreshCases} />
                <details open><summary>Criteria</summary><div><Table rows={res.result?.criteria || []} /></div></details>
                {Object.entries(res.findings || {}).map(([k, v]) => <details key={k}><summary>Specialist findings: {k.replace(/_/g, " ")}</summary><div><pre className="raw">{pretty(v)}</pre></div></details>)}
                {!!res.result?.missing_information?.length && <details><summary>Missing information</summary><div><ul>{res.result?.missing_information.map((m: any, i: number) => <li key={i}>{String(m)}</li>)}</ul></div></details>}
                {!!res.result?.uncertainties?.length && <details><summary>Uncertainties</summary><div><ul>{res.result?.uncertainties.map((m: any, i: number) => <li key={i}>{String(m)}</li>)}</ul></div></details>}
              </>)}
            </div>
          </div>)}
      </div>
      {insp && <Inspector events={events} state={(agent as any)?.state} result={res} tab={insp} setTab={setInsp} onClose={() => setInsp(null)} onClear={() => setEvents([])} />}
      <div className="panel"><h2 style={{ display: "flex", alignItems: "center" }}>Copilot <span style={{ flex: 1 }} /><button className="btn sm" onClick={() => setInsp("events")}>{"{ }"} AG-UI / A2UI JSON</button></h2><div className="chat"><CopilotChat agentId="preauth" /></div></div>
    </div>
  );
}

export default function Page() {
  return (<CopilotKitProvider runtimeUrl="/api/copilotkit" useSingleEndpoint><Workspace /></CopilotKitProvider>);
}
