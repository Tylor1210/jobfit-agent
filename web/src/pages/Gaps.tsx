// Ranked skill gaps across scored jobs: what to learn next.
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, useData } from "../api";
import type { Gap } from "../types";

const FILTERS = [
  { label: "All scored jobs", min: 0 },
  { label: "Jobs scoring 55+", min: 55 },
  { label: "Jobs scoring 75+", min: 75 },
];

export default function Gaps() {
  const [minFit, setMinFit] = useState(0);
  const [soft, setSoft] = useState(false);
  const { data, error } = useData(() => api.gaps(minFit, soft), [minFit, soft]);
  const [open, setOpen] = useState<string | null>(null);
  const top = (data ?? []).slice(0, 20);
  const max = Math.max(1, ...top.map((g) => g.weight));

  return (
    <div className="page" style={{ maxWidth: 1000 }}>
      <div className="page-head">
        <div>
          <h1>Skill gaps</h1>
          <p>Missing or partial requirements across scored jobs, ranked by how often they cost you. A missing required skill counts 1; partial or preferred counts less.</p>
        </div>
        <div className="row">
          <label className="row small" style={{ margin: 0, whiteSpace: "nowrap" }}>
            <input type="checkbox" checked={soft} onChange={(e) => setSoft(e.target.checked)} style={{ width: "auto" }} />
            Include soft skills
          </label>
          <select value={minFit} onChange={(e) => setMinFit(Number(e.target.value))} style={{ width: 180 }} aria-label="Filter jobs">
            {FILTERS.map((f) => <option key={f.min} value={f.min}>{f.label}</option>)}
          </select>
        </div>
      </div>
      {error && <div className="notice notice-bad">{error}</div>}
      {data && !data.length && <div className="notice">No gaps yet. Score some jobs first.</div>}
      <div className="panel panel-pad">
        {top.map((g: Gap) => (
          <div key={g.name}>
            <div className="bar-row">
              <button className="link" onClick={() => setOpen(open === g.name ? null : g.name)} aria-expanded={open === g.name}>
                {g.name}
              </button>
              <div className="bar-track"><div className="bar-fill" style={{ width: `${(g.weight / max) * 100}%` }} /></div>
              <div className="num small muted" style={{ textAlign: "right" }}>
                {g.jobs} job{g.jobs > 1 ? "s" : ""}
              </div>
            </div>
            {open === g.name && (
              <div className="small" style={{ margin: "2px 0 10px", paddingLeft: 12, borderLeft: "2px solid var(--border)" }}>
                <div className="muted" style={{ marginBottom: 4 }}>
                  Required in {g.required_in} · average fit of these jobs <span className="num">{g.avg_fit ?? "n/a"}</span>
                </div>
                {g.examples.map((ex) => (
                  <div key={ex.job_id + ex.requirement}>
                    <Link to={`/jobs/${encodeURIComponent(ex.job_id)}`}>{ex.company}, {ex.title}</Link>
                    <span className="faint">: {ex.requirement}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
