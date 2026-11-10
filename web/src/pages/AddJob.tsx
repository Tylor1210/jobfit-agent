// Add a job by hand: a Greenhouse/Lever/Ashby link, or pasted text (LinkedIn and Indeed postings
// can't be fetched automatically; their terms forbid it).
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Link2, ClipboardPaste } from "lucide-react";
import { api } from "../api";

export default function AddJob() {
  const [mode, setMode] = useState<"url" | "text">("url");
  const [form, setForm] = useState({ url: "", text: "", company: "", title: "", location: "" });
  const [err, setErr] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const navigate = useNavigate();
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    setForm({ ...form, [k]: e.target.value });

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSending(true);
    setErr(null);
    try {
      const body = mode === "url"
        ? { url: form.url.trim() }
        : { text: form.text, company: form.company.trim(), title: form.title.trim(),
            location: form.location.trim() || undefined, url: form.url.trim() || undefined };
      const { id } = await api.addJob(body);
      navigate(`/jobs/${encodeURIComponent(id)}`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setErr(msg);
      if (/paste the posting text/i.test(msg)) setMode("text");
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="page" style={{ maxWidth: 760 }}>
      <div className="page-head">
        <div>
          <h1>Add job</h1>
          <p>Scored against your evidence as soon as it's saved (about 30 seconds).</p>
        </div>
      </div>
      <div className="row" style={{ marginBottom: 14 }}>
        <button className={mode === "url" ? "btn-primary" : ""} onClick={() => setMode("url")}><Link2 size={14} />Link</button>
        <button className={mode === "text" ? "btn-primary" : ""} onClick={() => setMode("text")}><ClipboardPaste size={14} />Paste text</button>
      </div>
      <form className="panel panel-pad stack" onSubmit={submit}>
        {mode === "url" ? (
          <div>
            <label htmlFor="url">Posting link</label>
            <input id="url" required placeholder="https://job-boards.greenhouse.io/company/jobs/123"
              value={form.url} onChange={set("url")} />
            <div className="small faint" style={{ marginTop: 6 }}>
              Greenhouse, Lever and Ashby links use their public APIs. Other company pages are fetched once.
              LinkedIn and Indeed: switch to Paste text.
            </div>
          </div>
        ) : (
          <>
            <div className="grid-2">
              <div><label htmlFor="company">Company</label><input id="company" required value={form.company} onChange={set("company")} /></div>
              <div><label htmlFor="title">Role</label><input id="title" required value={form.title} onChange={set("title")} /></div>
              <div><label htmlFor="location">Location</label><input id="location" placeholder="Remote, US" value={form.location} onChange={set("location")} /></div>
              <div><label htmlFor="link">Link (optional)</label><input id="link" value={form.url} onChange={set("url")} /></div>
            </div>
            <div>
              <label htmlFor="text">Posting text</label>
              <textarea id="text" required rows={14} placeholder="Paste the full description, including requirements."
                value={form.text} onChange={set("text")} />
            </div>
          </>
        )}
        {err && <div className="error">{err}</div>}
        <div><button className="btn-primary" type="submit" disabled={sending}>{sending ? "Saving…" : "Add and score"}</button></div>
      </form>
    </div>
  );
}
