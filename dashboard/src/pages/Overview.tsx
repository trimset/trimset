import { useState } from "react";
import { api, type Key } from "../api.ts";
import { count, pct } from "../format.ts";
import * as mock from "../mock.ts";
import { href, Load, SamplePill, SeverityMark, Sparkline, Tile, Top, useLoad } from "../ui.tsx";

/** Real: a project with no proxy or ingest key can't have logged a decision yet. */
export function EmptyProject({ demo }: { demo?: boolean }) {
  const [secret, setSecret] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const create = async () => {
    setError(null);
    try {
      const k = await api<Key & { secret: string }>("/v1/admin/keys", { body: { scope: "proxy", label: "first app" } });
      setSecret(k.secret);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <section className="card col" aria-labelledby="empty-h" style={{ padding: 22, gap: 14 }}>
      <span className="lab">Empty project</span>
      <h2 id="empty-h" className="h" style={{ fontSize: 22 }}>No decisions yet</h2>
      <p className="t2" style={{ margin: 0, maxWidth: 720 }}>
        Point your app's model URL at trimset. Requests pass through unchanged, and every answer is logged.
      </p>
      <div className="split">
        <div className="col">
          <span className="mu small">Before</span>
          <pre>TYPESAFE_BASE_URL=https://&lt;your model server&gt;</pre>
        </div>
        <div className="col">
          <span className="mu small">After: one line</span>
          <pre>
            TYPESAFE_BASE_URL={window.location.origin}/p/<span style={{ color: "var(--acx)" }}>{secret ?? "<proxy key>"}</span>
          </pre>
        </div>
      </div>
      {secret && <p className="wa small" style={{ margin: 0 }}>▲ Copy the key now: this is the only time it is shown. trimset keeps a hash.</p>}
      {error && <p className="cr small" role="alert" style={{ margin: 0 }}>✕ {error}</p>}
      <div className="row">
        {!secret && (
          <button className="btn p" type="button" onClick={create} disabled={demo} title={demo ? "On the states board this button only shows the look" : undefined}>
            Create a proxy key
          </button>
        )}
        <a className="btn" href="https://github.com/trimset/trimset">Log from your backend instead</a>
      </div>
    </section>
  );
}

export function Overview() {
  const keys = useLoad((signal) => api<{ items: Key[] }>("/v1/admin/keys", { signal }), []);
  const data = useLoad(() => mock.overview(), []);
  const empty = keys.data && !keys.data.items.some((k) => !k.revoked_at && (k.scope === "proxy" || k.scope === "ingest"));

  return (
    <>
      <Top crumb="trimset" title="Overview">
        {!empty && <SamplePill endpoint={mock.ENDPOINT.overview} />}
      </Top>
      <div className="body">
        <Load state={keys} rows={3}>
          {() =>
            empty ? (
              <EmptyProject />
            ) : (
              <Load state={data}>
                {(o) => (
                  <>
                    <section aria-label="Today at a glance" className="tiles">
                      <Tile label="Decisions today" value={count(o.decisionsToday)} note={<><span className="m">{o.vsYesterday}</span> vs yesterday</>} />
                      <Tile label="Decided automatically" value={pct(o.automated)} note="ACCEPT, BLOCK or SELECT" />
                      <Tile label="Escalation rate" value={pct(o.escalated)} note="to a bigger model or a person" />
                      <Tile label="Outcome coverage, 30 days" value={pct(o.coverage30d)} note="chargebacks still arriving" />
                      <Tile label="Active policies" value={<>{o.activePolicies[0]}<span className="mu" style={{ fontSize: 16 }}> / {o.activePolicies[1]}</span></>} note="1 question has a draft only" />
                      <Tile label="Open alerts" value={o.openAlerts.total} note={`✕ ${o.openAlerts.critical} critical · ${o.openAlerts.warnings} warnings`} noteClass="cr" to="#/alerts" />
                    </section>
                    <section className="card" aria-labelledby="qh">
                      <div className="sec-h">
                        <h2 id="qh" className="h">Questions</h2>
                        <span className="mu small">7-day sparkline: decisions a day</span>
                        <a className="btn" href="#/questions">All questions</a>
                      </div>
                      <div className="tw">
                        <table className="t">
                          <thead>
                            <tr>
                              <th>Question</th><th>Type</th><th>Model served</th><th className="r">Decisions, 24 h</th><th className="r">Automated</th>
                              <th className="r">Outcomes known</th><th className="r">ECE</th><th>Policy</th><th>Health</th><th>7 days</th>
                            </tr>
                          </thead>
                          <tbody>
                            {o.questions.map((q) => (
                              <tr key={q.name}>
                                <td><a className="m" href={href("questions", q.name)}>{q.name}</a></td>
                                <td className="t2">{q.type}</td>
                                <td className="m t2">
                                  {(q.models.find((m) => m.isNew) ?? q.models[0]).name}
                                  {q.models.some((m) => m.isNew) && <span className="wa"> ▲ new</span>}
                                </td>
                                <td className="m r">{count(q.decisions24h)}</td>
                                <td className="m r">{pct(q.automated)}</td>
                                <td className="m r">{pct(q.outcomesKnown)}</td>
                                <td className={`m r${q.ece == null ? " mu" : ""}`}>{q.ece == null ? "n/a" : q.ece.toFixed(3)}</td>
                                <td className={q.policy === "draft" ? "mu" : "m"}>{q.policy}</td>
                                <td className={q.health.tone}>{q.health.tone === "mu" ? "◷ n = 412, too few" : q.health.text}</td>
                                <td><Sparkline points={q.spark} tone={q.health.tone === "ok" || q.health.tone === "mu" ? "var(--t2)" : `var(--${q.health.tone})`} /></td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </section>
                    <div className="split">
                      <section className="card" aria-labelledby="ah">
                        <div className="sec-h">
                          <h2 id="ah" className="h">Open alerts</h2>
                          <a href="#/alerts">All alerts</a>
                        </div>
                        <div className="tw">
                          <table className="t">
                            <tbody>
                              {o.alerts.map((a) => (
                                <tr key={a.text}>
                                  <td><SeverityMark s={a.severity} /></td>
                                  <td className="text">{a.text}</td>
                                  <td className="m t2">{a.question}</td>
                                  <td className="m mu r">{a.ago}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </section>
                      <section className="card pad col" aria-labelledby="oh" style={{ gap: 12 }}>
                        <div className="row">
                          <h2 id="oh" className="h grow" style={{ fontSize: 18 }}>Outcomes, last 24 hours</h2>
                          <a href="#/outcomes">Outcome feed</a>
                        </div>
                        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", gap: 12 }}>
                          {(
                            [
                              [o.outcomes24h.joined, "joined"],
                              [o.outcomes24h.corrections, "corrections"],
                              [o.outcomes24h.held, "held, decision not seen yet"],
                              [o.outcomes24h.byRule, "labelled by rule"],
                            ] as const
                          ).map(([v, l]) => (
                            <div key={l} className="col" style={{ gap: 2 }}>
                              <span className="m" style={{ fontSize: 20 }}>{count(v)}</span>
                              <span className="mu small">{l}</span>
                            </div>
                          ))}
                        </div>
                        <div className="stack" role="img" aria-label="Sources: human review 31%, downstream 27%, rule 42%" style={{ height: 10 }}>
                          <div style={{ flex: "31 1 0", background: "var(--ok)" }} />
                          <div style={{ flex: "27 1 0", background: "var(--t2)" }} />
                          <div style={{ flex: "42 1 0", background: "var(--bd2)" }} />
                        </div>
                        <div className="row t2 small">
                          <span><span className="m">31%</span> human review</span>
                          <span><span className="m">27%</span> downstream signal</span>
                          <span><span className="m">42%</span> rule (no chargeback in 120 days)</span>
                        </div>
                      </section>
                    </div>
                  </>
                )}
              </Load>
            )
          }
        </Load>
      </div>
    </>
  );
}
