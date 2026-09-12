export type Stats = {
  groups_total: number;
  by_state: Record<string, number>;
  missing_art: number;
  last_scan: string | null;
  active_jobs: number;
};

export type Candidate = {
  id: number;
  source: string;
  tier: "exact" | "strong" | "fuzzy" | "review";
  confidence: number;
  reason: string;
  image_url: string;
  provenance_url: string;
  width: number | null;
  height: number | null;
  cluster: number | null;
  cluster_size: number;
};

export type Track = {
  id: number;
  path: string;
  title: string;
  track_no: number | null;
  duration_s: number | null;
  has_embedded_art: boolean;
  disc?: number;
};

export type AuditEntry = {
  id: number;
  group_id: number;
  action: string;
  source: string;
  tier: string | null;
  image_url: string;
  image_sha256: string;
  tracks_written: number;
  wrote_cover_jpg: boolean;
  ok: boolean;
  error: string;
  created_at: string;
  undone_at: string | null;
};

export type AlbumSummary = {
  id: number;
  album_artist: string;
  album: string;
  disc: number;
  year: number | null;
  track_count: number;
  has_embedded_art: boolean;
  has_folder_art: boolean;
  state: string;
  google_enabled: boolean;
  is_compilation: boolean;
  common_dir: string;
  art_dupe_albums: number;
  updated_at: string;
  candidate_count: number;
  best_tier: string | null;
  discs?: number[];
  member_group_ids?: number[];
  is_loose_tracks?: boolean;
};

export type IdentifyCandidate = {
  mbid: string;
  release_group_id: string;
  title: string;
  artist: string;
  year: number | null;
  track_count: number;
  score: number;
  matched_titles: number;
  total_titles: number;
  cover_url: string | null;
};

export type AlbumDetail = AlbumSummary & {
  musicbrainz_albumid: string | null;
  musicbrainz_releasegroupid?: string | null;
  identified_album?: string | null;
  identified_artist?: string | null;
  identified_mbid?: string | null;
  identified_release_group_id?: string | null;
  tracks: Track[];
  candidates: Candidate[];
  audit: AuditEntry[];
};

export type Job = {
  id: number;
  kind: string;
  status: string;
  group_id: number | null;
  progress: number;
  message: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type TrackIssue = {
  track_id: number; path: string; title: string; track_no: number | null;
  album_id: number; album: string; album_artist: string; state: string;
};

export type LibraryTrack = Track & {
  artist: string;
  album: string;
  album_artist: string;
  disc: number;
  year: number | null;
  file_format: string;
  file_size: number;
};

export type ArtworkCluster = {
  id: number;
  score: number;
  confidence_label: "low" | "medium" | "high";
  source_count: number;
  observation_count: number;
  asset_id: number;
  width: number;
  height: number;
  sources: string[];
  observations: { source: string; reason: string; provenance_url: string; asset_id: number }[];
};

export type QueryProgress = {
  id: number;
  status: string;
  message: string;
  google_used: boolean;
  clusters: ArtworkCluster[];
};

export type SearchTarget = {
  id: number;
  track: LibraryTrack;
  status: string;
  stage: string;
  progress: number;
  message: string;
  error: string;
  applied: boolean;
  artwork_role: "album" | "track";
  selected_cluster_id: number | null;
  approved: boolean;
  current_art_url: string;
  search_metadata: { title: string; artist: string; album: string; album_artist: string };
  artwork_query: QueryProgress;
};

export type SessionAlbumTrack = {
  target_id: number;
  track_id: number;
  title: string;
  artist: string;
  disc: number;
  track_no: number | null;
  path: string;
  status: string;
  stage: string;
  progress: number;
  error: string;
  applied: boolean;
};

export type SessionAlbum = {
  album_query_id: number;
  title: string;
  album_artist: string;
  year: number | null;
  track_count: number;
  directories: string[];
  discs: number[];
  status: string;
  progress: number;
  message: string;
  google_used: boolean;
  clusters: ArtworkCluster[];
  selected_cluster_id: number | null;
  approved: boolean;
  applied: boolean;
  current_art_url: string;
  tracks: SessionAlbumTrack[];
};

export type SessionAlbums = {
  albums: SessionAlbum[];
  singles: SearchTarget[];
};

export type MergeSuggestionGroup = {
  id: number;
  album: string;
  disc: number;
  common_dir: string;
  track_count: number;
};

export type MergeSuggestion = {
  merge_key: string;
  title: string;
  album_artist: string;
  groups: MergeSuggestionGroup[];
};

export type SearchSession = {
  id: number;
  status: string;
  selection_kind: string;
  folder_path: string;
  recursive: boolean;
  total_tracks: number;
  completed_tracks: number;
  failed_tracks: number;
  progress: number;
  message: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  targets?: SearchTarget[];
};

export type SearchSelection = {
  track_ids?: number[];
  album_ids?: number[];
  folder_path?: string;
  recursive?: boolean;
  include_existing?: boolean;
  confirm_large?: boolean;
  overrides?: { track_id: number; title: string; artist: string; album: string; album_artist: string; year: number | null }[];
};

export type TrackAudit = {
  id: number; session_id: number; target_id: number; track_id: number;
  action: string; roles: string; ok: boolean; error: string;
  created_at: string; undone_at: string | null; wrote_cover_jpg: boolean;
};

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!r.ok) {
    const body = await r.text();
    throw new Error(`${r.status} ${body}`);
  }
  return r.status === 204 ? (undefined as T) : ((await r.json()) as T);
}

export const api = {
  stats: () => req<Stats>("/api/stats"),
  jobs: (activeOnly = false) => req<Job[]>(`/api/jobs?active_only=${activeOnly}`),
  startScan: () => req<{ job_id: number }>("/api/scan", { method: "POST" }),
  findAll: () => req<{ job_id: number }>("/api/albums/actions/find-all", { method: "POST" }),
  findSuspect: () => req<{ job_id: number }>("/api/albums/actions/find-suspect", { method: "POST" }),
  applyExact: () => req<{ queued: number }>("/api/albums/actions/apply-exact", { method: "POST" }),
  albums: (params: Record<string, string> = {}) =>
    req<AlbumSummary[]>("/api/albums?" + new URLSearchParams(params).toString()),
  album: (id: number) => req<AlbumDetail>(`/api/albums/${id}`),
  albumArtworkSession: (albumId: number) =>
    req<{ session_id: number; album_query_id: number | null }>(`/api/albums/${albumId}/artwork-session`, {
      method: "POST",
    }),
  identifyAlbum: (albumId: number) => req<IdentifyCandidate[]>(`/api/albums/${albumId}/identify`),
  applyIdentification: (albumId: number, body: {
    mbid?: string | null;
    release_group_id?: string | null;
    album: string;
    artist?: string;
    write_tags?: boolean;
  }) => req<{ ok: boolean }>(`/api/albums/${albumId}/identify`, {
    method: "POST",
    body: JSON.stringify(body),
  }),
  trackIssues: (params: Record<string, string> = {}) =>
    req<TrackIssue[]>("/api/albums/tracks/issues?" + new URLSearchParams(params).toString()),
  find: (id: number) => req<{ job_id: number }>(`/api/albums/${id}/find`, { method: "POST" }),
  enableGoogle: (id: number) =>
    req<{ job_id: number | null }>(`/api/albums/${id}/enable-google`, { method: "POST" }),
  skip: (id: number) => req<unknown>(`/api/albums/${id}/skip`, { method: "POST" }),
  apply: (id: number, candidate_id: number, approved: boolean, write_cover_jpg: boolean) =>
    req<{ job_id: number }>(`/api/albums/${id}/apply`, {
      method: "POST",
      body: JSON.stringify({ candidate_id, approved, write_cover_jpg }),
    }),
  undoAlbum: (id: number) => req<unknown>(`/api/albums/${id}/undo`, { method: "POST" }),
  audit: () => req<AuditEntry[]>("/api/audit"),
  undoAudit: (id: number) => req<unknown>(`/api/audit/${id}/undo`, { method: "POST" }),
  maStatus: () => req<{ token_configured: boolean; ws_url: string }>("/api/ma/status"),
  maSync: () => req<{ job_id: number }>("/api/ma/sync", { method: "POST" }),
  libraryTracks: (params: Record<string, string> = {}) =>
    req<LibraryTrack[]>("/api/library/tracks?" + new URLSearchParams(params).toString()),
  folders: (parent = "/") =>
    req<{ parent: string; folders: { name: string; path: string }[] }>(`/api/library/folders?parent=${encodeURIComponent(parent)}`),
  previewSearch: (selection: SearchSelection) =>
    req<{ selection_kind: string; folder_path: string; track_count: number; estimated_query_count: number; estimated_minutes: number; confirmation_required: boolean }>("/api/search-sessions/preview", {
      method: "POST", body: JSON.stringify(selection),
    }),
  startSearch: (selection: SearchSelection) =>
    req<{ session_id: number; job_id: number }>("/api/search-sessions", {
      method: "POST", body: JSON.stringify(selection),
    }),
  sessions: () => req<SearchSession[]>("/api/search-sessions"),
  // The review page polls once a second and reads albums from sessionAlbums(), so it asks for
  // the header only; the targets payload repeats each album's clusters once per member track.
  searchSession: (id: number, includeTargets = true) =>
    req<SearchSession>(`/api/search-sessions/${id}?include_targets=${includeTargets}`),
  sessionAlbums: (id: number) => req<SessionAlbums>(`/api/search-sessions/${id}/albums`),
  updateAlbumSelection: (sessionId: number, albumQueryId: number, body: {
    cluster_id: number | null; approved: boolean;
  }) => req<{ ok: boolean }>(`/api/search-sessions/${sessionId}/albums/${albumQueryId}/selection`, {
    method: "PUT", body: JSON.stringify(body),
  }),
  updateSelection: (sessionId: number, targetId: number, body: {
    cluster_id: number | null; approved: boolean;
  }) => req<{ ok: boolean }>(`/api/search-sessions/${sessionId}/targets/${targetId}/selections`, {
    method: "PUT", body: JSON.stringify(body),
  }),
  approveRecommended: (id: number, minimum_confidence: "low" | "medium" | "high" = "medium") =>
    req<{ approved_targets: number }>(`/api/search-sessions/${id}/approve-recommended`, {
      method: "POST", body: JSON.stringify({ minimum_confidence }),
    }),
  controlSession: (id: number, action: "pause" | "resume" | "cancel" | "retry" | "apply") =>
    req<SearchSession>(`/api/search-sessions/${id}/${action}`, { method: "POST" }),
  mergeSuggestions: () => req<MergeSuggestion[]>("/api/albums/merge-suggestions"),
  createMerge: (group_ids: number[], title?: string) =>
    req<{ id: number; merge_key: string; title: string; album_artist: string; group_ids: number[] }>("/api/albums/merges", {
      method: "POST", body: JSON.stringify({ group_ids, title }),
    }),
  deleteMerge: (mergeId: number) =>
    req<{ ok: boolean; unmerged_group_ids: number[] }>(`/api/albums/merges/${mergeId}`, { method: "DELETE" }),
  dismissMerge: (group_ids: number[]) =>
    req<{ dismissed_count: number }>("/api/albums/merge-suggestions/dismiss", {
      method: "POST", body: JSON.stringify({ group_ids }),
    }),
  trackAudit: () => req<TrackAudit[]>("/api/track-audit"),
  undoTrackAudit: (id: number) => req<{ restored: number }>(`/api/track-audit/${id}/undo`, { method: "POST" }),
};

export const candidateImg = (id: number) => `/api/candidates/${id}/image`;
export const albumArtwork = (id: number) => `/api/albums/${id}/artwork`;
export const assetImg = (id: number) => `/api/assets/${id}/image`;
