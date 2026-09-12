import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AlbumSummary, albumArtwork, api, LibraryTrack, SearchSelection } from "../api";

type Preview = Awaited<ReturnType<typeof api.previewSearch>>;
type SearchMode = "tracks" | "folder" | "albums";

export function SearchPage() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<SearchMode>("tracks");
  const [query, setQuery] = useState("");
  const [tracks, setTracks] = useState<LibraryTrack[]>([]);
  const [selectedTracks, setSelectedTracks] = useState<Set<number>>(new Set());

  const [albumQuery, setAlbumQuery] = useState("");
  const [albums, setAlbums] = useState<AlbumSummary[]>([]);
  const [selectedAlbums, setSelectedAlbums] = useState<Set<number>>(new Set());
  const [hideLoose, setHideLoose] = useState(true);

  const [folder, setFolder] = useState("/");
  const [folders, setFolders] = useState<{ name: string; path: string }[]>([]);
  const [recursive, setRecursive] = useState(true);

  const [preview, setPreview] = useState<Preview | null>(null);
  const [overrides, setOverrides] = useState<
    Record<number, { title: string; artist: string; album: string; album_artist: string; year: number | null }>
  >({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const selection = useMemo<SearchSelection>(() => {
    if (mode === "folder") {
      return { folder_path: folder, recursive, include_existing: true };
    }
    if (mode === "albums") {
      return { album_ids: [...selectedAlbums], recursive: true, include_existing: true };
    }
    return {
      track_ids: [...selectedTracks],
      recursive: true,
      include_existing: true,
      overrides: [...selectedTracks]
        .map((id) => ({ track_id: id, ...overrides[id] }))
        .filter((item) => item.title !== undefined),
    };
  }, [mode, folder, recursive, selectedAlbums, selectedTracks, overrides]);

  const loadTracks = useCallback(async () => {
    try {
      setTracks(await api.libraryTracks(query ? { q: query, limit: "200" } : { limit: "200" }));
    } catch (e) {
      setError(String(e));
    }
  }, [query]);

  const loadAlbums = useCallback(async () => {
    try {
      const p: Record<string, string> = { group_by_merge: "true", limit: "100" };
      if (albumQuery) p.q = albumQuery;
      if (hideLoose) p.exclude_loose = "true";
      setAlbums(await api.albums(p));
    } catch (e) {
      setError(String(e));
    }
  }, [albumQuery, hideLoose]);

  useEffect(() => {
    void loadTracks();
  }, [loadTracks]);

  useEffect(() => {
    if (mode === "albums") {
      void loadAlbums();
    }
  }, [mode, loadAlbums]);

  useEffect(() => {
    api.folders(folder).then((r) => setFolders(r.folders)).catch((e) => setError(String(e)));
  }, [folder]);

  useEffect(() => {
    setPreview(null);
  }, [selection]);

  async function doPreview() {
    setBusy(true);
    setError("");
    try {
      setPreview(await api.previewSearch(selection));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function start() {
    if (!preview) return;
    if (
      preview.confirmation_required &&
      !window.confirm(
        `This will search ${preview.track_count} tracks using about ${preview.estimated_query_count} provider queries. Continue?`
      )
    )
      return;
    setBusy(true);
    setError("");
    try {
      const result = await api.startSearch({ ...selection, confirm_large: preview.confirmation_required });
      navigate(`/search/${result.session_id}`);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  function toggleTrack(id: number) {
    setSelectedTracks((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function toggleAlbum(id: number) {
    setSelectedAlbums((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function edit(track: LibraryTrack, field: "title" | "artist" | "album" | "album_artist", value: string) {
    setOverrides((current) => ({
      ...current,
      [track.id]: {
        title: current[track.id]?.title ?? track.title,
        artist: current[track.id]?.artist ?? track.artist,
        album: current[track.id]?.album ?? track.album,
        album_artist: current[track.id]?.album_artist ?? track.album_artist,
        year: current[track.id]?.year ?? track.year,
        [field]: value,
      },
    }));
  }

  const parent = folder === "/" ? "/" : folder.slice(0, folder.lastIndexOf("/")) || "/";
  return (
    <div className="stack">
      <section className="card hero">
        <div>
          <h1>Find artwork</h1>
          <p className="muted">Choose tracks, albums, or a folder, then review and apply artwork.</p>
        </div>
        <div className="segmented">
          <button className={mode === "tracks" ? "active" : ""} onClick={() => setMode("tracks")}>
            Select tracks
          </button>
          <button className={mode === "albums" ? "active" : ""} onClick={() => setMode("albums")}>
            Select albums
          </button>
          <button className={mode === "folder" ? "active" : ""} onClick={() => setMode("folder")}>
            Select a folder
          </button>
        </div>
      </section>

      {error && <div className="card err">{error}</div>}

      {mode === "tracks" && (
        <section className="card">
          <div className="row searchbar">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Track, artist, album, or path"
              onKeyDown={(e) => e.key === "Enter" && void loadTracks()}
            />
            <button onClick={() => void loadTracks()}>Search</button>
            <span className="muted">{selectedTracks.size} selected</span>
            <button onClick={() => setSelectedTracks(new Set(tracks.map((track) => track.id)))}>
              Select shown
            </button>
            <button onClick={() => setSelectedTracks(new Set())}>Clear</button>
          </div>
          <div className="track-picker">
            {tracks.map((track) => (
              <div className="track-pick-wrap" key={track.id}>
                <label className="track-pick">
                  <input
                    type="checkbox"
                    checked={selectedTracks.has(track.id)}
                    onChange={() => toggleTrack(track.id)}
                  />
                  <span>
                    <strong>{track.title || "(untitled)"}</strong>
                    <small>
                      {track.artist || "unknown artist"} · {track.album || "unknown album"}
                    </small>
                  </span>
                  <code>{track.path}</code>
                </label>
                {selectedTracks.has(track.id) && (
                  <details className="search-override">
                    <summary>Edit search terms without changing tags</summary>
                    <div className="override-grid">
                      <input
                        aria-label="Track title"
                        value={overrides[track.id]?.title ?? track.title}
                        onChange={(e) => edit(track, "title", e.target.value)}
                        placeholder="Track title"
                      />
                      <input
                        aria-label="Track artist"
                        value={overrides[track.id]?.artist ?? track.artist}
                        onChange={(e) => edit(track, "artist", e.target.value)}
                        placeholder="Track artist"
                      />
                      <input
                        aria-label="Album"
                        value={overrides[track.id]?.album ?? track.album}
                        onChange={(e) => edit(track, "album", e.target.value)}
                        placeholder="Album"
                      />
                      <input
                        aria-label="Album artist"
                        value={overrides[track.id]?.album_artist ?? track.album_artist}
                        onChange={(e) => edit(track, "album_artist", e.target.value)}
                        placeholder="Album artist"
                      />
                    </div>
                  </details>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {mode === "albums" && (
        <section className="card">
          <div className="row searchbar" style={{ flexWrap: "wrap", gap: 10, alignItems: "center" }}>
            <input
              value={albumQuery}
              onChange={(e) => setAlbumQuery(e.target.value)}
              placeholder="Search artist or album"
              onKeyDown={(e) => e.key === "Enter" && void loadAlbums()}
              style={{ minWidth: 220 }}
            />
            <button onClick={() => void loadAlbums()}>Search</button>
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", fontSize: 13, userSelect: "none" }}>
              <input
                type="checkbox"
                checked={hideLoose}
                onChange={(e) => setHideLoose(e.target.checked)}
              />
              Hide loose folders
            </label>
            <span className="spacer" />
            <span className="muted" style={{ fontSize: 13 }}>
              {selectedAlbums.size > 0 ? `${selectedAlbums.size} selected for batch` : "Click an album to discover artwork"}
            </span>
            {selectedAlbums.size > 0 && (
              <>
                <button onClick={() => setSelectedAlbums(new Set(albums.map((a) => a.id)))}>
                  Select shown
                </button>
                <button onClick={() => setSelectedAlbums(new Set())}>Clear</button>
              </>
            )}
          </div>
          <div className="album-gallery" style={{ marginTop: 14 }}>
            {albums.map((a) => (
              <div
                key={a.id}
                className="album-card"
                style={{
                  position: "relative",
                  borderColor: selectedAlbums.has(a.id) ? "var(--accent)" : undefined,
                  cursor: "pointer",
                }}
                onClick={() => navigate(`/albums/${a.id}/artwork`)}
                title={`Open artwork discovery for ${a.album || "this album"}`}
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
                    cursor: "pointer",
                  }}
                  onClick={(e) => e.stopPropagation()}
                  title="Select for batch search"
                >
                  <input
                    type="checkbox"
                    checked={selectedAlbums.has(a.id)}
                    onChange={() => toggleAlbum(a.id)}
                  />
                </label>
                <div className="album-cover">
                  <AlbumCoverThumbnail albumId={a.id} albumTitle={a.album} />
                </div>
                <div className="album-card-body">
                  <strong>{a.album || (a.common_dir ? a.common_dir.split("/").filter(Boolean).pop() : "(untitled)")}</strong>
                  <span className="muted">{a.album_artist || "unknown artist"}</span>
                  <span className="muted">
                    {a.year ?? "—"} · {a.track_count} tracks
                  </span>
                  <div className="row" style={{ gap: 4, marginTop: 2 }}>
                    <span className="badge state">{a.state.replace(/_/g, " ")}</span>
                    {a.discs && a.discs.length > 1 && (
                      <span className="badge strong">{a.discs.length} discs</span>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {mode === "folder" && (
        <section className="card">
          <div className="row folder-head">
            <button disabled={folder === "/"} onClick={() => setFolder(parent)}>
              ↑ Parent
            </button>
            <code>{folder}</code>
            <label>
              <input
                type="checkbox"
                checked={recursive}
                onChange={(e) => setRecursive(e.target.checked)}
              />{" "}
              include nested folders
            </label>
          </div>
          <div className="folder-grid">
            {folders.map((item) => (
              <button key={item.path} onClick={() => setFolder(item.path)}>
                📁 {item.name}
              </button>
            ))}
            {!folders.length && <span className="muted">No child folders</span>}
          </div>
          <p className="muted">
            The current folder will be searched. Paths are restricted to the mounted music library.
          </p>
        </section>
      )}

      {((mode === "tracks" && selectedTracks.size > 0) ||
        (mode === "albums" && selectedAlbums.size > 0) ||
        mode === "folder") && (
        <section className="card launchbar">
          {mode === "albums" && (
            <div style={{ marginRight: 12 }}>
              <strong>Batch search:</strong> <span className="muted">{selectedAlbums.size} album{selectedAlbums.size === 1 ? "" : "s"}</span>
            </div>
          )}
          <button
            className="primary"
            disabled={
              busy ||
              (mode === "tracks" && selectedTracks.size === 0) ||
              (mode === "albums" && selectedAlbums.size === 0)
            }
            onClick={() => void doPreview()}
          >
            Preview search
          </button>
          {preview && (
            <>
              <strong>{preview.track_count} tracks</strong>
              <span className="muted">
                about {preview.estimated_query_count} unique queries · roughly {preview.estimated_minutes} min
              </span>
              {preview.confirmation_required && <span className="badge fuzzy">large search</span>}
              <button
                className="primary"
                disabled={busy || preview.track_count === 0}
                onClick={() => void start()}
              >
                Start artwork search
              </button>
            </>
          )}
        </section>
      )}
    </div>
  );
}

function AlbumCoverThumbnail({ albumId, albumTitle }: { albumId: number; albumTitle: string }) {
  const [missing, setMissing] = useState(false);
  return !missing ? (
    <img
      src={albumArtwork(albumId)}
      alt={`Artwork for ${albumTitle || "untitled album"}`}
      loading="lazy"
      onError={() => setMissing(true)}
    />
  ) : (
    <span>no artwork</span>
  );
}
