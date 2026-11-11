import { useEffect, useState } from "react";
import { api, type Key, type Project, type ProjectSettings } from "../api.ts";
import { ago, day } from "../format.ts";
import { Confirm, ErrorState, Load, Top, useLoad } from "../ui.tsx";

const SCOPES: [Key["scope"], string][] = [
  ["ingest", "ingest: log decisions and outcomes"],
  ["proxy", "proxy: call the model through trimset"],
  ["review", "review: label the queue"],
  ["admin", "admin: everything"],
];

export function Settings() {
  const project = useLoad((signal) => api<Project>("/v1/admin/project", { signal }), []);
  return (
    <>
      <Top crumb={project.data?.name ?? "Project"} title="Settings" />
      <div className="body" style={{ maxWidth: 1120 }}>
        <Keys />
        <Load state={project} rows={6}>
          {(p) => <ProjectForm project={p} onSaved={project.reload} />}
        </Load>
      </div>
    </>
  );
}

function Keys() {
  const keys = useLoad((signal) => api<{ items: Key[] }>("/v1/admin/keys", { signal }), []);
  const [scope, setScope] = useState<Key["scope"]>("ingest");
  const [label, setLabel] = useState("");
  const [secret, setSecret] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [revoke, setRevoke] = useState<Key | null>(null);
  const [error, setError] = useState<unknown>(null);

  const create = async (e: { preventDefault: () => void }) => {
    e.preventDefault();
    setError(null);
    try {
      const k = await api<Key & { secret: string }>("/v1/admin/keys", { body: { scope, label: label.trim() } });
      setSecret(k.secret);
      setCopied(false);
      setLabel("");
      keys.reload();
    } catch (err) {
      setError(err);
    }
  };
  const doRevoke = async () => {
    if (!revoke) return;
    setError(null);
    try {
      await api(`/v1/admin/keys/${revoke.key_id}`, { method: "DELETE" });
      keys.reload();
    } catch (err) {
      setError(err);
    }
    setRevoke(null);
  };

  return (
    <section className="card" aria-labelledby="ks">
      <form className="sec-h" onSubmit={create}>
        <h2 id="ks" className="h">API keys</h2>
        <select className="in" aria-label="Scope of the new key" value={scope} onChange={(e) => setScope(e.target.value as Key["scope"])}>
          {SCOPES.map(([s, l]) => <option key={s} value={s}>{l}</option>)}
        </select>
        <input className="in" aria-label="Who or what uses it" placeholder="Who or what uses it" required maxLength={100} value={label} onChange={(e) => setLabel(e.target.value)} />
        <button className="btn p" type="submit" disabled={!label.trim()}>Create key</button>
      </form>
      {secret && (
        <div className="col" style={{ padding: "4px 18px 12px", gap: 8 }}>
          <span className="wa small">▲ Copy it now: this is the only time the key is shown. trimset keeps a hash.</span>
          <div className="row">
            <code className="m" style={{ overflowWrap: "anywhere", background: "var(--bg)", border: "1px solid var(--bd)", borderRadius: 10, padding: "8px 12px" }}>{secret}</code>
            <button
              className="btn"
              type="button"
              onClick={() => navigator.clipboard?.writeText(secret).then(() => setCopied(true), () => setCopied(false))}
            >
              {copied ? "✓ Copied" : "Copy"}
            </button>
            <button className="btn" type="button" onClick={() => setSecret(null)}>Done</button>
          </div>
        </div>
      )}
      {error != null && <div style={{ padding: "0 18px 12px" }}><ErrorState error={error} /></div>}
      <Load state={keys} rows={3}>
        {({ items }) => (
          <div className="tw">
            <table className="t">
              <thead><tr><th>Label</th><th>Scope</th><th>Key</th><th>Created</th><th>Last used</th><th className="r"><span className="sr">Action</span></th></tr></thead>
              <tbody>
                {items.map((k) => (
                  <tr key={k.key_id} className={k.revoked_at ? "mu" : undefined}>
                    <td className={k.revoked_at ? "mu" : undefined}>{k.label}</td>
                    <td className="m">{k.scope}</td>
                    <td className="m t2">{k.prefix}…</td>
                    <td className="m t2">{day(k.created_at)}</td>
                    <td className={k.revoked_at ? "mu" : "m t2"}>{k.revoked_at ? `revoked ${day(k.revoked_at)}` : k.last_used_at ? ago(k.last_used_at) : "never"}</td>
                    <td className="r">{!k.revoked_at && <button className="btn" type="button" onClick={() => setRevoke(k)}>Revoke</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Load>
      <p className="mu small" style={{ margin: 0, padding: "10px 18px 14px" }}>A revoked key is refused everywhere within 60 seconds.</p>
      <Confirm open={revoke !== null} title={`Revoke “${revoke?.label}”?`} confirm="Revoke key" danger onConfirm={doRevoke} onClose={() => setRevoke(null)}>
        <span>
          The <span className="m">{revoke?.scope}</span> key <span className="m">{revoke?.prefix}…</span> stops working everywhere within 60 seconds. Anything still
          using it gets 401. This can't be undone; create a new key instead.
        </span>
      </Confirm>
    </section>
  );
}

function ProjectForm({ project, onSaved }: { project: Project; onSaved: () => void }) {
  const initial = () => ({
    raw: project.settings.privacy?.store_raw_state ?? false,
    rawDays: String(project.settings.privacy?.raw_state_retention_days ?? 30),
    tags: project.settings.privacy?.tag_allowlist?.join(", ") ?? "",
    retention: String(project.settings.retention_days ?? 400),
    upstream: project.settings.upstream?.name ?? "",
    credentials: project.settings.upstream?.credentials ?? "passthrough",
    timeout: String(project.settings.upstream?.timeout_ms ?? 10000),
  });
  const [f, setF] = useState(initial);
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState(false);
  const [confirmRaw, setConfirmRaw] = useState(false);
  useEffect(() => setF(initial()), [project]); // eslint-disable-line react-hooks/exhaustive-deps

  const upstreams = project.available?.upstreams ?? [];
  const chosen = upstreams.find((u) => u.name === f.upstream);
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => (setSaved(false), setF({ ...f, [k]: e.target.value }));

  const save = async () => {
    setError(null);
    const patch: ProjectSettings = {
      retention_days: Number(f.retention),
      privacy: {
        store_raw_state: f.raw,
        raw_state_retention_days: Number(f.rawDays),
        tag_allowlist: f.tags.trim() ? f.tags.split(",").map((t) => t.trim()).filter(Boolean) : null,
      },
      ...(f.upstream
        ? { upstream: { name: f.upstream, credentials: f.credentials as "passthrough" | "server", timeout_ms: Number(f.timeout) } }
        : {}),
    };
    try {
      await api<Project>("/v1/admin/project", { method: "PATCH", body: patch, merge: true });
      setSaved(true);
      onSaved();
    } catch (e) {
      setError(e);
    }
  };
  const submit = () => (f.raw && !project.settings.privacy?.store_raw_state ? setConfirmRaw(true) : save());

  return (
    <>
      <section className="card" aria-labelledby="pv">
        <div className="sec-h"><h2 id="pv" className="h">Privacy</h2></div>
        <div className="row pad" style={{ borderTop: "1px solid var(--dv)", alignItems: "flex-start" }}>
          <div className="col grow" style={{ flexBasis: 320 }}>
            <span id="rs">Store raw state</span>
            <span className="t2 small">
              {f.raw ? "On: trimset stores each state for a limited time." : "Off: trimset keeps a keyed hash of each state, never the state itself."} Reviewers open items in
              your own tool through each question's link template.
            </span>
            <span className="wa small">▲ Turning it on stores customer messages and tickets in trimset's database. Check with your privacy team first.</span>
          </div>
          <button type="button" role="switch" className="switch" aria-checked={f.raw} aria-labelledby="rs" onClick={() => (setSaved(false), setF({ ...f, raw: !f.raw }))}>
            <span />
          </button>
        </div>
        <div className="row pad" style={{ borderTop: "1px solid var(--dv)" }}>
          <label className="f" style={{ flex: "1 1 200px" }}>
            Keep raw state for (days)
            <input className="in m" type="number" min={1} max={400} value={f.rawDays} onChange={set("rawDays")} disabled={!f.raw} />
          </label>
          <label className="f" style={{ flex: "2 1 300px" }}>
            Allowed tag keys <span className="mu small">(empty: all)</span>
            <input className="in m" value={f.tags} onChange={set("tags")} placeholder="channel, region, amount" />
          </label>
        </div>
      </section>
      <section className="card" aria-labelledby="rt">
        <div className="sec-h"><h2 id="rt" className="h">Data retention</h2></div>
        <div className="row pad" style={{ borderTop: "1px solid var(--dv)" }}>
          <label className="row" style={{ gap: 8 }}>
            Keep decisions and outcomes for
            <input className="in m" type="number" min={1} max={3650} value={f.retention} onChange={set("retention")} aria-label="Days" style={{ width: 96 }} />
            days
          </label>
          <span className="t2 small grow">Refits read the labelled history inside this window. Older months are dropped whole.</span>
        </div>
      </section>
      <section className="card" aria-labelledby="up">
        <div className="sec-h"><h2 id="up" className="h">Model upstream for the proxy</h2></div>
        <p className="t2 small" style={{ margin: 0, padding: "0 18px 12px" }}>
          The server's operator defines upstreams and their keys; a project picks one. No URL or secret passes through this page.
        </p>
        <div className="row pad" style={{ borderTop: "1px solid var(--dv)" }}>
          <label className="f" style={{ flex: "2 1 260px" }}>
            Upstream
            <select className="in" value={f.upstream} onChange={set("upstream")}>
              <option value="">None</option>
              {upstreams.map((u) => (
                <option key={u.name} value={u.name}>{u.name} · {u.format} · {u.server_key ? "server key set" : "no server key"}</option>
              ))}
            </select>
          </label>
          <label className="f" style={{ flex: "2 1 220px" }}>
            Credentials
            <select className="in" value={f.credentials} onChange={set("credentials")} disabled={!f.upstream}>
              <option value="passthrough">Pass the app's own key through</option>
              <option value="server" disabled={!chosen?.server_key}>Use the operator's key</option>
            </select>
          </label>
          <label className="f" style={{ flex: "1 1 140px" }}>
            Timeout (ms)
            <input className="in m" type="number" min={100} max={120000} value={f.timeout} onChange={set("timeout")} disabled={!f.upstream} />
          </label>
        </div>
        {!upstreams.length && <p className="mu small" style={{ margin: 0, padding: "0 18px 14px" }}>The operator hasn't defined an upstream for this project (TRIMSET_UPSTREAM_&lt;NAME&gt;_*).</p>}
        <p className="mu small" style={{ margin: 0, padding: "0 18px 14px" }}>A shadow model is set per question, in the question's settings.</p>
      </section>
      {error != null && <ErrorState error={error} />}
      <div className="row" style={{ justifyContent: "flex-end" }}>
        {saved && <span className="ok small" role="status">✓ Saved</span>}
        <button className="btn" type="button" onClick={() => (setF(initial()), setError(null))}>Discard</button>
        <button className="btn p" type="button" onClick={submit}>Save settings</button>
      </div>
      <Confirm open={confirmRaw} title="Start storing raw state?" confirm="Store raw state" onConfirm={() => (setConfirmRaw(false), save())} onClose={() => setConfirmRaw(false)}>
        <span>From now on, trimset stores the state of each new decision (customer messages, tickets, any JSON your app sends) for {f.rawDays} days.</span>
        <span>Every read of a stored state is audited. Turning it off again stops new storage; stored states expire on schedule.</span>
      </Confirm>
    </>
  );
}
