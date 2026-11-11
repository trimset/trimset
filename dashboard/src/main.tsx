import "@fontsource-variable/inter";
import "@fontsource-variable/space-grotesk";
import "@fontsource-variable/jetbrains-mono";
import "./styles.css";

import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { ApiError, api, getKey, type Project, setKey } from "./api.ts";
import { Alerts } from "./pages/Alerts.tsx";
import { Decisions } from "./pages/Decisions.tsx";
import { Drift } from "./pages/Drift.tsx";
import { Outcomes } from "./pages/Outcomes.tsx";
import { Overview } from "./pages/Overview.tsx";
import { Policies } from "./pages/Policies.tsx";
import { QuestionPage, Questions } from "./pages/Questions.tsx";
import { Review } from "./pages/Review.tsx";
import { Settings } from "./pages/Settings.tsx";
import { Shadow } from "./pages/Shadow.tsx";
import { States } from "./pages/States.tsx";
import { ErrorState, Icon, initTheme, Top, useLoad, useRoute } from "./ui.tsx";

const NAV: { label: string; items: [id: string, label: string][] }[] = [
  { label: "", items: [["overview", "Overview"]] },
  { label: "Capture", items: [["decisions", "Decisions"], ["outcomes", "Outcomes"]] },
  { label: "Analyze", items: [["questions", "Questions"], ["drift", "Drift"], ["shadow", "Shadow"]] },
  { label: "Act", items: [["policies", "Policies"], ["review", "Review queue"], ["alerts", "Alerts"]] },
  { label: "", items: [["settings", "Settings"]] },
];

function Logo() {
  return (
    <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
      <rect width="22" height="22" rx="6" fill="#b8f36b" />
      <path d="M7 6v8.5a2 2 0 0 0 2 2h5M5 9h9" stroke="#0b0d10" strokeWidth="2.2" fill="none" strokeLinecap="round" />
    </svg>
  );
}

function SignIn() {
  const [key, setValue] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api<Project>("/v1/admin/project", { key: key.trim() });
      setKey(key.trim());
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 16 }}>
      <form className="card pad col" style={{ width: "min(460px, 100%)", gap: 14, padding: 24 }} onSubmit={submit}>
        <div className="row">
          <Logo />
          <span className="h" style={{ fontSize: 19, fontWeight: 700 }}>trimset</span>
        </div>
        <h1 className="h" style={{ fontSize: 22 }}>Sign in with an admin key</h1>
        <p className="t2" style={{ margin: 0, fontSize: 13 }}>
          Paste a key that starts with <span className="m">tsa_</span>. Each admin key belongs to one project. It stays in
          this tab only and goes nowhere but this server.
        </p>
        <label className="f">
          Admin key
          <input className="in m" type="password" autoComplete="off" spellCheck={false} required value={key} onChange={(e) => setValue(e.target.value)} placeholder="tsa_…" />
        </label>
        {error != null && (
          <p className="cr" role="alert" style={{ margin: 0, fontSize: 13 }}>
            ✕ {error instanceof ApiError && error.status === 401 ? "That key isn't valid, or it was revoked." : error instanceof ApiError && error.status === 403 ? "That key can't administer a project: use an admin key." : (error as Error).message}
          </p>
        )}
        <button className="btn p" type="submit" disabled={busy || !key.trim()}>
          {busy ? "Checking…" : "Sign in"}
        </button>
        <p className="mu small" style={{ margin: 0 }}>
          No key yet? <span className="m">trimset-server create-key --scope admin</span> on the server.
        </p>
      </form>
    </main>
  );
}

function Page({ route }: { route: string[] }) {
  const [first = "overview", ...rest] = route;
  switch (first) {
    case "overview":
      return <Overview />;
    case "decisions":
      return <Decisions />;
    case "outcomes":
      return <Outcomes />;
    case "questions":
      return rest[0] ? <QuestionPage name={rest[0]} tab={rest[1] ?? "calibration"} /> : <Questions />;
    case "drift":
      return <Drift />;
    case "shadow":
      return <Shadow />;
    case "policies":
      return <Policies />;
    case "review":
      return <Review />;
    case "alerts":
      return <Alerts />;
    case "settings":
      return <Settings />;
    case "states":
      return <States />;
    default:
      return (
        <>
          <Top crumb="trimset" title="Not found" />
          <div className="body">
            <p>
              No page at <span className="m">#{route.join("/")}</span>. <a href="#/">Go to the overview</a>.
            </p>
          </div>
        </>
      );
  }
}

function Shell() {
  const route = useRoute();
  const [menu, setMenu] = useState(false);
  const project = useLoad((signal) => api<Project>("/v1/admin/project", { signal }), []);
  useEffect(() => {
    const open = () => setMenu(true);
    window.addEventListener("trimset:menu", open);
    return () => window.removeEventListener("trimset:menu", open);
  }, []);
  useEffect(() => setMenu(false), [route.join("/")]);
  const active = route[0] ?? "overview";

  return (
    <div className="app">
      <div className={`scrim${menu ? " open" : ""}`} onClick={() => setMenu(false)} />
      <nav className={`side${menu ? " open" : ""}`} aria-label="Main">
        <a className="brand" href="#/">
          <Logo />
          <span>trimset</span>
        </a>
        <button className="proj" type="button" aria-label={`Switch project, current: ${project.data?.name ?? "loading"}. Signs out of this key.`} onClick={() => setKey(null)}>
          <span className="col grow" style={{ gap: 0 }}>
            <span className="mu" style={{ fontSize: 11 }}>Project</span>
            <span className="m" style={{ fontSize: 13, overflow: "hidden", textOverflow: "ellipsis" }}>{project.data?.name ?? "…"}</span>
          </span>
          <Icon d="M5 7l4 4 4-4" size={16} />
        </button>
        {NAV.map((g, i) => (
          <div className="grp" key={i}>
            {g.label && <div className="grp-l">{g.label}</div>}
            {g.items.map(([id, label]) => (
              <a key={id} className="nav-a" href={`#/${id}`} aria-current={id === active || (id === "questions" && active === "questions") ? "page" : undefined}>
                {label}
              </a>
            ))}
          </div>
        ))}
        <div className="col mu small" style={{ marginTop: "auto", padding: 10, gap: 4 }}>
          <span className="m">v0.2 · self-hosted</span>
          <a href="https://github.com/trimset/trimset" style={{ color: "var(--t2)" }}>Docs and source</a>
          <a href="#/states" style={{ color: "var(--t2)" }}>States board</a>
        </div>
      </nav>
      <main className="main">
        {project.error && !(project.error instanceof ApiError && project.error.status === 401) ? (
          <div className="body">
            <ErrorState error={project.error} retry={project.reload} />
          </div>
        ) : (
          <Page route={route} />
        )}
      </main>
    </div>
  );
}

function App() {
  const [signedIn, setSignedIn] = useState(() => getKey() !== null);
  useEffect(() => {
    const on = () => setSignedIn(getKey() !== null);
    window.addEventListener("trimset:key", on);
    return () => window.removeEventListener("trimset:key", on);
  }, []);
  return signedIn ? <Shell /> : <SignIn />;
}

initTheme();
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
