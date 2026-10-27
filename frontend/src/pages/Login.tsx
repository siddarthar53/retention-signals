import { FormEvent, useState } from "react";
import { api, ApiError } from "../api";

export default function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await api.post("/auth/login", { username, password });
      onSignedIn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The API is not reachable. Check that the server is running.");
    } finally { setBusy(false); }
  }

  return (
    <div className="login">
      <section className="login-copy">
        <div className="mark big"><span className="mark-glyph" aria-hidden="true" /><span>Retention Signals</span></div>
        <h1>Know who could use a conversation, and why.</h1>
        <p>
          Each quarter the model reads tenure, role history, workload and survey signals and suggests a short list of people
          for a supportive check-in. HR reviews every suggestion before a manager sees it. Nothing here is used for
          performance, pay or employment decisions.
        </p>
        <p className="quiet">All people and records in this demo are synthetic.</p>
      </section>
      <form className="login-form" onSubmit={submit} noValidate>
        <h2>Sign in</h2>
        <label htmlFor="u">Username</label>
        <input id="u" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required autoFocus />
        <label htmlFor="p">Password</label>
        <input id="p" type="password" autoComplete="current-password" value={password}
               onChange={(e) => setPassword(e.target.value)} required />
        {error && <p className="form-error" role="alert">{error}</p>}
        <button className="primary" disabled={busy || !username || !password}>{busy ? "Signing in…" : "Sign in"}</button>
        <p className="quiet small">Demo logins are written to <code>data/out/demo_credentials.txt</code> when the pipeline runs.</p>
      </form>
    </div>
  );
}
