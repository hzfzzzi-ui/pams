-- ============================================
-- PAMS 收支记账模块 - 数据库 Schema
-- ============================================

-- 交易记录表
CREATE TABLE IF NOT EXISTS pams_transactions (
    id SERIAL PRIMARY KEY,

    -- 基础信息
    date DATE NOT NULL DEFAULT CURRENT_DATE,       -- 交易日期
    amount DECIMAL(15, 2) NOT NULL,                -- 金额（正数=收入，负数=支出）
    currency VARCHAR(3) DEFAULT 'CNY',             -- 币种

    -- 分类信息
    category VARCHAR(50) NOT NULL,                 -- 分类（餐饮/交通/购物等）
    subcategory VARCHAR(50),                       -- 子分类（可选）
    account VARCHAR(50),                           -- 账户来源（微信/支付宝/telegram等）

    -- 详细信息
    description TEXT,                              -- 备注说明
    location VARCHAR(200),                         -- 地点

    -- 自动分类标记
    auto_classified BOOLEAN DEFAULT FALSE,         -- 是否为自动分类
    confidence REAL,                               -- 分类置信度（0-1）

    -- 预算关联
    budget_id INTEGER,                             -- 关联预算ID

    -- 元数据
    source VARCHAR(20) DEFAULT 'manual',           -- 数据来源（telegram/wechat/manual/import）
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 索引优化
CREATE INDEX IF NOT EXISTS idx_transactions_date ON pams_transactions(date DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON pams_transactions(category);
CREATE INDEX IF NOT EXISTS idx_transactions_month ON pams_transactions(DATE_TRUNC('month', date::timestamp));
CREATE INDEX IF NOT EXISTS idx_transactions_account ON pams_transactions(account);
CREATE INDEX IF NOT EXISTS idx_transactions_source ON pams_transactions(source);

-- 预算管理表
CREATE TABLE IF NOT EXISTS pams_budgets (
    id SERIAL PRIMARY KEY,

    -- 预算定义
    category VARCHAR(50) NOT NULL,                 -- 分类
    month DATE NOT NULL,                           -- 月份（YYYY-MM-01）
    budget_amount DECIMAL(15, 2) NOT NULL,         -- 预算金额
    currency VARCHAR(3) DEFAULT 'CNY',

    -- 预警设置
    alert_threshold REAL DEFAULT 0.8,              -- 预警阈值（80%）

    -- 元数据
    created_at TIMESTAMP DEFAULT NOW(),

    UNIQUE(category, month)
);

-- 分类规则表（自动分类）
CREATE TABLE IF NOT EXISTS pams_category_rules (
    id SERIAL PRIMARY KEY,

    -- 规则定义
    keyword VARCHAR(100) NOT NULL,                 -- 关键词
    category VARCHAR(50) NOT NULL,                 -- 目标分类
    subcategory VARCHAR(50),                       -- 子分类

    -- 规则优先级
    priority INTEGER DEFAULT 0,                    -- 优先级（越大越优先）

    -- 规则状态
    active BOOLEAN DEFAULT TRUE,

    -- 统计信息
    match_count INTEGER DEFAULT 0,                 -- 匹配次数

    created_at TIMESTAMP DEFAULT NOW()
);

-- 插入默认分类规则
INSERT INTO pams_category_rules (keyword, category, subcategory, priority) VALUES
    ('午餐', '餐饮', '午餐', 10),
    ('晚餐', '餐饮', '晚餐', 10),
    ('早餐', '餐饮', '早餐', 10),
    ('咖啡', '餐饮', '饮品', 5),
    ('奶茶', '餐饮', '饮品', 5),
    ('打车', '交通', '打车', 10),
    ('地铁', '交通', '公共交通', 10),
    ('公交', '交通', '公共交通', 10),
    ('超市', '购物', '日用品', 5),
    ('淘宝', '购物', '网购', 5),
    ('京东', '购物', '网购', 5),
    ('电影', '娱乐', '观影', 5),
    ('游戏', '娱乐', '游戏', 5),
    ('房租', '居住', '房租', 10),
    ('水电', '居住', '水电费', 10),
    ('工资', '收入', '工资', 10),
    ('奖金', '收入', '奖金', 10),
    ('红包', '收入', '红包', 5)
ON CONFLICT DO NOTHING;

-- 添加更新时间触发器
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_pams_transactions_updated_at BEFORE UPDATE
    ON pams_transactions FOR EACH ROW
    EXECUTE PROCEDURE update_updated_at_column();

-- 注释说明
COMMENT ON TABLE pams_transactions IS '个人收支交易记录表';
COMMENT ON COLUMN pams_transactions.amount IS '金额：正数表示收入，负数表示支出';
COMMENT ON COLUMN pams_transactions.auto_classified IS '是否由系统自动分类';
COMMENT ON COLUMN pams_transactions.source IS '数据来源：telegram/wechat/manual/import';

COMMENT ON TABLE pams_budgets IS '月度预算管理表';
COMMENT ON TABLE pams_category_rules IS '自动分类规则表';
