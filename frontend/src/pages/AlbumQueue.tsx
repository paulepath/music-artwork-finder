import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { AlbumSummary, albumArtwork, api } from "../api";

const STATES = [
  "", "scanned", "suspect_art", "pending_review", "google_only", "needs_tagging",
  "multi_album_parent", "has_art", "applied", "skipped", "error",
];

export function AlbumQueue() {
  const [params, setParams] = useSearchParams();
  const [albums, setAlbums] = useState<AlbumSummary[]>([]);
  const [err, setErr] = useState("");
  const state = params.get("state") ?? "";
  const q = params.get("q") ?? "";
  const offset = Number(params.get("offset") ?? "0");
  const limit = 48;

  useEffect(() => {
    const p: Record<string, string> = {};
    if (state) p.state = state;
    if (q) p.q = q;
    p.limit = String(limit);
    p.offset = String(offset);
    api.albums(p).then(setAlbums).catch((e) => setErr(String(e)));
  }, [state, q, offset]);

  const changePage = (next: number) => {
    const p = new URLSearchParams(params);
    next ? p.set("offset", String(next)) : p.delete("offset");
    setParams(p);
  };

  return (
    <div>
      <div className="card queue-toolbar">
      {err && <p className="err">{err}</p>}
      <div className="row" style={{ marginBottom: 12 }}>
        <select value={state} onChange={(e) => setParams(e.target.value ? { state: e.target.value } : {})}>
          {STATES.map((s) => <option key={s} value={s}>{s || "all states"}</option>)}
        </select>
        <input
          placeholder="search artist / album"
          defaultValue={q}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              const v = (e.target as HTMLInputElement).value;
              const next = new URLSearchParams(params);
              v ? next.set("q", v) : next.delete("q");
              setParams(next);
            }
          }}
        />
        <span className="muted">{albums.length} shown · page {Math.floor(offset / limit) + 1}</span>
        <button disabled={offset === 0} onClick={() => changePage(Math.max(0, offset - limit))}>Previous</button>
        <button disabled={albums.length < limit} onClick={() => changePage(offset + limit)}>Next</button>
      </div>
      <div className="album-gallery">
        {albums.map((a) => <AlbumCard key={a.id} album={a} />)}
      </div>
      </div>
    </div>
  );
}

function AlbumCard({ album: a }: { album: AlbumSummary }) {
  const [missing, setMissing] = useState(false);
  return <Link className="album-card" to={`/albums/${a.id}`}>
    <div className="album-cover">
      {!missing ? <img src={albumArtwork(a.id)} alt={`Artwork for ${a.album || "untitled album"}`} loading="lazy" onError={() => setMissing(true)} /> : <span>no artwork</span>}
    </div>
    <div className="album-card-body">
      <strong>{a.album || "(untitled)"}</strong>
      <span className="muted">{a.album_artist || "unknown artist"}</span>
      <span className="muted">{a.year ?? "—"} · {a.track_count} tracks</span>
      <span><span className="badge state">{a.state.replace(/_/g, " ")}</span>{a.art_dupe_albums > 0 && <span className="err"> ⚠ ×{a.art_dupe_albums}</span>}</span>
    </div>
  </Link>;
}
