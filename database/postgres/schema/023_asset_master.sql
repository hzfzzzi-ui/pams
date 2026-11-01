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

-- One physical table keeps the industry and theme domains separate by type.
-- Their data, keys and nullability remain independent.
CREATE TABLE IF NOT EXISTS asset_classifications (
    id BIGSERIAL PRIMARY KEY,
    asset_id VARCHAR(120) NOT NULL REFERENCES pams_asset_master(asset_id) ON DELETE CASCADE,
    classification_type VARCHAR(20) NOT NULL CHECK (classification_type IN ('industry', 'theme')),
    industry_system VARCHAR(40),
    industry_leaf VARCHAR(160),
    industry_sub_bucket VARCHAR(120),
    industry_bucket VARCHAR(80),
    classification_scope VARCHAR(80),
    is_primary BOOLEAN,
    theme_id VARCHAR(120),
    theme_name VARCHAR(160),
    parent_theme_id VARCHAR(120),
    theme_level VARCHAR(20),
    family VARCHAR(80),
    score NUMERIC(5, 2),
    role VARCHAR(40),
    source VARCHAR(80) NOT NULL DEFAULT 'system',
    confidence VARCHAR(20) NOT NULL DEFAULT 'medium',
    evidence TEXT,
    as_of DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT asset_classifications_kind_fields CHECK (
        (classification_type = 'industry' AND industry_system IS NOT NULL
         AND industry_bucket IS NOT NULL AND classification_scope IS NOT NULL AND is_primary IS NOT NULL
         AND theme_id IS NULL AND theme_name IS NULL AND parent_theme_id IS NULL
         AND theme_level IS NULL AND family IS NULL AND score IS NULL AND role IS NULL)
        OR
        (classification_type = 'theme' AND theme_id IS NOT NULL AND theme_name IS NOT NULL
         AND theme_level IS NOT NULL AND score IS NOT NULL AND role IS NOT NULL
         AND industry_system IS NULL AND industry_leaf IS NULL AND industry_sub_bucket IS NULL
         AND industry_bucket IS NULL AND classification_scope IS NULL AND is_primary IS NULL)
    ),
    UNIQUE (asset_id, classification_type, industry_system, classification_scope, is_primary),
    UNIQUE (asset_id, classification_type, theme_id, parent_theme_id, theme_level)
);

CREATE INDEX IF NOT EXISTS idx_asset_classifications_industry
    ON asset_classifications (industry_bucket, industry_sub_bucket) WHERE classification_type = 'industry';
CREATE INDEX IF NOT EXISTS idx_asset_classifications_theme
    ON asset_classifications (parent_theme_id, theme_level) WHERE classification_type = 'theme';

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
