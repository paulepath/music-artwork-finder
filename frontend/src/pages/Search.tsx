import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, LibraryTrack, SearchSelection } from "../api";

type Preview = Awaited<ReturnType<typeof api.previewSearch>>;

export function SearchPage() {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [tracks, setTracks] = useState<LibraryTrack[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [folder, setFolder] = useState("/");
  const [folderMode, setFolderMode] = useState(false);
  const [folders, setFolders] = useState<{ name: string; path: string }[]>([]);
  const [recursive, setRecursive] = useState(true);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [overrides, setOverrides] = useState<Record<number, { title: string; artist: string; album: string; album_artist: string; year: number | null }>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const selection = useMemo<SearchSelection>(() => folderMode
    ? { folder_path: folder, recursive, include_existing: true }
    : { track_ids: [...selected], recursive: true, include_existing: true,
        overrides: [...selected].map((id) => ({ track_id: id, ...overrides[id] })).filter((item) => item.title !== undefined) },
  [folderMode, folder, recursive, selected, overrides]);

  async function loadTracks() {
    try { setTracks(await api.libraryTracks(query ? { q: query, limit: "200" } : { limit: "200" })); }
    catch (e) { setError(String(e)); }
  }

  useEffect(() => { void loadTracks(); }, []);
  useEffect(() => {
    api.folders(folder).then((r) => setFolders(r.folders)).catch((e) => setError(String(e)));
  }, [folder]);
  useEffect(() => { setPreview(null); }, [selection]);

  async function doPreview() {
    setBusy(true); setError("");
    try { setPreview(await api.previewSearch(selection)); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }

  async function start() {
    if (!preview) return;
    if (preview.confirmation_required && !window.confirm(
      `This will search ${preview.track_count} tracks using about ${preview.estimated_query_count} provider queries. Continue?`
    )) return;
    setBusy(true); setError("");
    try {
      const result = await api.startSearch({ ...selection, confirm_large: preview.confirmation_required });
      navigate(`/search/${result.session_id}`);
    } catch (e) { setError(String(e)); setBusy(false); }
  }

  function toggle(id: number) {
    setSelected((current) => {
      const next = new Set(current);
      next.has(id) ? next.delete(id) : next.add(id);
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
  return <div className="stack">
    <section className="card hero">
      <div><h1>Find artwork</h1><p className="muted">Choose tracks or a folder, then compare album and single artwork from every source.</p></div>
      <div className="segmented">
        <button className={!folderMode ? "active" : ""} onClick={() => setFolderMode(false)}>Select tracks</button>
        <button className={folderMode ? "active" : ""} onClick={() => setFolderMode(true)}>Select a folder</button>
      </div>
    </section>

    {error && <div className="card err">{error}</div>}

    {!folderMode ? <section className="card">
      <div className="row searchbar">
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Track, artist, album, or path" onKeyDown={(e) => e.key === "Enter" && void loadTracks()} />
        <button onClick={() => void loadTracks()}>Search</button>
        <span className="muted">{selected.size} selected</span>
        <button onClick={() => setSelected(new Set(tracks.map((track) => track.id)))}>Select shown</button>
        <button onClick={() => setSelected(new Set())}>Clear</button>
      </div>
      <div className="track-picker">
        {tracks.map((track) => <div className="track-pick-wrap" key={track.id}>
          <label className="track-pick">
            <input type="checkbox" checked={selected.has(track.id)} onChange={() => toggle(track.id)} />
            <span><strong>{track.title || "(untitled)"}</strong><small>{track.artist || "unknown artist"} · {track.album || "unknown album"}</small></span>
            <code>{track.path}</code>
          </label>
          {selected.has(track.id) && <details className="search-override"><summary>Edit search terms without changing tags</summary><div className="override-grid">
            <input aria-label="Track title" value={overrides[track.id]?.title ?? track.title} onChange={(e) => edit(track, "title", e.target.value)} placeholder="Track title" />
            <input aria-label="Track artist" value={overrides[track.id]?.artist ?? track.artist} onChange={(e) => edit(track, "artist", e.target.value)} placeholder="Track artist" />
            <input aria-label="Album" value={overrides[track.id]?.album ?? track.album} onChange={(e) => edit(track, "album", e.target.value)} placeholder="Album" />
            <input aria-label="Album artist" value={overrides[track.id]?.album_artist ?? track.album_artist} onChange={(e) => edit(track, "album_artist", e.target.value)} placeholder="Album artist" />
          </div></details>}
        </div>)}
      </div>
    </section> : <section className="card">
      <div className="row folder-head">
        <button disabled={folder === "/"} onClick={() => setFolder(parent)}>↑ Parent</button>
        <code>{folder}</code>
        <label><input type="checkbox" checked={recursive} onChange={(e) => setRecursive(e.target.checked)} /> include nested folders</label>
      </div>
      <div className="folder-grid">
        {folders.map((item) => <button key={item.path} onClick={() => setFolder(item.path)}>📁 {item.name}</button>)}
        {!folders.length && <span className="muted">No child folders</span>}
      </div>
      <p className="muted">The current folder will be searched. Paths are restricted to the mounted music library.</p>
    </section>}

    <section className="card launchbar">
      <button className="primary" disabled={busy || (!folderMode && selected.size === 0)} onClick={() => void doPreview()}>Preview search</button>
      {preview && <>
        <strong>{preview.track_count} tracks</strong>
        <span className="muted">about {preview.estimated_query_count} unique queries · roughly {preview.estimated_minutes} min</span>
        {preview.confirmation_required && <span className="badge fuzzy">large search</span>}
        <button className="primary" disabled={busy || preview.track_count === 0} onClick={() => void start()}>Start artwork search</button>
      </>}
    </section>
  </div>;
}
