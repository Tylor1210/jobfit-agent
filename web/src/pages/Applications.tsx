// Applications: everything sent, found from email or logged by hand. Quick-add stays on top
// (one line -> preview -> confirm; built for a phone), then filters, the table, a detail drawer
// with the event timeline, the needs-review queue, and mail account status.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Inbox, Loader2, Mail, PenLine, RefreshCw, Sparkles, X } from "lucide-react";
import { api, useData } from "../api";
import { ago } from "../components/ui";
import {
  APP_STATUSES, type Application, type ApplicationDetail, type AppEvent, type AppSource, type AppStatus,
  type Channel, type MailStatus, type QuickAddDraft,
} from "../types";

const STATUS_CLS: Record<AppStatus, string> = {
  applied: "", screening: "tag-accent", interview: "tag-accent", offer: "tag-ok",
  rejected: "tag-bad", withdrawn: "", ghosted: "tag-warn",
};
const SOURCE_LABEL: Record<AppSource, string> = {
  email: "Email", manual: "Manual", quick_add: "Quick add", follow_up: "Found via follow-up",
};
const CHANNEL_LABEL: Record<Channel, string> = {
  linkedin: "LinkedIn", company_site: "Company site", recruiter: "Recruiter", referral: "Referral", other: "Other",
};
const KIND_LABEL: Record<string, string> = {
  application_received: "Application received", rejection: "Rejection", interview_request: "Interview request",
  assessment: "Assessment", recruiter_outreach: "Recruiter outreach", offer: "Offer", other: "Other",
  manual_update: "Logged", status_change: "Status changed",
};

const day = (iso: string | null) => (iso ? new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "");
// GOTCHA: toISOString() is UTC; late evening in Florida it is already tomorrow there.
const today = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

function StatusTag({ status }: { status: AppStatus }) {
  return <span className={`tag ${STATUS_CLS[status]}`}>{status}</span>;
}

// ---- quick add + full form -------------------------------------------------------------------

const emptyDraft = (): QuickAddDraft => ({ company: "", role: "", channel: "other", salary_entered: "", applied_on: today(), notes: "", resume_version: "" });

function DraftEditor({ draft, source, onDone, onCancel }: {
  draft: QuickAddDraft; source: "manual" | "quick_add"; onDone: () => void; onCancel: () => void;
}) {
  const [d, setD] = useState<QuickAddDraft>(draft);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof QuickAddDraft) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
    setD({ ...d, [k]: e.target.value });
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setErr(null);
    try {
      await api.createApplication({ ...d, role: d.role || null, salary_entered: d.salary_entered || null,
        notes: d.notes || null, resume_version: d.resume_version || null, source });
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setSaving(false);
    }
  };
  return (
    <form className="qa-preview" onSubmit={save}>
      <div className="small muted">{source === "quick_add" ? "Check this before it's saved" : "Log an application"}</div>
      <div className="qa-grid">
        <div><label htmlFor="qa-company">Company</label><input id="qa-company" required value={d.company} onChange={set("company")} /></div>
        <div><label htmlFor="qa-role">Role</label><input id="qa-role" value={d.role ?? ""} onChange={set("role")} /></div>
        <div><label htmlFor="qa-date">Applied</label><input id="qa-date" type="date" required value={d.applied_on} max={today()} onChange={set("applied_on")} /></div>
        <div>
          <label htmlFor="qa-channel">Source</label>
          <select id="qa-channel" value={d.channel} onChange={set("channel")}>
            {Object.entries(CHANNEL_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        <div><label htmlFor="qa-salary">Salary entered</label><input id="qa-salary" inputMode="text" value={d.salary_entered ?? ""} onChange={set("salary_entered")} /></div>
        <div><label htmlFor="qa-resume">Resume version</label><input id="qa-resume" value={d.resume_version ?? ""} onChange={set("resume_version")} /></div>
      </div>
      <div><label htmlFor="qa-notes">Notes</label><textarea id="qa-notes" rows={2} value={d.notes ?? ""} onChange={set("notes")} /></div>
      {err && <div className="error">{err}</div>}
      <div className="row qa-actions">
        <button className="btn-primary" type="submit" disabled={saving}><Check size={15} />{saving ? "Saving…" : "Save"}</button>
        <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function QuickAdd({ onSaved }: { onSaved: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ d: QuickAddDraft; source: "manual" | "quick_add" } | null>(null);
  const parse = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      setDraft({ d: { ...emptyDraft(), ...(await api.parseQuickAdd(text, today())) }, source: "quick_add" });
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };
  const done = () => {
    setDraft(null);
    setText("");
    onSaved();
  };
  return (
    <section className="panel panel-pad qa" aria-label="Quick add">
      {!draft && (
        <form className="qa-line" onSubmit={parse}>
          <input
            aria-label="Describe an application"
            placeholder="applied Acme junior AI engineer, put 85k"
            value={text} onChange={(e) => setText(e.target.value)}
            autoComplete="off" enterKeyHint="go"
          />
          <button className="btn-primary" type="submit" disabled={busy || !text.trim()}>
            {busy ? <Loader2 size={15} className="spin" /> : <Sparkles size={15} />}Parse
          </button>
          <button type="button" className="btn-ghost" onClick={() => setDraft({ d: emptyDraft(), source: "manual" })}>
            <PenLine size={15} />Full form
          </button>
        </form>
      )}
      {err && <div className="error small" style={{ marginTop: 8 }}>{err}</div>}
      {draft && <DraftEditor key={draft.source} draft={draft.d} source={draft.source} onDone={done} onCancel={() => setDraft(null)} />}
    </section>
  );
}

// ---- detail drawer ---------------------------------------------------------------------------

function Timeline({ events }: { events: AppEvent[] }) {
  if (!events.length) return <div className="faint small">No events yet.</div>;
  return (
    <ol className="timeline">
      {[...events].reverse().map((e) => (
        <li key={e.id}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <strong className="small">{KIND_LABEL[e.kind] ?? e.kind}</strong>
            <span className="small faint num">{day(e.occurred_at)}</span>
          </div>
          {e.snippet && <div className="small muted">{e.snippet}</div>}
          <div className="small faint">
            {e.account && <span>via {e.account}</span>}
            {e.extracted?.next_step && <span> · Next: {String(e.extracted.next_step)}</span>}
            {e.extracted?.deadline && <span> · Due {String(e.extracted.deadline)}</span>}
          </div>
          {e.injection_flags?.length > 0 && <div className="small error">Suspicious text removed before reading this email.</div>}
        </li>
      ))}
    </ol>
  );
}

function Drawer({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const { data, error, reload } = useData(() => api.application(id), [id]);
  const [notes, setNotes] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const patch = async (body: Partial<Application>) => {
    setErr(null);
    try {
      await api.patchApplication(id, body);
      await reload();
      onChanged();
    } catch (e) {
      setErr(errMsg(e));
    }
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const a: ApplicationDetail | null = data;
  return (
    <div className="drawer-back" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()} aria-label="Application detail">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>{a?.company ?? "…"}</h2>
          <button className="btn-ghost" onClick={onClose} aria-label="Close"><X size={16} /></button>
        </div>
        {error && <div className="error">{error}</div>}
        {a && (
          <div className="stack">
            <div className="muted">{a.role ?? "Role not known yet"}</div>
            {a.needs_review && (
              <div className="notice notice-warn row" style={{ justifyContent: "space-between" }}>
                <span className="small">{a.review_reason ?? "Needs a look"}</span>
                <button onClick={() => patch({ needs_review: false })}>Looks right</button>
              </div>
            )}
            <div className="grid-2">
              <div>
                <label htmlFor="d-status">Status</label>
                <select id="d-status" value={a.status} onChange={(e) => patch({ status: e.target.value as AppStatus })}>
                  {APP_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
              <div>
                <label htmlFor="d-role">Role</label>
                <input id="d-role" defaultValue={a.role ?? ""} onBlur={(e) => e.target.value !== (a.role ?? "") && patch({ role: e.target.value || null })} />
              </div>
            </div>
            <dl className="facts small">
              <dt>Applied</dt><dd>{a.applied_at ? day(a.applied_at) : "unknown"}</dd>
              <dt>Last activity</dt><dd>{ago(a.last_activity_at)}</dd>
              <dt>Source</dt><dd>{SOURCE_LABEL[a.source]} · {CHANNEL_LABEL[a.channel]}</dd>
              {a.salary_entered && <><dt>Salary entered</dt><dd className="num">{a.salary_entered}</dd></>}
              {a.resume_version && <><dt>Resume</dt><dd>{a.resume_version}</dd></>}
              <dt>Posting</dt>
              <dd>{a.job ? <Link to={`/jobs/${encodeURIComponent(a.job.id)}`}>{a.job.title}</Link> : <span className="faint">not on the board</span>}</dd>
            </dl>
            <div>
              <label htmlFor="d-notes">Notes</label>
              <textarea id="d-notes" rows={3} value={notes ?? a.notes ?? ""} onChange={(e) => setNotes(e.target.value)} />
              {notes !== null && notes !== (a.notes ?? "") && (
                <button style={{ marginTop: 6 }} onClick={() => patch({ notes }).then(() => setNotes(null))}>Save notes</button>
              )}
            </div>
            {err && <div className="error small">{err}</div>}
            <div>
              <h3 className="small muted" style={{ textTransform: "uppercase", letterSpacing: "0.06em" }}>Timeline</h3>
              <Timeline events={a.events} />
            </div>
          </div>
        )}
      </aside>
    </div>
  );
}

// ---- needs review ----------------------------------------------------------------------------

function ReviewItem({ ev, apps, onDone }: { ev: AppEvent; apps: Application[]; onDone: () => void }) {
  const [target, setTarget] = useState("");
  const [role, setRole] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const same = apps.filter((a) => ev.company && a.company.toLowerCase().includes(ev.company.toLowerCase().split(" ")[0]));
  const options = same.length ? same : apps;
  const act = async (body: { action: "assign" | "create" | "dismiss"; application_id?: string; role?: string }) => {
    setErr(null);
    try {
      await api.resolveReview(ev.id, body);
      onDone();
    } catch (e) {
      setErr(errMsg(e));
    }
  };
  return (
    <div className="panel panel-pad stack review-item">
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
        <strong>{ev.company ?? "Unknown company"}</strong>
        <span className="small faint">{KIND_LABEL[ev.kind] ?? ev.kind} · {day(ev.occurred_at)}{ev.account && ` · ${ev.account}`}</span>
      </div>
      {ev.snippet && <div className="small muted">{ev.snippet}</div>}
      <div className="small" style={{ color: "var(--warn)" }}>{ev.review_reason}</div>
      <div className="review-actions">
        <select aria-label="Assign to application" value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">Assign to…</option>
          {options.map((a) => <option key={a.id} value={a.id}>{a.company} · {a.role ?? "role unknown"}</option>)}
        </select>
        <button disabled={!target} onClick={() => act({ action: "assign", application_id: target })}>Assign</button>
        <input aria-label="Role for a new application" placeholder="Role (optional)" value={role} onChange={(e) => setRole(e.target.value)} />
        <button disabled={!ev.company} onClick={() => act({ action: "create", role: role || undefined })}>Create</button>
        <button className="btn-ghost" onClick={() => act({ action: "dismiss" })}>Dismiss</button>
      </div>
      {err && <div className="error small">{err}</div>}
    </div>
  );
}

// ---- mail accounts ---------------------------------------------------------------------------

function MailPanel({ status }: { status: MailStatus | null }) {
  if (!status) return null;
  return (
    <section className="panel panel-pad" aria-label="Mail accounts">
      <h3 className="small muted" style={{ margin: "0 0 8px", textTransform: "uppercase", letterSpacing: "0.06em" }}>Mail accounts</h3>
      <div className="mail-accounts">
        {status.accounts.map((a) => (
          <div key={a.label} className="small">
            <div className="row"><Mail size={13} /><strong>{a.label}</strong></div>
            {!a.configured ? <div className="faint">Not set up (MAIL_SETUP.md)</div>
              : a.last_error ? <div className="error">{a.last_error}</div>
              : <div className="muted">Synced {ago(a.last_synced_at)}</div>}
          </div>
        ))}
      </div>
    </section>
  );
}

// ---- page ------------------------------------------------------------------------------------

export default function Applications() {
  const [tab, setTab] = useState<"all" | "review">("all");
  const [f, setF] = useState({ status: "", company: "", source: "", date_from: "", date_to: "" });
  const [open, setOpen] = useState<string | null>(null);
  const list = useData(() => api.applications(f), [JSON.stringify(f)]);
  const all = useData(() => api.applications({}), []);
  const review = useData(api.reviewQueue, []);
  const summary = useData(api.appSummary, []);
  const [mail, setMail] = useState<MailStatus | null>(null);
  const [syncErr, setSyncErr] = useState<string | null>(null);
  const syncing = mail?.task?.status === "running";

  const refresh = useCallback(() => {
    void list.reload();
    void all.reload();
    void review.reload();
    void summary.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    api.mailStatus().then(setMail).catch(() => {});
  }, []);
  useEffect(() => {
    if (!syncing) return;
    const t = setInterval(async () => {
      try {
        const m = await api.mailStatus();
        setMail(m);
        if (m.task?.status !== "running") refresh();
      } catch { /* keep polling */ }
    }, 4000);
    return () => clearInterval(t);
  }, [syncing, refresh]);

  const sync = async () => {
    setSyncErr(null);
    try {
      await api.startMailSync();
      setMail(await api.mailStatus());
    } catch (e) {
      setSyncErr(errMsg(e));
    }
  };

  const reviewCount = (review.data?.events.length ?? 0) + (review.data?.applications.length ?? 0);
  const s = summary.data;
  const setFilter = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  const rows = list.data ?? [];
  const companies = useMemo(() => new Set(rows.map((a) => a.company)).size, [rows]);

  return (
    <div className="page apps-page">
      <div className="page-head">
        <div>
          <h1>Applications</h1>
          <p>{s ? `${s.total} sent · ${s.by_status.interview ?? 0} interviewing · ${s.by_status.offer ?? 0} offers · ${s.matched_postings} linked to postings` : "Found in your inboxes or logged by hand."}</p>
        </div>
        <button onClick={sync} disabled={syncing} title="Read new application emails from every connected inbox">
          {syncing ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
          {syncing ? "Syncing…" : "Sync mail"}
        </button>
      </div>

      <QuickAdd onSaved={refresh} />

      {syncErr && <div className="notice notice-bad">Could not start a sync: {syncErr}</div>}
      {mail?.task?.status === "failed" && <div className="notice notice-bad">Mail sync failed: {mail.task.error}</div>}

      <div className="row tabs" role="tablist">
        <button role="tab" aria-selected={tab === "all"} className={tab === "all" ? "btn-primary" : ""} onClick={() => setTab("all")}>All</button>
        <button role="tab" aria-selected={tab === "review"} className={tab === "review" ? "btn-primary" : ""} onClick={() => setTab("review")}>
          <Inbox size={14} />Needs review <span className="num">{reviewCount}</span>
        </button>
      </div>

      {tab === "all" && (
        <>
          <div className="filters">
            <select aria-label="Status" value={f.status} onChange={setFilter("status")}>
              <option value="">All statuses</option>
              {APP_STATUSES.map((st) => <option key={st} value={st}>{st}</option>)}
            </select>
            <input aria-label="Company" placeholder="Company" value={f.company} onChange={setFilter("company")} />
            <select aria-label="Source" value={f.source} onChange={setFilter("source")}>
              <option value="">All sources</option>
              {Object.entries(SOURCE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <input aria-label="From date" type="date" value={f.date_from} onChange={setFilter("date_from")} />
            <input aria-label="To date" type="date" value={f.date_to} onChange={setFilter("date_to")} />
          </div>
          {list.error && <div className="notice notice-bad">Could not load applications: {list.error}</div>}
          <div className="panel apps-table-wrap">
            <table className="apps-table">
              <thead>
                <tr><th>Company</th><th>Role</th><th>Status</th><th>Source</th><th>Applied</th><th>Last activity</th></tr>
              </thead>
              <tbody>
                {rows.map((a) => (
                  <tr key={a.id} onClick={() => setOpen(a.id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && setOpen(a.id)}>
                    <td data-label="Company"><strong>{a.company}</strong>{a.needs_review && <span className="tag tag-warn" style={{ marginLeft: 6 }}>review</span>}</td>
                    <td data-label="Role">{a.role ?? <span className="faint">unknown</span>}</td>
                    <td data-label="Status"><StatusTag status={a.status} /></td>
                    <td data-label="Source" className="small">{SOURCE_LABEL[a.source]}<div className="faint">{CHANNEL_LABEL[a.channel]}</div></td>
                    <td data-label="Applied" className="num small">{a.applied_at ? day(a.applied_at) : "-"}</td>
                    <td data-label="Last activity" className="small muted">{ago(a.last_activity_at)}</td>
                  </tr>
                ))}
                {list.data && rows.length === 0 && (
                  <tr><td colSpan={6} className="faint" style={{ textAlign: "center", padding: 24 }}>No applications match. Sync mail or add one above.</td></tr>
                )}
              </tbody>
            </table>
          </div>
          {rows.length > 0 && <p className="small faint">{rows.length} {rows.length === 1 ? "application" : "applications"} across {companies} {companies === 1 ? "company" : "companies"}.</p>}
        </>
      )}

      {tab === "review" && (
        <div className="stack">
          {review.data?.events.map((ev) => <ReviewItem key={ev.id} ev={ev} apps={all.data ?? []} onDone={refresh} />)}
          {review.data?.applications.map((a) => (
            <div key={a.id} className="panel panel-pad row review-item" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
              <div>
                <strong>{a.company}</strong> <span className="muted">{a.role ?? "role unknown"}</span>
                <div className="small" style={{ color: "var(--warn)" }}>{a.review_reason}</div>
              </div>
              <div className="row">
                <button onClick={() => setOpen(a.id)}>Open</button>
                <button onClick={() => api.patchApplication(a.id, { needs_review: false }).then(refresh)}>Looks right</button>
              </div>
            </div>
          ))}
          {review.data && reviewCount === 0 && <div className="faint">Nothing to review.</div>}
        </div>
      )}

      <MailPanel status={mail} />

      {open && <Drawer id={open} onClose={() => setOpen(null)} onChanged={refresh} />}
    </div>
  );
}
