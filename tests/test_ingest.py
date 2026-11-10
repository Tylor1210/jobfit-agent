"""Ingestion: adapters, sync/closing rules, prefilter, pipeline, manual add. Offline."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from ingest_fixtures import ADZUNA, ASHBY, GREENHOUSE, LEVER, REMOTEOK, REMOTIVE, USAJOBS

from jobfit.graphs.common import Deps
from jobfit.ingest.config import Board, Prefilter, load_ingest_config
from jobfit.ingest.manual import ManualAddError, from_text, from_url
from jobfit.ingest.prefilter import ProfileMatcher, evaluate, location_ok, title_ok
from jobfit.ingest.sources.base import Batch
from jobfit.ingest.sources.boards import Ashby, Greenhouse, Lever
from jobfit.ingest.sources.feeds import Adzuna, RemoteOK, Remotive, USAJobs
from jobfit.llm.cost import UsageRecord
from jobfit.llm.fake import FakeProvider
from jobfit.rag.embedder import HashEmbedder
from jobfit.schemas import JobPosting, RequirementsOut
from jobfit.store.ingest_store import EMPTY_GUARD, InMemoryIngestStore
from jobfit.store.repo import InMemoryRepo


def mock_client(routes: dict[str, Any], status: int = 200) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        for frag, body in routes.items():
            if frag in str(req.url):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


# ---- adapters ---------------------------------------------------------------------------------


def test_board_adapters_normalize() -> None:
    c = mock_client({"greenhouse.io": GREENHOUSE, "lever.co": LEVER, "ashbyhq.com": ASHBY})
    (gh,) = Greenhouse([Board(token="acme", name="Acme")]).fetch(c)
    (lv,) = Lever([Board(token="acme")]).fetch(c)
    (ab,) = Ashby([Board(token="acme")]).fetch(c)
    assert gh.full_snapshot and lv.full_snapshot and ab.full_snapshot
    j = gh.jobs[0]
    assert j.id == "greenhouse:101" and j.company == "Acme" and j.board == "acme"
    assert "Build AI agents with Python." in j.description and "&lt;" not in j.description
    assert j.pay == "$120,000 to $150,000/yr" and j.remote is True
    lj = lv.jobs[0]
    assert lj.location == "Remote (US)" and lj.pay == "$90,000 to $120,000/yr"
    assert "Requirements\n- 3+ years" in lj.description and lj.posted_at is not None
    assert [x.id for x in ab.jobs] == ["ashby:u-1"]  # unlisted job skipped
    assert ab.jobs[0].pay == "$100K to $130K" and "United States" in (ab.jobs[0].location or "")


def test_feed_adapters_normalize() -> None:
    c = mock_client(
        {
            "remotive.com": REMOTIVE,
            "remoteok.com": REMOTEOK,
            "usajobs.gov": USAJOBS,
            "adzuna.com": ADZUNA,
        }
    )
    (rm,) = Remotive(["software-dev"]).fetch(c)
    (ro,) = RemoteOK().fetch(c)
    (us,) = USAJobs(["it"], 10, "key", "me@example.com").fetch(c)
    (az,) = Adzuna("us", [{"what": "ai", "where": "Florida"}], 10, "id", "secret").fetch(c)
    assert not any(b.full_snapshot for b in (rm, ro, us, az))
    assert rm.jobs[0].url == "https://remotive.com/remote-jobs/7"  # link back, per their terms
    assert [j.id for j in ro.jobs] == ["remoteok:9"]  # legal notice row skipped
    assert ro.jobs[0].pay == "$70,000/yr"
    u = us.jobs[0]
    assert u.closes_at is not None and u.pay == "$80,000 to $104,000/yr"
    assert "- Duty A" in u.description
    assert az.jobs[0].title == "AI Engineer" and az.jobs[0].pay == "$110,000 to $140,000/yr"


def test_failed_fetch_is_contained_and_redacts_keys() -> None:
    c = mock_client({"adzuna.com": {}}, status=500)
    (b,) = Adzuna("us", [{"what": "ai"}], 10, "id", "SECRET123").fetch(c)
    assert b.error and not b.jobs
    assert "SECRET123" not in b.error


# ---- sync and closing -------------------------------------------------------------------------


def _job(n: int, board: str = "acme", source: str = "greenhouse", **kw: Any) -> JobPosting:
    return JobPosting(
        id=f"{source}:{n}",
        source=source,
        external_id=str(n),
        board=board,
        company="Acme",
        title=f"Solutions Engineer {n}",
        description="d",
        **kw,
    )


def test_sync_dedupes_closes_and_reopens() -> None:
    store = InMemoryIngestStore(InMemoryRepo())
    st = store.sync_batch(Batch("greenhouse", "acme", [_job(1), _job(2), _job(3)], True))
    assert (st.new, st.closed) == (3, 0)
    store.repo.set_stage("greenhouse:2", "applied")

    st = store.sync_batch(Batch("greenhouse", "acme", [_job(1)], True))
    assert (st.new, st.closed) == (0, 2)
    jobs = store.repo.jobs
    assert jobs["greenhouse:3"].status == "closed" and jobs["greenhouse:3"].stage == "closed"
    # Tylor's own tracking wins: an applied job closes but keeps its stage.
    assert jobs["greenhouse:2"].status == "closed" and jobs["greenhouse:2"].stage == "applied"

    st = store.sync_batch(Batch("greenhouse", "acme", [_job(1), _job(3)], True))
    assert st.reopened == 1
    reopened = store.repo.get_job("greenhouse:3")
    assert reopened is not None and (reopened.status, reopened.stage) == ("open", "new")


def test_failed_or_empty_fetch_never_mass_closes() -> None:
    store = InMemoryIngestStore(InMemoryRepo())
    store.sync_batch(Batch("greenhouse", "acme", [_job(i) for i in range(EMPTY_GUARD)], True))
    st = store.sync_batch(Batch("greenhouse", "acme", [], True, error="HTTP 500"))
    assert st.closed == 0 and st.errors
    st = store.sync_batch(Batch("greenhouse", "acme", [], True))
    assert st.closed == 0 and "guard" in st.errors[0]
    assert all(j.status == "open" for j in store.repo.jobs.values())


def test_search_sources_close_by_deadline_or_staleness() -> None:
    store = InMemoryIngestStore(InMemoryRepo())
    past = datetime.now(UTC) - timedelta(days=1)
    store.sync_batch(
        Batch("usajobs", "kw", [_job(1, "kw", "usajobs", closes_at=past), _job(2, "kw", "usajobs")])
    )
    assert store.close_stale("usajobs", 7) == 1
    old = store.repo.jobs["usajobs:2"].model_copy(
        update={"last_seen": datetime.now(UTC) - timedelta(days=8)}
    )
    store.repo.jobs["usajobs:2"] = old
    assert store.close_stale("usajobs", 7) == 1


# ---- prefilter --------------------------------------------------------------------------------

PF = load_ingest_config().prefilter


@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("Forward Deployed Engineer", True),
        ("Sr. Solutions Architect", True),
        ("Configuration Specialist II", True),
        ("Staff Software Engineer, AI Engineering", False),
        ("Director, Solutions Engineering", False),
        ("Account Executive", False),
        ("Implementation Intern", False),
    ],
)
def test_title_filter(title: str, ok: bool) -> None:
    assert title_ok(title, PF) is ok


@pytest.mark.parametrize(
    ("title", "loc", "remote", "ok"),
    [
        ("SE", "Remote USA", True, True),
        ("SE", "Remote (unspecified)", True, True),
        ("SE", "Remote (USA & Canada)", True, True),
        ("SE", "United States - Remote", True, True),
        ("SE", "Fort Lauderdale, Florida, United States", False, True),
        ("FDE (Fort Lauderdale/Miami)", "Remote USA", True, True),
        ("SE", "Austin, TX", False, False),
        ("SE", "Munich, DE", True, False),  # was a bug: DE matched Delaware
        ("SE (Remote Eligible in Germany)", "Munich, DE", True, False),
        ("SE", "Remote - Toronto", True, False),
        ("SE", "Remote (Europe)", True, False),
        ("SE", "Remote, Florida", True, True),
    ],
)
def test_location_filter(title: str, loc: str, remote: bool, ok: bool) -> None:
    j = JobPosting(
        id="x",
        source="x",
        external_id="x",
        company="c",
        title=title,
        location=loc,
        remote=remote,
        description="d",
    )
    assert location_ok(j, PF) is ok


def test_prefilter_ranks_local_first_and_marks_duplicates() -> None:
    cfg = Prefilter(**{**PF.model_dump(), "min_similarity": 0.0})
    matcher = ProfileMatcher(load_profile_cached(), HashEmbedder())

    def j(n: int, title: str, loc: str) -> JobPosting:
        return JobPosting(
            id=str(n),
            source="s",
            external_id=str(n),
            company="Acme",
            title=title,
            location=loc,
            remote=True,
            description="AI agents, Python",
        )

    out = evaluate(
        [
            j(1, "Forward Deployed Engineer (New York)", "Remote USA"),
            j(2, "Forward Deployed Engineer (Miami)", "Remote USA"),
            j(3, "Account Executive", "Remote USA"),
        ],
        cfg,
        matcher,
    )
    assert out["3"].verdict == "title"
    assert out["2"].verdict == "pass" and out["1"].verdict == "duplicate"
    assert (out["2"].rank or 0) > (out["1"].rank or 0)


def load_profile_cached() -> Any:
    from jobfit.profile.loader import load_profile

    return load_profile()


# ---- pipeline ---------------------------------------------------------------------------------


class FakeSource:
    name = "greenhouse"

    def __init__(self, jobs: list[JobPosting]) -> None:
        self.jobs = jobs

    def fetch(self, client: httpx.Client) -> list[Batch]:
        return [Batch(self.name, "acme", self.jobs, full_snapshot=True)]


def _pipeline(
    deps: Deps,
    fake: FakeProvider,
    jobs: list[JobPosting],
    top: int = 15,
    store: InMemoryIngestStore | None = None,
) -> Any:
    from test_graphs import _script_fit

    from jobfit.ingest.config import SourceKeys, Watchlist
    from jobfit.ingest.pipeline import run_ingest

    assert isinstance(deps.repo, InMemoryRepo)
    store = store or InMemoryIngestStore(deps.repo)
    _script_fit(fake)
    base = load_ingest_config()
    # HashEmbedder similarities are not on bge's scale, so disable the floor here.
    pf = base.prefilter.model_copy(update={"min_similarity": 0.0})
    cfg = base.model_copy(update={"score_top_n": top, "prefilter": pf})
    rep = run_ingest(
        deps, store, cfg, Watchlist(), SourceKeys(), HashEmbedder(), sources=[FakeSource(jobs)]
    )
    return rep, store


def _cand(n: int, title: str = "Forward Deployed Engineer", loc: str = "Remote USA") -> JobPosting:
    return JobPosting(
        id=f"greenhouse:{n}",
        source="greenhouse",
        external_id=str(n),
        board="acme",
        company=f"Co{n}",
        title=title,
        location=loc,
        remote=True,
        description="Build AI agents with tool calling in Python and TypeScript.",
    )


def test_pipeline_scores_top_n_once(deps: Deps, fake: FakeProvider) -> None:
    jobs = [_cand(i) for i in range(4)] + [_cand(9, "Account Executive")]
    rep, store = _pipeline(deps, fake, jobs, top=3)
    assert rep.status == "ok" and rep.per_source["greenhouse"].new == 5
    assert rep.prefilter == {"pass": 4, "title": 1}
    assert len(rep.scored) == 3 and rep.spend_usd > 0
    assert store.runs[rep.run_id]["scored"] == 3

    rep2, _ = _pipeline(deps, fake, jobs, top=3, store=store)  # same day, same jobs
    assert rep2.prefilter == {} and len(rep2.scored) == 1  # only the one left unscored


def test_pipeline_stops_cleanly_at_cost_cap(deps: Deps, fake: FakeProvider) -> None:
    deps.llm.ledger.record(UsageRecord("x", "n", "fake", "m", 1, 1, 999.0, 1))
    rep, store = _pipeline(deps, fake, [_cand(1), _cand(2)])
    assert rep.status == "capped" and rep.scored == []
    assert store.runs[rep.run_id]["status"] == "capped"
    assert rep.per_source["greenhouse"].new == 2  # ingestion itself still ran


# ---- manual add -------------------------------------------------------------------------------


def test_manual_add_rules() -> None:
    with pytest.raises(ManualAddError, match="Paste the posting text"):
        from_url("https://www.linkedin.com/jobs/view/123")
    with pytest.raises(ManualAddError, match="Paste the posting text"):
        from_url("https://indeed.com/viewjob?jk=1")
    c = mock_client({"boards-api.greenhouse.io/v1/boards/acme/jobs/101": GREENHOUSE["jobs"][0]})
    j = from_url("https://job-boards.greenhouse.io/acme/jobs/101", client=c)
    assert j.id == "greenhouse:101"
    with pytest.raises(ManualAddError, match="too short"):
        from_text("short", "Acme", "SE")
    assert from_text("x" * 300, "Acme", "SE").source == "manual"


# ---- Postgres ---------------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="needs DATABASE_URL")
def test_pg_sync_semantics() -> None:
    import uuid

    import psycopg

    from jobfit.store.ingest_store import PgIngestStore

    dsn = os.environ["DATABASE_URL"]
    board = f"pytest-{uuid.uuid4().hex[:6]}"
    store = PgIngestStore(dsn)

    def jb(n: int) -> JobPosting:
        return _job(n, board=board, source="pytest").model_copy(
            update={"id": f"pytest:{board}:{n}"}
        )

    try:
        st = store.sync_batch(Batch("pytest", board, [jb(1), jb(2)], True))
        assert (st.new, st.closed) == (2, 0)
        st = store.sync_batch(Batch("pytest", board, [jb(1)], True))
        assert (st.new, st.closed) == (0, 1)
        st = store.sync_batch(Batch("pytest", board, [jb(1), jb(2)], True))
        assert (st.new, st.reopened) == (0, 1)
        store.save_prefilter(f"pytest:{board}:1", "pass", 0.7, 0.78)
        assert [j.id for j in store.candidates(5) if j.board == board] == [f"pytest:{board}:1"]
        rid = store.start_run()
        store.finish_run(rid, status="ok", stats={"x": 1}, notes=["n"], scored=0)
    finally:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("delete from jobs where source = 'pytest'")
            c.execute("delete from ingest_runs where stats = '{\"x\": 1}'::jsonb")


def test_one_source_db_failure_does_not_stop_the_run(deps: Deps, fake: FakeProvider) -> None:
    class FlakyStore(InMemoryIngestStore):
        def sync_batch(self, b: Batch) -> Any:
            if b.board == "bad":
                raise TimeoutError("db dropped")
            return super().sync_batch(b)

    class BadSource(FakeSource):
        name = "lever"

        def fetch(self, client: httpx.Client) -> list[Batch]:
            return [Batch(self.name, "bad", self.jobs, full_snapshot=True)]

    from test_graphs import _script_fit

    from jobfit.ingest.config import SourceKeys, Watchlist
    from jobfit.ingest.pipeline import run_ingest

    assert isinstance(deps.repo, InMemoryRepo)
    store = FlakyStore(deps.repo)
    _script_fit(fake)
    rep = run_ingest(
        deps,
        store,
        load_ingest_config(),
        Watchlist(),
        SourceKeys(),
        HashEmbedder(),
        sources=[BadSource([_cand(1)]), FakeSource([_cand(2)])],
        score=False,
    )
    assert rep.status == "partial"
    assert "sync failed" in rep.per_source["lever"].errors[0]
    assert rep.per_source["greenhouse"].new == 1
    assert store.runs[rep.run_id]["status"] == "partial"


def test_remote_restriction_in_description_and_mojibake() -> None:
    from jobfit.ingest.text import html_to_text

    j = JobPosting(
        id="x",
        source="remoteok",
        external_id="x",
        company="c",
        title="SE",
        location="Remote (unspecified)",
        remote=True,
        description="Remote - Mostly remote (within Germany), monthly Berlin visits.",
    )
    assert location_ok(j, PF) is False
    us = j.model_copy(update={"description": "Remote anywhere in the US or Germany."})
    assert location_ok(us, PF) is True
    assert html_to_text("EUR 53,000 \u00e2\u0080\u0093 59,000") == "EUR 53,000 \u2013 59,000"
    assert html_to_text("caf\u00e9") == "caf\u00e9"  # already-correct text untouched


def test_pipeline_stops_on_provider_daily_quota(deps: Deps, fake: FakeProvider) -> None:
    from jobfit.llm.base import QuotaExhausted

    def exhausted(system: str, user: str) -> Any:
        raise QuotaExhausted("groq daily token limit reached")

    _, store = _pipeline(deps, fake, [_cand(1), _cand(2)])  # scores both normally
    fake2 = FakeProvider().script(RequirementsOut, exhausted)
    deps.llm.provider = fake2
    rep2, _ = _pipeline(deps, fake2, [_cand(3), _cand(4)], store=store)
    assert rep2.status == "capped" and rep2.scored == []
    assert len(fake2.calls) == 1  # stopped after the first quota error, not once per job
    assert "provider daily quota" in rep2.notes[0]
