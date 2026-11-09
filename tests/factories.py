"""Builders for valid (and deliberately broken) LLM outputs used across graph tests."""

from __future__ import annotations

from jobfit.schemas import (
    CoverLetterOut,
    DraftBulletOut,
    DraftJobOut,
    EmphasisOut,
    ParagraphOut,
    ResumeOut,
    SkillRowOut,
)


def resume(**overrides: object) -> ResumeOut:
    base = ResumeOut(
        summary=(
            "Systems Architect with 4+ years building AI agent workflows and full-stack "
            "platforms on Cloudflare Workers, with automated verification that blocks "
            "hallucinated claims before anything ships."
        ),
        summary_evidence_ids=["sum.1", "exp.tycodes.2"],
        skill_rows=[
            SkillRowOut(
                label="AI & Agent Systems",
                skill_ids=["skill.tool_calling", "skill.anthropic_api", "skill.icm"],
            ),
            SkillRowOut(label="Languages", skill_ids=["skill.python", "skill.typescript"]),
        ],
        jobs=[
            DraftJobOut(
                job_id="exp.tycodes",
                bullets=[
                    DraftBulletOut(
                        text="Built a 6-stage agent pipeline (intake, collect, brief, "
                        "config, verify, publish) with automated verification that "
                        "stops hallucinated pricing and claims.",
                        source_ids=["exp.tycodes.2"],
                    ),
                    DraftBulletOut(
                        text="Built tycodes.app, which turns a business URL into a 3D "
                        "scroll demo in about 1 min on Cloudflare Workers.",
                        source_ids=["exp.tycodes.1"],
                    ),
                ],
            ),
            DraftJobOut(
                job_id="exp.fairman",
                bullets=[
                    DraftBulletOut(
                        text="Built database-layer validation pipelines that eliminated "
                        "98% of invoicing errors.",
                        source_ids=["exp.fairman.2"],
                    ),
                ],
            ),
        ],
    )
    return base.model_copy(update=overrides)


_P1 = (
    "The Forward Deployed Engineer role caught my attention because it puts the engineer "
    "inside the client's problem, working on agent workflows that have to hold up in real "
    "use. That is the work I have been doing on my own products, and I want to do it next to "
    "customers every day. I like work where the result is judged by whether a client can "
    "actually use it on Monday, not by how the demo looked on Friday."
)
_P2 = (
    "My main project, tycodes.app, turns a business URL into a live product demo in about 1 "
    "min. It runs a 6-stage agent pipeline on Cloudflare Workers and Workflows. Models invent "
    "prices when they are unsure, so I added a verify stage that stops unsupported claims "
    "before anything is published. At Fairman & Associates I moved the team off legacy "
    "desktop tools and built validation at the database layer that removed 98% of invoicing "
    "errors."
)
_P3 = (
    "Where I am thinner is Kubernetes. I have shipped on serverless platforms instead, so "
    "running clusters in production would be new to me, and I would plan to close that gap "
    "early by pairing with your platform team and running a small service end to end."
)
_P4 = (
    "I would like to talk about the client problems on your list and how I could help with "
    "them. I am based in South Florida and can be in the Fort Lauderdale office as often as "
    "the team needs. Thank you for your time, and I hope we can find a time to speak soon "
    "about the role and the team."
)


def letter(**overrides: object) -> CoverLetterOut:
    base = CoverLetterOut(
        greeting="Dear TribalScale Hiring Team,",
        paragraphs=[
            ParagraphOut(kind="opening", text=_P1, evidence_ids=["exp.tycodes.2"]),
            ParagraphOut(
                kind="evidence",
                text=_P2,
                evidence_ids=["exp.tycodes.1", "exp.tycodes.2", "exp.fairman.2"],
            ),
            ParagraphOut(kind="gap", text=_P3, evidence_ids=[]),
            ParagraphOut(kind="closing", text=_P4, evidence_ids=[]),
        ],
    )
    return base.model_copy(update=overrides)


def plan() -> EmphasisOut:
    return EmphasisOut(
        angle="Agent engineer who ships verified pipelines for clients.",
        skill_ids=["skill.tool_calling", "skill.python"],
        bullet_ids=["exp.tycodes.2", "exp.tycodes.1", "exp.fairman.2"],
        posting_hooks=["AI agent workflows with tool calling", "enterprise clients"],
    )
