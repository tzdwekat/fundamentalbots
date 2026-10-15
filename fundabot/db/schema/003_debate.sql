CREATE TABLE estimates (
    cik int NOT NULL,
    fetched_at timestamptz NOT NULL,
    period text NOT NULL,
    period_end date NOT NULL,
    revenue_avg numeric, revenue_low numeric, revenue_high numeric,
    eps_avg numeric, eps_low numeric, eps_high numeric,
    analysts int,
    PRIMARY KEY (cik, fetched_at, period, period_end)
);

CREATE TABLE surprises (
    cik int NOT NULL,
    report_date date NOT NULL,
    eps_actual numeric, eps_estimated numeric,
    revenue_actual numeric, revenue_estimated numeric,
    PRIMARY KEY (cik, report_date)
);

CREATE TABLE price_targets (
    cik int NOT NULL,
    fetched_at timestamptz NOT NULL,
    consensus numeric, median numeric, high numeric, low numeric,
    PRIMARY KEY (cik, fetched_at)
);

CREATE TABLE rebuttals (
    snapshot_id text NOT NULL,
    persona text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    output jsonb NOT NULL,
    grounded jsonb,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (snapshot_id, persona, provider, model)
);

CREATE TABLE judgements (
    snapshot_id text NOT NULL,
    provider text NOT NULL,
    model text NOT NULL,
    round int NOT NULL,
    output jsonb NOT NULL,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (snapshot_id, provider, model, round)
);

CREATE TABLE verdicts (
    snapshot_id text NOT NULL,
    run_kind text NOT NULL,
    verdict jsonb NOT NULL,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (snapshot_id, run_kind)
);

ALTER TABLE analyses ADD COLUMN status text NOT NULL DEFAULT 'ok';
ALTER TABLE analyses ADD COLUMN error text;
