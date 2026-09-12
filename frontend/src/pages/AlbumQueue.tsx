import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { AlbumSummary, albumArtwork, api, MergeSuggestion } from "../api";

const STATES = [
  "", "scanned", "suspect_art", "pending_review", "google_only", "needs_tagging",
  "multi_album_parent", "has_art", "applied", "skipped", "error",
];

export function AlbumQueue() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [albums, setAlbums] = useState<AlbumSummary[]>([]);
  const [suggestions, setSuggestions] = useState<MergeSuggestion[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);

  const state = params.get("state") ?? "";
  const q = params.get("q") ?? "";
  const offset = Number(params.get("offset") ?? "0");
  const limit = 48;

  const loadAlbums = useCallback(async () => {
    const p: Record<string, string> = { group_by_merge: "true" };
    if (state) p.state = state;
    if (q) p.q = q;
    p.limit = String(limit);
    p.offset = String(offset);
    try {
      setAlbums(await api.albums(p));
    } catch (e) {
      setErr(String(e));
    }
  }, [state, q, offset]);

  const loadSuggestions = useCallback(async () => {
    try {
      setSuggestions(await api.mergeSuggestions());
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    void loadAlbums();
  }, [loadAlbums]);

  useEffect(() => {
    void loadSuggestions();
  }, [loadSuggestions]);

  const changePage = (next: number) => {
    const p = new URLSearchParams(params);
    if (next) {
      p.set("offset", String(next));
    } else {
      p.delete("offset");
    }
    setParams(p);
  };

  function toggle(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function selectShown() {
    setSelected(new Set(albums.filter((a) => a.state !== "multi_album_parent" && !a.is_loose_tracks).map((a) => a.id)));
  }

  function clearSelection() {
    setSelected(new Set());
  }

  async function handleMerge(sugg: MergeSuggestion) {
    setActionBusy(true);
    try {
      await api.createMerge(sugg.groups.map((g) => g.id), sugg.title);
      await Promise.all([loadSuggestions(), loadAlbums()]);
    } catch (e) {
      setErr(String(e));
    } finally {
      setActionBusy(false);
    }
  }

  async function handleDismiss(sugg: MergeSuggestion) {
    setActionBusy(true);
    try {
      await api.dismissMerge(sugg.groups.map((g) => g.id));
      await loadSuggestions();
    } catch (e) {
      setErr(String(e));
    } finally {
      setActionBusy(false);
    }
  }

  async function startSearchForAlbums() {
    if (selected.size === 0) return;
    setBusy(true);
    setErr("");
    try {
      const res = await api.startSearch({ album_ids: [...selected] });
      navigate(`/search/${res.session_id}`);
    } catch (e) {
      setErr(String(e));
      setBusy(false);
    }
  }

  async function handleFindTracks(folderPath: string) {
    setBusy(true);
    setErr("");
    try {
      const res = await api.startSearch({ folder_path: folderPath, recursive: false });
      navigate(`/search/${res.session_id}`);
    } catch (e) {
      setErr(String(e));
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      {err && <div className="card err">{err}</div>}

      {suggestions.length > 0 && (
        <section className="card" style={{ borderLeft: "4px solid var(--accent)" }}>
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
            <div>
              <strong>Merge suggestions ({suggestions.length})</strong>
              <div className="muted">These look like discs of multi-disc releases. Merge them into a single album.</div>
            </div>
          </div>
          <div className="stack">
            {suggestions.map((sugg) => (
              <div key={sugg.merge_key} className="card" style={{ background: "var(--panel-2)", padding: 12 }}>
                <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                  <div>
                    <strong>{sugg.title}</strong>
                    <span className="muted"> · {sugg.album_artist}</span>
                  </div>
                  <div className="row">
                    <button className="primary" disabled={actionBusy} onClick={() => void handleMerge(sugg)}>
                      Merge
                    </button>
                    <button disabled={actionBusy} onClick={() => void handleDismiss(sugg)}>
                      Dismiss
                    </button>
                  </div>
                </div>
                <ul style={{ margin: "8px 0 0", paddingLeft: 20, fontSize: 13 }} className="muted">
                  {sugg.groups.map((g) => (
                    <li key={g.id}>
                      Disc {g.disc}: {g.album} — <code>{g.common_dir}</code> ({g.track_count} tracks)
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </section>
      )}

      <section className="card queue-toolbar">
        <div className="row" style={{ marginBottom: 12 }}>
          <select
            value={state}
            onChange={(e) => setParams(e.target.value ? { state: e.target.value } : {})}
          >
            {STATES.map((s) => (
              <option key={s} value={s}>
                {s || "all states"}
              </option>
            ))}
          </select>
          <input
            placeholder="search artist / album"
            defaultValue={q}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                const v = (e.target as HTMLInputElement).value;
                const next = new URLSearchParams(params);
                if (v) {
                  next.set("q", v);
                } else {
                  next.delete("q");
                }
                setParams(next);
              }
            }}
          />
          <span className="muted">
            {albums.length} shown · page {Math.floor(offset / limit) + 1}
          </span>
          <button disabled={offset === 0} onClick={() => changePage(Math.max(0, offset - limit))}>
            Previous
          </button>
          <button disabled={albums.length < limit} onClick={() => changePage(offset + limit)}>
            Next
          </button>
          <span className="spacer" />
          <button onClick={selectShown}>Select shown</button>
          {selected.size > 0 && <button onClick={clearSelection}>Clear ({selected.size})</button>}
        </div>

        <div className="album-gallery">
          {albums.map((a) => (
            <AlbumCard
              key={a.id}
              album={a}
              selected={selected.has(a.id)}
              onToggle={toggle}
              onFindTracks={handleFindTracks}
            />
          ))}
        </div>
      </section>

      {selected.size > 0 && (
        <section className="card launchbar">
          <strong>{selected.size} album{selected.size === 1 ? "" : "s"} selected</strong>
          <span className="spacer" />
          <button onClick={clearSelection}>Clear</button>
          <button className="primary" disabled={busy} onClick={() => void startSearchForAlbums()}>
            Search artwork for {selected.size} album{selected.size === 1 ? "" : "s"}
          </button>
        </section>
      )}
    </div>
  );
}

function AlbumCard({
  album: a,
  selected,
  onToggle,
  onFindTracks,
}: {
  album: AlbumSummary;
  selected: boolean;
  onToggle: (id: number) => void;
  onFindTracks?: (folder: string) => void;
}) {
  const [missing, setMissing] = useState(false);
  const isLoose = Boolean(a.is_loose_tracks);
  const folderName = a.common_dir ? a.common_dir.split("/").filter(Boolean).pop() || a.common_dir : "";

  if (isLoose) {
    return (
      <div
        className="album-card"
        style={{
          position: "relative",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          padding: 12,
          minHeight: 240,
          background: "var(--panel-2, rgba(255,255,255,0.02))",
        }}
      >
        <label
          style={{
            position: "absolute",
            top: 8,
            left: 8,
            zIndex: 2,
            background: "#000a",
            borderRadius: 4,
            padding: "3px 6px",
            display: "flex",
            alignItems: "center",
            cursor: "not-allowed",
          }}
          title="Loose tracks get individual track artwork"
        >
          <input type="checkbox" checked={false} disabled={true} />
        </label>
        <div>
          <div style={{ marginTop: 24, marginBottom: 8 }}>
            <strong style={{ fontSize: 15 }}>{folderName || "(loose tracks)"}</strong>
            <div className="muted" style={{ fontSize: 12, wordBreak: "break-all" }}>{a.common_dir}</div>
          </div>
          <p className="muted" style={{ fontSize: 13, marginTop: 12, lineHeight: 1.4 }}>
            {a.track_count} loose track{a.track_count === 1 ? "" : "s"} in this folder — these get individual track artwork
          </p>
          <div className="row" style={{ gap: 4, marginTop: 8 }}>
            <span className="badge state">loose tracks</span>
          </div>
        </div>
        <div style={{ marginTop: 16 }}>
          <button
            className="secondary"
            style={{ fontSize: 13, width: "100%" }}
            onClick={() => onFindTracks?.(a.common_dir)}
          >
            Find artwork for these tracks
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      className="album-card"
      style={{
        position: "relative",
        borderColor: selected ? "var(--accent)" : undefined,
      }}
    >
      <label
        style={{
          position: "absolute",
          top: 8,
          left: 8,
          zIndex: 2,
          background: "#000a",
          borderRadius: 4,
          padding: "3px 6px",
          display: "flex",
          alignItems: "center",
          cursor: a.state === "multi_album_parent" ? "not-allowed" : "pointer",
        }}
        onClick={(e) => e.stopPropagation()}
        title={a.state === "multi_album_parent" ? "Multi-album folders cannot be searched as a single album" : undefined}
      >
        <input
          type="checkbox"
          checked={selected}
          disabled={a.state === "multi_album_parent"}
          onChange={() => onToggle(a.id)}
        />
      </label>
      <Link to={`/albums/${a.id}/artwork`} style={{ display: "block", color: "inherit", textDecoration: "none" }}>
        <div className="album-cover">
          {!missing ? (
            <img
              src={albumArtwork(a.id)}
              alt={`Artwork for ${a.album || "untitled album"}`}
              loading="lazy"
              onError={() => setMissing(true)}
            />
          ) : (
            <span>no artwork</span>
          )}
        </div>
        <div className="album-card-body">
          <strong>{a.album || "(untitled)"}</strong>
          <span className="muted">{a.album_artist || "unknown artist"}</span>
          <span className="muted">
            {a.year ?? "—"} · {a.track_count} tracks
          </span>
          <div className="row" style={{ gap: 4, marginTop: 2 }}>
            <span className="badge state">{a.state.replace(/_/g, " ")}</span>
            {a.discs && a.discs.length > 1 && (
              <span className="badge strong">{a.discs.length} discs</span>
            )}
            {a.art_dupe_albums > 0 && <span className="err"> ⚠ ×{a.art_dupe_albums}</span>}
          </div>
        </div>
      </Link>
    </div>
  );
}
