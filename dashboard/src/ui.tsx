import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type PolicyState } from "./api.ts";
import { prob } from "./format.ts";

// ------------------------------------------------------------------ routing

/** Hash routes: "#/questions/refund_fraud/simulator" → ["questions", "refund_fraud", "simulator"]. */
export function useRoute(): string[] {
  const read = () =>
    window.location.hash
      .replace(/^#\/?/, "")
      .split("?")[0]
      .split("/")
      .filter(Boolean)
      .map(decodeURIComponent);
  const [parts, setParts] = useState(read);
  useEffect(() => {
    const on = () => setParts(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return parts;
}

export const href = (...parts: string[]) => "#/" + parts.map(encodeURIComponent).join("/");

// ------------------------------------------------------------------ loading

export interface Loaded<T> {
  data?: T;
  error?: unknown;
  loading: boolean;
  reload: () => void;
}

export function useLoad<T>(fn: (signal: AbortSignal) => Promise<T>, deps: unknown[]): Loaded<T> {
  const [state, setState] = useState<{ data?: T; error?: unknown; loading: boolean }>({ loading: true });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const ctl = new AbortController();
    setState((s) => ({ data: s.data, loading: true }));
    fn(ctl.signal).then(
      (data) => !ctl.signal.aborted && setState({ data, loading: false }),
      (error) => !ctl.signal.aborted && setState({ error, loading: false }),
    );
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}

/** Renders the loading and error states, then the children with the data. */
export function Load<T>({ state, children, rows = 5 }: { state: Loaded<T>; children: (d: T) => ReactNode; rows?: number }) {
  if (state.error) return <ErrorState error={state.error} retry={state.reload} />;
  if (state.data === undefined) return <Skeleton rows={rows} />;
  return <>{children(state.data)}</>;
}

// ------------------------------------------------------------------ page frame

const THEME = "trimset.theme";

export function initTheme(): void {
  let t = "dark";
  try {
    t = localStorage.getItem(THEME) ?? "dark";
  } catch {
    // default
  }
  document.documentElement.dataset.theme = t;
}

function toggleTheme(): void {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(THEME, next);
  } catch {
    // per-session only
  }
}

/** Marks a screen whose endpoint isn't built yet. */
export function SamplePill({ endpoint }: { endpoint: string }) {
  return (
    <span className="pill mu" title={`Sample data from the design: ${endpoint} isn't built yet. Nothing here is saved.`}>
      Sample data
    </span>
  );
}

export function Top({ crumb, title, sub, children }: { crumb: ReactNode; title: ReactNode; sub?: ReactNode; children?: ReactNode }) {
  return (
    <header className="top">
      <button className="btn icon menu-b" type="button" aria-label="Open menu" onClick={() => window.dispatchEvent(new Event("trimset:menu"))}>
        <Icon d="M3 5h12M3 9h12M3 13h12" />
      </button>
      <div className="top-t">
        <span className="mu small">{crumb}</span>
        <h1 className="h" style={{ fontSize: 24 }}>{title}</h1>
        {sub && <span className="m mu small">{sub}</span>}
      </div>
      {children}
      <button className="btn icon" type="button" aria-label="Switch between light and dark theme" onClick={toggleTheme}>
        <Icon d="M9 2.5v1.5M9 14v1.5M2.5 9H4M14 9h1.5M4.4 4.4l1 1M12.6 12.6l1 1M4.4 13.6l1-1M12.6 5.4l1-1M9 6a3 3 0 1 0 0 6 3 3 0 0 0 0-6z" />
      </button>
    </header>
  );
}

export function Icon({ d, size = 18 }: { d: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 18 18" aria-hidden="true">
      <path d={d} stroke="currentColor" strokeWidth="1.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Tabs({ items }: { items: { label: string; to: string; current: boolean }[] }) {
  return (
    <nav className="tabs" aria-label="Question views">
      {items.map((t) => (
        <a key={t.to} href={t.to} aria-current={t.current ? "page" : undefined}>
          {t.label}
        </a>
      ))}
    </nav>
  );
}

export function Tile({ label, value, note, noteClass = "t2", to }: { label: string; value: ReactNode; note?: ReactNode; noteClass?: string; to?: string }) {
  const body = (
    <>
      <span className="mu small">{label}</span>
      <span className="v">{value}</span>
      {note && <span className={`${noteClass} small`}>{note}</span>}
    </>
  );
  return to ? <a className="card tile" href={to}>{body}</a> : <div className="card tile">{body}</div>;
}

// ------------------------------------------------------------------ badges: a mark and a word, never colour alone

export function ActionBadge({ action }: { action: string }) {
  if (action === "BLOCK") return <span className="b bad">⊘ BLOCK</span>;
  if (action === "ESCALATE") return <span className="b wa">↑ ESCALATE</span>;
  if (action.startsWith("SELECT")) return <span className="b">→ {action}</span>;
  return <span className="b t2">{action}</span>;
}

export function PolicyBadge({ state }: { state: PolicyState }) {
  switch (state) {
    case "draft":
      return <span className="b mu dashed">draft</span>;
    case "proposed":
      return <span className="b wa">◷ proposed</span>;
    case "approved":
      return <span className="b ok">✓ approved</span>;
    case "active":
      return <span className="b active">● active</span>;
    case "rejected":
      return <span className="b bad">✕ rejected</span>;
    default:
      return <span className="b mu">retired</span>;
  }
}

export type OutcomeState = "pending" | "right" | "wrong";

export function OutcomeMark({ state }: { state: OutcomeState }) {
  if (state === "right") return <span className="ok">✓ right</span>;
  if (state === "wrong") return <span className="bad">✕ wrong</span>;
  return <span className="mu">◷ pending</span>;
}

export type Severity = "critical" | "warning" | "info";

export function SeverityMark({ s }: { s: Severity }) {
  if (s === "critical") return <span className="cr">✕ Critical</span>;
  if (s === "warning") return <span className="wa">▲ Warning</span>;
  return <span className="mu">○ Info</span>;
}

// ------------------------------------------------------------------ states

export function Skeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="card pad col" aria-busy="true" aria-label="Loading" style={{ gap: 10 }}>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 10 }}>
        <div className="sk" style={{ height: 72 }} />
        <div className="sk" style={{ height: 72 }} />
        <div className="sk" style={{ height: 72 }} />
      </div>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="sk" style={{ height: 14, width: `${70 + ((i * 37) % 25)}%` }} />
      ))}
    </div>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const e = error instanceof ApiError ? error : null;
  const down = !e || e.status === 0 || e.status >= 500;
  return (
    <section className="card pad col errcard" role="alert" style={{ gap: 12 }}>
      <h2 className="h cr" style={{ fontSize: 18 }}>
        ✕ {down ? "The trimset server didn't answer" : e!.message}
      </h2>
      {down ? (
        <p style={{ margin: 0 }}>
          This page can't load right now. Your apps are not affected: the proxy keeps forwarding and holds decisions
          until the database is back.
        </p>
      ) : (
        e!.detail && <p style={{ margin: 0 }}>{e!.detail}</p>
      )}
      {e && e.status > 0 && (
        <pre>
          {e.status} · {e.type ?? "no problem type"}
          {e.errors.map((x) => `\n${x.pointer ?? ""} ${x.message ?? ""}`).join("")}
        </pre>
      )}
      {!e && error instanceof Error && <pre>{error.message}</pre>}
      <div className="row">
        {retry && (
          <button className="btn p" type="button" onClick={retry}>
            Try again
          </button>
        )}
        <a className="btn" href="https://github.com/trimset/trimset">
          Operator guide
        </a>
      </div>
    </section>
  );
}

/** "n = 43: too few to give a guarantee", wherever a guarantee would appear. */
export function TooFew({ title, n, need, children }: { title: string; n?: number; need?: number; children?: ReactNode }) {
  return (
    <div className="guar" role="status">
      <div className="row">
        <h3 className="h grow" style={{ fontSize: 15 }}>{title}</h3>
        <span className="mu small">◷ not yet</span>
      </div>
      {n != null ? (
        <p style={{ margin: 0, fontSize: 15 }}>
          <span className="m">n = {n.toLocaleString("en-US")}</span>: too few to give a guarantee
        </p>
      ) : (
        <p style={{ margin: 0, fontSize: 15 }}>Too few labels to give a guarantee</p>
      )}
      {children && <p className="t2 small" style={{ margin: 0 }}>{children}</p>}
      {n != null && need != null && need > 0 && (
        <div className="meter" role="img" aria-label={`${n} of ${need} labels`}>
          <div style={{ width: `${Math.min(100, (100 * n) / need)}%` }} />
        </div>
      )}
    </div>
  );
}

export function Pending({ labelled, title, children }: { labelled: number; title: ReactNode; children?: ReactNode }) {
  const l = Math.round(100 * labelled);
  return (
    <section className="card pad col" style={{ gap: 12 }}>
      <h2 className="h" style={{ fontSize: 18 }}>{title}</h2>
      {children && <p className="t2 small" style={{ margin: 0 }}>{children}</p>}
      <div className="stack" role="img" aria-label={`Labelled ${l}%, pending ${100 - l}%`}>
        <div style={{ flex: `${l} 1 0`, background: "var(--ok)" }} />
        <div className="hatch" style={{ flex: `${100 - l} 1 0` }} />
      </div>
      <div className="row t2 small">
        <span><span className="m">{l}%</span> labelled</span>
        <span>◷ <span className="m">{100 - l}%</span> pending</span>
      </div>
    </section>
  );
}

export function Lock({ children }: { children?: ReactNode }) {
  return (
    <div className="lock">
      <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
        <rect x="4" y="9" width="12" height="8" rx="2" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <path d="M7 9V6.5a3 3 0 0 1 6 0V9" fill="none" stroke="currentColor" strokeWidth="1.5" />
      </svg>
      <div className="col" style={{ gap: 2 }}>
        <span>State not stored: this project keeps hashes only.</span>
        {children}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ overlays

export function Drawer({ open, onClose, label, children }: { open: boolean; onClose: () => void; label: string; children: ReactNode }) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("button")?.focus();
    const key = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", key);
    return () => {
      window.removeEventListener("keydown", key);
      prev?.focus();
    };
  }, [open, onClose]);
  return (
    <aside ref={ref} className={`drawer${open ? " open" : ""}`} aria-label={label} aria-hidden={!open} inert={!open}>
      {children}
    </aside>
  );
}

/** A native modal dialog: Esc and the backdrop close it, focus stays inside. */
export function Confirm({
  open,
  title,
  children,
  confirm,
  danger,
  disabled,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirm: string;
  danger?: boolean;
  disabled?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} onClose={onClose} aria-labelledby="dlg-t">
      <div className="col" style={{ gap: 14 }}>
        <h2 id="dlg-t" className="h" style={{ fontSize: 20 }}>{title}</h2>
        <div className="col t2" style={{ gap: 8 }}>{children}</div>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button className="btn" type="button" onClick={onClose}>Cancel</button>
          <button className={`btn${danger ? "" : " p"}`} type="button" onClick={onConfirm} disabled={disabled} style={danger ? { borderColor: "var(--cr)", color: "var(--cr)" } : undefined}>
            {confirm}
          </button>
        </div>
      </div>
    </dialog>
  );
}

// ------------------------------------------------------------------ small charts

export function Sparkline({ points, tone = "var(--t2)" }: { points: number[]; tone?: string }) {
  const max = Math.max(...points),
    min = Math.min(...points);
  const pts = points.map((v, i) => `${(i * 96) / (points.length - 1)},${22 - ((v - min) / (max - min || 1)) * 20}`).join(" ");
  return (
    <svg width="96" height="24" viewBox="0 0 96 24" aria-hidden="true">
      <polyline points={pts} fill="none" stroke={tone} strokeWidth="1.5" />
    </svg>
  );
}

/** A probability with the policy's escalation band, 0 to 1. */
export function ProbBar({ p, lo, hi, label }: { p: number; lo: number; hi: number; label: string }) {
  return (
    <figure className="col" style={{ margin: 0, gap: 8 }} role="img" aria-label={`${label} ${prob(p)}, escalation band ${prob(lo)} to ${prob(hi)}`}>
      <figcaption className="mu small">{label}, with the policy's band</figcaption>
      <div className="probbar">
        <div className="band" style={{ left: `${lo * 100}%`, width: `${(hi - lo) * 100}%` }} />
        <div className="bar" style={{ width: `${p * 100}%` }} />
        <div className="m small" style={{ position: "absolute", left: `min(calc(${p * 100}% + 6px), calc(100% - 40px))`, top: 7 }}>
          {prob(p)}
        </div>
      </div>
      <div className="m mu" style={{ display: "flex", justifyContent: "space-between", fontSize: 11 }}>
        <span>0.00</span>
        <span>{prob(lo)}</span>
        <span>{prob(hi)}</span>
        <span>1.00</span>
      </div>
    </figure>
  );
}

export function HBars({ items, label }: { items: [string, number][]; label: string }) {
  return (
    <div className="hbars" role="img" aria-label={`${label}: ${items.map(([k, v]) => `${k} ${prob(v)}`).join(", ")}`}>
      {items.map(([k, v]) => (
        <span key={k} style={{ display: "contents" }}>
          <span className="m">{k}</span>
          <div className="track">
            <div style={{ width: `${v * 100}%` }} />
          </div>
          <span className="m r">{prob(v)}</span>
        </span>
      ))}
    </div>
  );
}
