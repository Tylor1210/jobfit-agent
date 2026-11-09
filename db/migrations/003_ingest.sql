-- Ingestion: per-board tracking, closing, prefilter results, and a run log for the Ops page.

alter table jobs add column if not exists board text;
alter table jobs add column if not exists remote boolean;
alter table jobs add column if not exists closes_at timestamptz;
alter table jobs add column if not exists closed_at timestamptz;
-- Prefilter outcome: 'pass' or why it was dropped ('title', 'location', 'similarity',
-- 'duplicate' = same role as a better-ranked posting from the same company).
-- NULL means not evaluated yet. Similarity is stored for every job that got that far.
alter table jobs add column if not exists prefilter text;
alter table jobs add column if not exists similarity real;
alter table jobs add column if not exists rank real;  -- similarity + local bonus

create index if not exists jobs_source_board_idx on jobs (source, board) where status = 'open';
drop index if exists jobs_candidates_idx;
create index if not exists jobs_candidates_rank_idx on jobs (rank desc)
    where status = 'open' and prefilter = 'pass' and fit is null;

create table if not exists ingest_runs (
    id           bigserial primary key,
    started_at   timestamptz not null default now(),
    finished_at  timestamptz,
    status       text not null default 'running',  -- running | ok | partial | capped | failed
    -- per source: {"greenhouse": {"fetched": n, "new": n, "closed": n, "errors": [...]}, ...}
    stats        jsonb not null default '{}',
    prefiltered  int not null default 0,
    scored       int not null default 0,
    spend_usd    numeric(12, 6) not null default 0,
    notes        jsonb not null default '[]'
);
create index if not exists ingest_runs_started_idx on ingest_runs (started_at desc);
