"""Profile integrity and constraint enforcement. Offline, no model download."""

from __future__ import annotations

import pytest

from jobfit.profile.chunks import build_chunks
from jobfit.profile.constraints import check_text
from jobfit.profile.loader import load_constraints, load_profile, validate_profile
from jobfit.profile.models import Profile


@pytest.fixture(scope="module")
def profile() -> Profile:
    return load_profile()


def test_profile_loads_clean(profile: Profile) -> None:
    assert validate_profile(profile, load_constraints()) == []
    assert profile.contact.name == "Tylor Clont"


def test_every_chunk_is_a_citable_id(profile: Profile) -> None:
    idx = profile.evidence_index()
    chunks = build_chunks(profile)
    assert len({c.id for c in chunks}) == len(chunks)
    assert all(c.id in idx for c in chunks)


def test_bas_is_in_progress(profile: Profile) -> None:
    bas = next(e for e in profile.education if e.id == "edu.bas_it")
    assert bas.status == "in_progress"
    assert "In progress" in profile.evidence_index()["edu.bas_it"]


def test_no_unclaimable_tools_in_profile(profile: Profile) -> None:
    names = " ".join(n for sk in profile.all_skills() for n in [sk.name, *sk.aliases]).lower()
    for tool in ["terraform", "ansible", "puppet", "kubernetes", "jenkins", "chef", "docker"]:
        assert tool not in names


def test_check_text_catches_each_rule() -> None:
    c = load_constraints()
    bad = (
        "I am excited to leverage my bachelor's degree and Kubernetes skills — "
        "a proven track record."
    )
    rules = {v.rule for v in check_text(bad, c)}
    assert rules == {"forbidden_char", "banned_phrase", "unclaimable_tool", "degree_claim"}


def test_check_text_allows_clean_text() -> None:
    c = load_constraints()
    ok = (
        "Built a 6-stage agent pipeline on Cloudflare Workers that verifies every claim "
        "before publish. BAS in IT at Broward College, in progress. 04/2026–Present."
    )
    assert check_text(ok, c) == []


def test_word_boundaries() -> None:
    c = load_constraints()
    assert check_text("We visited Chefchaouen and Cuba in May.", c) == []
