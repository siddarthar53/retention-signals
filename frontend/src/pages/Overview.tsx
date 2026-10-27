import { Link } from "react-router-dom";
import { useFetch } from "../hooks";
import { HBars } from "../charts";
import { Loading, PageHead, Problem, Stat } from "../ui";
import { FAMILY, fmtDate, REVIEW, STATUS } from "../words";
import { useSession } from "../App";

interface O {
  score_run_id: string; as_of_date: string; model_version: string; executed_at: string;
  bands: Record<string, number>; reviews: Record<string, number>; interventions: Record<string, number>;
  arms: Record<string, number>; families: { job_family: string; suggested: number; scored: number }[]; open_exceptions: number;
}

export default function Overview() {
  const { data, error } = useFetch<O>("/overview");
  const { user } = useSession();
  if (error) return <Problem error={error} />;
  if (!data) return <Loading what="the overview" />;
  const suggested = data.bands.suggested_checkin ?? 0;
  const scored = Object.values(data.bands).reduce((a, b) => a + b, 0);
  const pending = data.reviews.pending ?? 0;
  const done = data.interventions.completed ?? 0;
  return (
    <>
      <PageHead title={`Quarter ending ${fmtDate(data.as_of_date)}`}
        lede={<>Scoring run {data.score_run_id}, {data.model_version}. {scored.toLocaleString()} people were scored;
          the top 5% are suggested for a check-in and the next 10% are worth keeping an eye on.</>}>
        {user.role === "hr_partner" && pending > 0 && <Link className="button primary" to="/queue?status=pending&band=suggested_checkin">Review {pending} waiting</Link>}
      </PageHead>

      <section className="stats">
        <Stat value={suggested.toLocaleString()} label="Suggested check-ins" />
        <Stat value={pending.toLocaleString()} label="Waiting for HR review" />
        <Stat value={(data.reviews.approved ?? 0).toLocaleString()} label="Approved for a conversation" />
        <Stat value={done.toLocaleString()} label="Conversations completed" />
      </section>

      <div className="two">
        <section className="panel">
          <h2>Where the suggestions are</h2>
          <p className="quiet">Suggested check-ins as a share of the people scored in each job family.</p>
          <HBars rows={data.families.map((f) => ({ label: FAMILY[f.job_family] ?? f.job_family, value: f.suggested / f.scored,
                                                   note: `(${f.suggested} of ${f.scored})` }))}
                 fmt={(v) => `${(v * 100).toFixed(1)}%`} />
        </section>
        <section className="panel">
          <h2>Review and follow-up</h2>
          <dl className="facts">
            {Object.entries(data.reviews).sort().map(([k, v]) => (<div key={k}><dt>{REVIEW[k] ?? k}</dt><dd>{v}</dd></div>))}
          </dl>
          <h3>Conversations by status</h3>
          <dl className="facts">
            {Object.entries(data.interventions).sort().map(([k, v]) => (<div key={k}><dt>{STATUS[k] ?? k}</dt><dd>{v}</dd></div>))}
          </dl>
          <p className="quiet small">
            {data.arms.control ?? 0} approved cases were placed in the comparison group (about 1 in 5, decided by a fixed
            rule before any outreach), so the effect of conversations can be measured honestly.
          </p>
          <p className="small"><Link to="/quality">{data.open_exceptions.toLocaleString()} data issues and blocked actions are open</Link></p>
        </section>
      </div>
    </>
  );
}
