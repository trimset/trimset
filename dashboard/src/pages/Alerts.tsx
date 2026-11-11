import { useState } from "react";
import * as mock from "../mock.ts";
import { href, Load, SamplePill, SeverityMark, Top, useLoad } from "../ui.tsx";

const STATUS = { open: "Open", acknowledged: "Acknowledged", resolved: "Resolved" };
const LATER = "Alert rules arrive in M4";

export function Alerts() {
  const state = useLoad(() => mock.alerts(), []);
  const [show, setShow] = useState("active");
  const [acked, setAcked] = useState<string[]>([]);
  return (
    <>
      <Top crumb="Act" title="Alerts">
        <SamplePill endpoint={mock.ENDPOINT.alerts} />
        <select className="in" aria-label="Status" value={show} onChange={(e) => setShow(e.target.value)}>
          <option value="active">Open and acknowledged</option>
          <option value="all">All</option>
          <option value="resolved">Resolved</option>
        </select>
      </Top>
      <div className="body">
        <Load state={state}>
          {(a) => {
            const items = a.items
              .map((i) => (acked.includes(i.alert_id) ? { ...i, status: "acknowledged" as const, statusNote: "you (sample: not saved)" } : i))
              .filter((i) => show === "all" || (show === "resolved" ? i.status === "resolved" : i.status !== "resolved"));
            const action = (i: mock.AlertRow) =>
              i.link ? (
                <a className="btn" href={i.link}>Open drift</a>
              ) : i.status === "open" ? (
                <button className="btn" type="button" onClick={() => setAcked([...acked, i.alert_id])}>Acknowledge</button>
              ) : null;
            const status = (i: mock.AlertRow) =>
              i.status === "resolved" ? (
                <span className="ok">✓ Resolved {i.statusNote}</span>
              ) : (
                <span>{STATUS[i.status]}{i.statusNote && <> · <span className="m">{i.statusNote}</span></>}</span>
              );
            const question = (i: mock.AlertRow) => (i.question ? <a className="m" href={href("questions", i.question)}>{i.question}</a> : <span className="mu">all</span>);
            return (
              <>
                <section className="card" aria-labelledby="al">
                  <div className="sec-h"><h2 id="al" className="h">Alerts</h2></div>
                  <div className="tw table-wide">
                    <table className="t">
                      <thead><tr><th>Severity</th><th>Rule</th><th>Question</th><th>Status</th><th>Opened</th><th className="r"><span className="sr">Action</span></th></tr></thead>
                      <tbody>
                        {items.map((i) => (
                          <tr key={i.alert_id}>
                            <td><SeverityMark s={i.severity} /></td>
                            <td>{i.summary}{i.detail && <span className="m t2"> ({i.detail})</span>}</td>
                            <td>{question(i)}</td>
                            <td>{status(i)}</td>
                            <td className="m t2">{i.opened}</td>
                            <td className="r">{action(i)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="cards-narrow" style={{ gap: 0, padding: "0 12px 4px" }}>
                    {items.map((i) => (
                      <article key={i.alert_id} className="col" style={{ padding: "14px 6px", gap: 8, borderTop: "1px solid var(--dv)" }}>
                        <div className="row"><SeverityMark s={i.severity} /><span className="grow" /><span className="m mu small">{i.opened}</span></div>
                        <span>{i.summary}{i.detail && <span className="m t2"> ({i.detail})</span>}</span>
                        <div className="row small">{question(i)}<span className="t2">{status(i)}</span></div>
                        {action(i)}
                      </article>
                    ))}
                  </div>
                  {!items.length && <p className="mu" style={{ margin: 0, padding: "4px 18px 16px" }}>No alerts here.</p>}
                </section>
                <div className="split">
                  <section className="card pad col" aria-labelledby="nr" style={{ gap: 12 }}>
                    <h2 id="nr" className="h" style={{ fontSize: 18 }}>New rule</h2>
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12 }}>
                      <label className="f">Question<select className="in"><option>All questions</option><option>ticket_triage</option></select></label>
                      <label className="f">Metric
                        <select className="in">
                          {["Escalation rate", "PSI, top probability", "Jensen-Shannon, option mix", "Error rate, labelled", "ECE", "Latency p99", "Upstream error rate", "Review queue per day", "Model version changed"].map((m) => <option key={m}>{m}</option>)}
                        </select>
                      </label>
                      <label className="f">Above<input className="in m" defaultValue="12%" /></label>
                      <label className="f">For<select className="in"><option>2 windows of 1 hour</option><option>1 day</option></select></label>
                      <label className="f">Severity<select className="in"><option>Warning</option><option>Critical</option><option>Info</option></select></label>
                    </div>
                    <fieldset style={{ border: 0, padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
                      <legend style={{ fontSize: 13, marginBottom: 8 }}>Send to <span className="mu">(channels are set by the operator)</span></legend>
                      {a.channels.map((c, i) => (
                        <label key={c.name} className="row" style={{ gap: 8, fontSize: 13 }}>
                          <input type="radio" name="ch" defaultChecked={i === 0} /> {c.kind} · <span className="m">{c.name}</span>
                        </label>
                      ))}
                    </fieldset>
                    <div className="row">
                      <button className="btn" type="button" disabled title={LATER}>Send a test</button>
                      <button className="btn p" type="button" disabled title={LATER}>Save rule</button>
                      <span className="mu small">{LATER}.</span>
                    </div>
                  </section>
                  <section className="card" aria-labelledby="rl">
                    <div className="sec-h"><h2 id="rl" className="h">Rules</h2></div>
                    <div className="tw">
                      <table className="t">
                        <thead><tr><th>Metric</th><th>Question</th><th>When</th><th>Channel</th></tr></thead>
                        <tbody>
                          {a.rules.map(([m, q, when, ch]) => (
                            <tr key={m + q}><td>{m}</td><td className={q ? "m" : "mu"}>{q ?? "all"}</td><td className="m">{when}</td><td>{ch}</td></tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </section>
                </div>
              </>
            );
          }}
        </Load>
      </div>
    </>
  );
}
