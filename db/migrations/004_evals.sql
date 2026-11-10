-- Eval run history for the dashboard's Ops page (latest scores + trend line).
create table if not exists eval_runs (
    id       bigserial primary key,
    run_at   timestamptz not null,
    summary  jsonb not null,   -- jobfit.evals.runner.Summary
    cases    jsonb not null    -- list of CaseResult
);
create index if not exists eval_runs_run_at_idx on eval_runs (run_at desc);
