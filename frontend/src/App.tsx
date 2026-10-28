import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { api, ApiError, Role, User } from "./api";
import { ROLE, fmtDate } from "./words";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import Queue from "./pages/Queue";
import CasePage from "./pages/Case";
import MyCases from "./pages/MyCases";
import Interventions from "./pages/Interventions";
import Quality from "./pages/Quality";
import Evaluation from "./pages/Evaluation";
import Admin from "./pages/Admin";
import MyData from "./pages/MyData";

interface Session { user: User; now: string; signOut: () => Promise<void> }
const SessionCtx = createContext<Session | null>(null);
export const useSession = () => useContext(SessionCtx)!;

const NAV: Record<Role, { to: string; text: string }[]> = {
  hr_partner: [
    { to: "/overview", text: "Overview" }, { to: "/queue", text: "Review queue" },
    { to: "/interventions", text: "Conversations" }, { to: "/quality", text: "Data quality" },
    { to: "/evaluation", text: "Model evaluation" },
  ],
  manager: [{ to: "/my-team", text: "My team" }, { to: "/interventions", text: "Conversations" }],
  it_ops: [{ to: "/quality", text: "Data quality" }, { to: "/evaluation", text: "Model evaluation" }, { to: "/admin", text: "Scoring runs" }],
  admin: [
    { to: "/overview", text: "Overview" }, { to: "/admin", text: "Users and runs" },
    { to: "/quality", text: "Data quality" }, { to: "/evaluation", text: "Model evaluation" },
  ],
  employee: [{ to: "/me", text: "My data" }],
};
const HOME: Record<Role, string> = { hr_partner: "/queue", manager: "/my-team", it_ops: "/quality", admin: "/overview", employee: "/me" };

export default function App() {
  const [session, setSession] = useState<{ user: User; now: string } | null | undefined>(undefined);
  const nav = useNavigate();

  const load = useCallback(() => {
    api.get<{ user: User; now: string }>("/auth/me").then(setSession).catch((e) => {
      if (e instanceof ApiError && e.status === 401) setSession(null);
      else setSession(null);
    });
  }, []);
  useEffect(load, [load]);

  const signOut = useCallback(async () => {
    try { await api.post("/auth/logout"); } finally { setSession(null); nav("/"); }
  }, [nav]);

  if (session === undefined) return <div className="boot" aria-busy="true" />;
  if (session === null) return <Login onSignedIn={() => { load(); nav("/"); }} />;

  const { user } = session;
  const allowed = (roles: Role[], el: JSX.Element) => (roles.includes(user.role) ? el : <Navigate to={HOME[user.role]} replace />);

  return (
    <SessionCtx.Provider value={{ ...session, signOut }}>
      <div className="shell">
        <aside className="rail">
          <div className="mark">
            <span className="mark-glyph" aria-hidden="true" />
            <span>Retention Signals</span>
          </div>
          <nav aria-label="Main">
            {NAV[user.role].map((n) => (
              <NavLink key={n.to} to={n.to} className={({ isActive }) => "nav" + (isActive ? " on" : "")}>{n.text}</NavLink>
            ))}
          </nav>
          <div className="rail-foot">
            <div className="who">
              <strong>{user.display_name}</strong>
              <span>{ROLE[user.role]}</span>
            </div>
            <p className="clock" title="The demo runs on a simulated clock so the June 2026 list is current.">
              Working date {fmtDate(session.now)}
            </p>
            <button className="link" onClick={signOut}>Sign out</button>
          </div>
        </aside>
        <main className="work" id="main">
          <Routes>
            <Route path="/" element={<Navigate to={HOME[user.role]} replace />} />
            <Route path="/overview" element={allowed(["hr_partner", "admin"], <Overview />)} />
            <Route path="/queue" element={allowed(["hr_partner"], <Queue />)} />
            <Route path="/cases/:scoreId" element={allowed(["hr_partner", "manager"], <CasePage />)} />
            <Route path="/my-team" element={allowed(["manager"], <MyCases />)} />
            <Route path="/interventions" element={allowed(["hr_partner", "manager"], <Interventions />)} />
            <Route path="/quality" element={allowed(["hr_partner", "it_ops", "admin"], <Quality />)} />
            <Route path="/evaluation" element={allowed(["hr_partner", "it_ops", "admin"], <Evaluation />)} />
            <Route path="/admin" element={allowed(["it_ops", "admin"], <Admin />)} />
            <Route path="/me" element={allowed(["employee"], <MyData />)} />
            <Route path="*" element={<Navigate to={HOME[user.role]} replace />} />
          </Routes>
        </main>
      </div>
    </SessionCtx.Provider>
  );
}
