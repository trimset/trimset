import { useEffect, useState } from "react";
import { api, DEMO, q, type Question } from "../api.ts";
import { count } from "../format.ts";
import * as mock from "../mock.ts";
import { href, Load, SamplePill, Tabs, Top, useLoad } from "../ui.tsx";
import { Calibration } from "./Calibration.tsx";
import { DriftBody } from "./Drift.tsx";
import { PoliciesBody } from "./Policies.tsx";
import { Simulator } from "./Simulator.tsx";

export function Questions() {
  const state = useLoad(() => mock.questions(), []);
  const [name, setName] = useState("");
  return (
    <>
      <Top crumb="Analyze" title="Questions">
        <SamplePill endpoint={mock.ENDPOINT.questions} />
      </Top>
      <div className="body">
        <form
          className="card pad row"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) window.location.hash = href("questions", name.trim());
          }}
        >
          <label className="row grow" style={{ gap: 8 }}>
            <span style={{ fontSize: 13 }}>Open a question in this project by name or <span className="m">question_id</span></span>
            <input className="in m grow" value={name} onChange={(e) => setName(e.target.value)} placeholder="refund_fraud" style={{ minWidth: 180 }} />
          </label>
          <button className="btn p" type="submit" disabled={!name.trim()}>Open</button>
          <span className="mu small" style={{ flexBasis: "100%" }}>
            {DEMO && <>In the demo, <a href={href("questions", "refund_fraud")}>refund_fraud</a> has recorded data. </>}
            The list below is sample data until <span className="m">{mock.ENDPOINT.questions}</span> is built; opening a question reads your server.
          </span>
        </form>
        <p className="t2" style={{ margin: 0, maxWidth: 760 }}>
          A question is its exact wording, hashed into a <span className="m">question_id</span>. A name follows the newest wording; earlier
          wordings keep their own history.
        </p>
        <Load state={state}>
          {(rows) => (
            <section className="card" aria-label="Question groups">
              <div className="tw">
                <table className="t">
                  <thead>
                    <tr>
                      <th>Name and wording</th><th>Type</th><th>question_id</th><th>Models seen</th><th className="r">Decisions, 30 d</th><th>Policy</th><th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.name}>
                        <td className="wrap">
                          <a className="m" href={href("questions", r.name)}>{r.name}</a>
                          <div className="t2" style={{ fontSize: 13 }}>{r.wording}</div>
                          {r.note && <div className="mu small">{r.note}</div>}
                        </td>
                        <td className="t2">{r.type}</td>
                        <td className="m t2 small">{r.question_id}</td>
                        <td>
                          <div className="row" style={{ gap: 6 }}>
                            {r.models.map((m) => (
                              <span key={m.name} className={`pill m${m.isNew ? " wa" : ""}`}>{m.isNew ? `▲ ${m.name} · ${m.isNew}` : m.name}</span>
                            ))}
                          </div>
                        </td>
                        <td className="m r">{count(r.decisions30d)}</td>
                        <td className={r.policy === "draft" ? "mu" : "m"}>{r.policy === "draft" ? "draft" : `${r.policy} active`}</td>
                        <td className={`${r.health.tone} wrap`} style={{ minWidth: 0 }}>{r.health.text}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
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
      </div>
    </>
  );
}
