"""Pydantic models for the evidence base (profile/master_profile.yaml).

Loaded by jobfit.profile.loader. Every fact carries an `id`; those IDs are what the agent cites
as evidence and what the verifier checks drafts against.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    # WHY: forbid unknown keys so a YAML typo fails loudly instead of silently dropping a fact.
    model_config = ConfigDict(extra="forbid", frozen=True)


class Contact(_Strict):
    name: str
    short_name: str
    email: str
    location: str
    websites: list[str] = Field(default_factory=list)
    github: str | None = None


class SummaryItem(_Strict):
    id: str
    text: str


class Skill(_Strict):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    # Bullet IDs that back a skill not listed on the resume's skills line.
    evidence: list[str] = Field(default_factory=list)


class SkillCategory(_Strict):
    category: str
    render: bool = True
    items: list[Skill]


class Bullet(_Strict):
    id: str
    text: str
    skills: list[str] = Field(default_factory=list)


class Job(_Strict):
    id: str
    company: str
    title: str
    start: str
    end: str
    bullets: list[Bullet]


class Project(_Strict):
    id: str
    name: str
    url: str | None = None
    evidence: list[str]


class MilitaryService(_Strict):
    id: str
    branch: str
    title: str
    start: str
    end: str
    bullets: list[Bullet]


class Education(_Strict):
    id: str
    degree: str
    school: str
    status: Literal["in_progress", "completed"]
    focus: list[str] = Field(default_factory=list)
    honors: list[str] = Field(default_factory=list)


class Certification(_Strict):
    id: str
    name: str


class Profile(_Strict):
    contact: Contact
    years_experience: int
    summary: list[SummaryItem]
    skills: list[SkillCategory]
    experience: list[Job]
    projects: list[Project] = Field(default_factory=list)
    military: list[MilitaryService] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    partner_programs: list[Certification] = Field(default_factory=list)

    def all_skills(self) -> list[Skill]:
        return [s for cat in self.skills for s in cat.items]

    def all_bullets(self) -> list[Bullet]:
        return [b for j in self.experience for b in j.bullets] + [
            b for m in self.military for b in m.bullets
        ]

    def evidence_index(self) -> dict[str, str]:
        """Map every citable ID to a short human-readable text. Used by verify and the dashboard."""
        idx: dict[str, str] = {s.id: s.text for s in self.summary}
        idx["contact.location"] = f"Based in {self.contact.location}"
        for cat in self.skills:
            for sk in cat.items:
                idx[sk.id] = f"Skill: {sk.name}"
        for job in self.experience:
            idx[job.id] = f"{job.title} | {job.company} ({job.start} to {job.end})"
            idx.update({b.id: b.text for b in job.bullets})
        for mil in self.military:
            idx[mil.id] = f"{mil.title} | {mil.branch} ({mil.start} to {mil.end})"
            idx.update({b.id: b.text for b in mil.bullets})
        for p in self.projects:
            idx[p.id] = f"Project: {p.name}"
        for e in self.education:
            status = "In progress" if e.status == "in_progress" else "Completed"
            idx[e.id] = f"{e.degree}, {e.school} ({status})"
        for c in [*self.certifications, *self.partner_programs]:
            idx[c.id] = f"Certification: {c.name}"
        return idx
