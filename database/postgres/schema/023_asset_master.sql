-- Canonical investment asset master data and lookthrough relationships.
-- The analysis engine reads these tables first and falls back to rules only
-- when a security has not been mapped yet.

CREATE TABLE IF NOT EXISTS pams_asset_master (
    asset_id VARCHAR(120) PRIMARY KEY,
    canonical_name VARCHAR(240) NOT NULL,
    display_name VARCHAR(240),
    primary_symbol VARCHAR(120),
    exchange VARCHAR(80),
    isin VARCHAR(40),
    cusip VARCHAR(40),
    asset_type VARCHAR(80) NOT NULL,
    asset_subtype VARCHAR(120),
    market VARCHAR(80),
    listing_region VARCHAR(80),
    economic_region VARCHAR(80),
    currency VARCHAR(16),
    valuation_method VARCHAR(80),
    liquidity_profile VARCHAR(80),
    income_type VARCHAR(80),
    exposure_policy VARCHAR(80),
    status VARCHAR(40) NOT NULL DEFAULT 'active',
    source VARCHAR(80) NOT NULL DEFAULT 'system',
    confidence VARCHAR(20) NOT NULL DEFAULT 'medium',
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pams_asset_master_symbol
    ON pams_asset_master (primary_symbol);

CREATE INDEX IF NOT EXISTS idx_pams_asset_master_type
    ON pams_asset_master (asset_type, market, economic_region);

CREATE TABLE IF NOT EXISTS pams_asset_alias (
    id BIGSERIAL PRIMARY KEY,
    asset_id VARCHAR(120) NOT NULL REFERENCES pams_asset_master(asset_id) ON DELETE CASCADE,
    alias_type VARCHAR(40) NOT NULL,
    alias_value VARCHAR(240) NOT NULL,
    source VARCHAR(80) NOT NULL DEFAULT 'system',
    priority INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (asset_id, alias_type, alias_value)
);

CREATE INDEX IF NOT EXISTS idx_pams_asset_alias_value_lower
    ON pams_asset_alias (LOWER(alias_value));

CREATE TABLE IF NOT EXISTS pams_asset_industry_map (
    id BIGSERIAL PRIMARY KEY,
    asset_id VARCHAR(120) NOT NULL REFERENCES pams_asset_master(asset_id) ON DELETE CASCADE,
    industry_system VARCHAR(40) NOT NULL DEFAULT 'manual',
    industry_leaf VARCHAR(160),
    industry_sub_bucket VARCHAR(120),
    industry_bucket VARCHAR(80) NOT NULL,
    classification_scope VARCHAR(80) NOT NULL DEFAULT 'listed_equity',
    source VARCHAR(80) NOT NULL DEFAULT 'system',
    confidence VARCHAR(20) NOT NULL DEFAULT 'medium',
    evidence TEXT,
    as_of DATE,
    is_primary BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (asset_id, industry_system, classification_scope, is_primary)
);

CREATE INDEX IF NOT EXISTS idx_pams_asset_industry_bucket
    ON pams_asset_industry_map (industry_bucket, industry_sub_bucket);

CREATE TABLE IF NOT EXISTS pams_asset_theme_map (
    id BIGSERIAL PRIMARY KEY,
    asset_id VARCHAR(120) NOT NULL REFERENCES pams_asset_master(asset_id) ON DELETE CASCADE,
    theme_id VARCHAR(120) NOT NULL,
    theme_name VARCHAR(160) NOT NULL,
    parent_theme_id VARCHAR(120),
    theme_level VARCHAR(20) NOT NULL DEFAULT 'parent',
    family VARCHAR(80),
    score NUMERIC(5, 2) NOT NULL,
    role VARCHAR(40) NOT NULL,
    source VARCHAR(80) NOT NULL DEFAULT 'system',
    confidence VARCHAR(20) NOT NULL DEFAULT 'medium',
    evidence TEXT,
    as_of DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (asset_id, theme_id, parent_theme_id, theme_level)
);

CREATE INDEX IF NOT EXISTS idx_pams_asset_theme_parent
    ON pams_asset_theme_map (parent_theme_id, theme_level);

CREATE INDEX IF NOT EXISTS idx_pams_asset_theme_asset
    ON pams_asset_theme_map (asset_id);

CREATE TABLE IF NOT EXISTS pams_fund_underlying_position (
    id BIGSERIAL PRIMARY KEY,
    fund_asset_id VARCHAR(120) NOT NULL REFERENCES pams_asset_master(asset_id) ON DELETE CASCADE,
    underlying_asset_id VARCHAR(120) REFERENCES pams_asset_master(asset_id) ON DELETE SET NULL,
    underlying_name VARCHAR(240),
    underlying_symbol VARCHAR(120),
    underlying_weight NUMERIC(18, 10) NOT NULL,
    underlying_asset_type VARCHAR(80),
    as_of DATE,
    effective_source VARCHAR(80) NOT NULL DEFAULT 'manual',
    coverage_type VARCHAR(40) NOT NULL DEFAULT 'top10',
    confidence VARCHAR(20) NOT NULL DEFAULT 'medium',
    source VARCHAR(120) NOT NULL DEFAULT 'system',
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (fund_asset_id, underlying_asset_id, underlying_name, as_of, coverage_type)
);

CREATE INDEX IF NOT EXISTS idx_pams_fund_underlying_fund
    ON pams_fund_underlying_position (fund_asset_id, as_of DESC);

CREATE TABLE IF NOT EXISTS pams_asset_fixed_income_profile (
    asset_id VARCHAR(120) PRIMARY KEY REFERENCES pams_asset_master(asset_id) ON DELETE CASCADE,
    issuer_name VARCHAR(240),
    issuer_type VARCHAR(80),
    credit_rating VARCHAR(40),
    coupon_rate NUMERIC(10, 6),
    coupon_type VARCHAR(40),
    maturity_date DATE,
    duration NUMERIC(10, 4),
    yield_to_maturity NUMERIC(10, 6),
    seniority VARCHAR(80),
    collateral_type VARCHAR(120),
    rate_risk_bucket VARCHAR(80),
    credit_risk_bucket VARCHAR(80),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pams_asset_real_asset_profile (
    asset_id VARCHAR(120) PRIMARY KEY REFERENCES pams_asset_master(asset_id) ON DELETE CASCADE,
    property_type VARCHAR(80),
    city VARCHAR(120),
    country_region VARCHAR(120),
    ownership_ratio NUMERIC(12, 8),
    gross_value NUMERIC(24, 4),
    net_value NUMERIC(24, 4),
    debt_balance NUMERIC(24, 4),
    valuation_method VARCHAR(80),
    last_valuation_date DATE,
    rental_income_annual NUMERIC(24, 4),
    occupancy_status VARCHAR(80),
    liquidity_profile VARCHAR(80),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
