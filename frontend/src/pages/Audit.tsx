import { useEffect, useState } from "react";
import { AuditEntry, api } from "../api";

export function AuditLog() {
  const [rows, setRows] = useState<AuditEntry[]>([]);
  const [err, setErr] = useState("");

  const refresh = () => api.audit().then(setRows).catch((e) => setErr(String(e)));
  useEffect(() => { refresh(); }, []);

  const undo = async (id: number) => {
    try { await api.undoAudit(id); await refresh(); } catch (e) { setErr(String(e)); }
  };

  return (
    <div className="card">
      <h2>Audit log</h2>
      {err && <p className="err">{err}</p>}
      <table>
        <thead>
          <tr><th>When</th><th>Album</th><th>Action</th><th>Source</th><th>Tier</th>
            <th>Tracks</th><th>SHA</th><th>Result</th><th></th></tr>
        </thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.id}>
              <td className="muted">{new Date(a.created_at).toLocaleString()}</td>
              <td><a href={`/albums/${a.group_id}`}>#{a.group_id}</a></td>
              <td>{a.action}{a.undone_at ? " (undone)" : ""}</td>
              <td>{a.source}</td>
              <td>{a.tier && <span className={`badge ${a.tier}`}>{a.tier}</span>}</td>
              <td>{a.tracks_written}</td>
              <td className="muted" style={{ fontSize: 11 }}>{a.image_sha256.slice(0, 12)}</td>
              <td className={a.ok ? "" : "err"}>{a.ok ? "ok" : a.error}</td>
              <td>
                {a.action === "apply" && !a.undone_at && (
                  <button className="danger" onClick={() => undo(a.id)}>Undo</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
