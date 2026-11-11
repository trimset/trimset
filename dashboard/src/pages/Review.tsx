import { useEffect, useState } from "react";
import { safeHref } from "../format.ts";
import * as mock from "../mock.ts";
import { HBars, Load, Lock, SamplePill, Top, useLoad } from "../ui.tsx";

const PRIORITY = {
  high: <span className="cr">✕ High</span>,
  normal: <span className="wa">▲ Normal</span>,
  audit: <span className="mu">○ Audit</span>,
};

export function Review() {
  const state = useLoad(() => mock.review(), []);
  return (
    <>
      <Top crumb="Act" title="Review queue">
        <SamplePill endpoint={mock.ENDPOINT.review} />
      </Top>
      <div className="body">
        <Load state={state}>{(r) => <Queue data={r} />}</Load>
      </div>
    </>
  );
}

function Queue({ data }: { data: Awaited<ReturnType<typeof mock.review>> }) {
  const [items, setItems] = useState(data.items);
  const [at, setAt] = useState(0);
  const [done, setDone] = useState(data.labelledToday);
  const [last, setLast] = useState<string | null>(null);
  const item = items[Math.min(at, items.length - 1)];

  const finish = (label: string | null) => {
    if (!item) return;
    setLast(label ? `Labelled ${item.ref} as ${label} (sample: not saved)` : `Skipped ${item.ref}`);
    if (label) setDone((n) => n + 1);
    setItems(items.filter((i) => i !== item));
  };

  // 1–9 label, S skips, J and K move; ignored while typing in a field.
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (e.metaKey || e.ctrlKey || e.altKey || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName) || t.isContentEditable || !item) return;
      const n = Number(e.key);
      if (n >= 1 && n <= item.labels.length) finish(item.labels[n - 1]);
      else if (e.key === "s" || e.key === "S") finish(null);
      else if (e.key === "j" || e.key === "J") setAt((i) => Math.min(items.length - 1, i + 1));
      else if (e.key === "k" || e.key === "K") setAt((i) => Math.max(0, i - 1));
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });

  const link = safeHref(item?.link);
  return (
    <>
      <div className="row">
        <span className="pill"><span className="m">{done}</span> labelled by you today</span>
        <span className="t2 small" role="status">{last}</span>
      </div>
      <div className="split review">
        <section className="card" aria-labelledby="ql" style={{ flex: "1 1 300px", maxWidth: 440 }}>
          <div className="sec-h">
            <h2 id="ql" className="h">{items.length ? `${items.length} waiting` : "Queue empty"}</h2>
            <span className="mu small">priority, then oldest</span>
          </div>
          <ul className="qlist">
            {items.map((i) => (
              <li key={i.item_id}>
                <button type="button" aria-current={i === item} onClick={() => setAt(items.indexOf(i))}>
                  <span className="row" style={{ gap: 10 }}>
                    {PRIORITY[i.priority]}
                    <span className="m grow">{i.question}</span>
                    <span className="m mu small">{i.waiting}</span>
                  </span>
                  <span className="t2 small">{i.summary}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
        {item ? (
          <section className="card pad col rv-pane" aria-labelledby="rp" style={{ gap: 16, flex: "999 1 420px", padding: 20 }}>
            <div className="row" style={{ gap: 10 }}>
              {PRIORITY[item.priority]}
              <span className="m mu small">waiting {item.waiting}</span>
            </div>
            <span className="m mu small">{item.question} · {item.ref} · {item.decided}</span>
            <h2 id="rp" className="h" style={{ fontSize: 20 }}>{item.wording}</h2>
            <HBars label="Model probabilities" items={item.probs} />
            <Lock />
            {link && <a className="btn" href={link} target="_blank" rel="noopener noreferrer">Open {item.ref} in your tool</a>}
            <div role="group" aria-label="Label" className="labels">
              {item.labels.map((l, n) => (
                <button key={l} className="btn" type="button" onClick={() => finish(l)}>
                  {l} <kbd className="wide-only">{n + 1}</kbd>
                </button>
              ))}
              <button className="btn" type="button" onClick={() => finish(null)}>
                Skip <kbd className="wide-only">S</kbd>
              </button>
            </div>
            <span className="mu small">
              Labels go in as human review and become the outcome. <span className="wide-only"><kbd>J</kbd> <kbd>K</kbd> move through the queue.</span>
            </span>
          </section>
        ) : (
          <section className="card pad rv-pane" style={{ flex: "999 1 420px" }}>
            <p className="t2" style={{ margin: 0 }}>Nothing waiting. Escalated and audit items land here.</p>
          </section>
        )}
      </div>
    </>
  );
}
