import * as mock from "../mock.ts";
import { Load, SamplePill, Top, useLoad } from "../ui.tsx";

export function Drift() {
  return (
    <>
      <Top crumb="Analyze" title="Drift">
        <SamplePill endpoint={mock.ENDPOINT.drift} />
      </Top>
      <div className="body">
        <DriftBody bare />
      </div>
    </>
  );
}

type Series = { alert: number; values: number[]; significant: number[] };

function Line({ title, s, days, change, max, digits, caption }: { title: string; s: Series; days: string[]; change: number; max: number; digits: number; caption: string }) {
  const x = (i: number) => 60 + (i * 560) / (s.values.length - 1);
  const y = (v: number) => 200 - (Math.min(v, max) / max) * 180;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * max);
  const after = s.values.slice(change).map((v) => v.toFixed(digits)).join(" and ");
  return (
    <section className="card pad col" style={{ gap: 8 }}>
      <h2 className="h" style={{ fontSize: 16 }}>{title}</h2>
      <svg viewBox="0 0 640 240" role="img" aria-label={`${title}: near ${s.values[0].toFixed(digits)} before the model change, then ${after}, against an alert line at ${s.alert}`} style={{ width: "100%", height: "auto" }}>
        <g stroke="var(--dv)">{ticks.map((t) => <line key={t} x1="60" x2="620" y1={y(t)} y2={y(t)} />)}</g>
        <line x1="60" x2="620" y1={y(s.alert)} y2={y(s.alert)} stroke="var(--wa)" strokeDasharray="5 4" />
        <text x="616" y={y(s.alert) - 6} fill="var(--wa)" fontSize="11" textAnchor="end">alert {s.alert}</text>
        <line x1={x(change) - 20} x2={x(change) - 20} y1="20" y2="200" stroke="var(--t2)" strokeDasharray="2 3" />
        <text x={x(change) - 24} y="30" fill="var(--t2)" fontSize="11" textAnchor="end">▲ model changed</text>
        <polyline points={s.values.map((v, i) => `${x(i)},${y(v)}`).join(" ")} fill="none" stroke="var(--tx)" strokeWidth="1.8" />
        {s.values.map((v, i) => (
          <circle key={i} cx={x(i)} cy={y(v)} r="3.5" fill={s.significant.includes(i) ? "var(--wa)" : "var(--sf)"} stroke={s.significant.includes(i) ? "var(--wa)" : "var(--t2)"} strokeWidth="1.5" />
        ))}
        <g fill="var(--mu)" fontSize="11" fontFamily="JetBrains Mono Variable, monospace" textAnchor="end">
          {ticks.map((t) => <text key={t} x="52" y={y(t) + 4}>{t.toFixed(digits === 2 ? 2 : 3)}</text>)}
        </g>
        <g fill="var(--mu)" fontSize="11" fontFamily="JetBrains Mono Variable, monospace" textAnchor="middle">
          {[0, 4, 8, days.length - 1].map((i) => <text key={i} x={x(i)} y="220">{days[i]}</text>)}
        </g>
        <text x="340" y="236" fill="var(--t2)" fontSize="12" textAnchor="middle">{caption}</text>
      </svg>
    </section>
  );
}

export function DriftBody({ bare }: { bare?: boolean }) {
  const state = useLoad(() => mock.drift(), []);
  return (
    <Load state={state}>
      {(d) => (
        <>
          {!bare && (
            <div className="row">
              <SamplePill endpoint={mock.ENDPOINT.drift} />
              <span className="mu small">Drift checks arrive in M4: this tab shows the design's example for <span className="m">{d.question}</span>.</span>
            </div>
          )}
          <div role="status" className="card pad row">
            <span className="wa">▲ Model changed behind an alias</span>
            <span className="grow">
              <span className="m">{d.change.alias}</span> has answered as <span className="m">{d.change.to}</span> since {d.change.since} (was{" "}
              <span className="m">{d.change.from}</span>). Nobody changed the app.
            </span>
            <a className="btn" href="#/shadow">Compare the two</a>
          </div>
          <div className="split">
            <Line title="PSI of the top-option probability, daily" s={d.psi} days={d.days} change={d.change.index} max={0.4} digits={2} caption="Day (UTC) · reference: Oct 1–28 · filled dot: permutation test p < 0.01" />
            <Line title="Jensen-Shannon divergence of the option mix, daily" s={d.js} days={d.days} change={d.change.index} max={0.1} digits={3} caption="Day (UTC) · billing, shipping, returns, tech" />
          </div>
          <section className="card pad col" style={{ gap: 8 }}>
            <div className="row">
              <h2 className="h grow" style={{ fontSize: 16 }}>Top-option probability: reference against now</h2>
              <span className="t2 small row" style={{ gap: 6 }}><span className="sw" style={{ background: "var(--bd2)" }} />reference, Oct 1–28</span>
              <span className="t2 small row" style={{ gap: 6 }}><span className="sw" style={{ border: "2px solid var(--wa)" }} />last 24 hours</span>
            </div>
            <svg viewBox="0 0 640 230" role="img" aria-label="Before the change most answers were confident, above 0.8. In the last 24 hours the mass moved down: answers are less confident." style={{ width: "100%", height: "auto" }}>
              {d.hist.reference.map((v, i) => (
                <rect key={`r${i}`} x={60 + i * 56 + 4} width="48" y={190 - (v / 0.4) * 170} height={(v / 0.4) * 170} fill="var(--bd2)" />
              ))}
              {d.hist.now.map((v, i) => (
                <rect key={`n${i}`} x={60 + i * 56 + 10} width="36" y={190 - (v / 0.4) * 170} height={(v / 0.4) * 170} fill="none" stroke="var(--wa)" strokeWidth="2" />
              ))}
              <line x1="60" x2="620" y1="190" y2="190" stroke="var(--bd2)" />
              <g fill="var(--mu)" fontSize="11" fontFamily="JetBrains Mono Variable, monospace" textAnchor="end">
                {[0, 0.1, 0.2, 0.3].map((t) => <text key={t} x="52" y={194 - (t / 0.4) * 170}>{t * 100}%</text>)}
              </g>
              <g fill="var(--mu)" fontSize="11" fontFamily="JetBrains Mono Variable, monospace" textAnchor="middle">
                <text x="60" y="208">0.0</text><text x="340" y="208">0.5</text><text x="620" y="208">1.0</text>
              </g>
              <text x="340" y="226" fill="var(--t2)" fontSize="12" textAnchor="middle">Top-option probability · y: share of decisions</text>
            </svg>
          </section>
          <section aria-label="Health since the change" className="tiles">
            {d.panels.map((p) => (
              <div key={p.label} className="card tile">
                <span className="mu small">{p.label}</span>
                <span className="v" style={{ fontSize: 22 }}>{p.value}</span>
                <span className={`${p.tone} small`}>{p.note}</span>
              </div>
            ))}
          </section>
        </>
      )}
    </Load>
  );
}
