CREATE TABLE company_profile (
    cik int PRIMARY KEY, name text, sector text, industry text, exchange text, description text, employees int, beta numeric, ipo_date date,
    fetched_at timestamptz DEFAULT now()
);
CREATE TABLE peers (
    cik int NOT NULL, peer text NOT NULL, name text, market_cap numeric, price numeric, year_high numeric, year_low numeric,
    pe_ttm numeric, ev_sales_ttm numeric, ev_ebitda_ttm numeric, fetched_at timestamptz DEFAULT now(), PRIMARY KEY (cik, peer)
);
CREATE TABLE segments (
    cik int NOT NULL, kind text NOT NULL, fiscal_year int NOT NULL, name text NOT NULL, revenue numeric, fetched_at timestamptz DEFAULT now(),
    PRIMARY KEY (cik, kind, fiscal_year, name)
);
CREATE TABLE dividends (
    cik int NOT NULL, ex_date date NOT NULL, payment_date date, declaration_date date, amount numeric, adj_amount numeric, fetched_at timestamptz DEFAULT now(),
    PRIMARY KEY (cik, ex_date)
);
CREATE TABLE insider_trades (
    cik int NOT NULL, filing_date date NOT NULL, transaction_date date, reporting_name text NOT NULL, role text, transaction_type text, acquisition text,
    shares numeric, price numeric, url text, fetched_at timestamptz DEFAULT now(),
    PRIMARY KEY (cik, filing_date, reporting_name, transaction_type, shares)
);
CREATE TABLE holders (
    cik int NOT NULL, quarter_end date NOT NULL, investor text NOT NULL, shares numeric, market_value numeric, change_pct numeric, ownership_pct numeric,
    fetched_at timestamptz DEFAULT now(), PRIMARY KEY (cik, quarter_end, investor)
);
ALTER TABLE prices ADD COLUMN volume bigint;
