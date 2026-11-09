"""Deterministic draft verification. Used by the tailor graph's `verify` node and by evals
(hallucinated-claim count).

Checks, all against profile/master_profile.yaml:
- evidence IDs exist, and resume bullets only reuse bullets from their own role
- every number in a line appears in that line's cited evidence
- every known tech term (config/tech_lexicon.yaml) appears somewhere in the profile
- seniority words not in the profile ("Senior", "Principal", ...)
- hard rules from constraints.yaml (em dash, banned phrases, forbidden tools, degree claims)
- cover letter shape: 4 paragraphs, 250-350 words, a gap paragraph, no generic opener
- resume page count (via an injected renderer)

The LLM claim check (semantic "says more than its evidence") lives in the tailor graph and only
runs once these cheaper checks pass.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache

import yaml

from jobfit.config import ROOT
from jobfit.profile.constraints import Constraints, check_text
from jobfit.profile.models import Profile
from jobfit.schemas import CoverLetterOut, FitResult, Issue, ResumeOut

# Rules that block rendering even if Tylor approves (he can still edit the text to fix them).
HARD_RULES = {"forbidden_char", "banned_phrase", "unclaimable_tool", "degree_claim", "page_count"}

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Resume text uses implied first person: no pronouns, no name.
_RESUME_VOICE = re.compile(
    r"\b(I|I'm|I've|me|my|he|his|him|she|Tylor)\b"
)  # not they/their: often clients
_SENIORITY = re.compile(
    r"\b(Senior|Sr\.|Principal|Staff Engineer|Staff Software|Director|Head of|VP|Vice President|"
    r"Chief|Distinguished)\b"
)


def _nums(text: str) -> set[str]:
    # "8,000+" -> "8000", "$100M+" -> "100", "~1 min" -> "1". Compare cores only.
    return {m.replace(",", "") for m in _NUM.findall(text)}


@lru_cache(maxsize=1)
def tech_terms() -> tuple[str, ...]:
    raw = yaml.safe_load((ROOT / "config" / "tech_lexicon.yaml").read_text())
    return tuple(raw["terms"])


def _term_rx(term: str) -> re.Pattern[str]:
    # GOTCHA: case-sensitive on purpose. Drafts write tech names in their real case, and this
    # keeps "go", "spring", "chef" and "r" in ordinary English from tripping the check.
    return re.compile(rf"(?<![\w.]){re.escape(term)}(?![\w])")


@dataclass(frozen=True, slots=True)
class ProfileFacts:
    index: dict[str, str]  # evidence id -> text
    corpus: str  # lowercased text of the whole profile, for presence checks
    bullet_owner: dict[str, str]  # bullet id -> job/military id
    skill_ids: frozenset[str]
    role_ids: frozenset[str]

    @classmethod
    def of(cls, p: Profile) -> ProfileFacts:
        names = " ".join(n for s in p.all_skills() for n in [s.name, *s.aliases])
        idx = p.evidence_index()
        owner = {b.id: j.id for j in p.experience for b in j.bullets}
        owner |= {b.id: m.id for m in p.military for b in m.bullets}
        roles = {j.id for j in p.experience} | {m.id for m in p.military}
        corpus = " ".join([*idx.values(), names, p.contact.location]).lower()
        return cls(idx, corpus, owner, frozenset(s.id for s in p.all_skills()), frozenset(roles))

    def in_profile(self, term: str) -> bool:
        return re.search(rf"(?<![\w.]){re.escape(term.lower())}(?![\w])", self.corpus) is not None


def _rules(doc: str, text: str, c: Constraints, skip_tools: Iterable[str] = ()) -> list[Issue]:
    skip = {t.lower() for t in skip_tools}
    return [
        Issue(doc=doc, rule=v.rule, detail=f"{v.detail} in: {text[:90]}")
        for v in check_text(text, c)
        if not (v.rule == "unclaimable_tool" and v.detail.lower() in skip)
    ]


def _claim_checks(
    doc: str, text: str, cited: list[str], f: ProfileFacts, allow_terms: Iterable[str] = ()
) -> list[Issue]:
    out: list[Issue] = []
    unknown = [i for i in cited if i not in f.index]
    if unknown:
        out.append(
            Issue(
                doc=doc,
                rule="unknown_evidence_id",
                detail=f"{unknown} in: {text[:90]}",
            )
        )
    # WHY: with no citation, fall back to the whole profile, so uncited lines can still restate
    # profile facts but can never introduce a number that exists nowhere.
    source = " ".join(f.index[i] for i in cited if i in f.index) if cited else f.corpus
    missing_nums = sorted(_nums(text) - _nums(source))
    if missing_nums:
        out.append(
            Issue(
                doc=doc,
                rule="unsupported_number",
                detail=f"{missing_nums} not in cited evidence: {text[:90]}",
            )
        )
    allow = {t.lower() for t in allow_terms}
    for term in tech_terms():
        if term.lower() not in allow and _term_rx(term).search(text) and not f.in_profile(term):
            out.append(
                Issue(
                    doc=doc,
                    rule="unsupported_tool",
                    detail=f"{term!r} is not in the profile: {text[:90]}",
                )
            )
    for m in _SENIORITY.findall(text):
        if not f.in_profile(m):
            out.append(
                Issue(
                    doc=doc,
                    rule="unsupported_title",
                    detail=f"{m!r} is not in the profile: {text[:90]}",
                )
            )
    return out


def _voice(text: str) -> list[Issue]:
    found = sorted(set(_RESUME_VOICE.findall(text)))
    if not found:
        return []
    return [
        Issue(
            doc="resume",
            rule="resume_voice",
            detail=f"drop pronouns/name {found}; use implied first person: {text[:80]}",
        )
    ]


def verify_resume(
    r: ResumeOut, p: Profile, c: Constraints, page_count: Callable[[ResumeOut], int] | None = None
) -> list[Issue]:
    f = ProfileFacts.of(p)
    issues = _rules("resume", r.summary, c)
    issues += _voice(r.summary)
    issues += _claim_checks("resume", r.summary, r.summary_evidence_ids, f)
    for row in r.skill_rows:
        issues += _rules("resume", row.label, c)
        bad = [s for s in row.skill_ids if s not in f.skill_ids]
        if bad:
            issues.append(Issue(doc="resume", rule="unknown_skill_id", detail=str(bad)))
    seen_roles: set[str] = set()
    n_bullets = 0
    for job in r.jobs:
        if job.job_id not in f.role_ids:
            issues.append(Issue(doc="resume", rule="unknown_role", detail=job.job_id))
            continue
        if job.job_id in seen_roles:
            issues.append(Issue(doc="resume", rule="duplicate_role", detail=job.job_id))
        seen_roles.add(job.job_id)
        for b in job.bullets:
            n_bullets += 1
            issues += _rules("resume", b.text, c)
            issues += _voice(b.text)
            if not b.source_ids:
                issues.append(Issue(doc="resume", rule="uncited_bullet", detail=b.text[:90]))
            foreign = [s for s in b.source_ids if f.bullet_owner.get(s) != job.job_id]
            if foreign:
                issues.append(
                    Issue(
                        doc="resume",
                        rule="bullet_from_other_role",
                        detail=f"{foreign} used under {job.job_id}: {b.text[:60]}",
                    )
                )
            issues += _claim_checks("resume", b.text, b.source_ids, f)
    if n_bullets == 0:
        issues.append(Issue(doc="resume", rule="empty", detail="no experience bullets"))
    if page_count and not any(i.rule in HARD_RULES for i in issues):
        pages = page_count(r)
        if pages > c.resume.max_pages:
            issues.append(
                Issue(
                    doc="resume",
                    rule="page_count",
                    detail=f"renders to {pages} pages; cut 2 or 3 bullets or shorten "
                    "the summary to fit 1 page",
                )
            )
    return issues


def letter_words(letter: CoverLetterOut) -> int:
    return sum(len(p.text.split()) for p in letter.paragraphs)


def verify_cover_letter(
    letter: CoverLetterOut, p: Profile, c: Constraints, fit: FitResult | None = None
) -> list[Issue]:
    f = ProfileFacts.of(p)
    issues = _rules("cover_letter", letter.greeting, c)
    # A gap paragraph may *name* a tool the job wants and Tylor lacks. Only those terms, only there.
    gap_terms: list[str] = []
    if fit:
        gap_text = " ".join(a.requirement.text for a in fit.assessments if a.status != "met")
        gap_text += " " + " ".join(fit.top_gaps)
        gap_terms = [
            t
            for t in [*tech_terms(), *c.unclaimable_tools]
            if _term_rx(t).search(gap_text) or t.lower() in gap_text.lower()
        ]
    for para in letter.paragraphs:
        allow = gap_terms if para.kind == "gap" else []
        issues += _rules("cover_letter", para.text, c, skip_tools=allow)
        issues += _claim_checks("cover_letter", para.text, para.evidence_ids, f, allow_terms=allow)
        if para.kind in ("opening", "evidence") and not para.evidence_ids and _nums(para.text):
            issues.append(
                Issue(
                    doc="cover_letter",
                    rule="uncited_paragraph",
                    detail=f"{para.kind} paragraph has numbers but no evidence IDs",
                )
            )
    rule = c.cover_letter
    if rule.paragraphs and len(letter.paragraphs) != rule.paragraphs:
        issues.append(
            Issue(
                doc="cover_letter",
                rule="paragraph_count",
                detail=f"{len(letter.paragraphs)} paragraphs, need {rule.paragraphs}",
            )
        )
    words = letter_words(letter)
    if not rule.min_words <= words <= rule.max_words:
        issues.append(
            Issue(
                doc="cover_letter",
                rule="word_count",
                detail=f"{words} words, need {rule.min_words} to {rule.max_words}",
            )
        )
    if not any(pp.kind == "gap" for pp in letter.paragraphs):
        issues.append(
            Issue(
                doc="cover_letter", rule="no_gap", detail="name one real gap from the job honestly"
            )
        )
    if letter.paragraphs:
        first = letter.paragraphs[0].text.lower()
        for opener in c.generic_openers:
            if first.startswith(opener.lower()):
                issues.append(Issue(doc="cover_letter", rule="generic_opener", detail=opener))
    return issues


def hallucination_count(issues: list[Issue]) -> int:
    """Claims not backed by the profile. The eval target for this number is 0."""
    claim_rules = {
        "unknown_evidence_id",
        "unsupported_number",
        "unsupported_tool",
        "unsupported_title",
        "unclaimable_tool",
        "degree_claim",
        "unsupported_claim",
        "bullet_from_other_role",
        "unknown_skill_id",
        "unknown_role",
    }
    return sum(1 for i in issues if i.rule in claim_rules)
