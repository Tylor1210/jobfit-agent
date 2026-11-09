"""Cheap, LLM-free prefilter: title keywords, location, and embedding similarity to the profile.

Order matters for cost: the string checks run first, so only jobs that pass them get embedded.
Used by jobfit.ingest.pipeline; results are stored so a job is evaluated once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from jobfit.ingest.config import Prefilter
from jobfit.profile.chunks import build_chunks
from jobfit.profile.models import Profile
from jobfit.rag.embedder import Embedder, Vector
from jobfit.schemas import JobPosting

_STATES = (
    "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
    "hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
    "michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    "new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
    "rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|"
    "west virginia|wisconsin|wyoming"
)
_STATE_CODES = (
    # GOTCHA: DE and IN left out on purpose; "Munich, DE" and "Pune, IN" are countries.
    "AL|AK|AZ|AR|CA|CO|CT|FL|GA|HI|ID|IL|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|"
    "NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
_US = re.compile(rf"\b(us|u\.s\.?|usa|united states|americas|north america|{_STATES})\b", re.I)
# GOTCHA: case-sensitive and real codes only, so "Toronto, ON" is not mistaken for a US state.
_US_CODE = re.compile(rf",\s?({_STATE_CODES})\b")
_REMOTE_WORDS = re.compile(
    r"\b(remote|anywhere|worldwide|global|fully|distributed|work from home|wfh|unspecified|"
    r"location|flexible|hybrid|or|and|in|the|-|\(|\))\b|[()/,;|-]",
    re.I,
)


@dataclass(frozen=True, slots=True)
class Verdict:
    verdict: str  # "pass" | "title" | "location" | "similarity"
    similarity: float | None = None
    rank: float | None = None  # similarity + local bonus; candidates are scored in this order


def title_ok(title: str, cfg: Prefilter) -> bool:
    t = f" {title.lower()} "
    if any(x in t for x in cfg.title_exclude):
        return False
    return any(k in t for k in cfg.title_include)


def is_local(job: JobPosting, cfg: Prefilter) -> bool:
    # Title too: boards often put the city there ("Forward Deployed Engineer (Fort Lauderdale)").
    text = f"{job.title} {job.location or ''}".lower()
    return any(p in text for p in cfg.local_places)


def location_ok(job: JobPosting, cfg: Prefilter) -> bool:
    loc = job.location or ""
    low = loc.lower()
    if is_local(job, cfg):
        return True  # on-site, hybrid, or remote: all fine in South Florida
    remote = bool(job.remote) or "remote" in low or "anywhere" in low
    if not remote:
        return False
    us = bool(_US.search(loc) or _US_CODE.search(loc))
    both = f"{job.title} {loc}".lower()
    if any(r in both for r in cfg.remote_reject) and not us:
        return False
    if us:
        return True
    # "Remote", "Remote (Worldwide)", "Anywhere": no country named, so treat as open to the US.
    # "Remote - Toronto" names somewhere else: leftover words after removing remote-ish ones.
    leftover = _REMOTE_WORDS.sub(" ", low).strip()
    if leftover:
        return False
    # No country in the location field (Remote OK leaves it blank): feeds often put the real
    # restriction in the first lines of the description ("Mostly remote (within Germany)").
    head = job.description[:400].lower()
    return not (any(r in head for r in cfg.remote_reject) and not _US.search(head))


class ProfileMatcher:
    """Cosine similarity between a posting and the profile's centroid (summary + bullets)."""

    def __init__(self, profile: Profile, embedder: Embedder) -> None:
        texts = [c.text for c in build_chunks(profile) if c.kind in ("summary", "bullet")]
        vecs = np.stack(embedder.embed(texts))
        centroid = vecs.mean(axis=0)
        self._centroid: Vector = centroid / np.linalg.norm(centroid)
        self._embedder = embedder

    def scores(self, jobs: list[JobPosting]) -> list[float]:
        if not jobs:
            return []
        # WHY: title twice + the first 1500 chars. Long postings are mostly boilerplate, which
        # pulls every embedding toward the same "generic job ad" point.
        texts = [f"{j.title}. {j.title}. {j.description[:1500]}" for j in jobs]
        return [float(np.dot(v, self._centroid)) for v in self._embedder.embed(texts)]


def evaluate(jobs: list[JobPosting], cfg: Prefilter, matcher: ProfileMatcher) -> dict[str, Verdict]:
    out: dict[str, Verdict] = {}
    survivors: list[JobPosting] = []
    for j in jobs:
        if not title_ok(j.title, cfg):
            out[j.id] = Verdict("title")
        elif not location_ok(j, cfg):
            out[j.id] = Verdict("location")
        else:
            survivors.append(j)
    for j, sim in zip(survivors, matcher.scores(survivors), strict=True):
        rank = sim + (cfg.local_bonus if is_local(j, cfg) else 0.0)
        out[j.id] = Verdict(
            "pass" if sim >= cfg.min_similarity else "similarity", round(sim, 4), round(rank, 4)
        )
    # WHY: companies post one role once per city ("Forward Deployed Engineer (New York)",
    # "... (Atlanta)"). Scoring each copy spends the daily budget on the same answer. Keep the
    # best-ranked copy per (company, base title); mark the rest "duplicate".
    best: set[tuple[str, str]] = set()
    by_id = {j.id: j for j in survivors}
    for jid, v in sorted(out.items(), key=lambda kv: kv[1].rank or 0.0, reverse=True):
        if v.verdict != "pass":
            continue
        key = (by_id[jid].company.lower(), base_title(by_id[jid].title))
        if key in best:
            out[jid] = Verdict("duplicate", v.similarity, v.rank)
        else:
            best.add(key)
    return out


def base_title(title: str) -> str:
    """Title without parenthetical or trailing qualifier: "FDE (Atlanta)" -> "fde"."""
    t = re.sub(r"\([^)]*\)", " ", title.lower())
    t = re.split(r"\s[-|,]\s", t)[0]
    return re.sub(r"\s+", " ", t).strip()
