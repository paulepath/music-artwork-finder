import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ArtworkCluster, assetImg, SearchSession, SearchTarget } from "../api";

export function SearchSessionPage() {
  const id = Number(useParams().id);
  const [session, setSession] = useState<SearchSession | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(() => api.searchSession(id).then(setSession).catch((e) => setError(String(e))), [id]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(refresh, session?.status === "running" || session?.status === "applying" ? 1000 : 4000);
    return () => window.clearInterval(timer);
  }, [refresh, session?.status]);

  const groups = useMemo(() => {
    const result = new Map<string, SearchTarget[]>();
    for (const target of session?.targets ?? []) {
      const key = `${target.selected_album_cluster_id ?? "none"}|${target.selected_track_cluster_id ?? "none"}`;
      result.set(key, [...(result.get(key) ?? []), target]);
    }
    return [...result.values()];
  }, [session]);

  async function act(action: "pause" | "resume" | "cancel" | "retry" | "apply") {
    setBusy(true); setError("");
    try { await api.controlSession(id, action); await refresh(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }

  async function approveRecommended() {
    setBusy(true);
    try { await api.approveRecommended(id, "medium"); await refresh(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }

  async function save(target: SearchTarget, patch: Partial<Pick<SearchTarget,
    "selected_album_cluster_id" | "selected_track_cluster_id" | "album_approved" | "track_approved">>) {
    const next = { ...target, ...patch };
    try {
      await api.updateSelection(id, target.id, {
        album_cluster_id: next.selected_album_cluster_id,
        track_cluster_id: next.selected_track_cluster_id,
        album_approved: next.album_approved,
        track_approved: next.track_approved,
      });
      await refresh();
    } catch (e) { setError(String(e)); }
  }

  if (!session) return <div className="card">{error || "Loading search…"}</div>;
  const running = session.status === "queued" || session.status === "running" || session.status === "paused";
  const approved = (session.targets ?? []).reduce((sum, target) => sum + Number(target.album_approved) + Number(target.track_approved), 0);

  return <div className="stack">
    <section className="card session-head">
      <div>
        <div className="eyebrow">Search #{session.id}</div>
        <h1>{session.folder_path || `${session.total_tracks} selected tracks`}</h1>
        <p className="muted">{session.message}</p>
      </div>
      <span className={`status-pill ${session.status}`}>{session.status.replace(/_/g, " ")}</span>
      <div className="progress"><i style={{ width: `${Math.round(session.progress * 100)}%` }} /></div>
      <div className="row">
        {session.status === "running" && <button disabled={busy} onClick={() => void act("pause")}>Pause</button>}
        {session.status === "paused" && <button disabled={busy} onClick={() => void act("resume")}>Resume</button>}
        {running && <button className="danger" disabled={busy} onClick={() => void act("cancel")}>Cancel</button>}
        {session.failed_tracks > 0 && <button disabled={busy} onClick={() => void act("retry")}>Retry failed</button>}
        {!running && <button disabled={busy} onClick={() => void approveRecommended()}>Approve high & medium</button>}
        {!running && <button className="primary" disabled={busy || approved === 0} onClick={() => void act("apply")}>Apply {approved} approved artwork choice{approved === 1 ? "" : "s"}</button>}
      </div>
    </section>
    {error && <div className="card err">{error}</div>}

    {groups.map((targets) => <section className="result-group card" key={targets.map((t) => t.id).join("-")}>
      {targets.length > 1 && <div className="group-banner">Same recommended artwork · {targets.length} tracks</div>}
      {targets.map((target) => <TrackReview target={target} key={target.id} onSave={save} />)}
    </section>)}
  </div>;
}

function TrackReview({ target, onSave }: {
  target: SearchTarget;
  onSave: (target: SearchTarget, patch: Partial<SearchTarget>) => Promise<void>;
}) {
  return <article className="track-review">
    <header>
      <div><strong>{target.track.title || "(untitled)"}</strong><div className="muted">{target.track.artist || "unknown artist"} · {target.track.album || "unknown album"}</div></div>
      <div className="track-state"><span>{Math.round(target.progress * 100)}%</span><span className="badge state">{target.stage}</span></div>
    </header>
    {target.error && <p className="err">{target.error}</p>}
    <div className="art-roles">
      <RoleReview
        label="Album artwork" currentUrl={target.current_album_art_url}
        query={target.album_query} selected={target.selected_album_cluster_id}
        approved={target.album_approved}
        onSelect={(id) => onSave(target, { selected_album_cluster_id: id, album_approved: target.album_approved })}
        onApprove={(value) => onSave(target, { album_approved: value })}
      />
      <RoleReview
        label="Track / single artwork" currentUrl={target.current_track_art_url}
        query={target.track_query} selected={target.selected_track_cluster_id}
        approved={target.track_approved}
        onSelect={(id) => onSave(target, { selected_track_cluster_id: id, track_approved: target.track_approved })}
        onApprove={(value) => onSave(target, { track_approved: value })}
      />
    </div>
    <div className="muted path">{target.track.path}</div>
  </article>;
}

function RoleReview({ label, currentUrl, query, selected, approved, onSelect, onApprove }: {
  label: string; currentUrl: string; query: SearchTarget["album_query"];
  selected: number | null; approved: boolean;
  onSelect: (id: number) => Promise<void>; onApprove: (value: boolean) => Promise<void>;
}) {
  return <div className="role-review">
    <div className="role-title"><h3>{label}</h3><span className="muted">{query.message}</span>{query.google_used && <span className="badge review">Google fallback</span>}</div>
    <div className="candidate-strip">
      <figure className="current-art"><ImageOrEmpty src={currentUrl} /><figcaption>Current</figcaption></figure>
      {query.clusters.map((cluster) => <ClusterCard cluster={cluster} selected={cluster.id === selected} onSelect={() => onSelect(cluster.id)} key={cluster.id} />)}
      {query.status === "ready" && query.clusters.length === 0 && <div className="empty-art">No candidates</div>}
    </div>
    <label className="approve"><input type="checkbox" checked={approved} disabled={!selected} onChange={(e) => void onApprove(e.target.checked)} /> approve selected {label.toLowerCase()}</label>
  </div>;
}

function ClusterCard({ cluster, selected, onSelect }: { cluster: ArtworkCluster; selected: boolean; onSelect: () => void }) {
  return <figure className={`cluster-card ${selected ? "selected" : ""}`}>
    <button className="image-button" onClick={onSelect}><img src={assetImg(cluster.asset_id)} alt="Artwork candidate" loading="lazy" /></button>
    <figcaption>
      <span className={`confidence ${cluster.confidence_label}`}>{cluster.confidence_label}</span>
      <strong>{cluster.observation_count} match{cluster.observation_count === 1 ? "" : "es"}</strong>
      <small>{cluster.sources.join(" · ")} · {cluster.width}×{cluster.height}</small>
      <details><summary>Evidence & variants</summary>{cluster.observations.map((item, i) => <p className="variant" key={i}><img src={assetImg(item.asset_id)} alt="Variant" /> <span>{item.source}: {item.reason}{item.provenance_url && <> · <a href={item.provenance_url} target="_blank">source ↗</a></>}</span></p>)}</details>
    </figcaption>
  </figure>;
}

function ImageOrEmpty({ src }: { src: string }) {
  const [failed, setFailed] = useState(false);
  return failed ? <span>No current art</span> : <img src={src} alt="Current artwork" onError={() => setFailed(true)} />;
}

export function SearchSessionsPage() {
  const [sessions, setSessions] = useState<SearchSession[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    const load = () => api.sessions().then(setSessions).catch((e) => setError(String(e)));
    void load(); const timer = window.setInterval(load, 3000); return () => window.clearInterval(timer);
  }, []);
  return <div className="card"><div className="row"><h1>Search history</h1><span className="spacer" /><Link className="button-link" to="/search">New search</Link></div>
    {error && <p className="err">{error}</p>}
    <table><thead><tr><th>Search</th><th>Tracks</th><th>Status</th><th>Progress</th><th>Started</th></tr></thead><tbody>
      {sessions.map((session) => <tr key={session.id}><td><Link to={`/search/${session.id}`}>#{session.id} {session.folder_path || "selected tracks"}</Link></td><td>{session.total_tracks}</td><td>{session.status.replace(/_/g, " ")}</td><td>{Math.round(session.progress * 100)}%</td><td>{new Date(session.created_at).toLocaleString()}</td></tr>)}
    </tbody></table>
  </div>;
}
