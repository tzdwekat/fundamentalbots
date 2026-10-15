CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS schema_migrations (
    filename text PRIMARY KEY,
    applied_at timestamptz DEFAULT now()
);

CREATE TABLE raw_companyfacts (
    id serial PRIMARY KEY,
    cik int NOT NULL,
    fetched_at timestamptz DEFAULT now(),
    payload jsonb NOT NULL
);

CREATE TABLE facts (
    cik int NOT NULL,
    taxonomy text NOT NULL,
    tag text NOT NULL,
    unit text NOT NULL,
    start_date date,
    end_date date NOT NULL,
    value numeric NOT NULL,
    fy int,
    fp text,
    form text,
    accession text NOT NULL,
    filed date,
    UNIQUE NULLS NOT DISTINCT (cik, taxonomy, tag, unit, start_date, end_date, accession)
);
CREATE INDEX facts_cik_tag ON facts (cik, tag);

CREATE TABLE filings (
    accession text PRIMARY KEY,
    cik int NOT NULL,
    form text NOT NULL,
    filed date NOT NULL,
    period_end date,
    primary_doc_url text,
    items_ok bool
);

CREATE TABLE filing_sections (
    accession text NOT NULL,
    item text NOT NULL,
    text text NOT NULL,
    PRIMARY KEY (accession, item)
);

CREATE TABLE prices (
    cik int NOT NULL,
    date date NOT NULL,
    close numeric NOT NULL,
    PRIMARY KEY (cik, date)
);

CREATE TABLE chunks (
    id text PRIMARY KEY,
    accession text NOT NULL,
    item text NOT NULL,
    n int NOT NULL,
    text text NOT NULL,
    token_count int NOT NULL,
    embedding vector(1536)
);
CREATE INDEX chunks_accession ON chunks (accession);

CREATE TABLE snapshots (
    id text PRIMARY KEY,
    ticker text NOT NULL,
    cik int NOT NULL,
    companyfacts_fetch_id int,
    accessions jsonb NOT NULL,
    price_date date,
    latest_close numeric,
    shares_outstanding numeric,
    code_sha text,
    created_at timestamptz DEFAULT now()
);

CREATE TABLE metrics (
    snapshot_id text NOT NULL,
    metric_id text NOT NULL,
    period text NOT NULL,
    value numeric,
    unit text NOT NULL,
    formula_id text NOT NULL,
    inputs jsonb NOT NULL DEFAULT '{}',
    flags jsonb NOT NULL DEFAULT '[]',
    tag_used text,
    null_reason text,
    PRIMARY KEY (snapshot_id, metric_id, period)
);

CREATE TABLE red_flags (
    snapshot_id text NOT NULL,
    flag_id text NOT NULL,
    severity text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '[]',
    detail text,
    PRIMARY KEY (snapshot_id, flag_id)
);

CREATE TABLE analyses (
    snapshot_id text NOT NULL,
    persona text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    output jsonb NOT NULL,
    grounded jsonb,
    chunk_ids jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (snapshot_id, persona, provider, model)
);

CREATE TABLE model_calls (
    id serial PRIMARY KEY,
    snapshot_id text,
    persona text,
    provider text NOT NULL,
    model text NOT NULL,
    prompt_sha256 text,
    input_tokens int NOT NULL DEFAULT 0,
    output_tokens int NOT NULL DEFAULT 0,
    latency_ms int,
    cost_usd numeric NOT NULL DEFAULT 0,
    created_at timestamptz DEFAULT now()
);
