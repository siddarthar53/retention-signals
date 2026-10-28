// Thin fetch wrapper. Cookies carry the session; every write sends the CSRF header the API requires.
export type Role = "hr_partner" | "manager" | "it_ops" | "admin" | "employee";
export interface User { user_id: string; username: string; display_name: string; role: Role; employee_id: string | null }

export class ApiError extends Error {
  constructor(public status: number, message: string, public issueType?: string, public exceptionId?: string) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "retention-ui" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.ok) return (await res.json()) as T;
  let message = `Request failed (${res.status})`;
  let issue: string | undefined, xid: string | undefined;
  try {
    const j = await res.json();
    const d = j.detail;
    if (typeof d === "string") message = d;
    else if (d && typeof d === "object" && "message" in d) { message = d.message; issue = d.issue_type; xid = d.exception_id; }
    else if (Array.isArray(d)) message = d.map((e: { msg: string }) => e.msg).join("; ");
  } catch { /* body was not JSON */ }
  throw new ApiError(res.status, message, issue, xid);
}

export const api = {
  get: <T,>(p: string) => call<T>("GET", p),
  post: <T,>(p: string, b?: unknown) => call<T>("POST", p, b ?? {}),
};

// ---- response shapes (only the fields the UI reads)
export interface Contribution { feature: string; label: string; value: number | null; reference: number | null; log_odds: number }
export interface Theme { theme: string; text: string; guide: string[] }
export interface Review { review_id: string; decision: string; reason_code: string; comment: string | null; decided_at: string; reviewer: string }
export interface IvEvent { event_id: string; from: string | null; to: string; at: string; actor: string; note: string | null }
export interface Intervention { intervention_id: string; type: string; arm: string; source: string; assigned_at: string; owner: string; status: string | null; events: IvEvent[] }
export interface Context { team_id?: string; team_name: string; role_title: string; job_family: string; level?: number; is_people_manager?: boolean; hire_date: string }

export interface HrCase {
  view: "hr"; score_id: string; employee_id: string; score_run_id: string; is_active_run: boolean; as_of_date: string;
  model_version: string; scorer_type: string; risk_score: number; rank: number; population: number; band: string;
  contributions: Contribution[]; coverage: Record<string, boolean | number>; source_rows: number; themes: Theme[];
  context: Context; tenure: { tenure_months: number; months_in_role: number; months_since_promotion: number | null } | null;
  workload: { month: string; utilization_pct: number | null; after_hours_pct: number; oncall_days: number; after_as_of: boolean }[];
  surveys: { wave: string; status: string; engagement_index: number | null }[];
  scorecard: { rank: number; band: string; points: { rule: string; points: number }[] } | null;
  eligibility: string | null; reviews: Review[]; interventions: Intervention[];
  reason_codes: Record<string, string[]>; intervention_types: string[]; purpose: string;
}
export interface ManagerCase {
  view: "manager"; score_id: string; employee_id: string; as_of_date: string; context: Context;
  tenure_months: number | null; months_in_role: number | null; themes: Theme[]; approved_by: string | null;
  approved_reason: string | null; interventions: Intervention[]; intervention_types: string[]; eligibility: string | null; purpose: string;
}
export type CaseDetail = HrCase | ManagerCase;

export interface QueueItem {
  score_id: string; employee_id: string; risk_score: number; rank: number; band: string; themes: string[];
  job_family: string; role_title: string; team_name: string; review: string; intervention: string | null;
}
