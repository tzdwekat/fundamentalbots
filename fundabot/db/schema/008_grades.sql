CREATE TABLE grades (
    cik int NOT NULL,
    date date NOT NULL,
    company text NOT NULL,
    previous_grade text,
    new_grade text,
    action text,
    fetched_at timestamptz DEFAULT now(),
    PRIMARY KEY (cik, date, company)
);
CREATE TABLE ticker_facts (
    cik int NOT NULL,
    key text NOT NULL,
    value jsonb,
    fetched_at timestamptz DEFAULT now(),
    PRIMARY KEY (cik, key)
);
