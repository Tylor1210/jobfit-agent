"""Load and integrity-check the profile and constraints YAML files.

Called by the CLI, the RAG indexer, and (later) every graph node that needs evidence.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from jobfit.config import get_settings
from jobfit.profile.constraints import Constraints, check_text
from jobfit.profile.models import Profile


class ProfileIntegrityError(ValueError):
    """The profile breaks one of its own invariants (duplicate IDs, dangling refs, rule breaks)."""


def _read_yaml(path: Path) -> object:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_constraints(path: Path | None = None) -> Constraints:
    path = path or get_settings().profile_dir / "constraints.yaml"
    return Constraints.model_validate(_read_yaml(path))


def validate_profile(profile: Profile, constraints: Constraints) -> list[str]:
    """Return a list of integrity problems. Empty list means the profile is safe to cite."""
    problems: list[str] = []

    ids: list[str] = []
    ids += [s.id for s in profile.summary]
    ids += [sk.id for sk in profile.all_skills()]
    for job in profile.experience:
        ids.append(job.id)
    for mil in profile.military:
        ids.append(mil.id)
    ids += [b.id for b in profile.all_bullets()]
    ids += [p.id for p in profile.projects]
    ids += [e.id for e in profile.education]
    ids += [c.id for c in [*profile.certifications, *profile.partner_programs]]
    seen: set[str] = set()
    for i in ids:
        if i in seen:
            problems.append(f"duplicate id: {i}")
        seen.add(i)

    skill_ids = {sk.id for sk in profile.all_skills()}
    bullet_ids = {b.id for b in profile.all_bullets()}
    for b in profile.all_bullets():
        problems += [f"{b.id} references unknown skill {s}" for s in b.skills if s not in skill_ids]
    for sk in profile.all_skills():
        bad = [e for e in sk.evidence if e not in bullet_ids]
        problems += [f"{sk.id} cites unknown bullet {e}" for e in bad]
    for p in profile.projects:
        problems += [f"{p.id} cites unknown bullet {e}" for e in p.evidence if e not in bullet_ids]

    # CONSTRAINT: the profile itself must never contain a tool Tylor has not used.
    lowered_names = {n.lower() for sk in profile.all_skills() for n in [sk.name, *sk.aliases]}
    for tool in constraints.unclaimable_tools:
        if tool.lower() in lowered_names:
            problems.append(f"profile lists unclaimable tool: {tool}")

    if not constraints.degrees.has_bachelors:
        for e in profile.education:
            if e.degree.lower().startswith("bachelor") and e.status != "in_progress":
                problems.append(f"{e.id} is a bachelor's degree but not marked in_progress")

    # Bullets get reworded into drafts, so they must already satisfy the char rule.
    for b in profile.all_bullets():
        for v in check_text(b.text, constraints):
            if v.rule == "forbidden_char":
                problems.append(f"{b.id}: {v.detail}")
    return problems


@lru_cache(maxsize=4)
def profile_version(path: Path | None = None) -> str:
    """Short hash of the evidence base. Stamped on every fit so scores made against an older
    resume can be found and rescored (`jobfit rescore`, and the daily run's catch-up step)."""
    import hashlib

    path = path or get_settings().profile_dir / "master_profile.yaml"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


@lru_cache(maxsize=4)
def load_profile(path: Path | None = None) -> Profile:
    path = path or get_settings().profile_dir / "master_profile.yaml"
    profile = Profile.model_validate(_read_yaml(path))
    problems = validate_profile(profile, load_constraints(path.parent / "constraints.yaml"))
    if problems:
        raise ProfileIntegrityError("; ".join(problems))
    return profile
