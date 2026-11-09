"""PDF rendering with real Chromium. Skipped when Playwright's browser is not installed."""

from __future__ import annotations

import io
from collections.abc import Iterator

import pytest
from factories import letter, resume
from pypdf import PdfReader

from jobfit.profile.loader import load_profile
from jobfit.schemas import DraftBulletOut, DraftJobOut, JobPosting

pytestmark = pytest.mark.render


@pytest.fixture(scope="module")
def renderer() -> Iterator[object]:
    from jobfit.render.pdf import PdfRenderer

    try:
        r = PdfRenderer()
    except Exception as e:  # browser binary missing
        pytest.skip(f"chromium not available: {e}")
    yield r
    r.close()


JOB = JobPosting(
    id="x",
    source="x",
    external_id="x",
    company="TribalScale",
    title="Forward Deployed Engineer",
    description="",
)


def _text(pdf: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def test_resume_layout(renderer) -> None:  # type: ignore[no-untyped-def]
    from jobfit.render.pdf import output_names, resume_html

    out = renderer.render(resume_html(resume(), load_profile(), JOB))
    assert out.pages == 1
    text = _text(out.pdf)
    assert "TYLOR CLONT | Application for Forward Deployed Engineer, TribalScale" in text
    assert "Page 1 of 1" in text
    assert "Founder & Systems Architect" in text and "01/2022 to Present" in text
    assert "In progress" in text and "—" not in text
    fonts = {
        str(f)
        for p in PdfReader(io.BytesIO(out.pdf)).pages
        for f in p["/Resources"]["/Font"].values()  # type: ignore[index]
    }
    assert fonts  # embedded
    assert output_names(load_profile(), JOB) == (
        "Tylor C - TribalScale Forward Deployed Engineer Resume.pdf",
        "Tylor C - TribalScale Forward Deployed Engineer Cover Letter.pdf",
    )


def test_cover_letter_layout(renderer) -> None:  # type: ignore[no-untyped-def]
    from jobfit.render.pdf import cover_letter_html

    out = renderer.render(cover_letter_html(letter(), load_profile(), JOB))
    assert out.pages == 1
    text = _text(out.pdf)
    assert "Dear TribalScale Hiring Team," in text and "Sincerely," in text


def test_overlong_resume_is_detected(renderer) -> None:  # type: ignore[no-untyped-def]
    from jobfit.render.pdf import resume_html

    long_b = DraftBulletOut(
        text="Built database-layer validation pipelines that eliminated 98% "
        "of invoicing errors across every client account. " * 2,
        source_ids=["exp.fairman.2"],
    )
    r = resume(jobs=[DraftJobOut(job_id="exp.fairman", bullets=[long_b] * 30)])
    assert renderer.render(resume_html(r, load_profile(), JOB)).pages >= 2
