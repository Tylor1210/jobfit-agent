"""Turn the profile into small, citable chunks for vector search.

Called by the RAG indexer. One chunk per fact (bullet, skill, degree, cert) so a search hit maps
straight to one evidence ID. Each chunk's text carries a little context (role, category) because
bare bullets embed poorly for queries like "customer-facing implementation experience".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from jobfit.profile.models import Profile


@dataclass(frozen=True, slots=True)
class Chunk:
    id: str  # the evidence ID
    kind: str  # bullet | skill | summary | education | certification | project
    text: str
    meta: dict[str, str] = field(default_factory=dict)

    def content_hash(self, model: str) -> str:
        # WHY: the indexer skips re-embedding a chunk unless its text or the embedding model
        # changed. Including the model stops vectors from two models mixing in one table.
        return hashlib.sha256(f"{model}\n{self.text}".encode()).hexdigest()[:16]


def build_chunks(p: Profile) -> list[Chunk]:
    out: list[Chunk] = [Chunk(s.id, "summary", s.text) for s in p.summary]

    for cat in p.skills:
        for sk in cat.items:
            alias = f" (also: {', '.join(sk.aliases)})" if sk.aliases else ""
            out.append(
                Chunk(
                    sk.id,
                    "skill",
                    f"Skill in {cat.category}: {sk.name}{alias}",
                    {"category": cat.category},
                )
            )

    for job in p.experience:
        for b in job.bullets:
            out.append(
                Chunk(
                    b.id,
                    "bullet",
                    f"{job.title} at {job.company}: {b.text}",
                    {"parent": job.id, "company": job.company},
                )
            )
    for mil in p.military:
        for b in mil.bullets:
            out.append(
                Chunk(
                    b.id,
                    "bullet",
                    f"{mil.title}, {mil.branch}: {b.text}",
                    {"parent": mil.id, "company": mil.branch},
                )
            )

    for e in p.education:
        status = "in progress" if e.status == "in_progress" else "completed"
        extra = f" Focus: {', '.join(e.focus)}." if e.focus else ""
        out.append(Chunk(e.id, "education", f"{e.degree}, {e.school} ({status}).{extra}"))

    for c in [*p.certifications, *p.partner_programs]:
        out.append(Chunk(c.id, "certification", f"Certification: {c.name}"))

    return out
