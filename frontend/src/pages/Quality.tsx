import { useState } from "react";
import { useFetch } from "../hooks";
import { Loading, PageHead, Pill, Problem } from "../ui";
import { fmtDate, ISSUE } from "../words";

interface Det { issue: string; target: string; injected: number; controls: number; caught: number; missed: number; false_positives: number }
interface DQ {
  generation: { scale: number; seed: number; validator: Record<string, number>; fk_constraints: number; fk_orphans: number;
    detection: Det[]; voluntary_attrition_pct: number } | null;
  replay: { fixture_requests: number; replayed: number; skipped: Record<string, number>; caught: number; missed: number;
    false_positives: number; valid_allowed: number; by_variant: Record<string, { n: number; blocked: number; issue_types: Record<string, number> }> } | null;
}
interface Exc { exception_id: string; entity: string; record_id: string; issue_type: string; detector: string; detail: Record<string, unknown>; detected_at: string; status: string }

const VARIANT: Record<string, string> = {
  no_review: "No HR review", dismissed_review: "HR dismissed the case", stale_score_version: "Out-of-date score",
  other_team: "Manager of another team", valid: "Approved, current, own team",
};

export default function Quality() {
  const { data, error } = useFetch<DQ>("/data-quality");
  const [issue, setIssue] = useState("");
  const [page, setPage] = useState(0);
  const ex = useFetch<{ counts: { issue_type: string; entity: string; n: number }[]; total: number; items: Exc[] }>(
    `/exceptions?limit=25&offset=${page * 25}${issue ? `&issue_type=${issue}` : ""}`);

  if (error) return <Problem error={error} />;
  if (!data) return <Loading what="data quality" />;
  const g = data.generation, r = data.replay;
  const failing = g ? Object.entries(g.validator).filter(([, v]) => v > 0) : [];
  const byIssue = new Map<string, number>();
  ex.data?.counts.forEach((c) => byIssue.set(c.issue_type, (byIssue.get(c.issue_type) ?? 0) + c.n));

  return (
    <>
      <PageHead title="Data quality"
        lede="Checks on the generated dataset, the planted problem records and whether each was caught, and every blocked action in the app." />

      {g && (
        <div className="two">
          <section className="panel">
            <h2>Dataset checks</h2>
            <p>{failing.length === 0 ? <Pill tone="ok">All {Object.keys(g.validator).length} rules pass</Pill>
              : <Pill tone="stop">{failing.length} rules fail</Pill>}</p>
            <dl className="facts">
              <div><dt>Employees generated</dt><dd>{g.scale.toLocaleString()}</dd></div>
              <div><dt>Random seed</dt><dd>{g.seed}</dd></div>
              <div><dt>Voluntary attrition</dt><dd>{g.voluntary_attrition_pct}%</dd></div>
              <div><dt>Links between tables checked</dt><dd>{g.fk_constraints}</dd></div>
              <div><dt>Records pointing at nothing</dt><dd>{g.fk_orphans}</dd></div>
            </dl>
            {failing.length > 0 && <ul>{failing.map(([k, v]) => <li key={k}>{k}: {v}</li>)}</ul>}
          </section>
          <section className="panel">
            <h2>Planted problem records</h2>
            <p className="quiet">Each problem type was planted next to look-alike correct records, so a detector that flags everything would fail.</p>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Problem</th><th className="num">Planted</th><th className="num">Caught</th><th className="num">Missed</th><th className="num">False alarms</th></tr></thead>
                <tbody>
                  {g.detection.map((d) => (
                    <tr key={d.issue + d.target}>
                      <td>{ISSUE[d.issue] ?? d.issue}<div className="quiet small">{d.target}</div></td>
                      <td className="num">{d.injected}</td><td className="num">{d.caught}</td>
                      <td className="num">{d.missed}</td><td className="num">{d.false_positives}</td>
                    </tr>
                  ))}
                  {r && (
                    <tr>
                      <td>{ISSUE.UNREVIEWED_SCORE}<div className="quiet small">replayed through the live API</div></td>
                      <td className="num">{r.caught + r.missed}</td><td className="num">{r.caught}</td>
                      <td className="num">{r.missed}</td><td className="num">{r.false_positives}</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      )}

      {r && (
        <section className="panel">
          <h2>Approval rule, replayed through the API</h2>
          <p className="quiet">
            {r.replayed} of {r.fixture_requests} planted requests were sent to the real API with the app's own database
            permissions, then rolled back. {Object.entries(r.skipped).map(([k, v]) => `${v} skipped (${k.replace(/_/g, " ")})`).join(", ")}.
          </p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Request</th><th className="num">Sent</th><th className="num">Blocked</th><th>Reason given</th></tr></thead>
              <tbody>
                {Object.entries(r.by_variant).map(([k, v]) => (
                  <tr key={k}>
                    <td>{VARIANT[k] ?? k}</td><td className="num">{v.n}</td><td className="num">{v.blocked}</td>
                    <td>{Object.keys(v.issue_types).map((i) => ISSUE[i] ?? i).join(", ") || <span className="quiet">Allowed</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="panel">
        <h2>Issue log</h2>
        <div className="chips" role="group" aria-label="Filter by issue">
          <button className={"chip" + (issue === "" ? " on" : "")} onClick={() => { setIssue(""); setPage(0); }}>All</button>
          {[...byIssue.entries()].map(([k, n]) => (
            <button key={k} className={"chip" + (issue === k ? " on" : "")} onClick={() => { setIssue(k); setPage(0); }}>
              {ISSUE[k] ?? k} <span className="quiet">{n}</span>
            </button>
          ))}
        </div>
        {ex.error && <Problem error={ex.error} />}
        {ex.data && (
          <>
            <div className="table-wrap">
              <table>
                <thead><tr><th>ID</th><th>Issue</th><th>Record</th><th>Found by</th><th>When</th></tr></thead>
                <tbody>
                  {ex.data.items.map((x) => (
                    <tr key={x.exception_id}>
                      <td className="num">{x.exception_id}</td>
                      <td>{ISSUE[x.issue_type] ?? x.issue_type}</td>
                      <td><span className="mono-ish">{x.record_id}</span><div className="quiet small">{x.entity}</div></td>
                      <td className="small">{x.detector}</td>
                      <td className="num">{fmtDate(x.detected_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <nav className="pager">
              <button className="secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
              <span>{ex.data.total.toLocaleString()} issues, page {page + 1} of {Math.max(1, Math.ceil(ex.data.total / 25))}</span>
              <button className="secondary" disabled={(page + 1) * 25 >= ex.data.total} onClick={() => setPage(page + 1)}>Next</button>
            </nav>
          </>
        )}
      </section>
    </>
  );
}
