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
};

export type AlbumDetail = AlbumSummary & {
  musicbrainz_albumid: string | null;
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
};

export const candidateImg = (id: number) => `/api/candidates/${id}/image`;
export const albumArtwork = (id: number) => `/api/albums/${id}/artwork`;
