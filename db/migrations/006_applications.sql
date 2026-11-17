-- Application tracking. Email is the source of truth; manual and quick-add entries fill gaps.
-- CONSTRAINT: never store full email bodies. Events keep extracted fields + a <=200 char snippet.

create table if not exists applications (
    id                text primary key,
    company           text not null,
    company_norm      text not null,
    role              text,                 -- null when the email never named the role
    status            text not null default 'applied'
        check (status in ('applied', 'screening', 'interview', 'offer',
                          'rejected', 'withdrawn', 'ghosted')),
    source            text not null check (source in ('email', 'manual', 'quick_add', 'follow_up')),
    channel           text not null default 'other',  -- linkedin | company_site | recruiter | referral | other
    applied_at        timestamptz,
    last_activity_at  timestamptz not null default now(),
    job_id            text references jobs(id) on delete set null,  -- linked posting, if any
    resume_version    text,
    salary_entered    text,
    notes             text,
    needs_review      boolean not null default false,
    review_reason     text,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);
create index if not exists applications_company_idx on applications (company_norm);
create index if not exists applications_status_idx on applications (status, last_activity_at);

create table if not exists application_events (
    id              text primary key,
    application_id  text references applications(id) on delete cascade,  -- null = unmatched
    company         text,
    kind            text not null,   -- mail classification, or manual_update / status_change
    occurred_at     timestamptz not null,
    account         text,            -- mail account label from config/mail.yaml, never an address
    message_id      text,            -- RFC Message-ID (falls back to the provider's id)
    snippet         text check (snippet is null or length(snippet) <= 200),
    extracted       jsonb not null default '{}',
    confidence      real,
    injection_flags jsonb not null default '[]',
    needs_review    boolean not null default false,
    review_reason   text,
    created_at      timestamptz not null default now()
);
-- Dedup: the same message seen twice (rerun, overlap window, CC'd to two inboxes) is one event.
create unique index if not exists application_events_msg_idx
    on application_events (message_id) where message_id is not null;
create index if not exists application_events_app_idx on application_events (application_id, occurred_at);
create index if not exists application_events_review_idx on application_events (needs_review)
    where needs_review;

create table if not exists mail_sync_state (
    account         text primary key,
    last_synced_at  timestamptz,
    last_error      text,
    updated_at      timestamptz not null default now()
);

-- One run log for the Ops page: job ingests and mail syncs.
alter table ingest_runs add column if not exists kind text not null default 'jobs';
