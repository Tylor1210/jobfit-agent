"""Hard rules from profile/constraints.yaml plus the text checks that enforce them.

Used by tests now and by the `verify` node in the tailor graph. The checks here are deterministic
(no LLM), so they are cheap and can never be talked out of a rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class DegreeRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    has_bachelors: bool
    bas_status_label: str
    forbidden_claims: list[str]


class LengthRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    min_words: int = 0
    max_words: int = 10_000
    paragraphs: int | None = None


class ResumeRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_pages: int = 1


class Constraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    require_evidence: bool = True
    forbidden_tools: list[str]
    pending_tools: list[str] = Field(default_factory=list)
    degrees: DegreeRules
    forbidden_chars: list[str]
    banned_phrases: list[str]
    cover_letter: LengthRule
    resume: ResumeRule

    @property
    def unclaimable_tools(self) -> list[str]:
        """Tools that drafts may never claim: never used, or not yet earned by this project."""
        return [*self.forbidden_tools, *self.pending_tools]


@dataclass(frozen=True, slots=True)
class Violation:
    rule: str
    detail: str


def _word_pattern(term: str) -> re.Pattern[str]:
    # WHY: boundaries stop short terms matching inside unrelated words ("Chef" in "Chefchaouen",
    # "BA in" in "Cuba in"). Lookarounds instead of \b so terms ending in punctuation still work.
    return re.compile(rf"(?<![\w]){re.escape(term)}(?![\w])", re.IGNORECASE)


def check_text(text: str, c: Constraints) -> list[Violation]:
    """Return every hard-rule violation in a piece of generated text."""
    out: list[Violation] = []
    for ch in c.forbidden_chars:
        if ch in text:
            out.append(Violation("forbidden_char", f"contains {ch!r} (U+{ord(ch):04X})"))
    for phrase in c.banned_phrases:
        if _word_pattern(phrase).search(text):
            out.append(Violation("banned_phrase", phrase))
    for tool in c.unclaimable_tools:
        if _word_pattern(tool).search(text):
            out.append(Violation("unclaimable_tool", tool))
    for claim in c.degrees.forbidden_claims:
        if _word_pattern(claim).search(text):
            out.append(Violation("degree_claim", claim))
    return out
