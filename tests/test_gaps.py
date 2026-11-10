"""Skill-gap aggregation (offline)."""

from __future__ import annotations

from typing import Any

from jobfit.api.gaps import aggregate, bucket_names


def _row(jid: str, score: int, reqs: list[tuple[str, str, str, str]]) -> dict[str, Any]:
    return {
        "id": jid,
        "company": "Co",
        "title": "T",
        "fit_score": score,
        "fit": {
            "verdict": "moderate",
            "assessments": [
                {"status": st, "requirement": {"text": t, "kind": k, "category": c}}
                for t, k, c, st in reqs
            ],
        },
    }


def test_bucket_names() -> None:
    assert bucket_names("Kubernetes and Terraform", "tool") == ["Kubernetes", "Terraform"]
    assert bucket_names("5+ years with Spring Boot", "tool") == ["Spring Boot"]
    assert bucket_names("8+ years", "years_experience") == ["More years of experience"]
    assert bucket_names("Strong written communication!", "soft_skill") == [
        "Strong written communication"
    ]


def test_aggregate_ranks_by_weighted_frequency() -> None:
    rows = [
        _row(
            "a",
            80,
            [
                ("Kubernetes", "required", "tool", "missing"),
                ("Python", "required", "technical_skill", "met"),
            ],
        ),
        _row(
            "b",
            60,
            [
                ("Kubernetes and Helm", "preferred", "tool", "missing"),
                ("Azure", "required", "tool", "partial"),
            ],
        ),
        _row("c", 20, [("Java", "required", "technical_skill", "missing")]),
    ]
    out = aggregate(rows)
    assert out[0]["name"] == "Kubernetes" and out[0]["jobs"] == 2 and out[0]["weight"] == 1.5
    assert {g["name"] for g in out} == {"Kubernetes", "Helm", "Azure", "Java"}
    assert "Java" not in {g["name"] for g in aggregate(rows, min_fit=50)}


def test_soft_skills_excluded_by_default() -> None:
    rows = [
        _row(
            "a",
            80,
            [
                ("Executive presence", "required", "soft_skill", "missing"),
                ("Go", "required", "technical_skill", "missing"),
            ],
        )
    ]
    assert [g["name"] for g in aggregate(rows)] == ["Go"]
    assert len(aggregate(rows, include_soft=True)) == 2
