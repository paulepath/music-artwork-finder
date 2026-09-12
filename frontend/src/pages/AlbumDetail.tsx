import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { AlbumDetail as Detail, Candidate, api, candidateImg } from "../api";

export function AlbumDetail() {
  const { id } = useParams();
  const albumId = Number(id);
  const [album, setAlbum] = useState<Detail | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [writeCover, setWriteCover] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(() => {
    api.album(albumId).then(setAlbum).catch((e) => setErr(String(e)));
  }, [albumId]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [refresh]);

  if (err) return <div className="card err">{err}</div>;
  if (!album) return <div className="card">loading…</div>;

  const act = (fn: () => Promise<unknown>) => async () => {
    setBusy(true);
    try { await fn(); await refresh(); } catch (e) { setErr(String(e)); } finally { setBusy(false); }
  };

  const sel = album.candidates.find((c) => c.id === selected);
  const needsApproval = sel ? sel.tier === "fuzzy" || sel.tier === "review" : false;
  const google = album.candidates.filter((c) => c.source === "google");
  const trusted = album.candidates.filter((c) => c.source !== "google");

  return (
    <div className="row" style={{ flexDirection: "column", alignItems: "stretch" }}>
      <div className="card">
        <h2 style={{ margin: "0 0 4px" }}>{album.album || "(untitled)"}</h2>
        <div className="muted">
          {album.album_artist || "unknown artist"} · {album.track_count} tracks
          {album.year ? ` · ${album.year}` : ""} · <span className="badge state">{album.state}</span>
        </div>
        <p className="muted" style={{ fontSize: 12 }}>{album.common_dir}</p>
        <div className="row">
          <button onClick={act(() => api.find(albumId))} disabled={busy}>Re-search trusted sources</button>
          {!album.google_enabled ? (
            <button onClick={act(() => api.enableGoogle(albumId))} disabled={busy}>
              Enable Google Images (last resort)
            </button>
          ) : (
            <button onClick={act(() => api.enableGoogle(albumId))} disabled={busy}>Re-run Google search</button>
          )}
          <button onClick={act(() => api.skip(albumId))} disabled={busy}>Skip</button>
          <button className="danger" onClick={act(() => api.undoAlbum(albumId))} disabled={busy}>
            Undo last apply
          </button>
        </div>
      </div>

      {album.state === "multi_album_parent" && (
        <div className="card err">
          This folder holds multiple albums — excluded from batch artwork operations. Fix tags / split in Picard.
        </div>
      )}
      {album.state === "suspect_art" && (
        <div className="card err">
          This album's embedded cover is byte-identical to {album.art_dupe_albums} other unrelated
          album{album.art_dupe_albums === 1 ? "" : "s"} — likely a bad bulk embed. Re-search and replace it.
        </div>
      )}
      {album.state === "needs_tagging" && (
        <div className="card">Missing album tags — tag this in Picard first, then rescan.</div>
      )}

      <div className="card">
        <h3>Candidates {trusted.length + google.length === 0 && <span className="muted">— none yet</span>}</h3>
        <CandidateGrid
          candidates={trusted}
          selected={selected}
          onSelect={setSelected}
        />
        {google.length > 0 && (
          <>
            <h4>Google Images — pick the consensus cover (clusters largest first)</h4>
            <p className="muted">
              Grouped by visual similarity; the biggest group is usually the real cover. Verify before applying.
            </p>
            <CandidateGrid candidates={google} selected={selected} onSelect={setSelected} showCluster />
          </>
        )}
      </div>

      {sel && (
        <div className="card">
          <h3>Apply</h3>
          <div className="compare">
            <figure>
              <img src={candidateImg(sel.id)} alt="proposed" />
              <figcaption className="muted">
                proposed · {sel.source} · <span className={`badge ${sel.tier}`}>{sel.tier}</span>{" "}
                {Math.round(sel.confidence * 100)}%
              </figcaption>
            </figure>
          </div>
          <p className="muted">{sel.reason}</p>
          {sel.provenance_url && <p><a href={sel.provenance_url} target="_blank">source page ↗</a></p>}
          <label className="row" style={{ gap: 6 }}>
            <input type="checkbox" checked={writeCover} onChange={(e) => setWriteCover(e.target.checked)} />
            also write cover.jpg in the album folder
          </label>
          <div className="row" style={{ marginTop: 10 }}>
            <button
              className="primary"
              disabled={busy}
              onClick={act(() => api.apply(albumId, sel.id, needsApproval, writeCover))}
            >
              {needsApproval ? "Approve & embed into all tracks" : "Embed into all tracks"}
            </button>
            {needsApproval && <span className="muted">requires explicit approval ({sel.tier})</span>}
          </div>
        </div>
      )}

      <div className="card">
        <h3>Tracks</h3>
        <table>
          <thead><tr><th>#</th><th>Title</th><th>Art</th><th>Path</th></tr></thead>
          <tbody>
            {album.tracks.map((t) => (
              <tr key={t.id}>
                <td>{t.track_no ?? ""}</td>
                <td>{t.title}</td>
                <td>{t.has_embedded_art ? "🖼" : <span className="err">none</span>}</td>
                <td className="muted" style={{ fontSize: 12 }}>{t.path}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {album.audit.length > 0 && (
        <div className="card">
          <h3>History</h3>
          <table>
            <thead><tr><th>When</th><th>Action</th><th>Source</th><th>Tracks</th><th>Result</th></tr></thead>
            <tbody>
              {album.audit.map((a) => (
                <tr key={a.id}>
                  <td className="muted">{new Date(a.created_at).toLocaleString()}</td>
                  <td>{a.action}{a.undone_at ? " (undone)" : ""}</td>
                  <td>{a.source}</td>
                  <td>{a.tracks_written}</td>
                  <td className={a.ok ? "" : "err"}>{a.ok ? "ok" : a.error}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function CandidateGrid(props: {
  candidates: Candidate[];
  selected: number | null;
  onSelect: (id: number) => void;
  showCluster?: boolean;
}) {
  return (
    <div className="cands">
      {props.candidates.map((c) => (
        <div key={c.id} className={"cand" + (props.selected === c.id ? " selected" : "")}>
          <img src={candidateImg(c.id)} alt={c.source} onClick={() => props.onSelect(c.id)} loading="lazy" />
          <div className="meta">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className={`badge ${c.tier}`}>{c.tier}</span>
              <span className="muted">{c.source}</span>
            </div>
            {props.showCluster && (
              <div className="muted">cluster {c.cluster} · {c.cluster_size} matches</div>
            )}
            <div className="reason">{c.reason}</div>
            <div className="muted">{c.width}×{c.height}</div>
            <button onClick={() => props.onSelect(c.id)}>Select</button>
          </div>
        </div>
      ))}
    </div>
  );
}
