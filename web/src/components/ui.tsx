// Small shared pieces: score ring, verdict/status tags, theme toggle, relative time.
import { useEffect, useState } from "react";
import { Moon, Sun } from "lucide-react";
import type { ReqStatus, Verdict } from "../types";

export function ScoreRing({ score, size = 40, verdict }: { score: number | null; size?: number; verdict?: Verdict | null }) {
  const stroke = size >= 56 ? 5 : 4;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const unknown = score === null || verdict === "unknown";
  const pct = unknown ? 0 : Math.max(0, Math.min(100, score ?? 0)) / 100;
  const color =
    unknown ? "var(--faint)" : (score ?? 0) >= 75 ? "var(--accent)" : (score ?? 0) >= 55 ? "var(--warn)" : "var(--bad)";
  return (
    <svg width={size} height={size} role="img" aria-label={unknown ? "Not scored" : `Fit score ${score}`}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--ring-track)" strokeWidth={stroke} />
      <circle
        cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth={stroke}
        strokeDasharray={`${pct * c} ${c}`} strokeLinecap="butt"
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
      <text
        x="50%" y="50%" dominantBaseline="central" textAnchor="middle"
        fontFamily="var(--mono)" fontSize={size >= 56 ? 17 : 12} fontWeight={500} fill="var(--text)"
      >
        {unknown ? "?" : score}
      </text>
    </svg>
  );
}

const VERDICT: Record<Verdict, [string, string]> = {
  strong: ["Strong fit", "tag-ok"],
  moderate: ["Moderate", "tag-warn"],
  weak: ["Weak", "tag-bad"],
  not_a_fit: ["Not a fit", "tag-bad"],
  unknown: ["Not enough info", ""],
};

export function VerdictTag({ verdict }: { verdict: Verdict | null }) {
  if (!verdict) return <span className="tag">Not scored</span>;
  const [label, cls] = VERDICT[verdict];
  return <span className={`tag ${cls}`}>{label}</span>;
}

export function StatusTag({ status }: { status: ReqStatus }) {
  const cls = status === "met" ? "tag-ok" : status === "partial" ? "tag-warn" : "tag-bad";
  return <span className={`tag ${cls}`}>{status}</span>;
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<"dark" | "light">(() =>
    document.documentElement.dataset.theme === "light" ? "light" : "dark",
  );
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("jobfit-theme", theme);
    } catch {
      /* private mode: theme just won't persist */
    }
  }, [theme]);
  return (
    <button className="btn-ghost" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
      {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
      {theme === "dark" ? "Light" : "Dark"}
    </button>
  );
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export const SOURCE_LABEL: Record<string, string> = {
  greenhouse: "Greenhouse",
  lever: "Lever",
  ashby: "Ashby",
  remotive: "via Remotive",
  remoteok: "via Remote OK",
  usajobs: "USAJOBS",
  adzuna: "via Adzuna",
  manual: "Added by hand",
  eval: "Eval",
};
