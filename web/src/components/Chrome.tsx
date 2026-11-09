import { useState, type FormEvent } from "react";
import { api, AuthError } from "../api";
import { useApp } from "../store";
import { isActive } from "../types";

export function TopBar() {
  const { status: st, jobs, setJobsOpen, jobsOpen } = useApp();
  const active = jobs.filter(isActive);
  const running = active.find((j) => j.status === "running");
  return (
    <header className="topbar">
      <div className="brand">
        <img src="/favicon.svg" alt="" /> ansreview <small>{st ? "v" + st.version : ""}</small>
      </div>
      <div className="status-pills">
        {st && (
          <>
            <span className="pill" title={st.ollama_error || st.ollama_host}>
              <span className={"dot " + (st.ollama ? "ok" : "bad")} />
              {st.ollama ? `Ollama ${st.ollama}` : "Ollama unreachable"}
            </span>
            {st.ollama && (
              <span className="pill" title={st.model_available ? "model is pulled" : `run: ollama pull ${st.model}`}>
                <span className={"dot " + (st.model_available ? "ok" : "warn")} />
                {st.model}
              </span>
            )}
            <span className="pill" title="ansible-lint">
              <span className={"dot " + (st.ansible_lint ? "ok" : "warn")} />
              {st.ansible_lint ? "ansible-lint " + st.ansible_lint.split(" ")[0] : "no ansible-lint"}
            </span>
          </>
        )}
        <button className={"pill keep jobs-pill" + (active.length ? " busy" : "")} onClick={() => setJobsOpen(!jobsOpen)} aria-expanded={jobsOpen}>
          <span className={"dot " + (running ? "run" : active.length ? "wait" : "ok")} />
          {active.length ? (running ? `${running.action === "scan" ? "Scanning" : "Fixing"} ${running.repo_name}` : "Waiting") : "Jobs"}
          {active.length > 1 && <span className="count-badge">+{active.length - 1}</span>}
        </button>
      </div>
    </header>
  );
}

export function Login() {
  const { setAuthed, refreshRepos } = useApp();
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      await api("login", { method: "POST", body: { token } });
      setToken("");
      setAuthed(true);
      refreshRepos();
    } catch (err) {
      setError(err instanceof AuthError ? "Wrong token." : (err as Error).message);
    }
  };
  return (
    <div className="center-box">
      <img src="/favicon.svg" alt="" width={44} height={44} />
      <h1>Access token needed</h1>
      <p>Open the link that <code>ansreview serve</code> printed in the terminal, or paste the token here.</p>
      <form onSubmit={submit}>
        <input type="password" placeholder="token" autoComplete="off" required autoFocus value={token} onChange={(e) => setToken(e.target.value)} />
        <button className="btn primary" type="submit">Sign in</button>
      </form>
      <div className="form-error">{error}</div>
    </div>
  );
}

export function Toasts() {
  const { toasts, dismiss } = useApp();
  if (!toasts.length) return null;
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={"toast" + (t.kind ? " " + t.kind : "")}>
          <span>{t.text}</span>
          {t.link && (t.link.href ? (
            <a href={t.link.href} download={t.link.download || undefined} onClick={() => dismiss(t.id)}>{t.link.label}</a>
          ) : (
            <button className="linklike" onClick={() => { t.link!.onClick?.(); dismiss(t.id); }}>{t.link.label}</button>
          ))}
          <button className="toast-x" onClick={() => dismiss(t.id)} aria-label="Dismiss">✕</button>
        </div>
      ))}
    </div>
  );
}
