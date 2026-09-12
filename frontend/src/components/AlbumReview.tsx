import { Fragment, useState } from "react";
import {
  ArtworkCluster,
  assetImg,
  QueryProgress,
  SessionAlbum,
} from "../api";

export function AlbumReview({
  album,
  onSave,
}: {
  album: SessionAlbum;
  onSave: (albumQueryId: number, patch: { cluster_id: number | null; approved: boolean }) => Promise<void>;
}) {
  const queryProgress: QueryProgress = {
    id: album.album_query_id,
    status: album.status,
    message: album.message,
    google_used: album.google_used,
    clusters: album.clusters,
  };

  return (
    <article className="card result-group">
      <header className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", marginBottom: 12 }}>
        <div>
          <div className="row" style={{ alignItems: "center", gap: 8 }}>
            <h2 style={{ margin: 0 }}>{album.title || "(untitled)"}</h2>
            {album.discs && album.discs.length > 1 && (
              <span className="badge strong">{album.discs.length} discs</span>
            )}
          </div>
          <div className="muted" style={{ marginTop: 2 }}>
            {album.album_artist || "unknown artist"}
            {album.year ? ` · ${album.year}` : ""} · {album.track_count} track{album.track_count === 1 ? "" : "s"}
          </div>
        </div>
        <div className="row" style={{ alignItems: "center", gap: 8 }}>
          <span>{Math.round(album.progress * 100)}%</span>
          <span className="badge state">{album.status}</span>
          {album.applied && <span className="badge exact">applied</span>}
        </div>
      </header>

      <RoleReview
        label="Album artwork"
        currentUrl={album.current_art_url}
        query={queryProgress}
        selected={album.selected_cluster_id}
        approved={album.approved}
        onSelect={(clusterId) => onSave(album.album_query_id, { cluster_id: clusterId, approved: album.approved })}
        onApprove={(approved) => onSave(album.album_query_id, { cluster_id: album.selected_cluster_id, approved })}
      />

      <details style={{ marginTop: 12 }}>
        <summary className="muted" style={{ cursor: "pointer" }}>
          View {album.tracks.length} track{album.tracks.length === 1 ? "" : "s"}
        </summary>
        <table style={{ marginTop: 8 }}>
          <thead>
            <tr>
              <th style={{ width: 40 }}>#</th>
              <th>Title</th>
              <th>Artist</th>
              <th>Path</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {album.tracks.map((t, idx) => {
              const showDiscHeader =
                album.discs &&
                album.discs.length > 1 &&
                (idx === 0 || t.disc !== album.tracks[idx - 1].disc);
              return (
                <Fragment key={t.target_id}>
                  {showDiscHeader && (
                    <tr className="disc-header" style={{ background: "rgba(255, 255, 255, 0.05)" }}>
                      <td colSpan={5} style={{ fontWeight: 600, padding: "6px 8px" }}>
                        Disc {t.disc}
                      </td>
                    </tr>
                  )}
                  <tr>
                    <td>{t.disc > 1 ? `${t.disc}-${t.track_no ?? "—"}` : (t.track_no ?? "—")}</td>
                    <td><strong>{t.title || "(untitled)"}</strong></td>
                    <td className="muted">{t.artist}</td>
                    <td className="muted" style={{ fontSize: 12 }}><code>{t.path}</code></td>
                    <td><span className="badge state">{t.stage || t.status}</span></td>
                  </tr>
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </details>
    </article>
  );
}

export function RoleReview({
  label,
  currentUrl,
  query,
  selected,
  approved,
  onSelect,
  onApprove,
}: {
  label: string;
  currentUrl: string;
  query: QueryProgress;
  selected: number | null;
  approved: boolean;
  onSelect: (id: number) => Promise<void>;
  onApprove: (value: boolean) => Promise<void>;
}) {
  return (
    <div className="role-review">
      <div className="role-title">
        <h3>{label}</h3>
        <span className="muted">{query.message}</span>
        {query.google_used && <span className="badge review">Google fallback</span>}
      </div>
      <div className="candidate-strip">
        <figure className="current-art">
          <ImageOrEmpty src={currentUrl} />
          <figcaption>Current</figcaption>
        </figure>
        {query.clusters.map((cluster) => (
          <ClusterCard
            cluster={cluster}
            selected={cluster.id === selected}
            onSelect={() => void onSelect(cluster.id)}
            key={cluster.id}
          />
        ))}
        {query.status === "ready" && query.clusters.length === 0 && (
          <div className="empty-art">No candidates</div>
        )}
      </div>
      <label className="approve">
        <input
          type="checkbox"
          checked={approved}
          disabled={!selected}
          onChange={(e) => void onApprove(e.target.checked)}
        />{" "}
        approve selected {label.toLowerCase()}
      </label>
    </div>
  );
}

export function ClusterCard({
  cluster,
  selected,
  onSelect,
}: {
  cluster: ArtworkCluster;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <figure className={`cluster-card ${selected ? "selected" : ""}`}>
      <button className="image-button" onClick={onSelect}>
        <img src={assetImg(cluster.asset_id)} alt="Artwork candidate" loading="lazy" />
      </button>
      <figcaption>
        <span className={`confidence ${cluster.confidence_label}`}>{cluster.confidence_label}</span>
        <strong>{cluster.observation_count} match{cluster.observation_count === 1 ? "" : "es"}</strong>
        <small>
          {cluster.sources.join(" · ")} · {cluster.width}×{cluster.height}
        </small>
        <details>
          <summary>Evidence & variants</summary>
          {cluster.observations.map((item, i) => (
            <p className="variant" key={i}>
              <img src={assetImg(item.asset_id)} alt="Variant" />
              <span>
                {item.source}: {item.reason}
                {item.provenance_url && (
                  <> · <a href={item.provenance_url} target="_blank" rel="noreferrer">source ↗</a></>
                )}
              </span>
            </p>
          ))}
        </details>
      </figcaption>
    </figure>
  );
}

export function ImageOrEmpty({ src }: { src: string }) {
  const [failed, setFailed] = useState(false);
  return failed ? <span>No current art</span> : <img src={src} alt="Current artwork" onError={() => setFailed(true)} />;
}
