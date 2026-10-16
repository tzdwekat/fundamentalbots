CREATE TABLE transcripts (
    cik int NOT NULL, fy int NOT NULL, fq int NOT NULL, call_date date, content text NOT NULL,
    fetched_at timestamptz DEFAULT now(), PRIMARY KEY (cik, fy, fq)
);
CREATE TABLE guidance (
    cik int NOT NULL, fy int NOT NULL, fq int NOT NULL, statements jsonb NOT NULL DEFAULT '[]', tone text, tone_quote text, tone_chunk text,
    model text, created_at timestamptz DEFAULT now(), PRIMARY KEY (cik, fy, fq)
);
CREATE TABLE institutional (
    cik int NOT NULL, quarter_end date NOT NULL, holders int, holders_change int, shares numeric, shares_change numeric,
    fetched_at timestamptz DEFAULT now(), PRIMARY KEY (cik, quarter_end)
);
CREATE TABLE insiders (
    cik int NOT NULL, year int NOT NULL, quarter int NOT NULL, acquired numeric, disposed numeric, purchases int, sales int,
    fetched_at timestamptz DEFAULT now(), PRIMARY KEY (cik, year, quarter)
);
CREATE TABLE watchlist (
    ticker text PRIMARY KEY, cik int NOT NULL, added_at timestamptz DEFAULT now(), note text, last_poll timestamptz
);
CREATE TABLE run_queue (
    id serial PRIMARY KEY, ticker text NOT NULL, cik int NOT NULL, period_key text NOT NULL, reason text, accession text,
    queued_at timestamptz DEFAULT now(), not_before timestamptz, started_at timestamptz, finished_at timestamptz,
    status text NOT NULL DEFAULT 'queued', error text, UNIQUE (cik, period_key)
);
CREATE TABLE outcomes (
    snapshot_id text NOT NULL, run_kind text NOT NULL, horizon_days int NOT NULL, t0 date, t1 date,
    ticker_return numeric, spy_return numeric, excess_return numeric, next_eps_surprise numeric, max_drawdown numeric, reason text,
    computed_at timestamptz DEFAULT now(), PRIMARY KEY (snapshot_id, run_kind, horizon_days)
);
CREATE TABLE calibration_runs (id serial PRIMARY KEY, run_at timestamptz DEFAULT now(), horizon_days int, n int, stats jsonb);
CREATE TABLE reviews (id serial PRIMARY KEY, snapshot_id text NOT NULL, agent text NOT NULL, vote text NOT NULL, note text, reviewed_at timestamptz DEFAULT now());
CREATE TABLE benchmark_prices (ticker text NOT NULL, date date NOT NULL, close numeric NOT NULL, PRIMARY KEY (ticker, date));
