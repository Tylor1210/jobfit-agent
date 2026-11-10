from __future__ import annotations

from factories import letter, resume

from jobfit.graphs.verify import (
    hallucination_count,
    letter_words,
    verify_cover_letter,
    verify_resume,
)
from jobfit.profile.loader import load_constraints, load_profile
from jobfit.schemas import (
    Assessment,
    DraftBulletOut,
    DraftJobOut,
    FitResult,
    ParagraphOut,
    Requirement,
)

P = load_profile()
C = load_constraints()


def _fit_with_gap(text: str) -> FitResult:
    r = Requirement(id="r1", text=text, kind="preferred", category="tool")
    return FitResult(
        fit_score=70,
        verdict="moderate",
        verdict_line="ok",
        top_gaps=[text],
        assessments=[Assessment(requirement=r, status="missing", evidence=[], note="")],
    )


def rules(issues: list) -> set[str]:  # type: ignore[type-arg]
    return {i.rule for i in issues}


def test_clean_drafts_pass() -> None:
    assert verify_resume(resume(), P, C) == []
    lt = letter()
    assert 250 <= letter_words(lt) <= 350
    assert verify_cover_letter(lt, P, C, _fit_with_gap("Kubernetes")) == []


def test_invented_number_is_caught() -> None:
    bad = resume(
        jobs=[
            DraftJobOut(
                job_id="exp.fairman",
                bullets=[
                    DraftBulletOut(
                        text="Eliminated 99% of invoicing errors.", source_ids=["exp.fairman.2"]
                    )
                ],
            )
        ]
    )
    issues = verify_resume(bad, P, C)
    assert "unsupported_number" in rules(issues)
    assert hallucination_count(issues) >= 1


def test_bullet_moved_between_roles_is_caught() -> None:
    bad = resume(
        jobs=[
            DraftJobOut(
                job_id="exp.linkme",
                bullets=[
                    DraftBulletOut(
                        text="Built database-layer validation pipelines.",
                        source_ids=["exp.fairman.2"],
                    )
                ],
            )
        ]
    )
    assert "bullet_from_other_role" in rules(verify_resume(bad, P, C))


def test_unknown_tool_title_and_hard_rules() -> None:
    bad = resume(
        summary="Senior engineer — passionate about Kafka and Kubernetes.",
        summary_evidence_ids=["sum.1"],
    )
    got = rules(verify_resume(bad, P, C))
    assert {
        "unsupported_tool",
        "unsupported_title",
        "forbidden_char",
        "banned_phrase",
        "unclaimable_tool",
    } <= got


def test_gap_paragraph_may_name_the_gap_but_others_may_not() -> None:
    fit = _fit_with_gap("Kubernetes")
    lt = letter()
    assert verify_cover_letter(lt, P, C, fit) == []
    paras = list(lt.paragraphs)
    paras[1] = paras[1].model_copy(update={"text": paras[1].text + " I also run Kubernetes."})
    got = rules(verify_cover_letter(lt.model_copy(update={"paragraphs": paras}), P, C, fit))
    assert "unclaimable_tool" in got


def test_letter_shape_rules() -> None:
    lt = letter(
        paragraphs=[ParagraphOut(kind="opening", text="I am writing to apply.", evidence_ids=[])]
    )
    got = rules(verify_cover_letter(lt, P, C))
    assert {"paragraph_count", "word_count", "no_gap", "generic_opener"} <= got


def test_page_count_check_uses_renderer() -> None:
    assert "page_count" in rules(verify_resume(resume(), P, C, page_count=lambda r: 2))
    assert verify_resume(resume(), P, C, page_count=lambda r: 1) == []


def test_resume_voice() -> None:
    bad = resume(
        summary="Tylor is a systems architect. He built agent pipelines.",
        summary_evidence_ids=["sum.1"],
    )
    assert "resume_voice" in rules(verify_resume(bad, P, C))


def test_invented_platform_feature_is_caught() -> None:
    lt = letter()
    paras = list(lt.paragraphs)
    paras[1] = paras[1].model_copy(update={"text": paras[1].text + " I used Workers KV too."})
    got = rules(verify_cover_letter(lt.model_copy(update={"paragraphs": paras}), P, C))
    assert "unsupported_tool" in got


def test_clean_model_maps_lookalikes_but_keeps_em_dash() -> None:
    from jobfit.graphs.tailor import clean_model

    r = clean_model(resume(summary="Six\u2011stage $100\u00a0M pipeline \u2014 shipped."))
    assert r.summary == "Six-stage $100 M pipeline \u2014 shipped."


def test_employer_name_is_not_an_unsupported_tool() -> None:
    lt = letter()
    paras = list(lt.paragraphs)
    paras[0] = paras[0].model_copy(update={"text": paras[0].text + " Smartsheet is the place."})
    edited = lt.model_copy(update={"paragraphs": paras})
    fit = _fit_with_gap("Kubernetes")
    assert "unsupported_tool" in rules(verify_cover_letter(edited, P, C, fit))
    assert verify_cover_letter(edited, P, C, fit, allow_terms=["Smartsheet"]) == []
