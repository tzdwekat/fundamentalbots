CREATE TABLE splits (
    cik int NOT NULL,
    date date NOT NULL,
    ratio numeric NOT NULL,
    PRIMARY KEY (cik, date)
);
