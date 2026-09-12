import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  api,
  IdentifyCandidate,
  SearchSession,
  SessionAlbums,
} from "../api";
import { AlbumReview } from "../components/AlbumReview";

export function AlbumArtwork() {
  const albumId = Number(useParams().id);
  const [sessionInfo, setSessionInfo] = useState<{ session_id: number; album_query_id: number | null } | null>(null);
  const [session, setSession] = useState<SearchSession | null>(null);
  const [sessionAlbums, setSessionAlbums] = useState<SessionAlbums | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const [identifying, setIdentifying] = useState(false);
  const [identifyCandidates, setIdentifyCandidates] = useState<IdentifyCandidate[] | null>(null);
  const [identifyError, setIdentifyError] = useState("");
  const [applyingIdentity, setApplyingIdentity] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");

    api.albumArtworkSession(albumId)
      .then((res) => {
        if (active) {
          setSessionInfo(res);
          setLoading(false);
        }
      })
      .catch((e: unknown) => {
        if (active) {
          setError(String(e));
          setLoading(false);
        }
      });

    return () => {
      active = false;
    };
  }, [albumId]);

  const refresh = useCallback(async () => {
    if (!sessionInfo) return;
    try {
      const [s, sa] = await Promise.all([
        api.searchSession(sessionInfo.session_id, false),
        api.sessionAlbums(sessionInfo.session_id),
      ]);
      setSession(s);
      setSessionAlbums(sa);
    } catch (e) {
      setError(String(e));
    }
  }, [sessionInfo]);

  useEffect(() => {
    if (!sessionInfo) return;
    void refresh();
    const intervalMs =
      session?.status === "running" || session?.status === "queued" || session?.status === "applying"
        ? 1000
        : 3000;
    const timer = window.setInterval(refresh, intervalMs);
    return () => window.clearInterval(timer);
  }, [refresh, sessionInfo, session?.status]);

  async function saveAlbum(albumQueryId: number, patch: { cluster_id: number | null; approved: boolean }) {
    if (!sessionInfo) return;
    try {
      await api.updateAlbumSelection(sessionInfo.session_id, albumQueryId, patch);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  }

  async function applyArtwork() {
    if (!sessionInfo) return;
    setBusy(true);
    setError("");
    try {
      await api.controlSession(sessionInfo.session_id, "apply");
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function startIdentification() {
    setIdentifying(true);
    setIdentifyError("");
    try {
      const cands = await api.identifyAlbum(albumId);
      setIdentifyCandidates(cands);
    } catch (e) {
      setIdentifyError(String(e));
    } finally {
      setIdentifying(false);
    }
  }

  async function selectIdentity(cand: IdentifyCandidate) {
    setApplyingIdentity(true);
    setIdentifyError("");
    try {
      await api.applyIdentification(albumId, {
        mbid: cand.mbid,
        release_group_id: cand.release_group_id,
        album: cand.title,
        artist: cand.artist,
        write_tags: true,
      });
      const newSession = await api.albumArtworkSession(albumId);
      setSessionInfo(newSession);
      setIdentifyCandidates(null);
      await refresh();
    } catch (e) {
      setIdentifyError(String(e));
    } finally {
      setApplyingIdentity(false);
    }
  }

  if (loading) {
    return (
      <div className="stack">
        <div className="row" style={{ marginBottom: 12 }}>
          <Link to="/albums" className="muted">← All albums</Link>
        </div>
        <div className="card">Loading album artwork search…</div>
      </div>
    );
  }

  if (!sessionInfo && error) {
    return (
      <div className="stack">
        <div className="row" style={{ marginBottom: 12 }}>
          <Link to="/albums" className="muted">← All albums</Link>
          <span className="spacer" />
          <Link to={`/albums/${albumId}`} className="button-link secondary">Legacy view ↗</Link>
        </div>
        <div className="card">
          <h2>Could not start album artwork search</h2>
          <p className="err" style={{ marginTop: 8 }}>{error}</p>
          <p className="muted" style={{ marginTop: 8 }}>
            If this album has missing or incomplete tags, you can identify it with MusicBrainz or search artwork for individual tracks.
          </p>
          <div className="row" style={{ marginTop: 16, gap: 12 }}>
            <button className="primary" onClick={() => void startIdentification()} disabled={identifying}>
              {identifying ? "Searching MusicBrainz…" : "Identify Album"}
            </button>
            <Link to="/search" className="button-link secondary">Search by tracks</Link>
            <Link to="/albums" className="button-link secondary">Back to albums</Link>
          </div>

          {identifyCandidates && (
            <div className="stack" style={{ gap: 12, marginTop: 16 }}>
              <h3>MusicBrainz Matches</h3>
              {identifyCandidates.map((cand) => (
                <div key={cand.mbid} className="card row" style={{ justifyContent: "space-between", alignItems: "center", padding: 12 }}>
                  <div>
                    <strong>{cand.title}</strong>
                    <div className="muted">{cand.artist} · {cand.track_count} tracks · score {cand.score}</div>
                  </div>
                  <button className="primary" onClick={() => void selectIdentity(cand)} disabled={applyingIdentity}>
                    Select & Retag
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  const targetAlbum =
    sessionAlbums?.albums.find((a) => a.album_query_id === sessionInfo?.album_query_id) ||
    sessionAlbums?.albums[0];

  const running =
    session?.status === "queued" ||
    session?.status === "running" ||
    session?.status === "paused";

  return (
    <div className="stack">
      <div className="row" style={{ marginBottom: 12, alignItems: "center" }}>
        <Link to="/albums" className="muted">← All albums</Link>
        <span className="spacer" />
        <button
          onClick={() => void startIdentification()}
          disabled={identifying}
          style={{ marginRight: 8, fontSize: 13 }}
        >
          {identifying ? "Searching MusicBrainz…" : "Identify Album"}
        </button>
        <Link to={`/albums/${albumId}`} className="button-link secondary" style={{ fontSize: 13 }}>
          Legacy view ↗
        </Link>
      </div>

      {error && <p className="err">{error}</p>}

      {identifyCandidates && (
        <section className="card" style={{ marginBottom: 16 }}>
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <h2 style={{ margin: 0 }}>MusicBrainz Album Matches</h2>
            <button onClick={() => setIdentifyCandidates(null)}>Close</button>
          </div>
          {identifyError && <p className="err" style={{ marginTop: 8 }}>{identifyError}</p>}
          <p className="muted" style={{ marginTop: 4 }}>
            Select the matching release. Once identified, artwork recovery will search and cluster covers for this album.
          </p>
          <div className="stack" style={{ gap: 12, marginTop: 12 }}>
            {identifyCandidates.map((cand) => (
              <div
                key={cand.mbid}
                className="card row"
                style={{
                  alignItems: "center",
                  justifyContent: "space-between",
                  padding: 12,
                  background: "var(--bg-subtle, rgba(255,255,255,0.03))",
                }}
              >
                <div className="row" style={{ alignItems: "center", gap: 12 }}>
                  {cand.cover_url && (
                    <img
                      src={cand.cover_url}
                      alt={cand.title}
                      style={{ width: 60, height: 60, objectFit: "cover", borderRadius: 4 }}
                      onError={(e) => { (e.currentTarget as HTMLElement).style.display = "none"; }}
                    />
                  )}
                  <div>
                    <strong>{cand.title}</strong>
                    <div className="muted" style={{ fontSize: 13 }}>
                      {cand.artist} · {cand.year ?? "—"} · {cand.track_count} tracks
                    </div>
                    <div style={{ fontSize: 12, marginTop: 2 }}>
                      <span className="badge strong" style={{ marginRight: 6 }}>
                        {Math.round(cand.score * 100)}% match
                      </span>
                      <span className="muted">
                        {cand.matched_titles}/{cand.total_titles} tracks matched
                      </span>
                    </div>
                  </div>
                </div>
                <button
                  className="primary"
                  disabled={applyingIdentity}
                  onClick={() => void selectIdentity(cand)}
                >
                  {applyingIdentity ? "Applying…" : "Identify as this album"}
                </button>
              </div>
            ))}
            {identifyCandidates.length === 0 && (
              <p className="muted">No candidate releases found on MusicBrainz.</p>
            )}
          </div>
        </section>
      )}

      {running && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
            <span>Searching candidates…</span>
            <span>{Math.round((session?.progress ?? 0) * 100)}%</span>
          </div>
          <div className="progress-bar">
            <div className="progress-fill" style={{ width: `${Math.round((session?.progress ?? 0) * 100)}%` }} />
          </div>
          <p className="muted" style={{ marginTop: 8, fontSize: 13 }}>{session?.message}</p>
        </div>
      )}

      {targetAlbum ? (
        <>
          <AlbumReview album={targetAlbum} onSave={saveAlbum} />

          <section className="card launchbar" style={{ marginTop: 16 }}>
            <div>
              <strong>Apply to album</strong>
              <div className="muted" style={{ fontSize: 13, marginTop: 2 }}>
                will write to {targetAlbum.track_count} track{targetAlbum.track_count === 1 ? "" : "s"}
              </div>
            </div>
            <span className="spacer" />
            {targetAlbum.applied || session?.status === "done" ? (
              <span className="badge exact">applied</span>
            ) : (
              <button
                className="primary"
                disabled={
                  busy ||
                  !targetAlbum.selected_cluster_id ||
                  !targetAlbum.approved ||
                  session?.status === "applying"
                }
                onClick={() => void applyArtwork()}
              >
                {session?.status === "applying" ? "Applying artwork…" : "Apply to album"}
              </button>
            )}
          </section>
        </>
      ) : (
        !running && (
          <div className="card">
            {sessionAlbums?.singles && sessionAlbums.singles.length > 0 ? (
              <div>
                <h2>Untagged folder</h2>
                <p className="muted">
                  These tracks have no album tags, so they were searched as individual singles.
                  Use <strong>Identify Album</strong> to match this folder to a MusicBrainz release and find one cover across all tracks.
                </p>
                <div className="row" style={{ marginTop: 12, gap: 10 }}>
                  <button className="primary" onClick={() => void startIdentification()} disabled={identifying}>
                    {identifying ? "Searching MusicBrainz…" : "Identify Album"}
                  </button>
                  {sessionInfo && (
                    <Link to={`/search/${sessionInfo.session_id}`} className="button-link secondary">
                      View singles search #{sessionInfo.session_id}
                    </Link>
                  )}
                </div>
              </div>
            ) : (
              <div>No album artwork candidates found yet.</div>
            )}
          </div>
        )
      )}
    </div>
  );
}
