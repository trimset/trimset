import { count, pct } from "../format.ts";
import * as mock from "../mock.ts";
import { Load, SamplePill, Tile, Top, useLoad } from "../ui.tsx";

export function Shadow() {
  const state = useLoad(() => mock.shadow(), []);
  return (
    <>
      <Top crumb="Analyze" title="Shadow comparison">
        <SamplePill endpoint={mock.ENDPOINT.shadow} />
      </Top>
      <div className="body">
        <Load state={state}>
          {(s) => (
            <>
              <div className="row">
                <span className="pill m">primary {s.primary_model}, pinned</span>
                <span className="mu">against</span>
                <span className="pill m">candidate {s.candidate_model}</span>
                <span className="t2">on the same {count(s.pairs)} live {s.question} calls since {s.since}</span>
              </div>
              <section aria-label="Summary" className="tiles">
                <Tile label="Agreement" value={pct(s.agreement)} note="same top option" />
                <Tile label="Flip rate" value={pct(s.flip_rate)} note="changes the action taken" />
                <Tile
                  label="Accuracy, labelled"
                  value={`${(s.accuracy.primary * 100).toFixed(1)} → ${pct(s.accuracy.candidate)}`}
                  note={`✕ ${s.accuracy.delta} · n = ${count(s.labeled_pairs)}`}
                  noteClass="bad"
                />
                <Tile label="Cost per 1,000" value={`+$${(s.cost_per_1000.candidate - s.cost_per_1000.primary).toFixed(2)}`} note={`$${s.cost_per_1000.primary.toFixed(2)} → $${s.cost_per_1000.candidate.toFixed(2)}`} />
                <Tile label="Latency p50 / p99" value={s.latency.delta} note={`${s.latency.primary} → ${s.latency.candidate} ms`} />
              </section>
              <div className="split">
                <section className="card" aria-labelledby="mc">
                  <div className="sec-h"><h2 id="mc" className="h">McNemar test on the labelled calls</h2></div>
                  <div className="tw">
                    <table className="t">
                      <thead><tr><th /><th className="r">Candidate right</th><th className="r">Candidate wrong</th></tr></thead>
                      <tbody className="m">
                        <tr><td>Primary right</td><td className="r">{count(s.mcnemar.both_right)}</td><td className="r bad">{count(s.mcnemar.b)}</td></tr>
                        <tr><td>Primary wrong</td><td className="r ok">{count(s.mcnemar.c)}</td><td className="r">{count(s.mcnemar.both_wrong)}</td></tr>
                      </tbody>
                    </table>
                  </div>
                  <div className="col pad" style={{ gap: 6 }}>
                    <p style={{ margin: 0 }}>
                      Of the {s.mcnemar.b + s.mcnemar.c} calls where they disagree and one is right, the candidate is wrong on{" "}
                      <span className="m">{s.mcnemar.b}</span>. Exact test: <span className="m">p = {s.mcnemar.p_value}</span>.
                    </p>
                    <p className="mu small" style={{ margin: 0 }}>Only the discordant pairs count. Labels: the team that closed the ticket.</p>
                  </div>
                </section>
                <section className="card" aria-labelledby="fl">
                  <div className="sec-h"><h2 id="fl" className="h">Where they disagree</h2></div>
                  <div className="tw">
                    <table className="t">
                      <thead><tr><th>Primary says</th><th>Candidate says</th><th className="r">Calls</th><th className="r">Candidate right when labelled</th></tr></thead>
                      <tbody>
                        {s.flips.map((f) => (
                          <tr key={f.primary + f.candidate}>
                            <td className="m">{f.primary}</td>
                            <td className="m">{f.candidate}</td>
                            <td className="m r">{count(f.n)}</td>
                            <td className={`m r ${f.tone}`}>{f.candidateRight}</td>
                          </tr>
                        ))}
                        <tr><td className="mu" colSpan={2}>{s.other.pairs} other pairs</td><td className="m r">{count(s.other.n)}</td><td className="m r mu">n &lt; 30 each</td></tr>
                      </tbody>
                    </table>
                  </div>
                </section>
              </div>
              <section className="card pad col" aria-labelledby="vd" style={{ gap: 12 }}>
                <h2 id="vd" className="h" style={{ fontSize: 18 }}>What this says</h2>
                <p style={{ margin: 0 }}>
                  The candidate sends more tickets to returns, and it is wrong more often when it does. Keep the primary pinned until a refit on{" "}
                  <span className="m">{s.candidate_model}</span> has enough labels.
                </p>
                <div className="row">
                  <button className="btn" type="button" disabled title="Shadow models arrive in M5">Keep watching</button>
                  <button className="btn p" type="button" disabled title="Shadow models arrive in M5">Pin {s.primary_model} for this question</button>
                </div>
              </section>
            </>
          )}
        </Load>
      </div>
    </>
  );
}
