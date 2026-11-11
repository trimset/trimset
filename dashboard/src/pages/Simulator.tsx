import { useEffect, useMemo, useState } from "react";
import { ApiError, api, type Bin, type Costs, type Fit, type Job, type PolicyMetrics, q, type Question, type Rule, type SimulateResult, type Target, waitJob } from "../api.ts";
import { candidate, count, prob, sameScalar, scalarName, simView, startThreshold } from "../format.ts";
import { ErrorState, href, Load, Skeleton, Tile, TooFew, useLoad } from "../ui.tsx";
import { FitNow, useLatestFit } from "./Calibration.tsx";

// FR-26. The browser never computes a rate, an interval or a guarantee: every number
// below is a field of POST /simulate or of the compile job's result, formatted.

export function Simulator({ name, question, model }: { name: string; question: Question; model: string }) {
  const fit = useLatestFit(name, model);
  return (
    <Load state={fit}>
      {(f) =>
        f === null ? (
          <section className="card pad col" style={{ gap: 12 }}>
            <h2 className="h" style={{ fontSize: 18 }}>The simulator needs a calibration fit for <span className="m">{model}</span></h2>
            <FitNow name={name} model={model} onDone={fit.reload} />
          </section>
        ) : (
          <WithShapes name={name} question={question} model={model} fit={f} />
        )
      }
    </Load>
  );
}

/** The rule shapes (ids, actions, scalars, ops) come from the server's own grid. */
function WithShapes({ name, question, model, fit }: { name: string; question: Question; model: string; fit: Fit }) {
  const shapes = useLoad(
    (signal) =>
      api<SimulateResult>(`/v1/admin/questions/${q(name)}/simulate`, { body: { model_served: model, fit_id: fit.fit_id }, signal }).then((r) => {
        if (!r.rows.length) throw new ApiError(422, "The simulator returned no rows for this question");
        return r.rows[0].rules;
      }),
    [name, model, fit.fit_id],
  );
  return <Load state={shapes}>{(rules) => <Sim key={fit.fit_id} name={name} question={question} model={model} fit={fit} shapes={rules} />}</Load>;
}

function costsFrom(target: Target | undefined): Costs {
  return { escalation: target?.costs?.escalation ?? 0, wrong: { ...target?.costs?.wrong } };
}

const hasCosts = (c: Costs) => (c.escalation ?? 0) > 0 || Object.values(c.wrong ?? {}).some((v) => (v ?? 0) > 0);

function Sim({ name, question, model, fit, shapes }: { name: string; question: Question; model: string; fit: Fit; shapes: Rule[] }) {
  const [t, setT] = useState(() => shapes.map(startThreshold));
  const [costs, setCosts] = useState(() => costsFrom(question.settings.target));
  const [metrics, setMetrics] = useState<PolicyMetrics | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  // One debounced request per change; a newer one aborts the older.
  useEffect(() => {
    const ctl = new AbortController();
    const timer = setTimeout(async () => {
      setBusy(true);
      try {
        const body = {
          model_served: model,
          fit_id: fit.fit_id,
          candidates: [candidate(shapes, t)],
          ...(hasCosts(costs) ? { costs } : {}),
        };
        const r = await api<SimulateResult>(`/v1/admin/questions/${q(name)}/simulate`, { body, signal: ctl.signal });
        setMetrics(r.rows[0]?.metrics ?? null);
        setError(null);
      } catch (e) {
        if ((e as Error).name !== "AbortError") setError(e);
      } finally {
        if (!ctl.signal.aborted) setBusy(false);
      }
    }, 150);
    return () => {
      clearTimeout(timer);
      ctl.abort();
    };
  }, [name, model, fit.fit_id, shapes, t, costs]);

  const pair = shapes.length === 2 && sameScalar(shapes[0].scalar, shapes[1].scalar);
  const axis = shapes.every((r) => r.scalar === "p" || r.scalar === "top");
  // Where no rule fires on the shared axis: the escalation band, for the picture only.
  const lo = Math.max(0, ...shapes.map((r, i) => (r.op === "lt" ? t[i] : 0)));
  const hi = Math.min(1, ...shapes.map((r, i) => (r.op === "ge" ? t[i] : 1)));

  const move = (i: number, v: number) => {
    const next = [...t];
    next[i] = v;
    if (pair) {
      // Keep the "below" rule under the "from" rule so the band stays a band.
      const j = 1 - i;
      if (shapes[i].op === "lt" && v > next[j] - 0.01) next[i] = Math.max(0, next[j] - 0.01);
      if (shapes[i].op === "ge" && v < next[j] + 0.01) next[i] = Math.min(1, next[j] + 0.01);
    }
    setT(next);
  };

  const view = metrics && simView(metrics);
  const yes = question.type === "noul" ? ["yes", "no"] : ["top option wrong", "top option right"];

  return (
    <>
      <section className="card pad col" aria-labelledby="sim" style={{ gap: 16, padding: 20 }}>
        <div className="row" style={{ alignItems: "baseline" }}>
          <h2 id="sim" className="h grow" style={{ fontSize: 18 }}>Where should the uncertain middle go?</h2>
          {axis && (
            <>
              <span className="t2 small row" style={{ gap: 6 }}><span className="sw" style={{ background: "var(--ok)" }} />{yes[1]}</span>
              <span className="t2 small row" style={{ gap: 6 }}><span className="sw" style={{ background: "var(--bad)" }} />{yes[0]} (real outcome)</span>
              <span className="t2 small row" style={{ gap: 6 }}><span className="sw" style={{ border: "1px dashed var(--wa)" }} />escalation band</span>
            </>
          )}
        </div>
        {axis && <Histogram bins={fit.reliability.calibrated} lo={lo} hi={hi} noul={question.type === "noul"} scalar={scalarName(shapes[0].scalar)} />}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 16 }}>
          {shapes.map((r, i) => (
            <label key={r.id} className="col" style={{ gap: 4, fontSize: 13 }}>
              <span>
                {r.action} when {scalarName(r.scalar)} {r.op === "lt" ? "<" : "≥"} <span className="m">{prob(t[i])}</span>
              </span>
              <input type="range" min={0} max={1} step={0.01} value={t[i]} onChange={(e) => move(i, Number(e.target.value))} aria-label={`${r.action} threshold`} />
            </label>
          ))}
        </div>
        <p className="mu small" style={{ margin: 0 }}>
          Anything no rule takes is escalated. Numbers come from the server on fit <span className="m">{fit.fit_id.slice(0, 8)}…</span>'s evaluation split.
        </p>
      </section>

      <section aria-label="What these thresholds do" aria-busy={busy} aria-live="polite" className="col" style={{ gap: 12 }}>
        {error != null ? (
          <ErrorState error={error} />
        ) : !view ? (
          <Skeleton rows={1} />
        ) : (
          <>
            <div className="tiles">
              <Tile label="Decided automatically" value={view.automated} note={<>n = <span className="m">{view.n}</span> in the evaluation split</>} />
              <Tile label="Escalated" value={view.escalated} note="to a bigger model or a person" />
              <Tile label="Cost per 1,000 decisions" value={view.cost ?? "–"} note={view.cost ? "with the costs below" : "set costs below to see it"} />
            </div>
            <div className="tiles">
              {view.rules.map((r) => (
                <div key={r.id} className="card tile">
                  <span className="mu small">Wrong when <span className="m">{r.id}</span> fires</span>
                  <span className="v">{r.rate}</span>
                  <span className="m t2 small">95% interval {r.ci95}</span>
                  <span className="t2 small">
                    fired <span className="m">{r.fired}</span> · wrong <span className="m">{r.wrong}</span> · unlabelled <span className="m">{r.unlabeled}</span>
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
      </section>

      <section className="card pad row" aria-label="Costs" style={{ gap: 16 }}>
        <h2 className="h" style={{ fontSize: 16 }}>Costs</h2>
        <CostInput label="Each escalation" value={costs.escalation ?? 0} onChange={(v) => setCosts({ ...costs, escalation: v })} />
        {[...new Set(shapes.map((r) => r.action))].map((a) => (
          <CostInput key={a} label={`Each wrong ${a}`} value={costs.wrong?.[a] ?? 0} onChange={(v) => setCosts({ ...costs, wrong: { ...costs.wrong, [a]: v } })} />
        ))}
      </section>

      <Compile name={name} question={question} model={model} fit={fit} shapes={shapes} costs={costs} />
    </>
  );
}

function CostInput({ label, value, onChange }: { label: string; value: number; onChange: (v: number) => void }) {
  return (
    <label className="row" style={{ gap: 8, fontSize: 13 }}>
      {label} $
      <input className="in m" type="number" min={0} step="any" value={value} onChange={(e) => onChange(Math.max(0, Number(e.target.value) || 0))} style={{ width: 96 }} />
    </label>
  );
}

function Histogram({ bins, lo, hi, noul, scalar }: { bins: Bin[]; lo: number; hi: number; noul: boolean; scalar: string }) {
  const counts = bins.map((b) => b.count);
  const sorted = [...counts].sort((a, b) => b - a);
  // A tall first bin would flatten the rest: cap the axis and mark the broken bar.
  const cap = sorted[0] > 3 * (sorted[1] || 1) ? Math.max(1, Math.round(sorted[1] * 1.5)) : Math.max(1, sorted[0]);
  const bars = useMemo(
    () =>
      bins.map((b) => {
        const h = Math.max(b.count ? 2 : 0, (Math.min(b.count, cap) / cap) * 200);
        // observed is P(true) for noul and P(top right) otherwise; orange is "yes" / "wrong".
        const orange = b.observed == null ? null : noul ? b.observed : 1 - b.observed;
        const mid = (b.lo + b.hi) / 2;
        return { b, h, orange, inBand: mid >= lo && mid < hi };
      }),
    [bins, cap, lo, hi, noul],
  );
  return (
    <figure style={{ margin: 0 }} className="col">
      <div style={{ display: "flex", gap: 10 }}>
        <div className="m mu" style={{ display: "flex", flexDirection: "column", justifyContent: "space-between", height: 216, fontSize: 11, textAlign: "right", paddingBottom: 14 }}>
          <span>{count(cap)}{cap < sorted[0] ? "+" : ""}</span>
          <span>{count(Math.round(cap / 2))}</span>
          <span>0</span>
        </div>
        <div className="hist grow" role="img" aria-label={`Evaluation-split decisions by ${scalar}, coloured by real outcome, escalation band ${prob(lo)} to ${prob(hi)}`}>
          {bars.map(({ b, h, orange, inBand }) => (
            <div key={b.lo} className={inBand ? "in-band" : ""} title={`${prob(b.lo)}–${prob(b.hi)}: ${b.count} decisions`}>
              {b.count > cap && <span className="m t2" style={{ fontSize: 10, textAlign: "center" }}>{count(b.count)} ⌇</span>}
              <div className="seg" style={{ height: h, outline: inBand ? "1px dashed var(--wa)" : undefined, outlineOffset: 1 }}>
                {orange == null ? (
                  <div style={{ flex: "1 1 auto", background: "var(--bd2)" }} />
                ) : (
                  <>
                    <div style={{ height: `${orange * 100}%`, background: "var(--bad)" }} />
                    <div style={{ flex: "1 1 auto", background: "var(--ok)" }} />
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="m mu" style={{ display: "flex", justifyContent: "space-between", fontSize: 11, paddingLeft: 40 }}>
        <span>0.00</span><span>0.25</span><span>0.50</span><span>0.75</span><span>1.00</span>
      </div>
      <figcaption className="t2 small" style={{ paddingLeft: 40 }}>Calibrated {scalar}, the fit's bins · y: decisions per bin</figcaption>
    </figure>
  );
}

type CompileResult =
  | { policy_id: string; version: number }
  | { not_enough_labels: { message: string; needed?: number } }
  | { infeasible: { message: string; binding?: string; best?: Record<string, number> } };

function Compile({ name, question, model, fit, shapes, costs }: { name: string; question: Question; model: string; fit: Fit; shapes: Rule[]; costs: Costs }) {
  const saved = question.settings.target;
  const [kind, setKind] = useState<"risk" | "cost">(saved?.kind ?? "risk");
  const [metric, setMetric] = useState(saved?.metric ?? (shapes.some((r) => r.id === "accept") ? "false_pass" : "error_budget"));
  const [alpha, setAlpha] = useState(String(((saved?.alpha ?? 0.02) * 100).toFixed(2).replace(/\.?0+$/, "")));
  const [delta, setDelta] = useState(saved?.delta ?? 0.05);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CompileResult | null>(null);
  const [error, setError] = useState<unknown>(null);

  const propose = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    const target: Target =
      kind === "cost"
        ? { kind, costs }
        : { kind, metric, alpha: Number(alpha) / 100, delta, ...(saved?.max_escalation != null ? { max_escalation: saved.max_escalation } : {}) };
    try {
      const job = await waitJob(await api<Job>(`/v1/admin/questions/${q(name)}/compile`, { body: { model_served: model, fit_id: fit.fit_id, target } }));
      if (job.status === "failed") throw new ApiError(422, "The compile failed", { detail: job.error ?? undefined });
      setResult(job.result as CompileResult);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const alphaOk = kind === "cost" || (Number(alpha) >= 0.01 && Number(alpha) < 100);
  return (
    <section className="card pad col" aria-labelledby="tg" style={{ gap: 14 }}>
      <div className="row" style={{ gap: 16 }}>
        <h2 id="tg" className="h" style={{ fontSize: 16 }}>Target</h2>
        <select className="in" aria-label="Target kind" value={kind} onChange={(e) => setKind(e.target.value as "risk" | "cost")}>
          <option value="risk">An error rate, with confidence</option>
          <option value="cost">The lowest expected cost (no guarantee)</option>
        </select>
        {kind === "risk" && (
          <>
            <select className="in" aria-label="Which errors" value={metric} onChange={(e) => setMetric(e.target.value as "false_pass" | "error_budget")}>
              {shapes.some((r) => r.id === "accept") && <option value="false_pass">False passes (wrong among ACCEPT)</option>}
              <option value="error_budget">Every rule's errors</option>
            </select>
            <label className="row" style={{ gap: 8, fontSize: 13 }}>
              ≤ <input className="in m" type="number" min={0.01} max={99} step={0.1} value={alpha} onChange={(e) => setAlpha(e.target.value)} aria-label="Largest error rate, percent" style={{ width: 84 }} />%
            </label>
            <label className="row" style={{ gap: 8, fontSize: 13 }}>
              with
              <select className="in" aria-label="Confidence" value={delta} onChange={(e) => setDelta(Number(e.target.value))}>
                <option value={0.1}>90%</option><option value={0.05}>95%</option><option value={0.01}>99%</option>
              </select>
              confidence
            </label>
          </>
        )}
        <span className="grow" />
        <button className="btn p" type="button" onClick={propose} disabled={busy || !alphaOk}>
          {busy ? "Compiling…" : "Propose policy"}
        </button>
      </div>
      <p className="mu small" style={{ margin: 0 }}>
        The server's compiler picks the thresholds that meet the target on the tuning split and states the guarantee; the handles above only explore.
      </p>
      {busy && <span className="mu small" role="status">◷ Compiling on the worker</span>}
      {error != null && <ErrorState error={error} />}
      {result && "policy_id" in result && (
        <div className="guar okb" role="status">
          <span className="ok small">✓ Draft compiled</span>
          <span>
            <span className="m">{question.name ?? name} v{result.version}</span> for <span className="m">{model}</span>
          </span>
          <span className="m t2 small">{result.policy_id}</span>
          <span className="t2 small"><a href={href("questions", name, "policies")}>Open it in Policies</a> to see its guarantee and how it differs from the active version.</span>
        </div>
      )}
      {result && "not_enough_labels" in result && (
        <TooFew title={`Guarantee · ${question.name ?? name}`} need={result.not_enough_labels.needed}>
          {result.not_enough_labels.message}
        </TooFew>
      )}
      {result && "infeasible" in result && (
        <div className="guar wab" role="status">
          <span className="wa small">▲ Can't meet this target</span>
          <span>{result.infeasible.message}</span>
        </div>
      )}
    </section>
  );
}
