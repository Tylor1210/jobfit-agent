"""Render resume and cover letter drafts to PDF in Tylor's layout (HTML/CSS -> Chromium).

Called by the tailor graph's `verify` (page count) and `render_pdfs` nodes.
Titles, companies, dates, education, and skill names come from the profile, never from the LLM,
so the only generated text on the page is the summary, bullets, skill row labels, and letter.

GOTCHA: the footer's "Page 1 of 1" is static. Both documents must be one page, and the resume
page check rejects anything longer before it gets here, so a dynamic counter is never needed.
"""

from __future__ import annotations

import base64
import io
import re
import threading
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pypdf import PdfReader

from jobfit.profile.models import Profile
from jobfit.schemas import CoverLetterOut, JobPosting, ResumeOut

HERE = Path(__file__).parent
_env = Environment(
    loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html", "j2"])
)


def _month(d: str) -> str:
    """'2026-04' -> '04/2026', 'present' -> 'Present', '2017' -> '2017'."""
    if d.lower() == "present":
        return "Present"
    m = re.fullmatch(r"(\d{4})-(\d{2})", d)
    return f"{m.group(2)}/{m.group(1)}" if m else d


def _dates(start: str, end: str) -> str:
    # WHY: "to" instead of a dash, matching the reference layout and the no-em-dash rule.
    return f"{_month(start)} to {_month(end)}"


@lru_cache(maxsize=1)
def _base_css() -> str:
    # WHY: fonts inlined as data URIs. A page loaded via set_content() has no origin, so
    # Chromium will not fetch file:// fonts, and the PDF would silently fall back to Arial.
    def uri(weight: int) -> str:
        data = (HERE / "fonts" / f"roboto-latin-{weight}-normal.woff2").read_bytes()
        return "data:font/woff2;base64," + base64.b64encode(data).decode()

    return _env.get_template("base.css").render(font_400=uri(400), font_700=uri(700))


def _common(p: Profile, job: JobPosting) -> dict[str, object]:
    c = p.contact
    return {
        "base_css": _base_css(),
        "contact": c,
        "contact_items": [c.location, c.email, *c.websites[:1], *([c.github] if c.github else [])],
        "footer_left": f"{c.name.upper()} | Application for {job.title}, {job.company}",
    }


def resume_html(r: ResumeOut, p: Profile, job: JobPosting) -> str:
    skills = {s.id: s.name for s in p.all_skills()}
    exp = {j.id: j for j in p.experience}
    mil = {m.id: m for m in p.military}
    roles, military = [], []
    for dj in r.jobs:
        bullets = [b.text for b in dj.bullets]
        if dj.job_id in exp:
            j = exp[dj.job_id]
            roles.append(
                {
                    "title": j.title,
                    "org": j.company,
                    "dates": _dates(j.start, j.end),
                    "bullets": bullets,
                }
            )
        elif dj.job_id in mil:
            m = mil[dj.job_id]
            military.append(
                {
                    "title": m.title,
                    "org": m.branch,
                    "dates": _dates(m.start, m.end),
                    "bullets": bullets,
                }
            )
    education = [
        {
            "degree": e.degree,
            "where": " | ".join(
                [
                    e.school,
                    "In progress"
                    if e.status == "in_progress"
                    else ", ".join(e.honors) or "Completed",
                ]
            ),
        }
        for e in p.education
    ]
    return _env.get_template("resume.html.j2").render(
        **_common(p, job),
        title=f"{p.contact.short_name} - {job.company} {job.title} Resume",
        resume=r,
        skill_rows=[
            {
                "label": row.label,
                "skills_text": ", ".join(skills[s] for s in row.skill_ids if s in skills),
            }
            for row in r.skill_rows
        ],
        roles=roles,
        military=military,
        education=education,
        certifications=", ".join(c.name for c in p.certifications),
        partners=", ".join(c.name for c in p.partner_programs),
    )


def cover_letter_html(
    letter: CoverLetterOut, p: Profile, job: JobPosting, on: date | None = None
) -> str:
    d = on or date.today()
    return _env.get_template("cover_letter.html.j2").render(
        **_common(p, job),
        title=f"{p.contact.short_name} - {job.company} {job.title} Cover Letter",
        letter=letter,
        date=f"{d:%B} {d.day}, {d.year}",
    )


@dataclass(frozen=True, slots=True)
class Rendered:
    pdf: bytes
    pages: int  # effective pages: content that collides with the footer counts as 2


class PdfRenderer:
    """One headless Chromium reused across renders (startup is the slow part)."""

    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()

    def render(self, html: str) -> Rendered:
        page = self._browser.new_page()
        try:
            page.set_content(html, wait_until="load")
            page.evaluate("document.fonts.ready")
            page.emulate_media(media="print")
            # Content bottom vs footer top, in CSS px. Overlap means it does not fit one page.
            overlap = page.evaluate(
                """() => {
                  const c = document.querySelector('.content');
                  const f = document.querySelector('footer');
                  if (!c || !f) return false;
                  return c.getBoundingClientRect().bottom > f.getBoundingClientRect().top - 4;
                }"""
            )
            fonts_ok = page.evaluate("document.fonts.check('700 10pt Roboto')")
            pdf = page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
        finally:
            page.close()
        if not fonts_ok:
            raise RuntimeError("Roboto failed to load; refusing to render with a fallback font")
        pages = page_count(pdf)
        return Rendered(pdf, pages + 1 if overlap and pages == 1 else pages)

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()


def page_count(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages)


def safe_filename(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', " ", s).strip()


def output_names(p: Profile, job: JobPosting) -> tuple[str, str]:
    base = safe_filename(f"{p.contact.short_name} - {job.company} {job.title}")
    return f"{base} Resume.pdf", f"{base} Cover Letter.pdf"


_local = threading.local()


def default_renderer() -> PdfRenderer:
    """One renderer per thread. GOTCHA: Playwright's sync API is bound to the thread that
    started it; the API runs tailoring on worker threads, so a shared instance would crash."""
    r: PdfRenderer | None = getattr(_local, "renderer", None)
    if r is None:
        r = _local.renderer = PdfRenderer()
    return r
