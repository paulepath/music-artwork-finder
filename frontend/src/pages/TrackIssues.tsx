import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, TrackIssue } from "../api";

export function TrackIssues() {
  const [rows, setRows] = useState<TrackIssue[]>([]);
  const [offset, setOffset] = useState(0);
  const [err, setErr] = useState("");
  const limit = 100;
  useEffect(() => { api.trackIssues({ limit: String(limit), offset: String(offset) }).then(setRows).catch(e => setErr(String(e))); }, [offset]);
  return <div className="card">
    <h2>Tracks missing embedded artwork</h2>
    <p className="muted">These tracks need attention. Open their album to inspect existing art, candidates, and history.</p>
    {err && <p className="err">{err}</p>}
    <table><thead><tr><th>Track</th><th>Album</th><th>State</th><th>Path</th></tr></thead><tbody>
      {rows.map(t => <tr key={t.track_id}><td>{t.track_no ?? ""} {t.title || <span className="muted">(untitled)</span>}</td>
        <td><Link to={`/albums/${t.album_id}`}>{t.album || "(untitled)"}</Link><div className="muted">{t.album_artist}</div></td>
        <td><span className="badge state">{t.state.replace(/_/g, " ")}</span></td><td className="muted">{t.path}</td></tr>)}
    </tbody></table>
    <div className="row" style={{ marginTop: 14 }}><button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - limit))}>Previous</button><button disabled={rows.length < limit} onClick={() => setOffset(offset + limit)}>Next</button><span className="muted">{rows.length} shown</span></div>
  </div>;
}
