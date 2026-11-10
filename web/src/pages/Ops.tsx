// Ops: last ingestion run per source, LLM spend vs the daily cap, eval scores with a trend line,
// and links into Langfuse traces.
import { ExternalLink } from "lucide-react";
import { Link } from "react-router-dom";
import { api, useData } from "../api";
import { ago } from "../components/ui";
import type { EvalSummary, Ops } from "../types";

function Sparkline({ values, max = 1, height = 36, width = 160 }: { values: number[]; max?: number; height?: number; width?: number }) {
  if (values.length < 2) return <span className="small faint">{values.length ? "1 run so far" : "No runs yet"}</span>;
  const step = width / (values.length - 1);
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(height - (v / max) * (height - 4) - 2).toFixed(1)}`);
  return (
    <svg width={width} height={height} role="img" aria-label={`Trend over ${values.length} runs`}>
      <polyline points={pts.join(" ")} fill="none" stroke="var(--accent)" strokeWidth={1.5} />
      {values.map((v, i) => (
        <circle key={i} cx={i * step} cy={height - (v / max) * (height - 4) - 2} r={i === values.length - 1 ? 2.5 : 1.5} fill="var(--accent)" />
      ))}
    </svg>
  );
}

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "n/a" : `${Math.round(v * 100)}%`);

function Spend({ ops }: { ops: Ops }) {
  const ratio = ops.cap_usd ? ops.today_usd / ops.cap_usd : 0;
  const cls = ratio >= 1 ? "full" : ratio >= 0.75 ? "hot" : "";
  const maxDay = Math.max(0.0001, ...ops.spend.map((d) => Number(d.usd)));
  return (
    <div className="panel panel-pad">
      <h3>LLM spend today</h3>
      <div className="row" style={{ alignItems: "baseline", marginTop: 6 }}>
        <span className="num" style={{ fontSize: 22 }}>${ops.today_usd.toFixed(4)}</span>
        <span className="muted">of ${ops.cap_usd.toFixed(2)} cap · {ops.provider}</span>
        {ratio >= 1 && <span className="tag tag-bad">Cap reached, runs stopped</span>}
      </div>
      <div className={`meter ${cls}`}><div style={{ width: `${Math.min(100, ratio * 100)}%` }} /></div>
      <div className="small faint" style={{ marginTop: 6 }}>Groq free tier bills $0; amounts are list price, so the cap still means something.</div>
      <div className="row" style={{ alignItems: "flex-end", gap: 3, height: 44, marginTop: 12 }} aria-label="Spend, last 14 days">
        {ops.spend.map((d) => (
          <div key={d.day} title={`${d.day}: $${Number(d.usd).toFixed(4)}, ${d.calls} calls`}
            style={{ flex: 1, maxWidth: 22, height: `${Math.max(4, (Number(d.usd) / maxDay) * 100)}%`, background: "var(--border-strong)", borderRadius: 2 }} />
        ))}
      </div>
    </div>
  );
}

function Evals({ evals }: { evals: { run_at: string; summary: EvalSummary }[] }) {
  const latest = evals[0]?.summary;
  const series = [...evals].reverse().map((e) => e.summary);
  if (!latest) return <div className="panel panel-pad"><h3>Evals</h3><p className="muted">No eval runs yet. Run <code>make eval</code>.</p></div>;
  return (
    <div className="panel panel-pad stack">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h3>Evals</h3>
        <span className={`tag ${latest.gate_passed ? "tag-ok" : "tag-bad"}`}>Gate {latest.gate_passed ? "passing" : "failing"}</span>
      </div>
      <table>
        <thead><tr><th>Metric</th><th>Latest</th><th>Trend</th></tr></thead>
        <tbody>
          <tr><td>Verdict accuracy</td><td className="num">{pct(latest.verdict_accuracy)}</td><td><Sparkline values={series.map((s) => s.verdict_accuracy ?? 0)} /></td></tr>
          <tr><td>Gap recall</td><td className="num">{pct(latest.gap_recall)}</td><td><Sparkline values={series.map((s) => s.gap_recall ?? 0)} /></td></tr>
          <tr><td>Hallucinated claims (final)</td><td className="num">{latest.hallucinated_claims}</td>
            <td><Sparkline values={series.map((s) => s.hallucinated_claims)} max={Math.max(1, ...series.map((s) => s.hallucinated_claims))} /></td></tr>
          <tr><td>Caught by verify (first drafts)</td><td className="num">{latest.first_draft_hallucinations}</td><td /></tr>
          <tr><td>Injection cases</td><td className="num">{latest.injection_passed}/{latest.injection_total}</td><td /></tr>
          <tr><td>Cost per suite</td><td className="num">${latest.total_cost_usd.toFixed(4)}</td><td className="small muted">{ago(latest.run_at)}</td></tr>
        </tbody>
      </table>
    </div>
  );
}

export default function OpsPage() {
  const { data: ops, error } = useData(api.ops, []);
  if (error) return <div className="page"><div className="notice notice-bad">{error}</div></div>;
  if (!ops) return <div className="page muted">Loading…</div>;
  const last = ops.runs[0];
  return (
    <div className="page">
      <div className="page-head">
        <div><h1>Ops</h1><p>Daily ingestion, spend, eval health, traces.</p></div>
      </div>
      <div className="kpis">
        {[["Open jobs", ops.counts.open], ["Scored", ops.counts.scored], ["Queued to score", ops.counts.queued], ["Closed", ops.counts.closed]].map(([k, v]) => (
          <div className="panel panel-pad kpi" key={k as string}><h3>{k}</h3><div className="value">{v}</div></div>
        ))}
      </div>
      <div className="grid-2" style={{ marginBottom: 16 }}>
        <Spend ops={ops} />
        <Evals evals={ops.evals} />
      </div>

      <div className="panel panel-pad" style={{ marginBottom: 16 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h3>Last ingestion run</h3>
          {last && <span className="small muted">#{last.id} · {ago(last.started_at)} · <span className={`tag ${last.status === "ok" ? "tag-ok" : last.status === "partial" || last.status === "capped" ? "tag-warn" : "tag-bad"}`}>{last.status}</span></span>}
        </div>
        {!last ? <p className="muted">No runs yet. The daily job runs at 11:00 UTC, or run <code>jobfit ingest</code>.</p> : (
          <>
            <table style={{ marginTop: 8 }}>
              <thead><tr><th>Source</th><th>Fetched</th><th>New</th><th>Reopened</th><th>Closed</th><th>Errors</th></tr></thead>
              <tbody>
                {Object.entries(last.stats).map(([src, s]) => (
                  <tr key={src}>
                    <td>{src}</td><td className="num">{s.fetched}</td><td className="num">{s.new}</td>
                    <td className="num">{s.reopened}</td><td className="num">{s.closed}</td>
                    <td>{s.errors.length ? <span className="error small" title={s.errors.join("\n")}>{s.errors.length}: {s.errors[0]}</span> : <span className="faint">0</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="small muted" style={{ marginTop: 8 }}>
              Prefiltered <span className="num">{last.prefiltered}</span> · scored <span className="num">{last.scored}</span> · spent <span className="num">${Number(last.spend_usd).toFixed(4)}</span>
              {last.notes.filter((n) => !n.startsWith("prefilter")).map((n, i) => <div key={i} className="faint">{n}</div>)}
            </div>
            <div className="small faint" style={{ marginTop: 8 }}>
              Earlier: {ops.runs.slice(1, 7).map((r) => `#${r.id} ${r.status} (${r.scored} scored, ${ago(r.started_at)})`).join(" · ") || "none"}
            </div>
          </>
        )}
      </div>

      <div className="panel panel-pad">
        <h3>Recent traces</h3>
        {!ops.traces.length ? <p className="muted">No Langfuse traces yet. Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.</p> : (
          <table style={{ marginTop: 8 }}>
            <tbody>
              {ops.traces.map((t) => (
                <tr key={t.kind + t.id + t.at}>
                  <td style={{ width: 70 }}><span className="tag">{t.kind}</span></td>
                  <td><Link to={`/jobs/${encodeURIComponent(t.id)}`}>{t.company}, {t.title}</Link></td>
                  <td className="small muted">{ago(t.at)}</td>
                  <td><a href={t.url} target="_blank" rel="noreferrer" className="row small" style={{ gap: 4 }}>Langfuse<ExternalLink size={12} /></a></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
