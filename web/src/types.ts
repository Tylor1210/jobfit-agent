// Mirrors of the backend models (src/jobfit/schemas.py, src/jobfit/api/*). Keep in sync by hand;
// the API is small enough that codegen would cost more than it saves.

export const STAGES = ["new", "shortlisted", "applied", "interview", "offer", "rejected", "closed"] as const;
export type Stage = (typeof STAGES)[number];
export type Verdict = "strong" | "moderate" | "weak" | "not_a_fit" | "unknown";
export type ReqStatus = "met" | "partial" | "missing";

export interface Task {
  kind: "score" | "tailor" | "review";
  started_at: string;
  status: "running" | "done" | "failed";
  error: string | null;
}

export interface Card {
  id: string;
  company: string;
  title: string;
  location: string | null;
  pay: string | null;
  remote: boolean | null;
  source: string;
  url: string | null;
  stage: Stage;
  status: "open" | "closed";
  fit_score: number | null;
  verdict: Verdict | null;
  top_gap: string | null;
  tailor_status: string | null;
  task: Task | null;
}

export interface Requirement {
  id: string;
  text: string;
  kind: "required" | "preferred";
  category: string;
}

export interface EvidenceRef {
  id: string;
  text: string;
  score: number;
}

export interface Assessment {
  requirement: Requirement;
  status: ReqStatus;
  evidence: EvidenceRef[];
  note: string;
}

export interface Fit {
  fit_score: number;
  verdict: Verdict;
  verdict_line: string;
  assessments: Assessment[];
  top_gaps: string[];
  injection_flags: string[];
  trace_url: string | null;
}

export interface DraftBullet {
  text: string;
  source_ids: string[];
}
export interface DraftJob {
  job_id: string;
  bullets: DraftBullet[];
}
export interface SkillRow {
  label: string;
  skill_ids: string[];
}
export interface Resume {
  summary: string;
  summary_evidence_ids: string[];
  skill_rows: SkillRow[];
  jobs: DraftJob[];
}
export interface Paragraph {
  kind: "opening" | "evidence" | "gap" | "closing";
  text: string;
  evidence_ids: string[];
}
export interface CoverLetter {
  greeting: string;
  paragraphs: Paragraph[];
}
export interface Issue {
  doc: "resume" | "cover_letter";
  rule: string;
  detail: string;
}

export interface JobDetail extends Card {
  description: string;
  posted_at: string | null;
  first_seen: string | null;
  last_seen: string | null;
  closes_at: string | null;
  fit: Fit | null;
  thread_id: string | null;
  drafts: { resume: Resume; cover_letter: CoverLetter } | null;
  issues: Issue[] | null;
  has_resume_pdf: boolean;
  has_cover_pdf: boolean;
  tailor_trace_url: string | null;
}

export interface Gap {
  name: string;
  weight: number;
  jobs: number;
  required_in: number;
  avg_fit: number | null;
  examples: { job_id: string; company: string; title: string; requirement: string }[];
}

export interface SourceStats {
  fetched: number;
  new: number;
  reopened: number;
  closed: number;
  errors: string[];
}

export interface IngestRun {
  id: number;
  started_at: string;
  finished_at: string | null;
  status: string;
  stats: Record<string, SourceStats>;
  prefiltered: number;
  scored: number;
  spend_usd: number;
  notes: string[];
}

export interface EvalSummary {
  run_at: string;
  mode: string;
  verdict_accuracy: number | null;
  verdict_within_one: number | null;
  gap_recall: number | null;
  hallucinated_claims: number;
  first_draft_hallucinations: number;
  injection_passed: number;
  injection_total: number;
  total_cost_usd: number;
  total_latency_ms: number;
  gate_passed: boolean;
}

export interface Ops {
  runs: IngestRun[];
  spend: { day: string; usd: number; input_tokens: number; output_tokens: number; calls: number }[];
  evals: { run_at: string; summary: EvalSummary }[];
  traces: { id: string; company: string; title: string; at: string; kind: string; url: string }[];
  counts: { open: number; closed: number; scored: number; queued: number };
  today_usd: number;
  cap_usd: number;
  provider: string;
}
