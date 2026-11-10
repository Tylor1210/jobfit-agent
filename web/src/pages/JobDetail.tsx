// One job: posting, requirement-by-requirement assessment with clickable evidence, and the
// human approval step (Approve and tailor -> review drafts -> approve / edit / send back -> PDFs).
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { AlertTriangle, ArrowLeft, Download, ExternalLink, FileCheck2, Loader2, RotateCcw, Send, Wand2 } from "lucide-react";
import { api, useData } from "../api";
import { ScoreRing, SOURCE_LABEL, StatusTag, VerdictTag, ago } from "../components/ui";
import type { CoverLetter, Issue, JobDetail, Resume } from "../types";

const BUSY_STATUSES = new Set(["drafting", "checking", "rendering"]);
const TAILOR_LABEL: Record<string, string> = {
  drafting: "Drafting resume and cover letter. This takes 2 to 4 minutes on the free model tier.",
  checking: "Re-checking your edits against the evidence.",
  rendering: "Rendering PDFs.",
};

function isBusy(d: JobDetail) {
  return d.task?.status === "running" || BUSY_STATUSES.has(d.tailor_status ?? "");
}

function Evidence({ id, text }: { id: string; text?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span>
      <button className="ev" onClick={() => setOpen(!open)} aria-expanded={open}>{id}</button>
      {open && <div className="ev-pop">{text ?? "Unknown evidence ID"}</div>}
    </span>
  );
}

function Requirements({ job, evidence }: { job: JobDetail; evidence: Record<string, string> }) {
  const fit = job.fit!;
  return (
    <div className="panel">
      <table>
        <thead>
          <tr><th style={{ width: "44%" }}>Requirement</th><th>Status</th><th>Evidence</th></tr>
        </thead>
        <tbody>
          {fit.assessments.map((a) => (
            <tr key={a.requirement.id}>
              <td>
                <div>{a.requirement.text}</div>
                <div className="small faint">{a.requirement.kind} · {a.requirement.category.replace("_", " ")}</div>
              </td>
              <td><StatusTag status={a.status} /></td>
              <td>
                <div className="row" style={{ flexWrap: "wrap", gap: 4 }}>
                  {a.evidence.map((e) => <Evidence key={e.id} id={e.id} text={evidence[e.id] ?? e.text} />)}
                </div>
                {a.note && <div className="small muted" style={{ marginTop: 4 }}>{a.note}</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Cites({ ids }: { ids: string[] }) {
  return ids.length ? <span className="cite">[{ids.join(", ")}]</span> : null;
}

function ResumeEditor({ value, onChange, evidence }: { value: Resume; onChange: (r: Resume) => void; evidence: Record<string, string> }) {
  const setBullet = (ji: number, bi: number, text: string) =>
    onChange({
      ...value,
      jobs: value.jobs.map((j, i) =>
        i !== ji ? j : { ...j, bullets: j.bullets.map((b, k) => (k === bi ? { ...b, text } : b)) }),
    });
  const roleTitle = (id: string) => (evidence[id] ?? id).split(" (")[0];
  return (
    <div className="draft">
      <h4>Summary <Cites ids={value.summary_evidence_ids} /></h4>
      <textarea rows={4} value={value.summary} onChange={(e) => onChange({ ...value, summary: e.target.value })} />
      <h4>Skills</h4>
      {value.skill_rows.map((r) => (
        <div key={r.label} className="small" style={{ marginBottom: 4 }}>
          <b>{r.label}:</b> <span className="muted">{r.skill_ids.map((s) => (evidence[s] ?? s).replace("Skill: ", "")).join(", ")}</span>
        </div>
      ))}
      {value.jobs.map((j, ji) => (
        <div key={j.job_id}>
          <h4>{roleTitle(j.job_id)}</h4>
          {j.bullets.map((b, bi) => (
            <div className="bullet" key={bi}>
              <textarea rows={2} value={b.text} onChange={(e) => setBullet(ji, bi, e.target.value)} />
              <Cites ids={b.source_ids} />
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function LetterEditor({ value, onChange }: { value: CoverLetter; onChange: (c: CoverLetter) => void }) {
  const words = value.paragraphs.reduce((n, p) => n + p.text.split(/\s+/).filter(Boolean).length, 0);
  return (
    <div className="draft">
      <h4>Greeting</h4>
      <input value={value.greeting} onChange={(e) => onChange({ ...value, greeting: e.target.value })} />
      {value.paragraphs.map((p, i) => (
        <div key={i}>
          <h4>{p.kind} <Cites ids={p.evidence_ids} /></h4>
          <textarea
            rows={6} value={p.text}
            onChange={(e) => onChange({
              ...value, paragraphs: value.paragraphs.map((q, k) => (k === i ? { ...q, text: e.target.value } : q)),
            })}
          />
        </div>
      ))}
      <div className={`small ${words < 250 || words > 350 ? "error" : "muted"}`} style={{ marginTop: 6 }}>
        <span className="num">{words}</span> words (250 to 350)
      </div>
    </div>
  );
}

function Issues({ issues }: { issues: Issue[] }) {
  if (!issues.length) return null;
  return (
    <div className="notice notice-warn stack" style={{ gap: 4 }}>
      <div className="row"><AlertTriangle size={15} /><b>The checker could not resolve {issues.length} problem{issues.length > 1 ? "s" : ""}</b></div>
      <div className="small">Edit the text below to fix them, or send it back with a note. Rule breaks (em dashes, banned phrases, tools you have not used, degree claims, page count) block rendering.</div>
      <ul style={{ margin: "4px 0 0", paddingLeft: 18 }} className="small">
        {issues.map((i, k) => <li key={k}><span className="num">{i.doc === "resume" ? "Resume" : "Letter"} · {i.rule}</span>: {i.detail}</li>)}
      </ul>
    </div>
  );
}

function Review({ job, evidence, onAction }: { job: JobDetail; evidence: Record<string, string>; onAction: () => void }) {
  const [resume, setResume] = useState<Resume>(job.drafts!.resume);
  const [letter, setLetter] = useState<CoverLetter>(job.drafts!.cover_letter);
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const dirty = JSON.stringify(resume) !== JSON.stringify(job.drafts!.resume)
    || JSON.stringify(letter) !== JSON.stringify(job.drafts!.cover_letter);

  const act = async (body: Parameters<typeof api.review>[1]) => {
    setSending(true);
    setErr(null);
    try {
      await api.review(job.id, body);
      onAction();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="stack">
      <Issues issues={job.issues ?? []} />
      <div className="grid-2">
        <div className="panel panel-pad"><h2>Resume</h2><ResumeEditor value={resume} onChange={setResume} evidence={evidence} /></div>
        <div className="panel panel-pad"><h2>Cover letter</h2><LetterEditor value={letter} onChange={setLetter} /></div>
      </div>
      <div className="panel panel-pad stack">
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
          <div className="row">
            <button className="btn-primary" disabled={sending || dirty} onClick={() => act({ action: "approve" })}
              title={dirty ? "Save your edits first" : undefined}>
              <FileCheck2 size={15} />Approve and render PDFs
            </button>
            <button disabled={sending || !dirty} onClick={() => act({ action: "edit", resume, cover_letter: letter })}>
              Save edits and re-check
            </button>
            {dirty && <button className="btn-ghost" onClick={() => { setResume(job.drafts!.resume); setLetter(job.drafts!.cover_letter); }}>
              <RotateCcw size={14} />Discard edits
            </button>}
          </div>
        </div>
        <div>
          <label htmlFor="note">Send back with a note</label>
          <div className="row">
            <input id="note" value={note} placeholder="e.g. Lead with the Fairman migration; drop the trading engine"
              onChange={(e) => setNote(e.target.value)} />
            <button disabled={sending || !note.trim()} onClick={() => act({ action: "revise", note })}><Send size={14} />Send back</button>
          </div>
        </div>
        {err && <div className="error">{err}</div>}
      </div>
    </div>
  );
}

function Tailoring({ job, evidence, restart }: { job: JobDetail; evidence: Record<string, string>; restart: () => void }) {
  const [err, setErr] = useState<string | null>(null);
  const status = job.tailor_status;
  const start = async () => {
    setErr(null);
    try {
      await api.tailor(job.id);
      restart();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  if (isBusy(job) && status) {
    return <div className="notice"><Loader2 size={16} className="spin" />{TAILOR_LABEL[status] ?? "Working…"}</div>;
  }
  if (status === "awaiting_review" && job.drafts) {
    // key: remount editors with fresh state whenever the server sends new drafts
    return <Review key={`${job.thread_id ?? ""}:${JSON.stringify(job.drafts)}`} job={job} evidence={evidence} onAction={restart} />;
  }
  return (
    <div className="stack">
      {status === "rendered" && (
        <div className="panel panel-pad row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
          <div><b>PDFs ready.</b> <span className="muted">Both passed every check and fit on one page.</span></div>
          <div className="row">
            {job.has_resume_pdf && <a className="btn" href={api.pdfUrl(job.id, "resume")}><Download size={14} />Resume PDF</a>}
            {job.has_cover_pdf && <a className="btn" href={api.pdfUrl(job.id, "cover")}><Download size={14} />Cover letter PDF</a>}
          </div>
        </div>
      )}
      {status === "failed" && <div className="notice notice-bad">Tailoring failed: {job.issues?.[0]?.detail ?? job.task?.error ?? "unknown error"}</div>}
      {job.task?.status === "failed" && job.task.kind !== "score" && status !== "failed" && (
        <div className="notice notice-bad">Last run failed: {job.task.error}</div>
      )}
      <div className="row">
        <button className="btn-primary" onClick={start} disabled={!job.fit || job.fit.verdict === "unknown"}>
          <Wand2 size={15} />{status ? "Tailor again" : "Approve and tailor"}
        </button>
        <span className="small muted">Writes a resume and cover letter from your evidence, checks every claim, then waits for your review.</span>
      </div>
      {err && <div className="error">{err}</div>}
    </div>
  );
}

export default function JobDetailPage() {
  const { id = "" } = useParams();
  const { data: job, error, restart } = useData(() => api.job(id), [id], { shouldPoll: isBusy });
  const { data: evidence } = useData(api.evidence, []);
  const [showPosting, setShowPosting] = useState(false);

  if (error) return <div className="page"><div className="notice notice-bad">{error}</div></div>;
  if (!job) return <div className="page muted">Loading…</div>;
  const scoring = job.task?.kind === "score" && job.task.status === "running";
  const fit = job.fit;

  return (
    <div className="page stack" style={{ gap: 18 }}>
      <Link to="/" className="small row muted"><ArrowLeft size={14} />Pipeline</Link>
      <div className="panel panel-pad row" style={{ gap: 18, alignItems: "flex-start" }}>
        <ScoreRing score={fit?.fit_score ?? null} verdict={fit?.verdict} size={64} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row" style={{ flexWrap: "wrap" }}>
            <h1>{job.title}</h1>
            <VerdictTag verdict={fit?.verdict ?? null} />
            {job.status === "closed" && <span className="tag tag-bad">Posting closed</span>}
          </div>
          <div className="muted" style={{ marginTop: 2 }}>
            {job.company}{job.location ? ` · ${job.location}` : ""}{job.pay ? ` · ${job.pay}` : ""}
          </div>
          {fit && <p style={{ margin: "10px 0 0" }}>{fit.verdict_line}</p>}
          <div className="row small faint" style={{ marginTop: 10, flexWrap: "wrap", gap: 14 }}>
            <span>{SOURCE_LABEL[job.source] ?? job.source}</span>
            <span>First seen {ago(job.first_seen)}</span>
            <span>Stage: {job.stage}</span>
            {job.url && <a href={job.url} target="_blank" rel="noreferrer" className="row" style={{ gap: 4 }}>Original posting<ExternalLink size={12} /></a>}
            {fit?.trace_url && <a href={fit.trace_url} target="_blank" rel="noreferrer">Fit trace</a>}
            {job.tailor_trace_url && <a href={job.tailor_trace_url} target="_blank" rel="noreferrer">Tailor trace</a>}
          </div>
        </div>
      </div>

      {fit && fit.injection_flags.length > 0 && (
        <div className="notice notice-warn">
          <AlertTriangle size={16} />
          <div>
            <b>This posting contained text aimed at AI screeners.</b> It was removed before scoring and could not affect the score.
            <ul className="small" style={{ margin: "4px 0 0", paddingLeft: 18 }}>{fit.injection_flags.map((f, i) => <li key={i}>{f}</li>)}</ul>
          </div>
        </div>
      )}

      {scoring && <div className="notice"><Loader2 size={16} className="spin" />Scoring this job against your evidence…</div>}
      {job.task?.kind === "score" && job.task.status === "failed" && (
        <div className="notice notice-bad">Scoring failed: {job.task.error} <button onClick={async () => { await api.rescore(job.id); restart(); }}>Retry</button></div>
      )}

      {fit && fit.verdict !== "unknown" && (
        <>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h3>Requirements</h3>
            {fit.top_gaps.length > 0 && <span className="small muted">Top gaps: {fit.top_gaps.join("; ")}</span>}
          </div>
          <Requirements job={job} evidence={evidence ?? {}} />
          <h3>Tailored application</h3>
          <Tailoring job={job} evidence={evidence ?? {}} restart={restart} />
        </>
      )}

      <div>
        <button className="btn-ghost" onClick={() => setShowPosting(!showPosting)}>{showPosting ? "Hide posting" : "Show posting"}</button>
        {showPosting && <div className="panel panel-pad posting" style={{ marginTop: 8 }}>{job.description}</div>}
      </div>
    </div>
  );
}
