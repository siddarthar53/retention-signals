import { useFetch } from "../hooks";
import { MonthLine } from "../charts";
import { Loading, PageHead, Problem, Purpose, Stat } from "../ui";
import { FAMILY, fmtDate, months } from "../words";

interface D {
  employee_id: string; as_of: string;
  job: { role_title: string; job_family: string; level: number; team_name: string; hire_date: string; months_with_company: number;
    role_since: string; months_in_role: number; last_promotion: string | null };
  manager: { status: "assigned" | "self" | "vacant"; employee_id?: string; role_title?: string; text?: string };
  team_members: { employee_id: string; role_title: string }[];
  workload: { month: string; utilization_pct: number | null; after_hours_pct: number; oncall_days: number }[];
  earned_leave: { year: number; balance: number; accrued: number; taken: number; as_of: string; note: string };
  notice: string;
}

export default function MyData() {
  const { data, error } = useFetch<D>("/my-data");
  if (error) return <Problem error={error} />;
  if (!data) return <Loading what="your records" />;
  const { job, manager, earned_leave: el } = data;
  const util = data.workload.map((w) => ({ x: w.month.slice(2).replace("-", "/"), y: w.utilization_pct }));
  const recent = data.workload.slice(-3);
  const avg = (k: "after_hours_pct" | "oncall_days") => recent.length ? recent.reduce((a, w) => a + w[k], 0) / recent.length : 0;

  return (
    <>
      <PageHead title="My data" lede={`${job.role_title}, ${job.team_name}. Your records as of ${fmtDate(data.as_of)}.`} />
      <Purpose text={data.notice} />

      <section className="stats">
        <Stat value={months(job.months_with_company)} label="With the company" note={`Joined ${fmtDate(job.hire_date)}`} />
        <Stat value={months(job.months_in_role)} label="In current role" note={`Since ${fmtDate(job.role_since)}`} />
        <Stat value={`${el.balance} days`} label={`Earned leave left in ${el.year}`} note={`${el.accrued} accrued, ${el.taken} taken`} />
        <Stat value={job.last_promotion ? fmtDate(job.last_promotion) : "None yet"} label="Last promotion" />
      </section>

      <div className="two">
        <section className="panel">
          <h2>Workload, last 12 recorded months</h2>
          <MonthLine points={util} refValue={100} refLabel="Full capacity" unit="%" label="My monthly utilisation"
                     max={Math.max(150, ...util.map((u) => u.y ?? 0))} />
          <dl className="facts">
            <div><dt>After-hours share, last 3 months</dt><dd>{avg("after_hours_pct").toFixed(1)}%</dd></div>
            <div><dt>On-call days a month, last 3 months</dt><dd>{avg("oncall_days").toFixed(1)}</dd></div>
          </dl>
          <p className="quiet small">A month appears once it has been recorded, usually early the following month.</p>
        </section>
        <section className="panel">
          <h2>My role and team</h2>
          <dl className="facts">
            <div><dt>Role</dt><dd>{job.role_title}</dd></div>
            <div><dt>Job family</dt><dd>{FAMILY[job.job_family] ?? job.job_family}, level {job.level}</dd></div>
            <div><dt>Team</dt><dd>{job.team_name}</dd></div>
            <div><dt>Manager</dt><dd>{manager.status === "assigned" ? `${manager.employee_id}, ${manager.role_title}` : manager.text}</dd></div>
          </dl>
          <h3>Team members ({data.team_members.length})</h3>
          <ul className="members">
            {data.team_members.map((m) => <li key={m.employee_id}><span>{m.employee_id}</span><span className="quiet">{m.role_title}</span></li>)}
          </ul>
          <p className="quiet small">{el.note} Leave balance is as of {fmtDate(el.as_of)}.</p>
        </section>
      </div>
    </>
  );
}
