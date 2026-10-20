CREATE TABLE fmp_statements (
    cik int NOT NULL, statement text NOT NULL, period_type text NOT NULL, fiscal_year int NOT NULL, period text NOT NULL,
    date date NOT NULL, filing_date date, data jsonb NOT NULL, fetched_at timestamptz DEFAULT now(),
    PRIMARY KEY (cik, statement, period_type, fiscal_year, period)
);
