import { useEffect, useState } from "react";
import { ApiError, api, DEMO, type PolicyView, q, type Question } from "../api.ts";
import { day } from "../format.ts";
import { href, Load, Tabs, Top, useLoad } from "../ui.tsx";
import { Calibration } from "./Calibration.tsx";
import { DriftBody } from "./Drift.tsx";
import { PoliciesBody } from "./Policies.tsx";
import { Simulator } from "./Simulator.tsx";

/** "choice · 4 options", "score · 4 levels", "noul". */
function kind(question: Question): string {
  const d = question.definition;
  if (d.options) return `choice · ${d.options.length} options`;
  if (d.levels) return `score · ${d.levels.length} levels`;
  return question.type;
}

export function Questions() {
  const state = useLoad(
    (signal) =>
      Promise.all([
        api<{ items: Question[] }>("/v1/admin/questions?limit=500", { signal }),
        api<{ items: PolicyView[] }>("/v1/admin/policies?limit=1000", { signal }),
      ]),
    [],
  );
  const [find, setFind] = useState("");
  return (
    <>
      <Top crumb="Analyze" title="Questions">
        <input className="in" type="search" placeholder="Name, wording or question_id" aria-label="Find a question" value={find} onChange={(e) => setFind(e.target.value)} style={{ width: 260, maxWidth: "100%" }} />
      </Top>
      <div className="body">
        <p className="t2" style={{ margin: 0, maxWidth: 760 }}>
          A question is its exact wording, hashed into a <span className="m">question_id</span>. A name follows the newest wording; earlier
          wordings keep their own history.
        </p>
        <Load state={state}>
          {([{ items }, { items: policies }]) => {
            const f = find.trim().toLowerCase();
            const rows = items.filter((r) => !f || [r.name ?? "", wording(r), r.question_id].some((x) => x.toLowerCase().includes(f)));
            const earlier = new Set(items.map((r) => r.previous_question_id).filter(Boolean));
            return !items.length ? (
              <section className="card pad">
                <p className="t2" style={{ margin: 0 }}>No questions yet. A question appears the first time a decision asks it.</p>
              </section>
            ) : (
              <section className="card" aria-label="Questions">
                <div className="tw">
                  <table className="t">
                    <thead>
                      <tr>
                        <th>Name and wording</th><th>Type</th><th>question_id</th><th>Models served</th><th>Active policy</th><th>First seen</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((r) => {
                        const active = policies.filter((p) => p.state === "active" && p.policy.question_id === r.question_id);
                        const drafts = policies.filter((p) => p.state !== "active" && p.policy.question_id === r.question_id).length;
                        return (
                          <tr key={r.question_id}>
                            <td className="wrap">
                              <a className="m" href={href("questions", r.name && !earlier.has(r.question_id) ? r.name : r.question_id)}>
                                {r.name ?? "(no name)"}
                              </a>
                              <div className="t2" style={{ fontSize: 13 }}>{wording(r)}</div>
                              {earlier.has(r.question_id) && <div className="mu small">an earlier wording</div>}
                            </td>
                            <td className="t2">{kind(r)}</td>
                            <td className="m t2 small" title={r.question_id}>{r.question_id.slice(0, 14)}…</td>
                            <td>
                              <div className="row" style={{ gap: 6 }}>
                                {servedModels(r).map((m) => <span key={m} className="pill m">{m}</span>)}
                                {!r.models.length && <span className="mu">none yet</span>}
                              </div>
                            </td>
                            <td>
                              {active.length ? (
                                active.map((p) => <div key={p.policy.policy_id} className="m">v{p.policy.version} · {p.policy.model_served}</div>)
                              ) : (
                                <span className="mu">none{drafts ? ` · ${drafts} other version${drafts > 1 ? "s" : ""}` : ""}</span>
                              )}
                            </td>
                            <td className="m t2">{day(r.first_seen)}</td>
                          </tr>
                        );
                      })}
                      {!rows.length && <tr><td colSpan={6} className="mu">No question matches “{find}”.</td></tr>}
                    </tbody>
                  </table>
                </div>
              </section>
            );
          }}
        </Load>
      </div>
    </>
  );
}

export function wording(question: Question): string {
  const i = question.definition.instructions;
  if (typeof i === "string") return i;
  if (i && typeof i === "object" && "$name" in i) return String((i as { $name: unknown }).$name);
  return JSON.stringify(i);
}

/** The newest served model first: that's usually the one to calibrate. */
export const servedModels = (question: Question): string[] =>
  [...question.models]
    .sort((a, b) => b.first_seen.localeCompare(a.first_seen))
    .map((m) => m.model_served)
    .filter((m, i, all) => all.indexOf(m) === i);

const TABS = [
  ["calibration", "Calibration"],
  ["simulator", "Thresholds and escalation"],
  ["drift", "Drift"],
  ["policies", "Policies"],
] as const;

export function QuestionPage({ name, tab }: { name: string; tab: string }) {
  const state = useLoad((signal) => api<Question>(`/v1/admin/questions/${q(name)}`, { signal }), [name]);
  const [model, setModel] = useState("");
  useEffect(() => {
    if (state.data && !servedModels(state.data).includes(model)) setModel(servedModels(state.data)[0] ?? "");
  }, [state.data, model]);
  const question = state.data;

  return (
    <>
      <Top
        crumb={<><a href="#/questions">Questions</a> / {question?.name ?? name}</>}
        title={question ? wording(question) : name}
        sub={question && `${question.type} · ${question.question_id}`}
      >
        {question && servedModels(question).length > 0 && (tab === "calibration" || tab === "simulator") && (
          <label className="row" style={{ gap: 8 }}>
            <span className="mu small">Model served</span>
            <select className="in m" value={model} onChange={(e) => setModel(e.target.value)}>
              {servedModels(question).map((m) => <option key={m}>{m}</option>)}
            </select>
          </label>
        )}
      </Top>
      <div className="body">
        <Tabs items={TABS.map(([id, label]) => ({ label, to: href("questions", name, id), current: id === tab }))} />
        {DEMO && state.error instanceof ApiError && state.error.status === 404 ? (
          <section className="card pad col" style={{ gap: 10 }}>
            <h2 className="h" style={{ fontSize: 18 }}>
              <span className="m">{name}</span> is a design sample in the demo
            </h2>
            <p className="t2" style={{ margin: 0 }}>
              The demo's real numbers come from one question, recorded from a trimset server running the refund fraud example. The
              other questions on the Overview and Questions screens show the design's sample data.
            </p>
            <div className="row">
              <a className="btn p" href={href("questions", "refund_fraud", tab)}>Open refund_fraud</a>
            </div>
          </section>
        ) : (
        <Load state={state}>
          {(question) =>
            !model && (tab === "calibration" || tab === "simulator") ? (
              <p className="t2">No model has answered this question yet, so there is nothing to calibrate.</p>
            ) : tab === "simulator" ? (
              <Simulator name={name} question={question} model={model} />
            ) : tab === "drift" ? (
              <DriftBody />
            ) : tab === "policies" ? (
              <PoliciesBody question={name} />
            ) : (
              <Calibration name={name} question={question} model={model} />
            )
          }
        </Load>
        )}
      </div>
    </>
  );
}
