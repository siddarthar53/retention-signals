import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { QueueItem } from "../api";
import { useFetch } from "../hooks";
import { Loading, PageHead, Pill, Problem } from "../ui";
import { BAND, FAMILY, REVIEW, STATUS } from "../words";

const PAGE = 50;

export default function Queue() {
  const [sp, setSp] = useSearchParams();
  const band = sp.get("band") ?? "suggested_checkin";
  const status = sp.get("status") ?? "pending";
  const family = sp.get("family") ?? "";
  const q = sp.get("q") ?? "";
  const page = Number(sp.get("page") ?? "0");
  const [search, setSearch] = useState(q);
  useEffect(() => setSearch(q), [q]);

  const qs = new URLSearchParams({ limit: String(PAGE), offset: String(page * PAGE) });
  if (band !== "all") qs.set("band", band);
  if (status !== "all") qs.set("status", status);
  if (family) qs.set("family", family);
  if (q) qs.set("q", q);
  const { data, error, loading } = useFetch<{ score_run_id: string; total: number; items: QueueItem[] }>(`/queue?${qs}`);

  const set = (k: string, v: string) => {
    const n = new URLSearchParams(sp);
    if (v) n.set(k, v); else n.delete(k);
    n.delete("page");
    setSp(n);
  };

  return (
    <>
      <PageHead title="Review queue"
        lede="Approve a case to share it with the person's manager, dismiss it if HR already has the context, or ask for more information." />
      <form className="filters" onSubmit={(e) => { e.preventDefault(); set("q", search.trim()); }} role="search">
        <label>List
          <select value={band} onChange={(e) => set("band", e.target.value)}>
            <option value="suggested_checkin">{BAND.suggested_checkin}</option>
            <option value="monitor">{BAND.monitor}</option>
            <option value="all">Both lists</option>
          </select>
        </label>
        <label>Review
          <select value={status} onChange={(e) => set("status", e.target.value)}>
            <option value="pending">{REVIEW.pending}</option>
            <option value="approved">{REVIEW.approved}</option>
            <option value="dismissed">{REVIEW.dismissed}</option>
            <option value="needs_info">{REVIEW.needs_info}</option>
            <option value="all">Any</option>
          </select>
        </label>
        <label>Job family
          <select value={family} onChange={(e) => set("family", e.target.value)}>
            <option value="">All families</option>
            {Object.entries(FAMILY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label className="grow">Employee ID
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="EMP-004790" maxLength={20} />
        </label>
        <button className="secondary">Find</button>
      </form>

      {error && <Problem error={error} />}
      {!data && loading && <Loading what="the queue" />}
      {data && (
        <>
          <p className="count" aria-live="polite">{data.total.toLocaleString()} {data.total === 1 ? "case" : "cases"}</p>
          {data.items.length === 0 ? (
            <div className="empty">
              <p>No cases match these filters.</p>
              {status === "pending" && <p className="quiet">Every suggestion in this list has been reviewed. Switch the review filter to see decisions.</p>}
            </div>
          ) : (
            <ol className="cases">
              {data.items.map((c) => (
                <li key={c.score_id}>
                  <Link to={`/cases/${c.score_id}`} className="case-row">
                    <span className="case-rank" title="Position in this quarter's list">{c.rank}</span>
                    <span className="case-who">
                      <strong>{c.employee_id}</strong>
                      <span className="quiet">{c.role_title}, {c.team_name}</span>
                    </span>
                    <span className="case-why">{c.themes.length ? c.themes.join(". ") + "." : <span className="quiet">No single factor stands out.</span>}</span>
                    <span className="case-state">
                      {c.intervention ? <Pill tone={c.intervention === "completed" ? "ok" : "muted"}>{STATUS[c.intervention]}</Pill>
                        : <Pill tone={c.review === "pending" ? "signal" : c.review === "approved" ? "ok" : "muted"}>{REVIEW[c.review]}</Pill>}
                      {band === "all" && <span className="quiet small">{BAND[c.band]}</span>}
                    </span>
                  </Link>
                </li>
              ))}
            </ol>
          )}
          {data.total > PAGE && (
            <nav className="pager" aria-label="Pages">
              <button className="secondary" disabled={page === 0} onClick={() => setSp(new URLSearchParams({ ...Object.fromEntries(sp), page: String(page - 1) }))}>Previous</button>
              <span>Page {page + 1} of {Math.ceil(data.total / PAGE)}</span>
              <button className="secondary" disabled={(page + 1) * PAGE >= data.total} onClick={() => setSp(new URLSearchParams({ ...Object.fromEntries(sp), page: String(page + 1) }))}>Next</button>
            </nav>
          )}
        </>
      )}
    </>
  );
}
