-- Daily FX observations; primary key matches the running asset service.
CREATE TABLE IF NOT EXISTS pams_fx_rates_daily (
    as_of DATE NOT NULL,
    base_currency TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    rate NUMERIC(20,8) NOT NULL,
    source TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (as_of, base_currency, quote_currency)
);
