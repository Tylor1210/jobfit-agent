import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { Activity, BarChart3, KanbanSquare, Plus } from "lucide-react";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./theme.css";
import { ThemeToggle } from "./components/ui";
import Pipeline from "./pages/Pipeline";
import JobDetailPage from "./pages/JobDetail";
import AddJob from "./pages/AddJob";
import Gaps from "./pages/Gaps";
import OpsPage from "./pages/Ops";

function App() {
  return (
    <BrowserRouter>
      <div className="shell">
        <nav className="nav">
          <div className="brand">
            <span className="brand-mark" />
            Job Fit
          </div>
          <NavLink to="/" end><KanbanSquare size={16} />Pipeline</NavLink>
          <NavLink to="/add"><Plus size={16} />Add job</NavLink>
          <NavLink to="/gaps"><BarChart3 size={16} />Skill gaps</NavLink>
          <NavLink to="/ops"><Activity size={16} />Ops</NavLink>
          <div className="spacer" />
          <ThemeToggle />
        </nav>
        <main className="main">
          <Routes>
            <Route path="/" element={<Pipeline />} />
            <Route path="/jobs/:id" element={<JobDetailPage />} />
            <Route path="/add" element={<AddJob />} />
            <Route path="/gaps" element={<Gaps />} />
            <Route path="/ops" element={<OpsPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
