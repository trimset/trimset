import { useState } from "react";
import { count } from "../format.ts";
import * as mock from "../mock.ts";
import { Load, SamplePill, Tile, Top, useLoad } from "../ui.tsx";

export function Outcomes() {
  const state = useLoad(() => mock.outcomes(), []);
  const [source, setSource] = useState("");
  const [question, setQuestion] = useState("");
  const [corrections, setCorrections] = useState(false);
  return (
    <>
      <Top crumb="Capture" title="Outcomes">
        <SamplePill endpoint={mock.ENDPOINT.outcomes} />
      </Top>
      <div className="body">
        <Load state={state}>
          {(o) => {
            const feed = o.feed.filter(
              (r) => (!source || r.source === source) && (!question || r.question === question) && (!corrections || r.correction),
            );
            return (
              <>
                <section aria-label="Last 24 hours" className="tiles">
                  <Tile label="Joined" value={count(o.tiles.joined)} />
                  <Tile label="Corrections" value={count(o.tiles.corrections)} />
                  <Tile label="Held, decision not seen yet" value={count(o.tiles.held)} />
                  <Tile label="Labelled by rule" value={count(o.tiles.byRule)} />
                  <Tile label="Median delay, refunds" value={o.tiles.medianDelay} />
                </section>
                <div role="group" aria-label="Filters" className="row">
                  <select className="in" aria-label="Source" value={source} onChange={(e) => setSource(e.target.value)}>
                    <option value="">Any source</option>
                    <option>Human review</option><option>Downstream signal</option><option>Rule</option>
                  </select>
                  <select className="in" aria-label="Question" value={question} onChange={(e) => setQuestion(e.target.value)}>
                    <option value="">All questions</option>
                    {[...new Set(o.feed.map((r) => r.question))].map((x) => <option key={x}>{x}</option>)}
                  </select>
                  <label className="row" style={{ gap: 8, fontSize: 13 }}>
                    <input type="checkbox" checked={corrections} onChange={(e) => setCorrections(e.target.checked)} /> Corrections only
                  </label>
                </div>
                <section className="card" aria-labelledby="fh">
                  <div className="sec-h"><h2 id="fh" className="h">Arriving outcomes</h2></div>
                  <div className="tw">
                    <table className="t">
                      <thead>
                        <tr>
                          <th>Received</th><th>Question</th><th>Names the decision by</th><th>Label</th><th>Source</th>
                          <th className="r">Delay since decision</th><th>Change</th><th>Sent by</th>
                        </tr>
                      </thead>
                      <tbody>
                        {feed.map((r, i) => (
                          <tr key={i}>
                            <td className="m">{r.at}</td>
                            <td className="m">{r.question}</td>
                            <td className="m t2">{r.by}</td>
                            <td className="m">{r.label}</td>
                            <td>{r.source}</td>
                            <td className="m r">{r.delay}</td>
                            <td className={r.correction ? "bad" : "t2"}>{r.correction ? <>✕ Correction: {r.correction}</> : r.note}</td>
                            <td className={`m ${r.sender === "worker" ? "mu" : "t2"}`}>{r.sender}</td>
                          </tr>
                        ))}
                        {feed.length === 0 && <tr><td colSpan={8} className="mu">No outcomes match these filters.</td></tr>}
                      </tbody>
                    </table>
                  </div>
                </section>
                <section className="card" aria-labelledby="uh">
                  <div className="sec-h">
                    <h2 id="uh" className="h">Held: no decision with this external_ref yet</h2>
                    <span className="mu small">Joined automatically when the decision arrives; dropped after 30 days</span>
                  </div>
                  <div className="tw">
                    <table className="t">
                      <thead>
                        <tr><th>Received</th><th>Question</th><th>external_ref</th><th>Label</th><th>Source</th><th className="r">Expires in</th><th>Likely reason</th></tr>
                      </thead>
                      <tbody>
                        {o.held.map((r) => (
                          <tr key={r.ref}>
                            <td className="m">{r.at}</td>
                            <td className="m">{r.question}</td>
                            <td className="m">{r.ref}</td>
                            <td className="m">{r.label}</td>
                            <td>{r.source}</td>
                            <td className={`m r${r.soon ? " wa" : ""}`}>{r.soon && "▲ "}{r.expires}</td>
                            <td className="t2">{r.reason}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              </>
            );
          }}
        </Load>
      </div>
    </>
  );
}
