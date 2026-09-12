import { NavLink, Route, Routes } from "react-router-dom";
import { Dashboard } from "./pages/Dashboard";
import { AlbumQueue } from "./pages/AlbumQueue";
import { AlbumDetail } from "./pages/AlbumDetail";
import { AlbumArtwork } from "./pages/AlbumArtwork";
import { AuditLog } from "./pages/Audit";
import { TrackIssues } from "./pages/TrackIssues";
import { SearchPage } from "./pages/Search";
import { SearchSessionPage, SearchSessionsPage } from "./pages/SearchSession";

export function App() {
  return (
    <>
      <header className="top">
        <strong>artwork-recovery</strong>
        <nav>
          <NavLink to="/" end>Find artwork</NavLink>
          <NavLink to="/albums">Albums</NavLink>
          <NavLink to="/searches">Search history</NavLink>
          <NavLink to="/maintenance">Maintenance</NavLink>
          <NavLink to="/audit">Audit</NavLink>
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<SearchPage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/searches" element={<SearchSessionsPage />} />
          <Route path="/search/:id" element={<SearchSessionPage />} />
          <Route path="/maintenance" element={<Dashboard />} />
          <Route path="/albums" element={<AlbumQueue />} />
          <Route path="/queue" element={<AlbumQueue />} />
          <Route path="/albums/:id/artwork" element={<AlbumArtwork />} />
          <Route path="/albums/:id" element={<AlbumDetail />} />
          <Route path="/tracks/issues" element={<TrackIssues />} />
          <Route path="/audit" element={<AuditLog />} />
        </Routes>
      </main>
    </>
  );
}
