import { FormEvent, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError, CaseDetail, HrCase, Intervention, ManagerCase, Theme } from "../api";
import { useFetch } from "../hooks";
import { FactorBars, MonthLine } from "../charts";
import { Loading, Pill, Problem, Purpose } from "../ui";
import { useSession } from "../App";
import { ACTION_FOR, BAND, FAMILY, fmtDate, IV_TYPE, label, months, NEXT, REASON, REVIEW, STATUS } from "../words";

const TYPE_FOR_THEME: Record<string, string> = {
  career: "career_conversation", engagement: "checkin", workload: "workload_rebalance", settling_in: "checkin",
};

export default function CasePage() {
  const { scoreId } = useParams();
  const { data, error, reload } = useFetch<CaseDetail>(`/cases/${scoreId}`);
  if (error) return <><BackLink /><Problem error={error} /></>;
  if (!data) return <Loading what="the case" />;
  return data.view === "hr" ? <HrView c={data} reload={reload} /> : <ManagerView c={data} reload={reload} />;
}

function BackLink() {
  const { user } = useSession();
  return user.role === "manager" ? <Link className="back" to="/my-team">My team</Link> : <Link className="back" to="/queue">Review queue</Link>;
}

/* ------------------------------------------------------------------ HR */
function HrView({ c, reload }: { c: HrCase; reload: () => void }) {
  const latest = c.reviews[c.reviews.length - 1];
  const approved = latest?.decision === "approved";
  const locked = c.interventions.length > 0;
  const util = c.workload.map((w) => ({ x: w.month.slice(2).replace("-", "/"), y: w.utilization_pct, dim: w.after_as_of }));
  const surveys = c.surveys.map((s) => ({ x: s.wave, y: s.engagement_index }));
  const noSurvey = c.surveys.filter((s) => s.engagement_index == null).length;

  return (
    <>
      <BackLink />
      <header className="case-head">
        <div>
          <h1>{c.employee_id}</h1>
          <p className="lede">{c.context.role_title}, {c.context.team_name}. {FAMILY[c.context.job_family]}, level {c.context.level}.
            Joined {fmtDate(c.context.hire_date)}.</p>
        </div>
        <div className="case-badges">
          <Pill tone={c.band === "suggested_checkin" ? "signal" : c.band === "monitor" ? "watch" : "muted"}>{BAND[c.band]}</Pill>
          <span className="quiet">{c.rank.toLocaleString()} of {c.population.toLocaleString()} in the {fmtDate(c.as_of_date)} list</span>
        </div>
      </header>
      {!c.is_active_run && <div className="problem"><strong>This score is from an older or secondary run.</strong><span>Reviews and actions are only possible on the current run.</span></div>}
      {c.eligibility && <div className="problem"><strong>No conversation needed</strong><span>{c.eligibility[0].toUpperCase() + c.eligibility.slice(1)}.</span></div>}
      <Purpose text={c.purpose} />

      <div className="case-grid">
        <div className="case-main">
          <section className="panel">
            <h2>Why this person is on the list</h2>
            {c.themes.length ? (
              <ul className="themes">{c.themes.map((t) => <li key={t.theme}>{t.text}</li>)}</ul>
            ) : <p className="quiet">No single factor stands out; several small ones add up.</p>}
            <h3>How each signal compares with the average employee</h3>
            <FactorBars items={c.contributions} />
            <p className="quiet small">
              Bars are the model's log-odds contributions: an association in past data, not a cause and not a verdict about
              the person. Estimated chance of leaving within two quarters: {(c.risk_score * 100).toFixed(1)}%.
            </p>
          </section>

          <section className="panel">
            <h2>Workload, last 12 recorded months</h2>
            <MonthLine points={util} refValue={100} refLabel="Full capacity" unit="%" max={Math.max(150, ...util.map((u) => u.y ?? 0))}
                       label="Monthly utilisation percentage" />
            {c.workload.some((w) => w.after_as_of) && <p className="quiet small">Faded dots are months after the scoring date; the model did not use them.</p>}
          </section>

          <section className="panel">
            <h2>Engagement survey</h2>
            <MonthLine points={surveys} refValue={3} refLabel="Scale midpoint" unit=" of 5" max={5} label="Engagement index by survey wave" />
            {noSurvey > 0 && <p className="quiet small">{noSurvey} {noSurvey === 1 ? "wave has" : "waves have"} no usable answer. Missing answers are never treated as low scores.</p>}
          </section>

          <section className="panel">
            <h2>Career timeline</h2>
            <dl className="facts">
              <div><dt>Time with the company</dt><dd>{months(c.tenure?.tenure_months)}</dd></div>
              <div><dt>Time in current role</dt><dd>{months(c.tenure?.months_in_role)}</dd></div>
              <div><dt>Since last promotion</dt><dd>{months(c.tenure?.months_since_promotion)}</dd></div>
              <div><dt>People manager</dt><dd>{c.context.is_people_manager ? "Yes" : "No"}</dd></div>
            </dl>
          </section>

          {c.scorecard && (
            <section className="panel">
              <h2>Second opinion: points scorecard</h2>
              <p className="quiet">A simple rule-based scorecard ranks this person {c.scorecard.rank.toLocaleString()} ({BAND[c.scorecard.band].toLowerCase()}).</p>
              {c.scorecard.points.length ? (
                <ul className="points">{c.scorecard.points.map((p) => <li key={p.rule}><span>{p.rule}</span><span>+{p.points}</span></li>)}</ul>
              ) : <p className="quiet">No scorecard rule applies.</p>}
            </section>
          )}
          <p className="quiet small">Built from {c.source_rows} source records, all recorded on or before {fmtDate(c.as_of_date)}.</p>
        </div>

        <aside className="case-side">
          <section className="panel decide">
            <h2>HR review</h2>
            {latest && <p><Pill tone={approved ? "ok" : "muted"}>{REVIEW[latest.decision]}</Pill> <span className="quiet">by {latest.reviewer}, {fmtDate(latest.decided_at)}</span></p>}
            {latest && <p className="small">{label(REASON, latest.reason_code)}{latest.comment ? `: ${latest.comment}` : ""}</p>}
            {locked ? <p className="quiet small">The review is locked because a conversation has been started.</p>
              : c.is_active_run && !c.eligibility && <ReviewForm c={c} onDone={reload} again={!!latest} />}
          </section>
          {approved && (
            <ConversationPanel scoreId={c.score_id} interventions={c.interventions} themes={c.themes}
                               types={c.intervention_types} onDone={reload} canStart={!c.eligibility && c.is_active_run} />
          )}
          {c.reviews.length > 1 && (
            <section className="panel">
              <h3>Review history</h3>
              <ol className="history">{c.reviews.map((r) => <li key={r.review_id}>{REVIEW[r.decision]}, {label(REASON, r.reason_code)}. <span className="quiet">{r.reviewer}, {fmtDate(r.decided_at)}</span></li>)}</ol>
            </section>
          )}
        </aside>
      </div>
    </>
  );
}

function ReviewForm({ c, onDone, again }: { c: HrCase; onDone: () => void; again: boolean }) {
  const [decision, setDecision] = useState<"approved" | "dismissed" | "needs_info">("approved");
  const [reason, setReason] = useState(c.reason_codes.approved[0]);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [open, setOpen] = useState(!again);

  if (!open) return <button className="secondary" onClick={() => setOpen(true)}>Change the decision</button>;

  async function submit(e: FormEvent) {
    e.preventDefault(); setBusy(true); setErr(null);
    try {
      await api.post(`/cases/${c.score_id}/review`, { decision, reason_code: reason, comment: comment.trim() || null });
      onDone();
    } catch (x) { setErr(x as ApiError); } finally { setBusy(false); }
  }
  const verb = decision === "approved" ? "Approve and share with manager" : decision === "dismissed" ? "Dismiss" : "Ask for more information";
  return (
    <form onSubmit={submit} className="stack">
      <fieldset className="choice">
        <legend>Decision</legend>
        {(["approved", "dismissed", "needs_info"] as const).map((d) => (
          <label key={d} className={decision === d ? "on" : ""}>
            <input type="radio" name="decision" value={d} checked={decision === d}
                   onChange={() => { setDecision(d); setReason(c.reason_codes[d][0]); }} />
            {d === "approved" ? "Approve" : d === "dismissed" ? "Dismiss" : "Needs information"}
          </label>
        ))}
      </fieldset>
      <label>Reason
        <select value={reason} onChange={(e) => setReason(e.target.value)}>
          {c.reason_codes[decision].map((r) => <option key={r} value={r}>{label(REASON, r)}</option>)}
        </select>
      </label>
      <label>Note for the record <span className="quiet">(optional; avoid health or personal details)</span>
        <textarea value={comment} onChange={(e) => setComment(e.target.value)} maxLength={1000} rows={3} />
      </label>
      {err && <Problem error={err} />}
      <button className="primary" disabled={busy}>{busy ? "Saving…" : verb}</button>
      {decision === "approved" && <p className="quiet small">The manager will see the themes and a conversation guide, never the score.</p>}
    </form>
  );
}

/* ------------------------------------------------------------------ shared: conversations */
function ConversationPanel({ scoreId, interventions, themes, types, onDone, canStart }: {
  scoreId: string; interventions: Intervention[]; themes: Theme[]; types: string[]; onDone: () => void; canStart: boolean;
}) {
  const suggested = TYPE_FOR_THEME[themes[0]?.theme] ?? "checkin";
  const [type, setType] = useState(suggested);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [result, setResult] = useState<{ arm: string } | null>(null);

  async function start(e: FormEvent) {
    e.preventDefault(); setBusy(true); setErr(null);
    try {
      const r = await api.post<{ arm: string }>("/interventions", { score_id: scoreId, intervention_type: type });
      setResult(r); onDone();
    } catch (x) { setErr(x as ApiError); } finally { setBusy(false); }
  }

  return (
    <section className="panel decide">
      <h2>Conversation</h2>
      {result?.arm === "control" && (
        <p className="note">This case was placed in the comparison group. No outreach is recorded for it, so the effect of
          conversations can be measured fairly. You do not need to do anything.</p>
      )}
      {interventions.map((iv) => <IvCard key={iv.intervention_id} iv={iv} onDone={onDone} />)}
      {interventions.length === 0 && canStart && (
        <form onSubmit={start} className="stack">
          <label>Kind of conversation
            <select value={type} onChange={(e) => setType(e.target.value)}>
              {types.map((t) => <option key={t} value={t}>{IV_TYPE[t]}{t === suggested ? " (suggested)" : ""}</option>)}
            </select>
          </label>
          {err && <Problem error={err} />}
          <button className="primary" disabled={busy}>{busy ? "Starting…" : "Start conversation"}</button>
          <p className="quiet small">About 1 in 5 approved cases are set aside as a comparison group when started.</p>
        </form>
      )}
      {interventions.length === 0 && !canStart && <p className="quiet">No conversation can be started for this case.</p>}
    </section>
  );
}

function IvCard({ iv, onDone }: { iv: Intervention; onDone: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [note, setNote] = useState("");
  const next = iv.arm === "control" ? [] : NEXT[iv.status ?? ""] ?? [];

  async function move(to: string) {
    setBusy(to); setErr(null);
    try { await api.post(`/interventions/${iv.intervention_id}/status`, { to_status: to, note: note.trim() || null }); setNote(""); onDone(); }
    catch (x) { setErr(x as ApiError); } finally { setBusy(null); }
  }
  return (
    <div className="iv">
      <p><strong>{IV_TYPE[iv.type]}</strong> <Pill tone={iv.status === "completed" ? "ok" : iv.arm === "control" ? "muted" : "watch"}>
        {iv.arm === "control" ? "Comparison group" : STATUS[iv.status ?? ""]}</Pill></p>
      <ol className="history">
        {iv.events.map((e) => <li key={e.event_id}>{STATUS[e.to]} <span className="quiet">{fmtDate(e.at)}, {e.actor}</span>{e.note && <div className="small">{e.note}</div>}</li>)}
      </ol>
      {next.length > 0 && (
        <div className="stack">
          <label>Note <span className="quiet">(optional)</span>
            <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000} />
          </label>
          <div className="row">
            {next.map((s) => (
              <button key={s} className={s === next[0] ? "primary" : "secondary"} disabled={!!busy} onClick={() => move(s)}>
                {busy === s ? "Saving…" : ACTION_FOR[s]}
              </button>
            ))}
          </div>
          {err && <Problem error={err} />}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ manager */
function ManagerView({ c, reload }: { c: ManagerCase; reload: () => void }) {
  return (
    <>
      <BackLink />
      <header className="case-head">
        <div>
          <h1>{c.employee_id}</h1>
          <p className="lede">{c.context.role_title}. {months(c.tenure_months)} with the company, {months(c.months_in_role)} in this role.</p>
        </div>
        <div className="case-badges"><span className="quiet">Approved by {c.approved_by}</span></div>
      </header>
      <Purpose text={c.purpose} />
      <div className="case-grid">
        <div className="case-main">
          <section className="panel">
            <h2>What HR noticed</h2>
            {c.themes.length === 0 && <p className="quiet">A general check-in is suggested; no single theme stands out.</p>}
            {c.themes.map((t) => (
              <div key={t.theme} className="guide">
                <h3>{t.text}</h3>
                <ul>{t.guide.map((g) => <li key={g}>{g}</li>)}</ul>
              </div>
            ))}
          </section>
          <section className="panel">
            <h2>Before you meet</h2>
            <ul className="plain">
              <li>Keep it informal and private. Do not mention lists, scores or surveys.</li>
              <li>Ask, listen, and agree one thing you can change.</li>
              <li>Record the outcome here so HR can follow up. Do not record health or personal details.</li>
            </ul>
          </section>
        </div>
        <aside className="case-side">
          <ConversationPanel scoreId={c.score_id} interventions={c.interventions} themes={c.themes}
                             types={c.intervention_types} onDone={reload} canStart={!c.eligibility} />
        </aside>
      </div>
    </>
  );
}
