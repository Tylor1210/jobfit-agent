-- Profile evidence chunks for RAG. One row per citable fact (bullet, skill, degree, cert).
-- CONSTRAINT: vector(384) must match settings.embedding_dim. Changing models means a new migration.
create extension if not exists vector;

create table if not exists profile_chunks (
    id            text primary key,          -- evidence ID from profile/master_profile.yaml
    kind          text not null,
    text          text not null,
    meta          jsonb not null default '{}',
    content_hash  text not null,
    embedding     vector(384) not null,
    updated_at    timestamptz not null default now()
);

-- WHY: no ANN index. ~70 rows, exact scan is faster than HNSW at this size and always correct.
