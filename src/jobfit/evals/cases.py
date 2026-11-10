"""Load eval cases from evals/cases/cases.yaml and their posting text files."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from jobfit.config import ROOT
from jobfit.schemas import JobPosting, Verdict

CASES_DIR = ROOT / "evals" / "cases"


class ExpectedGap(BaseModel):
    name: str
    match: list[str]
    confirmed: bool = False

    def found_in(self, text: str) -> bool:
        low = text.lower()
        return any(m.lower() in low for m in self.match)


class Case(BaseModel):
    id: str
    company: str
    title: str
    url: str | None = None
    note: str | None = None
    status: Literal["ready", "needs_posting"] = "ready"
    expected_verdict: Verdict
    expected_gaps: list[ExpectedGap] = Field(default_factory=list)
    tailor: bool = False
    injection: Literal["obvious", "subtle"] | None = None
    max_score: int | None = None

    def posting_path(self, root: Path = CASES_DIR) -> Path:
        return root / f"{self.id}.txt"

    def job(self, root: Path = CASES_DIR) -> JobPosting:
        text = self.posting_path(root).read_text(encoding="utf-8")
        return JobPosting(
            id=f"eval:{self.id}",
            source="eval",
            external_id=self.id,
            board="eval",
            company=self.company,
            title=self.title,
            url=self.url,
            description=text,
        )

    def runnable(self, root: Path = CASES_DIR) -> bool:
        return self.status == "ready" and self.posting_path(root).exists()


def load_cases(root: Path = CASES_DIR, only: set[str] | None = None) -> list[Case]:
    raw = yaml.safe_load((root / "cases.yaml").read_text(encoding="utf-8")) or []
    cases = [Case.model_validate(c) for c in raw]
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids in cases.yaml")
    return [c for c in cases if only is None or c.id in only]
