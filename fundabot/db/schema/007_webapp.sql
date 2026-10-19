CREATE TABLE news (
    id text PRIMARY KEY,
    cik int NOT NULL,
    published timestamptz NOT NULL,
    publisher text,
    site text,
    title text NOT NULL,
    url text NOT NULL,
    text text,
    kind text NOT NULL,
    fetched_at timestamptz DEFAULT now()
);
CREATE INDEX news_cik_published ON news (cik, published DESC);
CREATE TABLE narratives (
    cik int NOT NULL,
    window_days int NOT NULL,
    inputs_sha text NOT NULL,
    output jsonb NOT NULL,
    dropped int NOT NULL DEFAULT 0,
    cost_usd numeric NOT NULL DEFAULT 0,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (cik, window_days)
);
CREATE TABLE jobs (
    id serial PRIMARY KEY,
    ticker text NOT NULL,
    kind text NOT NULL,
    profile text NOT NULL,
    status text NOT NULL DEFAULT 'running',
    started_at timestamptz DEFAULT now(),
    finished_at timestamptz,
    error text,
    report_path text,
    cost_usd numeric
);
