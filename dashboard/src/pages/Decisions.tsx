import { useEffect, useState } from "react";
import { api, type DecisionRecord, type PolicyView, type Project, questionOf } from "../api.ts";
import { count, optionNames, ruleText, top } from "../format.ts";
import { ActionBadge, Drawer, ErrorState, HBars, Icon, Lock, ProbBar, Skeleton, Top, useLoad } from "../ui.tsx";
import { wording } from "./Questions.tsx";

const PAGE = 50;
const EMPTY = { question: "", model: "", action: "", pending: false, ref: "" };
type Filters = typeof EMPTY;

function params(f: Filters, extra: Record<string, string> = {}): string {
  const p = new URLSearchParams({ limit: String(PAGE), ...extra });
  if (f.question) p.set("question", f.question);
  if (f.model) p.set("model_served", f.model);
  if (f.action) p.set("action", f.action);
  if (f.ref) p.set("external_ref", f.ref);
  return p.toString();
}

interface Page {
  items: DecisionRecord[];
  pending: Set<string>;
  more: boolean;
}

/** A page of decisions, and which of them still wait for an outcome. */
async function load(f: Filters, to: string | undefined, signal: AbortSignal): Promise<Page> {
  const extra: Record<string, string> = to ? { to } : {};
  const [all, pending] = await Promise.all([
    api<{ items: DecisionRecord[] }>(`/v1/admin/decisions?${params(f, f.pending ? { ...extra, outcome: "pending" } : extra)}`, { signal }),
    // The newest PAGE pending decisions include every pending one on this page.
    api<{ items: DecisionRecord[] }>(`/v1/admin/decisions?${params(f, { ...extra, outcome: "pending" })}`, { signal }),
  ]);
  return { items: all.items, pending: new Set(pending.items.map((d) => d.decision_id)), more: all.items.length === PAGE };
}

export function Decisions() {
  const [draft, setDraft] = useState(EMPTY);
  const [f, setF] = useState(EMPTY);
  const [tag, setTag] = useState("");
  const [rows, setRows] = useState<Page | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [names, setNames] = useState<Record<string, string[] | null>>({});
  const [open, setOpen] = useState<DecisionRecord | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const ctl = new AbortController();
    setRows(null);
    setError(null);
    load(f, undefined, ctl.signal).then(setRows, (e) => (e as Error).name !== "AbortError" && setError(e));
    return () => ctl.abort();
  }, [f, tick]);

  // Option and level names, one question definition each.
  useEffect(() => {
    for (const id of new Set(rows?.items.map((r) => r.question_id))) {
      if (id in names) continue;
      questionOf(id).then((question) => setNames((n) => ({ ...n, [id]: optionNames(question?.definition) })));
    }
  }, [rows, names]);

  const more = async () => {
    if (!rows) return;
    const last = rows.items[rows.items.length - 1];
    const next = await load(f, last.ts, new AbortController().signal);
    const seen = new Set(rows.items.map((r) => r.decision_id));
    setRows({
      items: [...rows.items, ...next.items.filter((r) => !seen.has(r.decision_id))],
      pending: new Set([...rows.pending, ...next.pending]),
      more: next.more,
    });
  };

  const shown = rows?.items.filter((r) => !tag.trim() || Object.entries(r.tags ?? {}).some(([k, v]) => `${k}=${v}`.includes(tag.trim()))) ?? [];
  const field = (k: "question" | "model" | "ref") => (e: { target: { value: string } }) => setDraft({ ...draft, [k]: e.target.value });

  return (
    <>
      <Top crumb="Capture" title="Decisions" />
      <div className="body">
        <form
          role="search"
          aria-label="Filters"
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            setF({ ...draft });
          }}
        >
          <input className="in m" aria-label="Question name or id" placeholder="question" value={draft.question} onChange={field("question")} style={{ width: 170 }} />
          <input className="in m" aria-label="Model served" placeholder="model served" value={draft.model} onChange={field("model")} style={{ width: 150 }} />
          <select className="in" aria-label="Action" value={draft.action} onChange={(e) => setF({ ...draft, action: e.target.value })}>
            <option value="">Any action</option>
            {["ACCEPT", "BLOCK", "ESCALATE", "SELECT"].map((x) => <option key={x}>{x}</option>)}
          </select>
          <label className="row" style={{ gap: 8, fontSize: 13 }}>
            <input type="checkbox" checked={draft.pending} onChange={(e) => setF({ ...draft, pending: e.target.checked })} /> Outcome pending only
          </label>
          <input className="in m" type="search" aria-label="external_ref" placeholder="external_ref" value={draft.ref} onChange={field("ref")} style={{ width: 150 }} />
          <button className="btn" type="submit">Apply</button>
          <button className="btn" type="button" onClick={() => (setDraft(EMPTY), setF(EMPTY), setTag(""))}>Clear</button>
          <input className="in m" aria-label="Tag, filters the rows shown" placeholder="tag: channel=web" value={tag} onChange={(e) => setTag(e.target.value)} style={{ width: 170 }} />
        </form>
        {error != null ? (
          <ErrorState error={error} retry={() => setTick((t) => t + 1)} />
        ) : rows === null ? (
          <Skeleton />
        ) : (
          <section className="card" aria-label="Decisions">
            <div className="tw">
              <table className="t">
                <thead>
                  <tr>
                    <th>Time (UTC)</th><th>Question</th><th>Model served</th><th className="r">Top probability</th><th>Action</th>
                    <th>Policy</th><th>Outcome</th><th className="r">Latency</th><th className="r">Cost</th>
                  </tr>
                </thead>
                <tbody className="m" style={{ fontSize: 12 }}>
                  {shown.map((r) => (
                    <tr
                      key={r.decision_id}
                      className={`click${open?.decision_id === r.decision_id ? " sel" : ""}`}
                      tabIndex={0}
                      aria-label={`Open decision ${r.decision_id}`}
                      onClick={() => setOpen(r)}
                      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), setOpen(r))}
                    >
                      <td>{r.ts.slice(11, 23)}</td>
                      <td>{r.question_name ?? r.question_id.slice(0, 14) + "…"}</td>
                      <td className="t2">{r.model_served}</td>
                      <td className={`r${r.refusal ? " mu" : ""}`}>{top(r, names[r.question_id] ?? null).label}</td>
                      <td>{r.action_taken ? <ActionBadge action={r.action_taken} /> : <span className="mu">not reported</span>}</td>
                      <td className={r.policy_version ? "" : "mu"}>{r.policy_version ? `v${r.policy_version}` : "none"}</td>
                      <td>{rows.pending.has(r.decision_id) ? <span className="mu">◷ pending</span> : <span className="t2">● labelled</span>}</td>
                      <td className="r">{r.latency_ms == null ? "–" : `${Math.round(r.latency_ms)} ms`}</td>
                      <td className="r">{r.cost_usd == null ? "–" : `$${r.cost_usd.toFixed(5)}`}</td>
                    </tr>
                  ))}
                  {shown.length === 0 && (
                    <tr>
                      <td colSpan={9} className="mu">
                        {rows.items.length ? "No decisions on this page carry that tag." : "No decisions match. Decisions appear here as soon as the proxy or the SDK logs them."}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <div className="foot">
              <span className="mu small grow">
                Showing {count(shown.length)} · newest first · right and wrong arrive with review (M5)
              </span>
              <button className="btn" type="button" onClick={more} disabled={!rows.more}>Load more</button>
            </div>
          </section>
        )}
      </div>
      <Drawer open={open !== null} onClose={() => setOpen(null)} label="Decision detail">
        {open && <Detail d={open} names={names[open.question_id] ?? null} pending={rows?.pending.has(open.decision_id) ?? true} onClose={() => setOpen(null)} />}
      </Drawer>
    </>
  );
}

function Detail({ d, names, pending, onClose }: { d: DecisionRecord; names: string[] | null; pending: boolean; onClose: () => void }) {
  const policy = useLoad(
    (signal) =>
      d.policy_fingerprint
        ? api<{ items: PolicyView[] }>(`/v1/admin/policies?question=${encodeURIComponent(d.question_id)}&model_served=${encodeURIComponent(d.model_served)}`, { signal }).then(
            (r) => r.items.find((v) => v.policy.fingerprint === d.policy_fingerprint) ?? null,
          )
        : Promise.resolve(null),
    [d.decision_id],
  );
  const project = useLoad((signal) => api<Project>("/v1/admin/project", { signal }), []);
  const question = useLoad(() => questionOf(d.question_id), [d.question_id]);
  const ps = d.probabilities;
  const p = policy.data?.policy;
  const pair = p && p.type === "noul" && p.rules.length === 2 ? p.rules : null;
  const lo = pair?.find((r) => r.op === "lt")?.threshold;
  const hi = pair?.find((r) => r.op === "ge")?.threshold;

  return (
    <>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <div className="col grow" style={{ gap: 4 }}>
          <span className="mu small">Decision · {d.question_name ?? d.question_id}</span>
          <span className="m" style={{ fontSize: 13, overflowWrap: "anywhere" }}>{d.decision_id}</span>
        </div>
        <button className="btn icon" type="button" aria-label="Close detail" onClick={onClose}>
          <Icon d="M5 5l8 8M13 5l-8 8" size={16} />
        </button>
      </div>
      <div className="row">
        {d.action_taken && <ActionBadge action={d.action_taken} />}
        <span className="pill">{pending ? <span className="mu">◷ outcome pending</span> : <span className="t2">● outcome received</span>}</span>
        {d.audit && <span className="pill mu">○ audit sample</span>}
        {d.external_ref && <span className="pill m t2">{d.external_ref}</span>}
      </div>
      {!ps || d.refusal ? (
        <p className="mu" style={{ margin: 0 }}>The model refused: no probabilities.</p>
      ) : d.type === "noul" && lo != null && hi != null ? (
        <ProbBar p={ps[0]} lo={lo} hi={hi} label="Raw P(yes)" />
      ) : d.type === "noul" ? (
        <HBars label="Raw probability" items={[["yes", ps[0]], ["no", 1 - ps[0]]]} />
      ) : (
        <HBars label="Raw probabilities" items={ps.map((v, i) => [names?.[i] ?? (d.type === "score" ? `level ${i}` : `#${i + 1}`), v] as [string, number])} />
      )}
      <dl className="kv">
        <dt>Policy</dt>
        <dd>
          {d.policy_version ? (
            <>
              <a className="m" href="#/policies">{d.question_name ?? "policy"} v{d.policy_version}</a>{" "}
              <span className="m mu small">{d.policy_fingerprint?.slice(0, 15)}…</span>
              {p && <div className="t2 small">{p.rules.map(ruleText).join(" · ")}</div>}
            </>
          ) : (
            <span className="mu">none: the app decided without a policy</span>
          )}
        </dd>
        <dt>Action from</dt>
        <dd>{d.action_source ?? "–"}</dd>
        <dt>Model</dt>
        <dd className="m">{d.model_requested === d.model_served ? d.model_served : `${d.model_requested} → ${d.model_served}`}</dd>
        <dt>Question</dt>
        <dd className="t2">{question.data ? wording(question.data) : d.question_id}</dd>
        <dt>Tags</dt>
        <dd className="row" style={{ gap: 6 }}>
          {Object.entries(d.tags ?? {}).map(([k, v]) => <span key={k} className="pill m">{k}={v}</span>)}
          {!Object.keys(d.tags ?? {}).length && <span className="mu">none</span>}
        </dd>
        <dt>State hash</dt>
        <dd className="m t2 small">{d.state_hash ? `${d.state_hash_alg} · ${d.state_hash}` : "–"}</dd>
        <dt>Latency · cost</dt>
        <dd className="m">{d.latency_ms == null ? "–" : `${Math.round(d.latency_ms)} ms`} · {d.cost_usd == null ? "–" : `$${d.cost_usd.toFixed(5)}`}</dd>
        <dt>Time</dt>
        <dd className="m">{d.ts}</dd>
      </dl>
      {project.data?.settings.privacy?.store_raw_state ? (
        <p className="t2 small" style={{ margin: 0 }}>This project stores raw state; reading it (audited) arrives with <span className="m">{"GET /v1/admin/decisions/{id}"}</span>.</p>
      ) : (
        <Lock>
          <a href="#/settings" className="small">Privacy settings</a>
        </Lock>
      )}
      <section className="col" aria-labelledby="th" style={{ gap: 8 }}>
        <h2 id="th" className="h" style={{ fontSize: 15 }}>Outcome history</h2>
        <p className="t2 small" style={{ margin: 0 }}>
          Every label received, corrections included, arrives with <span className="m">{"GET /v1/admin/decisions/{id}"}</span>. The last label received wins.
        </p>
      </section>
    </>
  );
}

