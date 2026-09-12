import { useEffect, useState } from "react";
import { AuditEntry, api, TrackAudit } from "../api";

export function AuditLog() {
  const [rows, setRows] = useState<AuditEntry[]>([]);
  const [trackRows, setTrackRows] = useState<TrackAudit[]>([]);
  const [err, setErr] = useState("");

  const refresh = () => Promise.all([api.audit(), api.trackAudit()])
    .then(([legacy, tracks]) => { setRows(legacy); setTrackRows(tracks); })
    .catch((e) => setErr(String(e)));
  useEffect(() => { refresh(); }, []);

  const undo = async (id: number) => {
    try { await api.undoAudit(id); await refresh(); } catch (e) { setErr(String(e)); }
  };
  const undoTrack = async (id: number) => {
    try { await api.undoTrackAudit(id); await refresh(); } catch (e) { setErr(String(e)); }
  };

  return (
    <div className="card">
      <h2>Audit log</h2>
      {err && <p className="err">{err}</p>}
      <h3>Track-first writes</h3>
      <table>
        <thead><tr><th>When</th><th>Search</th><th>Track</th><th>Action</th><th>Roles</th><th>Result</th><th></th></tr></thead>
        <tbody>{trackRows.map((a) => <tr key={`track-${a.id}`}>
          <td className="muted">{new Date(a.created_at).toLocaleString()}</td>
          <td><a href={`/search/${a.session_id}`}>#{a.session_id}</a></td><td>#{a.track_id}</td>
          <td>{a.action}{a.undone_at ? " (undone)" : ""}</td><td>{a.roles || "—"}</td>
          <td className={a.ok ? "" : "err"}>{a.ok ? "ok" : a.error}</td>
          <td>{a.action === "apply" && !a.undone_at && a.ok && <button className="danger" onClick={() => void undoTrack(a.id)}>Undo</button>}</td>
        </tr>)}</tbody>
      </table>
      <h3>Legacy album writes</h3>
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
