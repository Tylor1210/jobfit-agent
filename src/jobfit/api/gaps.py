"""Skill-gap aggregation for the dashboard's Skill gaps page: which missing skills show up most
across scored jobs, so Tylor knows what to learn next. Pure functions, unit-tested offline.

Grouping: a requirement that names a known technology (config/tech_lexicon.yaml) counts under
that name, so "Kubernetes and Terraform" lands in two buckets and "5+ years with Java" in Java.
Degrees, years and clearances get fixed buckets. Anything else falls back to its own short text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from jobfit.graphs.verify import tech_terms

_FIXED = {
    "years_experience": "More years of experience",
    "education": "Bachelor's degree",
    "clearance": "Security clearance",
}
_STATUS_WEIGHT = {"missing": 1.0, "partial": 0.5}
# WHY: "what to learn next" is about skills, tools, domains and credentials. Soft skills and
# location are real gaps but not things a course fixes; they drown the signal with few jobs.
SOFT_CATEGORIES = {"soft_skill", "location"}
_KIND_WEIGHT = {"required": 1.0, "preferred": 0.5}


@dataclass
class GapBucket:
    name: str
    weight: float = 0.0
    jobs: set[str] = field(default_factory=set)
    required_in: int = 0
    fit_scores: list[int] = field(default_factory=list)
    examples: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight": round(self.weight, 2),
            "jobs": len(self.jobs),
            "required_in": self.required_in,
            "avg_fit": round(sum(self.fit_scores) / len(self.fit_scores))
            if self.fit_scores
            else None,
            "examples": self.examples[:4],
        }


def bucket_names(text: str, category: str) -> list[str]:
    if category in _FIXED:
        return [_FIXED[category]]
    found = [t for t in tech_terms() if re.search(rf"(?<![\w.]){re.escape(t)}(?![\w])", text, re.I)]
    # Prefer the longest match when one term contains another ("Spring Boot" over "Spring").
    found = [t for t in found if not any(t != o and t.lower() in o.lower() for o in found)]
    if found:
        return found
    short = re.sub(r"[^\w\s/+#.-]", "", text).strip()
    return [" ".join(short.split()[:6]).capitalize()] if short else []


def aggregate(
    rows: list[dict[str, Any]], min_fit: int = 0, include_soft: bool = False
) -> list[dict[str, Any]]:
    """rows: {id, company, title, fit_score, fit (FitResult dict)}. Ranked by weight: a missing
    required item in one job counts 1.0, partial or preferred counts less."""
    buckets: dict[str, GapBucket] = {}
    for row in rows:
        fit = row.get("fit") or {}
        if (row.get("fit_score") or 0) < min_fit or fit.get("verdict") == "unknown":
            continue
        seen_here: set[str] = set()
        for a in fit.get("assessments", []):
            status = a.get("status")
            if status not in _STATUS_WEIGHT:
                continue
            req = a["requirement"]
            if not include_soft and req.get("category") in SOFT_CATEGORIES:
                continue
            w = _STATUS_WEIGHT[status] * _KIND_WEIGHT.get(req.get("kind", "required"), 1.0)
            for name in bucket_names(req["text"], req.get("category", "other")):
                key = name.lower()
                b = buckets.setdefault(key, GapBucket(name))
                if key in seen_here:
                    continue  # count each gap once per job
                seen_here.add(key)
                b.weight += w
                b.jobs.add(row["id"])
                b.required_in += req.get("kind") == "required"
                b.fit_scores.append(int(row.get("fit_score") or 0))
                b.examples.append(
                    {
                        "job_id": row["id"],
                        "company": row["company"],
                        "title": row["title"],
                        "requirement": req["text"],
                    }
                )
    ranked = sorted(buckets.values(), key=lambda b: (b.weight, len(b.jobs)), reverse=True)
    return [b.as_dict() for b in ranked]
