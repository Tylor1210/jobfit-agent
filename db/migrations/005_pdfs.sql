-- Rendered PDFs stored in Postgres, not on local disk. WHY: Cloud Run's filesystem is wiped on
-- every restart and scale-to-zero, so a file path would point at nothing. A resume or letter is
-- ~20-60 KB; even hundreds fit easily in the Supabase free tier.
create table if not exists tailor_pdfs (
    job_id      text not null references jobs(id) on delete cascade,
    kind        text not null check (kind in ('resume', 'cover')),
    filename    text not null,
    pdf         bytea not null,
    created_at  timestamptz not null default now(),
    primary key (job_id, kind)
);
