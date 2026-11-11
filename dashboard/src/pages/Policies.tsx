import { useState } from "react";
import { api, type PolicyDiff, type PolicyMetrics, type PolicyView } from "../api.ts";
import { count, day, interval, pct, prob, usd } from "../format.ts";
import { href, Load, PolicyBadge, Top, useLoad } from "../ui.tsx";

export function Policies() {
  return (
    <>
      <Top crumb="Act" title="Policies" />
      <div className="body">
        <PoliciesBody />
      </div>
    </>
  );
}

const group = (v: PolicyView) => `${v.policy.question_name ?? v.policy.question_id} · ${v.policy.model_served}`;

export function PoliciesBody({ question }: { question?: string }) {
  const state = useLoad(
    (signal) => api<{ items: PolicyView[] }>(`/v1/admin/policies?${new URLSearchParams(question ? { question } : {})}`, { signal }),
    [question],
  );
  const [picked, setPicked] = useState<string | null>(null);
  return (
    <Load state={state}>
      {({ items }) => {
        if (!items.length)
          return (
            <section className="card pad col" style={{ gap: 8 }}>
              <h2 className="h" style={{ fontSize: 18 }}>No policies yet</h2>
              <p className="t2" style={{ margin: 0 }}>
                Compile one from a question's <span className="m">Thresholds and escalation</span> tab
                {question && <> (<a href={href("questions", question, "simulator")}>open it</a>)</>}, or with <span className="m">trimset policy compile</span>.
              </p>
            </section>
          );
        const groups = [...new Set(items.map(group))];
        const current = items.find((v) => v.policy.policy_id === picked) ?? items[0];
        const siblings = items.filter((v) => group(v) === group(current));
        return (
          <div className="split">
            <section className="card" aria-labelledby="vh" style={{ flex: "1 1 280px", maxWidth: 420 }}>
              <div className="sec-h"><h2 id="vh" className="h">Versions</h2></div>
              {groups.map((g) => (
                <div key={g}>
                  <div className="m mu small" style={{ padding: "8px 18px 4px" }}>{g}</div>
                  <ul className="qlist">
                    {items.filter((v) => group(v) === g).map((v) => (
                      <li key={v.policy.policy_id}>
                        <button type="button" aria-current={v === current} onClick={() => setPicked(v.policy.policy_id)}>
                          <span className="row" style={{ gap: 8 }}>
                            <span className="m">v{v.policy.version}</span>
                            <PolicyBadge state={v.state} />
                            {v.guarantee_status === "suspended" && <span className="b wa">▲ adaptive, no guarantee</span>}
                          </span>
                          <span className="mu small">{day(v.policy.created_at)} · target {target(v)}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </section>
            <Detail key={current.policy.policy_id} view={current} siblings={siblings} />
          </div>
        );
      }}
    </Load>
  );
}

function target(v: PolicyView): string {
  const t = v.policy.target;
  if (t.kind === "cost") return "lowest cost";
  return `${t.metric === "false_pass" ? "false passes" : "errors"} ≤ ${pct(t.alpha ?? null)} at ${pct(1 - (t.delta ?? 0.05), 0)}`;
}

const LIFECYCLE = "Approve, reject and roll back arrive with the policy lifecycle in M3 (FR-27 to FR-29). Until then every policy stays a draft.";

function Detail({ view, siblings }: { view: PolicyView; siblings: PolicyView[] }) {
  const p = view.policy;
  const [against, setAgainst] = useState("");
  const diff = useLoad(
    (signal) => api<PolicyDiff>(`/v1/admin/policies/${p.policy_id}/diff?${new URLSearchParams(against ? { against } : {})}`, { signal }),
    [p.policy_id, against],
  );
  const others = siblings.filter((s) => s.policy.policy_id !== p.policy_id);
  const before = siblings.find((s) => s.policy.policy_id === (against || diff.data?.against));

  return (
    <div className="col" style={{ gap: 16, flex: "999 1 520px" }}>
      <section className="card" aria-labelledby="df">
        <div className="sec-h">
          <h2 id="df" className="h">
            {before ? `v${before.policy.version} → ` : ""}v{p.version}
          </h2>
          <label className="row" style={{ gap: 8 }}>
            <span className="mu small">Compare with</span>
            <select className="in" value={against} onChange={(e) => setAgainst(e.target.value)}>
              <option value="">the active version</option>
              {others.map((o) => <option key={o.policy.policy_id} value={o.policy.policy_id}>v{o.policy.version} ({o.state})</option>)}
            </select>
          </label>
          <button className="btn" type="button" disabled title={LIFECYCLE}>Reject</button>
          <button className="btn p" type="button" disabled title={LIFECYCLE}>Approve v{p.version}</button>
        </div>
        <p className="mu small" style={{ margin: 0, padding: "0 18px 8px" }}>{LIFECYCLE}</p>
        <Load state={diff} rows={4}>
          {(d) => (
            <>
              <div className="tw">
                <table className="t">
                  <thead><tr><th>Threshold</th><th className="r">{before ? `v${before.policy.version}, ${before.state}` : "before"}</th><th className="r">v{p.version}, {view.state}</th></tr></thead>
                  <tbody className="m">
                    {d.rules.map((r) => {
                      const rule = p.rules.find((x) => x.id === r.id);
                      return (
                        <tr key={r.id}>
                          <td>{rule ? `${rule.action} ${rule.op === "lt" ? "below" : "from"}` : r.id}</td>
                          <td className="r">{prob(r.before)}</td>
                          <td className="r">{prob(r.after)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <Metrics before={d.metrics.before} after={d.metrics.after} />
            </>
          )}
        </Load>
        <div className="col m mu small" style={{ padding: "8px 18px 16px", gap: 2, overflowWrap: "anywhere" }}>
          {before && <span>v{before.policy.version} {before.policy.fingerprint}</span>}
          <span>v{p.version} {p.fingerprint}</span>
        </div>
      </section>
      <GuaranteeBox view={view} />
      <section className="card" aria-labelledby="au">
        <div className="sec-h">
          <h2 id="au" className="h">Audit trail</h2>
          <button className="btn" type="button" disabled title={LIFECYCLE}>Roll back…</button>
        </div>
        {view.events.length ? (
          <div className="tw">
            <table className="t">
              <thead><tr><th>When (UTC)</th><th>Key</th><th>What</th><th>Note</th></tr></thead>
              <tbody>
                {view.events.map((e, i) => (
                  <tr key={i}>
                    <td className="m">{e.at.slice(0, 16).replace("T", " ")}</td>
                    <td className="m t2">{e.key_id.slice(0, 8)}…</td>
                    <td>{e.from_state ?? "new"} → {e.to_state}</td>
                    <td className="t2">{e.reason ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="t2 small" style={{ margin: 0, padding: "4px 18px 16px" }}>
            Compiled {p.created_at.slice(0, 16).replace("T", " ")} UTC from fit <span className="m">{p.fit_id.slice(0, 8)}…</span>. No one has acted on it yet.
          </p>
        )}
      </section>
    </div>
  );
}

function Metrics({ before, after }: { before: PolicyMetrics | null; after: PolicyMetrics }) {
  const pp = (a: number | null | undefined, b: number | null | undefined) =>
    a == null || b == null ? "" : `${b - a > 0 ? "+" : b - a < 0 ? "−" : "±"}${Math.abs(100 * (b - a)).toFixed(2)} pp`;
  const rows: [string, string, string, string][] = [
    ["Decided automatically", pct(before?.automated), pct(after.automated), pp(before?.automated, after.automated)],
    ["Escalated", pct(before?.escalated), pct(after.escalated), pp(before?.escalated, after.escalated)],
    ...Object.entries(after.rules).map(([id, r]): [string, string, string, string] => [
      `Wrong when ${id} fires (95% interval)`,
      before?.rules[id] ? `${pct(before.rules[id].rate, 2)} (${interval(before.rules[id].ci95)})` : "–",
      `${pct(r.rate, 2)} (${interval(r.ci95)})`,
      pp(before?.rules[id]?.rate, r.rate),
    ]),
  ];
  if (after.expected_cost_per_1000 != null)
    rows.push(["Cost per 1,000 decisions", usd(before?.expected_cost_per_1000), usd(after.expected_cost_per_1000), ""]);
  return (
    <div className="tw">
      <table className="t">
        <thead>
          <tr><th>On the evaluation split, n = {count(after.n)}</th><th className="r">Before</th><th className="r">After</th><th className="r">Change</th></tr>
        </thead>
        <tbody className="m">
          {rows.map(([label, a, b, c]) => (
            <tr key={label}><td>{label}</td><td className="r">{a}</td><td className="r">{b}</td><td className="r t2">{c}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** What the policy promises and on what assumptions, as the server certified it. */
export function GuaranteeBox({ view }: { view: PolicyView }) {
  const g = view.policy.guarantee;
  const title = `Guarantee · v${view.policy.version}${view.state === "active" ? ", active" : ""}`;
  if (g.status === "certified" && view.guarantee_status === "certified")
    return (
      <section className="guar okb" aria-label={title}>
        <div className="row"><h3 className="h grow" style={{ fontSize: 15 }}>{title}</h3><span className="ok small">✓ holds</span></div>
        <p style={{ margin: 0 }}>{g.statement}</p>
        <ul className="t2 small" style={{ margin: 0, paddingLeft: 18 }}>
          <li>Labelled n: {Object.entries(g.n).map(([k, n]) => `${k} ${count(n)}`).join(", ")}, decisions from {day(g.window.from)} to {day(g.window.to)}</li>
          {g.assumptions.map((a) => <li key={a}>{a}</li>)}
        </ul>
      </section>
    );
  if (g.status === "certified")
    return (
      <section className="guar wab" aria-label={title}>
        <div className="row"><h3 className="h grow" style={{ fontSize: 15 }}>{title}</h3><span className="b wa">▲ adaptive, no guarantee</span></div>
        <p style={{ margin: 0 }}>{g.statement} <span className="mu">(was promised)</span></p>
        <p className="wa small" style={{ margin: 0 }}>▲ An assumption is broken{view.suspended_by ? <>: see drift report <span className="m">{view.suspended_by}</span></> : ""}. The policy keeps running; the promise does not.</p>
      </section>
    );
  return (
    <section className="guar" aria-label={title}>
      <div className="row">
        <h3 className="h grow" style={{ fontSize: 15 }}>{title}</h3>
        <span className={g.reason === "adaptive_under_shift" ? "b wa" : "mu small"}>{g.reason === "adaptive_under_shift" ? "▲ adaptive, no guarantee" : "no guarantee"}</span>
      </div>
      <p style={{ margin: 0 }}>
        {g.reason === "cost_target" ? "A cost target picks the cheapest expected point. It makes no promise about errors, and says so." : "Traffic has shifted since the fit: thresholds adapt, without a guarantee."}
      </p>
      {g.detail && <p className="t2 small" style={{ margin: 0 }}>{g.detail}</p>}
    </section>
  );
}
