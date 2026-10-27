import { ReactNode } from "react";
import { ApiError } from "./api";
import { ISSUE } from "./words";

export function PageHead({ title, lede, children }: { title: string; lede?: ReactNode; children?: ReactNode }) {
  return (
    <header className="page-head">
      <div>
        <h1>{title}</h1>
        {lede && <p className="lede">{lede}</p>}
      </div>
      {children && <div className="page-actions">{children}</div>}
    </header>
  );
}

export function Loading({ what }: { what: string }) {
  return <p className="quiet" aria-busy="true">Loading {what}…</p>;
}

export function Problem({ error }: { error: ApiError }) {
  const fix =
    error.status === 401 ? "Your session ended. Sign in again." :
    error.status === 403 ? "Your role does not include this. Ask an administrator if you need it." :
    error.status === 404 ? "It may have been removed, or it is not shared with your role." :
    error.status === 0 ? "The API is not reachable. Check that the server is running on port 8000." :
    "Try again. If it keeps happening, tell IT operations.";
  return (
    <div className="problem" role="alert">
      <strong>{error.issueType ? ISSUE[error.issueType] ?? error.issueType : error.message}</strong>
      <span>{error.issueType ? error.message : fix}</span>
      {error.exceptionId && <span className="quiet">Logged as {error.exceptionId}.</span>}
    </div>
  );
}

export function Pill({ tone, children }: { tone: "signal" | "watch" | "ok" | "muted" | "stop"; children: ReactNode }) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

export function Stat({ value, label, note }: { value: ReactNode; label: string; note?: string }) {
  return (
    <div className="stat">
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
      {note && <span className="stat-note">{note}</span>}
    </div>
  );
}

export function Purpose({ text }: { text: string }) {
  return <p className="purpose">{text}</p>;
}
