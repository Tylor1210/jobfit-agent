-- Jobs, fit results, tailoring drafts, and LLM spend. Idempotent.

create table if not exists jobs (
    id            text primary key,              -- "{source}:{external_id}"
    source        text not null,
    external_id   text not null,
    company       text not null,
    title         text not null,
    location      text,
    url           text,
    description   text not null,
    pay           text,
    posted_at     timestamptz,
    first_seen    timestamptz not null default now(),
    last_seen     timestamptz not null default now(),
    status        text not null default 'open'  check (status in ('open', 'closed')),
    stage         text not null default 'new'
                  check (stage in ('new','shortlisted','applied','interview','offer','rejected','closed')),
    fit_score     int,
    fit           jsonb,                         -- FitResult
    injection_flags jsonb not null default '[]',
    scored_at     timestamptz,
    unique (source, external_id)
);
create index if not exists jobs_stage_idx on jobs (stage);
create index if not exists jobs_fit_score_idx on jobs (fit_score desc nulls last);

create table if not exists tailor_runs (
    job_id        text primary key references jobs(id) on delete cascade,
    thread_id     text not null,                 -- LangGraph checkpoint thread
    status        text not null,                 -- drafting | awaiting_review | rendered | failed
    drafts        jsonb,
    issues        jsonb not null default '[]',
    resume_pdf    text,
    cover_pdf     text,
    trace_url     text,
    updated_at    timestamptz not null default now()
);

create table if not exists llm_usage (
    id            bigserial primary key,
    run_id        text not null,
    node          text not null,
    provider      text not null,
    model         text not null,
    input_tokens  int not null,
    output_tokens int not null,
    cost_usd      numeric(12, 6) not null,
    latency_ms    int not null,
    created_at    timestamptz not null default now()
);
create index if not exists llm_usage_created_idx on llm_usage (created_at);
