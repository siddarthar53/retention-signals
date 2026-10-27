import { Link, useSearchParams } from "react-router-dom";
import { useFetch } from "../hooks";
import { Loading, PageHead, Pill, Problem } from "../ui";
import { fmtDate, IV_TYPE, STATUS } from "../words";

interface Iv { intervention_id: string; employee_id: string; score_id: string; type: string; arm: string; source: string;
  assigned_at: string; owner: string; status: string; updated_at: string }

export default function Interventions() {
  const [sp, setSp] = useSearchParams();
  const status = sp.get("status") ?? "";
  const { data, error } = useFetch<{ items: Iv[] }>(`/interventions${status ? `?status=${status}` : ""}`);
  return (
    <>
      <PageHead title="Conversations" lede="Every conversation started from an approved case, newest first. Open one to record progress." />
      <div className="filters">
        <label>Status
          <select value={status} onChange={(e) => setSp(e.target.value ? { status: e.target.value } : {})}>
            <option value="">Any status</option>
            {Object.entries(STATUS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
      </div>
      {error && <Problem error={error} />}
      {!data && !error && <Loading what="conversations" />}
      {data && (data.items.length === 0 ? (
        <div className="empty"><p>No conversations{status ? " with this status" : " yet"}.</p>
          <p className="quiet">Conversations start from an approved case.</p></div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Employee</th><th>Kind</th><th>Status</th><th>Owner</th><th>Started</th><th>Last update</th></tr></thead>
            <tbody>
              {data.items.map((i) => (
                <tr key={i.intervention_id}>
                  <td><Link to={`/cases/${i.score_id}`}>{i.employee_id}</Link></td>
                  <td>{IV_TYPE[i.type]}</td>
                  <td>{i.arm === "control" ? <Pill tone="muted">Comparison group</Pill>
                    : <Pill tone={i.status === "completed" ? "ok" : i.status === "declined" || i.status === "cancelled" ? "muted" : "watch"}>{STATUS[i.status]}</Pill>}</td>
                  <td>{i.owner}</td>
                  <td className="num">{fmtDate(i.assigned_at)}</td>
                  <td className="num">{fmtDate(i.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  );
}
