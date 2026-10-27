import { useFetch } from "../hooks";
import { Loading, PageHead, Pill, Problem } from "../ui";
import { useSession } from "../App";
import { fmtDate, ROLE } from "../words";

interface Run { score_run_id: string; as_of_date: string; scorer_type: string; model_version: string; config_hash: string;
  model_frozen_at: string; executed_at: string; scored: number; suggested: number }
interface U { user_id: string; username: string; display_name: string; role: string; employee_id: string | null; is_active: boolean; active_sessions: number }

export default function Admin() {
  const { user } = useSession();
  const runs = useFetch<{ items: Run[] }>("/runs");
  const users = useFetch<{ items: U[] }>(user.role === "admin" ? "/users" : null);
  return (
    <>
      <PageHead title={user.role === "admin" ? "Users and runs" : "Scoring runs"}
        lede="The logistic-regression run is the one HR works from. The scorecard run is kept as a second opinion." />
      <section className="panel">
        <h2>Scoring runs</h2>
        {runs.error && <Problem error={runs.error} />}
        {!runs.data && !runs.error && <Loading what="runs" />}
        {runs.data && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Run</th><th>Scorer</th><th>List date</th><th>Model frozen</th><th className="num">People scored</th><th className="num">Suggested</th><th>Config hash</th></tr></thead>
              <tbody>{runs.data.items.map((r) => (
                <tr key={r.score_run_id}>
                  <td>{r.score_run_id}</td>
                  <td>{r.scorer_type === "logistic_regression" ? "Model (logistic regression)" : "Points scorecard"}<div className="quiet small">{r.model_version}</div></td>
                  <td className="num">{fmtDate(r.as_of_date)}</td><td className="num">{fmtDate(r.model_frozen_at)}</td>
                  <td className="num">{r.scored.toLocaleString()}</td><td className="num">{r.suggested}</td>
                  <td className="small" title={r.config_hash}>{r.config_hash.slice(0, 12)}</td>
                </tr>))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      {user.role === "admin" && (
        <section className="panel">
          <h2>Users</h2>
          <p className="quiet">Employees are the subject of the data and do not sign in. Managers see only HR-approved cases in their own team.</p>
          {users.error && <Problem error={users.error} />}
          {users.data && (
            <div className="table-wrap">
              <table>
                <thead><tr><th>User</th><th>Role</th><th>Linked employee</th><th>Status</th><th className="num">Open sessions</th></tr></thead>
                <tbody>{users.data.items.map((u) => (
                  <tr key={u.user_id}>
                    <td><strong>{u.display_name}</strong><div className="quiet small">{u.username}, {u.user_id}</div></td>
                    <td>{ROLE[u.role]}</td><td>{u.employee_id ?? <span className="quiet">None</span>}</td>
                    <td>{u.is_active ? <Pill tone="ok">Active</Pill> : <Pill tone="muted">Disabled</Pill>}</td>
                    <td className="num">{u.active_sessions}</td>
                  </tr>))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </>
  );
}
