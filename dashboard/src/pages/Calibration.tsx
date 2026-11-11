import { useState } from "react";
import { ApiError, api, type Bin, type CoveragePath, type Fit, type Job, q, type Question, waitJob } from "../api.ts";
import { count, day, pct } from "../format.ts";
import { ErrorState, Load, Pending, useLoad } from "../ui.tsx";

export function useLatestFit(name: string, model: string) {
  return useLoad(
    (signal) =>
      api<{ items: Fit[] }>(`/v1/admin/questions/${q(name)}/fits?model_served=${encodeURIComponent(model)}`, { signal }).then(
        (r) => r.items[0] ?? null,
      ),
    [name, model],
  );
}

/** POST .../fits runs on the worker; this waits for it and reports the server's answer. */
export function FitNow({ name, model, onDone, label = "Fit calibration now" }: { name: string; model: string; onDone: () => void; label?: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const job = await waitJob(await api<Job>(`/v1/admin/questions/${q(name)}/fits`, { body: { model_served: model } }));
      if (job.status === "failed") throw new ApiError(422, "The fit failed", { detail: job.error ?? undefined });
      onDone();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="col" style={{ gap: 10 }}>
      <div className="row">
        <button className="btn p" type="button" onClick={run} disabled={busy}>
          {busy ? "Fitting…" : label}
        </button>
        {busy && <span className="mu small" role="status">◷ Running on the worker</span>}
      </div>
      {error != null && <ErrorState error={error} />}
    </div>
  );
}

export function Calibration({ name, question, model }: { name: string; question: Question; model: string }) {
  const fit = useLatestFit(name, model);
  const coverage = useLoad(
    (signal) => api<{ paths: CoveragePath[] }>(`/v1/admin/questions/${q(name)}/coverage?model_served=${encodeURIComponent(model)}`, { signal }),
    [name, model],
  );
  const event = question.type === "noul" ? "true" : "top option right";

  return (
    <>
      <Load state={fit}>
        {(f) =>
          f === null ? (
            <section className="card pad col" style={{ gap: 12 }}>
              <h2 className="h" style={{ fontSize: 18 }}>No calibration fit for <span className="m">{model}</span> yet</h2>
              <p className="t2" style={{ margin: 0 }}>
                A fit maps the model's probabilities to observed rates, from labelled decisions. Thresholds and guarantees need one.
              </p>
              <FitNow name={name} model={model} onDone={fit.reload} />
            </section>
          ) : (
            <>
              <div className="row mu small">
                <span>
                  Fit <span className="m">{f.fit_id}</span> · {f.calibration.method} · {day(f.created_at)} · window {day(f.provenance.window.from)} – {day(f.provenance.window.to)}
                </span>
                <span className="grow" />
                <FitNow name={name} model={model} onDone={fit.reload} label="Refit now" />
              </div>
              <div className="split">
                <section className="card pad col" aria-labelledby="rd" style={{ gap: 10 }}>
                  <div className="row">
                    <h2 id="rd" className="h grow" style={{ fontSize: 18 }}>Reliability, evaluation split</h2>
                    <span className="t2 small row" style={{ gap: 6 }}><Legend dashed tone="var(--bad)" />raw</span>
                    <span className="t2 small row" style={{ gap: 6 }}><Legend tone="var(--ok)" />calibrated ({f.calibration.method})</span>
                  </div>
                  <Reliability raw={f.reliability.raw} cal={f.reliability.calibrated} event={event} />
                </section>
                <div className="col" style={{ gap: 16 }}>
                  <section className="card" aria-labelledby="mt">
                    <div className="sec-h"><h2 id="mt" className="h">Metrics, evaluation split</h2></div>
                    <div className="tw">
                      <table className="t">
                        <thead><tr><th>Metric</th><th className="r">Raw</th><th className="r">Calibrated</th><th className="r">Change</th></tr></thead>
                        <tbody className="m">
                          {(
                            [
                              ["Log loss", "log_loss", 3],
                              ["Brier score", "brier", 4],
                              [`ECE, equal width (${f.metrics.raw.bins} bins)`, "ece_equal_width", 3],
                              [`ECE, equal mass (${f.metrics.raw.bins} bins)`, "ece_equal_mass", 3],
                            ] as const
                          ).map(([label, k, digits]) => {
                            const a = f.metrics.raw[k],
                              b = f.metrics.calibrated[k];
                            const change = a > 0 ? (b - a) / a : null;
                            return (
                              <tr key={k}>
                                <td>{label}</td>
                                <td className="r">{a.toFixed(digits)}</td>
                                <td className="r">{b.toFixed(digits)}</td>
                                <td className={`r ${change == null ? "mu" : change <= 0 ? "ok" : "bad"}`}>
                                  {change == null ? "–" : `${change <= 0 ? "▼ −" : "▲ +"}${Math.abs(change * 100).toFixed(1)}%`}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                    <p className="mu small" style={{ margin: 0, padding: "10px 18px 14px" }}>n = <span className="m">{count(f.metrics.calibrated.n)}</span> labelled in the evaluation split.</p>
                  </section>
                  <section className="card" aria-labelledby="fm">
                    <div className="sec-h"><h2 id="fm" className="h">Fit methods compared</h2></div>
                    <div className="tw">
                      <table className="t">
                        <thead><tr><th>Method</th><th className="r">Cross-validated log loss</th><th>Chosen</th></tr></thead>
                        <tbody>
                          {Object.entries(f.selection.cv_log_loss).map(([method, loss]) => (
                            <tr key={method}>
                              <td style={{ textTransform: "capitalize" }}>{method}</td>
                              <td className="m r">{loss?.toFixed(4)}</td>
                              <td className={method === f.calibration.method ? "ok" : "mu"}>{method === f.calibration.method ? "✓ chosen" : "no"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <p className="mu small" style={{ margin: 0, padding: "10px 18px 14px" }}>
                      Chosen by {f.selection.cv_folds}-fold cross-validation inside the fit split; reported on evaluation, which no fit saw.
                    </p>
                  </section>
                  <section className="card pad col" aria-labelledby="sp" style={{ gap: 10 }}>
                    <h2 id="sp" className="h" style={{ fontSize: 18 }}>Splits</h2>
                    <div className="stack" role="img" aria-label={`Fit ${f.provenance.n.fit}, tune ${f.provenance.n.tune}, evaluation ${f.provenance.n.eval}`}>
                      <div style={{ flex: `${f.provenance.n.fit} 1 0`, background: "var(--t2)" }} />
                      <div style={{ flex: `${f.provenance.n.tune} 1 0`, background: "var(--bd2)", borderLeft: "2px solid var(--sf)" }} />
                      <div style={{ flex: `${f.provenance.n.eval} 1 0`, background: "var(--ok)", borderLeft: "2px solid var(--sf)" }} />
                    </div>
                    <div className="row" style={{ gap: 20 }}>
                      {(
                        [
                          ["fit", "fits the calibration map"],
                          ["tune", "chooses thresholds"],
                          ["eval", "reports, and the simulator"],
                        ] as const
                      ).map(([k, what]) => (
                        <div key={k} className="col" style={{ gap: 0 }}>
                          <span className="m">{count(f.provenance.n[k])}</span>
                          <span className="mu small">{k} · {what}</span>
                        </div>
                      ))}
                    </div>
                    <p className="t2 small" style={{ margin: 0 }}>
                      Decisions older than the label horizon (<span className="m">{f.provenance.label_horizon_days} d</span>), split by a seeded hash of the decision id.
                    </p>
                  </section>
                </div>
              </div>
            </>
          )
        }
      </Load>
      <Load state={coverage} rows={2}>
        {(c) => <Coverage paths={c.paths} />}
      </Load>
    </>
  );
}

function Legend({ tone, dashed }: { tone: string; dashed?: boolean }) {
  return (
    <svg width="22" height="10" aria-hidden="true">
      <line x1="0" y1="5" x2="22" y2="5" stroke={tone} strokeWidth="2" strokeDasharray={dashed ? "4 3" : undefined} />
    </svg>
  );
}

function Reliability({ raw, cal, event }: { raw: Bin[]; cal: Bin[]; event: string }) {
  const x = (p: number) => 50 + p * 440;
  const y = (p: number) => 340 - p * 320;
  const line = (bins: Bin[]) =>
    bins
      .filter((b) => b.mean_predicted != null && b.observed != null && b.count > 0)
      .map((b) => `${x(b.mean_predicted!)},${y(b.observed!)}`)
      .join(" ");
  const maxLog = Math.log10(Math.max(10, ...cal.map((b) => b.count)));
  return (
    <svg viewBox="0 0 520 470" role="img" aria-label={`Reliability diagram: predicted probability against observed rate of ${event}, raw and calibrated, with decisions per bin`} style={{ width: "100%", height: "auto" }}>
      <g stroke="var(--dv)">
        {[0.25, 0.5, 0.75, 1].map((t) => <line key={t} x1="50" x2="490" y1={y(t)} y2={y(t)} />)}
      </g>
      <line x1="50" y1="340" x2="490" y2="340" stroke="var(--bd2)" />
      <line x1="50" y1="20" x2="50" y2="340" stroke="var(--bd2)" />
      <line x1="50" y1="340" x2="490" y2="20" stroke="var(--mu)" strokeDasharray="3 4" />
      <polyline points={line(raw)} fill="none" stroke="var(--bad)" strokeWidth="2" strokeDasharray="5 4" />
      <polyline points={line(cal)} fill="none" stroke="var(--ok)" strokeWidth="2" />
      <g fill="var(--mu)" fontFamily="JetBrains Mono Variable, monospace" fontSize="11" textAnchor="end">
        {[0, 0.25, 0.5, 0.75, 1].map((t) => <text key={t} x="44" y={y(t) + 4}>{t.toFixed(2)}</text>)}
      </g>
      <text x="14" y="180" fill="var(--t2)" fontSize="12" transform="rotate(-90 14 180)" textAnchor="middle">Observed rate: {event}</text>
      <g fill="var(--bd2)">
        {cal.map((b) => {
          const h = b.count > 0 ? (Math.log10(b.count + 1) / maxLog) * 56 : 0;
          return <rect key={b.lo} x={x(b.lo) + 1} width={Math.max(1, (b.hi - b.lo) * 440 - 2)} y={420 - h} height={h}><title>{`${b.lo.toFixed(2)}–${b.hi.toFixed(2)}: ${b.count} decisions`}</title></rect>;
        })}
      </g>
      <g fill="var(--mu)" fontFamily="JetBrains Mono Variable, monospace" fontSize="11" textAnchor="middle">
        {[0, 0.25, 0.5, 0.75, 1].map((t) => <text key={t} x={x(t)} y="437">{t.toFixed(2)}</text>)}
      </g>
      <text x="270" y="460" fill="var(--t2)" fontSize="12" textAnchor="middle">Predicted probability · bars: decisions per bin, log scale</text>
    </svg>
  );
}

function Coverage({ paths }: { paths: CoveragePath[] }) {
  const decisions = paths.reduce((s, p) => s + p.decisions, 0);
  const labeled = paths.reduce((s, p) => s + p.labeled, 0);
  if (decisions === 0) return <p className="t2">No decisions for this model in the last 90 days.</p>;
  return (
    <>
      <Pending labelled={labeled / decisions} title={`${pct(1 - labeled / decisions, 0)} of the last 90 days' decisions have no outcome yet`}>
        Metrics and guarantees use labelled decisions only, and say how many.
      </Pending>
      <section className="card" aria-labelledby="cv">
        <div className="sec-h"><h2 id="cv" className="h">Label coverage by action path, 90 days</h2></div>
        <div className="tw">
          <table className="t">
            <thead>
              <tr><th>Model served</th><th>Action</th><th className="r">Decisions</th><th className="r">Matured</th><th className="r">Labelled</th><th className="r">Audited</th><th className="r">Median delay</th></tr>
            </thead>
            <tbody className="m">
              {paths.map((p) => (
                <tr key={p.model_served + p.action}>
                  <td>{p.model_served}</td>
                  <td>{p.action}</td>
                  <td className="r">{count(p.decisions)}</td>
                  <td className="r">{count(p.matured)}</td>
                  <td className="r">{count(p.labeled)}</td>
                  <td className="r">{count(p.audited_labeled ?? 0)}</td>
                  <td className="r">{p.median_delay_hours == null ? "–" : p.median_delay_hours < 48 ? `${p.median_delay_hours.toFixed(1)} h` : `${(p.median_delay_hours / 24).toFixed(0)} d`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
