import { NavLink, Route, Routes } from "react-router-dom";
import { Dashboard } from "./pages/Dashboard";
import { AlbumQueue } from "./pages/AlbumQueue";
import { AlbumDetail } from "./pages/AlbumDetail";
import { AuditLog } from "./pages/Audit";
import { TrackIssues } from "./pages/TrackIssues";

export function App() {
  return (
    <>
      <header className="top">
        <strong>artwork-recovery</strong>
        <nav>
          <NavLink to="/" end>Dashboard</NavLink>
          <NavLink to="/queue">Queue</NavLink>
          <NavLink to="/queue?state=suspect_art">Suspect art</NavLink>
          <NavLink to="/queue?state=needs_tagging">Needs tagging</NavLink>
          <NavLink to="/tracks/issues">Track issues</NavLink>
          <NavLink to="/audit">Audit</NavLink>
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/queue" element={<AlbumQueue />} />
          <Route path="/albums/:id" element={<AlbumDetail />} />
          <Route path="/tracks/issues" element={<TrackIssues />} />
          <Route path="/audit" element={<AuditLog />} />
        </Routes>
      </main>
    </>
  );
}
