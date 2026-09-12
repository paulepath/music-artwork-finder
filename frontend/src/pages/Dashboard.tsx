import { useEffect, useState } from "react";
import { api, Job, Stats } from "../api";

export function Dashboard() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [err, setErr] = useState("");

  async function refresh() {
    try {
      setStats(await api.stats());
      setJobs(await api.jobs());
    } catch (e) {
      setErr(String(e));
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, []);

  const run = (fn: () => Promise<unknown>) => async () => {
    try { await fn(); await refresh(); } catch (e) { setErr(String(e)); }
  };

  return (
    <div className="row" style={{ flexDirection: "column", alignItems: "stretch" }}>
      {err && <div className="card err">{err}</div>}
      <div className="card">
        <div className="row">
          <button className="primary" onClick={run(api.startScan)}>Rescan library</button>
          <button onClick={run(api.findAll)}>Find artwork (trusted sources)</button>
          <button onClick={run(api.findSuspect)}>Re-check suspect artwork</button>
          <button onClick={run(api.applyExact)}>Apply all exact matches</button>
          <button onClick={run(api.maSync)}>Trigger Music Assistant sync</button>
          <div className="spacer" />
          <span className="muted">{stats?.active_jobs ?? 0} active job(s)</span>
        </div>
      </div>

      {stats && (
        <div className="card">
          <div className="grid-stats">
            <div className="stat"><b>{stats.groups_total}</b><span>albums</span></div>
            <div className="stat"><b>{stats.missing_art}</b><span>missing art</span></div>
            {Object.entries(stats.by_state).map(([k, v]) => (
              <div className="stat" key={k}><b>{v}</b><span>{k.replace(/_/g, " ")}</span></div>
            ))}
          </div>
          <p className="muted">Last scan: {stats.last_scan ? new Date(stats.last_scan).toLocaleString() : "never"}</p>
        </div>
      )}

      <div className="card">
        <h3>Recent jobs</h3>
        <table>
          <thead><tr><th>#</th><th>kind</th><th>status</th><th>progress</th><th>message</th></tr></thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id}>
                <td>{j.id}</td><td>{j.kind}</td><td>{j.status}</td>
                <td>{Math.round(j.progress * 100)}%</td>
                <td className="muted">{j.message}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
