// Kanban over the job stages. Drag a card to move it; the stage is saved optimistically and
// rolled back if the API refuses. "Check for jobs" runs the ingest pipeline on the server; New
// cards from earlier checks collapse into a History group at the bottom of the New column, and
// cards scoring under LOW_MATCH go to a Low match group (the daily run never rescores those).
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  DndContext, DragOverlay, PointerSensor, useDraggable, useDroppable, useSensor, useSensors,
  type DragEndEvent, type DragStartEvent,
} from "@dnd-kit/core";
import { ArrowDownCircle, ChevronDown, ChevronRight, History, Loader2, RefreshCw } from "lucide-react";
import { api, useData } from "../api";
import { ScoreRing, SOURCE_LABEL } from "../components/ui";
import { STAGES, type Card, type IngestTask, type Stage } from "../types";

// GOTCHA: the browser fires a click on pointer-up after a drag; ignore clicks right after a drop
// so dropping a card doesn't also open it.
let lastDrop = 0;

// Keep in sync with rescore_min_score in config/ingest.yaml.
const LOW_MATCH = 60;

const LABEL: Record<Stage, string> = {
  new: "New", shortlisted: "Shortlisted", applied: "Applied", interview: "Interview",
  offer: "Offer", rejected: "Rejected", closed: "Closed",
};

function JobCard({ card, overlay = false }: { card: Card; overlay?: boolean }) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({ id: card.id, disabled: overlay });
  const navigate = useNavigate();
  const busy = card.task?.status === "running";
  return (
    <div
      ref={setNodeRef} {...listeners} {...attributes}
      className={`card ${isDragging ? "dragging" : ""} ${card.status === "closed" ? "closed" : ""}`}
      // A click (not a drag) opens the job. dnd-kit only starts a drag after 6px of movement.
      onClick={() => {
        if (Date.now() - lastDrop > 300) navigate(`/jobs/${encodeURIComponent(card.id)}`);
      }}
    >
      <div style={{ minWidth: 0 }}>
        <div className="co">{card.company}</div>
        <div className="role">{card.title}</div>
        <div className="meta">
          {card.location && <span className="loc" title={card.location}>{card.location}</span>}
          {card.pay && <span className="num">{card.pay}</span>}
          <span className="faint">{SOURCE_LABEL[card.source] ?? card.source}</span>
        </div>
        <div className="chips">
          {card.top_gap && <span className="tag" title={`Top gap: ${card.top_gap}`}>Gap: {card.top_gap}</span>}
          {card.status === "closed" && <span className="tag tag-bad">Posting closed</span>}
          {card.tailor_status === "awaiting_review" && <span className="tag tag-accent">Drafts ready</span>}
          {card.tailor_status === "rendered" && <span className="tag tag-ok">PDFs ready</span>}
          {busy && <span className="tag"><Loader2 size={11} className="spin" />{card.task?.kind}</span>}
        </div>
      </div>
      <ScoreRing score={card.fit_score} verdict={card.verdict} />
    </div>
  );
}

function Group({ label, icon, cards }: { label: string; icon: ReactNode; cards: Card[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="history">
      <button className="history-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        {icon}
        {label}
        <span className="num">{cards.length}</span>
      </button>
      {open && <div className="history-body">{cards.map((c) => <JobCard key={c.id} card={c} />)}</div>}
    </div>
  );
}

function Column({ stage, cards }: { stage: Stage; cards: Card[] }) {
  const { setNodeRef, isOver } = useDroppable({ id: stage });
  // Only the New column splits: low matches go to their own group, and the rest that weren't
  // scored in the latest check are history, not news.
  const isNew = stage === "new";
  const low = isNew ? cards.filter((c) => c.fit_score !== null && c.fit_score < LOW_MATCH) : [];
  const rest = isNew ? cards.filter((c) => !low.includes(c)) : cards;
  const current = isNew ? rest.filter((c) => c.fresh) : cards;
  const past = isNew ? rest.filter((c) => !c.fresh) : [];
  return (
    <section ref={setNodeRef} className={`col ${isOver ? "over" : ""}`} aria-label={LABEL[stage]}>
      <div className="col-head">
        <span>{LABEL[stage]}</span>
        <span className="num">{current.length}</span>
      </div>
      <div className="col-body">
        {current.length ? current.map((c) => <JobCard key={c.id} card={c} />)
          : <div className="empty-col">{stage === "new" ? "Nothing new since the last check" : "Drop here"}</div>}
        {past.length > 0 && <Group label="History" icon={<History size={14} />} cards={past} />}
        {low.length > 0 && (
          <Group label="Low match" icon={<ArrowDownCircle size={14} />} cards={low} />
        )}
      </div>
    </section>
  );
}

function useJobCheck(onDone: () => void) {
  const [task, setTask] = useState<IngestTask | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const running = task?.status === "running";

  useEffect(() => {
    api.ingestStatus().then(setTask).catch(() => {});
  }, []);
  useEffect(() => {
    if (!running) return;
    // A check fetches every source, then scores the top N: usually 1 to 5 minutes.
    const timer = setInterval(async () => {
      try {
        const t = await api.ingestStatus();
        setTask(t);
        if (t?.status !== "running") onDone();
      } catch { /* keep polling */ }
    }, 4000);
    return () => clearInterval(timer);
  }, [running, onDone]);

  const start = async () => {
    setStartError(null);
    try {
      await api.startIngest();
      setTask(await api.ingestStatus());
    } catch (e) {
      setStartError(e instanceof Error ? e.message : String(e));
    }
  };
  return { task, running, start, startError };
}

function checkNote(t: IngestTask | null): string | null {
  if (!t || t.status !== "done" || !t.result) return null;
  const r = t.result;
  return `Last check: ${r.new} new postings, ${r.scored} scored, ${r.rescored} rescored, $${r.spend_usd.toFixed(3)} spent.`;
}

export default function Pipeline() {
  const { data, error, setData, reload } = useData(api.jobs, [], {
    intervalMs: 5000,
    shouldPoll: (d) => d.some((c) => c.task?.status === "running"),
  });
  const [dragging, setDragging] = useState<Card | null>(null);
  const [moveError, setMoveError] = useState<string | null>(null);
  const check = useJobCheck(reload);
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 6 } }));

  const columns = useMemo(() => {
    const by = Object.fromEntries(STAGES.map((s) => [s, [] as Card[]])) as Record<Stage, Card[]>;
    for (const c of data ?? []) by[c.stage]?.push(c);
    return by;
  }, [data]);

  const onStart = (e: DragStartEvent) => setDragging((data ?? []).find((c) => c.id === e.active.id) ?? null);
  const onEnd = async (e: DragEndEvent) => {
    setDragging(null);
    lastDrop = Date.now();
    const to = e.over?.id as Stage | undefined;
    const card = (data ?? []).find((c) => c.id === e.active.id);
    if (!to || !card || card.stage === to) return;
    const before = data;
    setData((data ?? []).map((c) => (c.id === card.id ? { ...c, stage: to } : c)));
    try {
      await api.setStage(card.id, to);
      setMoveError(null);
    } catch (err) {
      setData(before);
      setMoveError(`Could not move ${card.company}: ${err instanceof Error ? err.message : err}`);
    }
  };

  const scored = (data ?? []).filter((c) => c.fit_score !== null).length;
  return (
    <div className="page" style={{ maxWidth: "none" }}>
      <div className="page-head">
        <div>
          <h1>Pipeline</h1>
          <p>
            {data ? `${data.length} jobs, ${scored} scored. ` : ""}Drag cards between stages. Click a card for detail.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button onClick={check.start} disabled={check.running} title="Fetch every source and score the best matches">
            {check.running ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
            {check.running ? "Checking…" : "Check for jobs"}
          </button>
          <Link className="btn btn-primary" to="/add">Add job</Link>
        </div>
      </div>
      {check.startError && <div className="notice notice-bad" style={{ marginBottom: 12 }}>Could not start a check: {check.startError}</div>}
      {check.task?.status === "failed" && <div className="notice notice-bad" style={{ marginBottom: 12 }}>Job check failed: {check.task.error}</div>}
      {checkNote(check.task) && <p className="muted" style={{ margin: "-8px 0 12px" }}>{checkNote(check.task)}</p>}
      {error && <div className="notice notice-bad">Could not load jobs: {error}</div>}
      {moveError && <div className="notice notice-bad" style={{ marginBottom: 12 }}>{moveError}</div>}
      {!data && !error && <div className="muted">Loading…</div>}
      {data && (
        <DndContext sensors={sensors} onDragStart={onStart} onDragEnd={onEnd} onDragCancel={() => setDragging(null)}>
          <div className="board">
            {STAGES.map((s) => <Column key={s} stage={s} cards={columns[s]} />)}
          </div>
          <DragOverlay>{dragging ? <JobCard card={dragging} overlay /> : null}</DragOverlay>
        </DndContext>
      )}
    </div>
  );
}
