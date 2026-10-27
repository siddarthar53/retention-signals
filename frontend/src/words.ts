// One vocabulary for the whole interface: the same action keeps the same name everywhere.
export const ROLE: Record<string, string> = {
  hr_partner: "HR partner", manager: "Manager", it_ops: "IT operations", admin: "Administrator",
};
export const BAND: Record<string, string> = {
  suggested_checkin: "Suggested check-in", monitor: "Keep an eye on", none: "No action",
};
export const REVIEW: Record<string, string> = {
  pending: "Waiting for review", approved: "Approved", dismissed: "Dismissed", needs_info: "Needs more information",
};
export const STATUS: Record<string, string> = {
  proposed: "Proposed", scheduled: "Scheduled", completed: "Completed", declined: "Declined by employee", cancelled: "Cancelled",
};
export const IV_TYPE: Record<string, string> = {
  checkin: "Check-in conversation", workload_rebalance: "Workload rebalance",
  career_conversation: "Career conversation", internal_mobility: "Internal move discussion",
};
export const REASON: Record<string, string> = {
  supportive_checkin: "A supportive check-in is appropriate",
  workload_concern: "Workload looks heavy",
  career_growth: "Growth conversation is due",
  new_joiner_support: "New joiner support",
  context_known_to_hr: "HR already knows the context",
  recent_conversation_held: "A conversation happened recently",
  data_quality_concern: "The data looks wrong",
  not_appropriate_now: "Not appropriate right now",
  await_manager_input: "Waiting for manager input",
  await_data_correction: "Waiting for a data correction",
};
export const FAMILY: Record<string, string> = {
  engineering: "Engineering", data: "Data", product: "Product", design: "Design", operations: "Operations",
  support: "Support", sales: "Sales", finance_admin: "Finance and admin",
};
export const ISSUE: Record<string, string> = {
  FUTURE_LEAK: "Data recorded after the scoring date",
  MISSING_AS_NEGATIVE: "Missing survey treated as a low score",
  LABEL_DATE_MISMATCH: "Outcome label contradicts the record",
  UNREVIEWED_SCORE: "Action requested without HR approval",
  NOT_AUTHORIZED: "Outside the requester's team",
  STALE_SCORE_VERSION: "Out-of-date score",
  NOT_ELIGIBLE: "Person not eligible",
  CONFLICTING_INTERVENTION: "Action already exists",
  CONTROL_ARM_BLOCKED: "Comparison group, no outreach",
  QUARANTINED_ROW: "Row rejected by a database rule",
  INSUFFICIENT_HISTORY: "Not enough history",
  UNRESOLVED_FOLLOWUP: "Follow-up not closed",
  OTHER: "Other",
};
export const NEXT: Record<string, string[]> = {
  proposed: ["scheduled", "declined", "cancelled"],
  scheduled: ["completed", "declined", "cancelled"],
};
export const ACTION_FOR: Record<string, string> = {
  scheduled: "Mark as scheduled", completed: "Mark as completed", declined: "Employee declined", cancelled: "Cancel",
};

export const label = (map: Record<string, string>, k: string | null | undefined) => (k ? map[k] ?? k : "");

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? iso + "T00:00:00Z" : iso);
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}
export function months(m: number | null | undefined): string {
  if (m == null) return "Never";
  if (m < 12) return `${Math.round(m)} months`;
  const y = Math.floor(m / 12), r = Math.round(m % 12);
  return r ? `${y} yr ${r} mo` : `${y} yr`;
}
export const pct = (x: number, d = 1) => `${(x * 100).toFixed(d)}%`;
