import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  api,
  SearchSession,
  SearchTarget,
  SessionAlbums,
} from "../api";
import { AlbumReview, RoleReview } from "../components/AlbumReview";

export function SearchSessionPage() {
  const id = Number(useParams().id);
  const [session, setSession] = useState<SearchSession | null>(null);
  const [sessionAlbums, setSessionAlbums] = useState<SessionAlbums | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, sa] = await Promise.all([
        api.searchSession(id, false),
        api.sessionAlbums(id),
      ]);
      setSession(s);
      setSessionAlbums(sa);
    } catch (e) {
      setError(String(e));
    }
  }, [id]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(
      refresh,
      session?.status === "running" || session?.status === "applying" ? 1000 : 4000
    );
    return () => window.clearInterval(timer);
  }, [refresh, session?.status]);

  async function act(action: "pause" | "resume" | "cancel" | "retry" | "apply") {
    setBusy(true);
    setError("");
    try {
      await api.controlSession(id, action);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function approveRecommended() {
    setBusy(true);
    try {
      await api.approveRecommended(id, "medium");
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveAlbum(albumQueryId: number, patch: { cluster_id: number | null; approved: boolean }) {
    try {
      await api.updateAlbumSelection(id, albumQueryId, patch);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  async function saveSingle(targetId: number, patch: { cluster_id: number | null; approved: boolean }) {
    try {
      await api.updateSelection(id, targetId, patch);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  if (!session) return <div className="card">{error || "Loading search…"}</div>;
  const running = session.status === "queued" || session.status === "running" || session.status === "paused";
  const approvedCount =
    (sessionAlbums?.albums.filter((a) => a.approved).length ?? 0) +
    (sessionAlbums?.singles.filter((s) => s.approved).length ?? 0);

  return (
    <div className="stack">
      <section className="card session-head">
        <div>
          <div className="eyebrow">Search #{session.id}</div>
          <h1>{session.folder_path || `${session.total_tracks} selected tracks`}</h1>
          <p className="muted">{session.message}</p>
        </div>
        <span className={`status-pill ${session.status}`}>{session.status.replace(/_/g, " ")}</span>
        <div className="progress">
          <i style={{ width: `${Math.round(session.progress * 100)}%` }} />
        </div>
        <div className="row">
          {session.status === "running" && (
            <button disabled={busy} onClick={() => void act("pause")}>
              Pause
            </button>
          )}
          {session.status === "paused" && (
            <button disabled={busy} onClick={() => void act("resume")}>
              Resume
            </button>
          )}
          {running && (
            <button className="danger" disabled={busy} onClick={() => void act("cancel")}>
              Cancel
            </button>
          )}
          {session.failed_tracks > 0 && (
            <button disabled={busy} onClick={() => void act("retry")}>
              Retry failed
            </button>
          )}
          {!running && (
            <button
              disabled={busy}
              onClick={() => void approveRecommended()}
              title="Sets approval on all items matching confidence threshold, replacing any existing approvals below it"
            >
              Approve high & medium (replaces lower)
            </button>
          )}
          {!running && (
            <button
              className="primary"
              disabled={busy || approvedCount === 0}
              onClick={() => void act("apply")}
            >
              Apply {approvedCount} approved artwork choice{approvedCount === 1 ? "" : "s"}
            </button>
          )}
        </div>
      </section>

      {error && <div className="card err">{error}</div>}

      <div className="stack">
        {sessionAlbums?.albums.map((album) => (
          <AlbumReview key={album.album_query_id} album={album} onSave={saveAlbum} />
        ))}
      </div>

      {sessionAlbums?.singles && sessionAlbums.singles.length > 0 && (
        <section className="stack" style={{ marginTop: 24 }}>
          <h2>Singles & Track Artwork ({sessionAlbums.singles.length})</h2>
          {sessionAlbums.singles.map((single) => (
            <SingleReview key={single.id} target={single} onSave={saveSingle} />
          ))}
        </section>
      )}
    </div>
  );
}


function SingleReview({
  target,
  onSave,
}: {
  target: SearchTarget;
  onSave: (targetId: number, patch: { cluster_id: number | null; approved: boolean }) => Promise<void>;
}) {
  return (
    <article className="card result-group">
      <header className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", marginBottom: 12 }}>
        <div>
          <h3 style={{ margin: 0 }}>{target.track.title || "(untitled)"}</h3>
          <div className="muted">
            {target.track.artist || "unknown artist"} · {target.track.album || "unknown album"}
          </div>
        </div>
        <div className="row" style={{ alignItems: "center", gap: 8 }}>
          <span>{Math.round(target.progress * 100)}%</span>
          <span className="badge state">{target.stage || target.status}</span>
          {target.applied && <span className="badge exact">applied</span>}
        </div>
      </header>
      {target.error && <p className="err">{target.error}</p>}

      <RoleReview
        label="Track / single artwork"
        currentUrl={target.current_art_url}
        query={target.artwork_query}
        selected={target.selected_cluster_id}
        approved={target.approved}
        onSelect={(clusterId) => onSave(target.id, { cluster_id: clusterId, approved: target.approved })}
        onApprove={(approved) => onSave(target.id, { cluster_id: target.selected_cluster_id, approved })}
      />
      <div className="muted path" style={{ marginTop: 8, fontSize: 12 }}>
        <code>{target.track.path}</code>
      </div>
    </article>
  );
}

export function SearchSessionsPage() {
  const [sessions, setSessions] = useState<SearchSession[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    const load = () => api.sessions().then(setSessions).catch((e) => setError(String(e)));
    void load();
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <div className="card">
      <div className="row">
        <h1>Search history</h1>
        <span className="spacer" />
        <Link className="button-link" to="/search">New search</Link>
      </div>
      {error && <p className="err">{error}</p>}
      <table>
        <thead>
          <tr>
            <th>Search</th>
            <th>Tracks</th>
            <th>Status</th>
            <th>Progress</th>
            <th>Started</th>
          </tr>
        </thead>
        <tbody>
          {sessions.map((session) => (
            <tr key={session.id}>
              <td>
                <Link to={`/search/${session.id}`}>
                  #{session.id} {session.folder_path || "selected tracks"}
                </Link>
              </td>
              <td>{session.total_tracks}</td>
              <td>{session.status.replace(/_/g, " ")}</td>
              <td>{Math.round(session.progress * 100)}%</td>
              <td>{new Date(session.created_at).toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
