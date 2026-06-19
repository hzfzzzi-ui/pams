"""
PIT Reader 测试。

用少量 INSERT 样例数据验证:
1. PIT 截断 (不返回 > trade_date 的数据)
2. get_tradeable_symbols 排除退市/停牌/ST
3. 缺数据 raise RuntimeError
4. get_benchmark_series 返回正确格式
5. get_st_flags 正确标记
6. get_trade_dates 返回交易日
7. get_security_names 返回名称
"""

import os
import pytest
import psycopg2

# Skip all tests if no DB connection available
pytestmark = pytest.mark.skipif(
    not os.environ.get("PGHOST"),
    reason="No PGHOST env var — skip DB-dependent tests",
)


@pytest.fixture(scope="module")
def conn():
    """Shared test DB connection with sample data."""
    c = psycopg2.connect(
        host=os.environ.get("PGHOST", "pams-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "pams"),
        user=os.environ.get("PGUSER", "pams"),
        password=os.environ.get("PGPASSWORD", ""),
    )
    c.autocommit = True
    cur = c.cursor()

    # Insert sample data into PIT tables
    # Security master
    cur.execute("""
        INSERT INTO pit_security_master (ts_code, symbol, name, exchange, list_date, delist_date, industry, market_type)
        VALUES
            ('000001.SZ', 'sz000001', '平安银行', 'SZSE', '1991-04-03', NULL, '银行', 'CN_A'),
            ('600519.SH', 'sh600519', '贵州茅台', 'SSE', '2001-08-27', NULL, '白酒', 'CN_A'),
            ('000999.SZ', 'sz000999', '华润三九', 'SZSE', '2001-09-18', NULL, '中药', 'CN_A'),
            ('600100.SH', 'sh600100', '已退市股', 'SSE', '2000-01-01', '2020-06-30', '其他', 'CN_A')
        ON CONFLICT (ts_code) DO NOTHING
    """)

    # Trade calendar
    cur.execute("""
        INSERT INTO pit_trade_cal (exchange, cal_date, is_open)
        VALUES
            ('SSE', '2024-01-02', TRUE),
            ('SSE', '2024-01-03', TRUE),
            ('SSE', '2024-01-04', TRUE),
            ('SSE', '2024-01-05', TRUE),
            ('SSE', '2024-01-06', FALSE),
            ('SSE', '2024-01-07', FALSE)
        ON CONFLICT (exchange, cal_date) DO NOTHING
    """)

    # OHLCV data
    cur.execute("""
        INSERT INTO pams_ohlcv_daily (symbol, market, trade_date, open, high, low, close, volume, turnover, adj_factor, is_suspended)
        VALUES
            ('sz000001', 'CN_A', '2024-01-02', 10.0, 10.5, 9.8, 10.3, 1000000, 10300000, 120.5, FALSE),
            ('sz000001', 'CN_A', '2024-01-03', 10.3, 10.8, 10.1, 10.6, 1200000, 12720000, 120.5, FALSE),
            ('sz000001', 'CN_A', '2024-01-04', 10.6, 11.0, 10.4, 10.9, 1100000, 11990000, 121.0, FALSE),
            ('sh600519', 'CN_A', '2024-01-02', 1800, 1850, 1790, 1840, 50000, 9200000, 50.0, FALSE),
            ('sh600519', 'CN_A', '2024-01-03', 1840, 1860, 1830, 1855, 45000, 8347500, 50.0, FALSE),
            ('sz000999', 'CN_A', '2024-01-02', 30.0, 31.0, 29.5, 30.5, 200000, 6100000, 80.0, TRUE),
            ('sz000999', 'CN_A', '2024-01-03', 30.5, 31.5, 30.0, 31.0, 220000, 6820000, 80.0, FALSE)
        ON CONFLICT (symbol, market, trade_date) DO NOTHING
    """)

    # ST history: sz000999 was ST from 2023-06 to 2024-02
    cur.execute("""
        INSERT INTO pit_st_history (ts_code, start_date, end_date, st_type, name_after)
        VALUES ('000999.SZ', '2023-06-01', '2024-02-01', 'ST', 'ST华润三九')
        ON CONFLICT (ts_code, start_date) DO NOTHING
    """)

    # Index daily
    cur.execute("""
        INSERT INTO pit_index_daily (index_code, trade_date, open, high, low, close, volume, turnover)
        VALUES
            ('000300.SH', '2024-01-02', 3400, 3450, 3380, 3430, 500000000, 4000000000),
            ('000300.SH', '2024-01-03', 3430, 3460, 3410, 3445, 480000000, 3800000000)
        ON CONFLICT (index_code, trade_date) DO NOTHING
    """)

    cur.close()
    yield c

    # Cleanup: remove sample data
    cur = c.cursor()
    cur.execute("DELETE FROM pit_st_history WHERE ts_code = '000999.SZ' AND start_date = '2023-06-01'")
    cur.execute("DELETE FROM pit_index_daily WHERE index_code = '000300.SH' AND trade_date IN ('2024-01-02', '2024-01-03')")
    cur.execute("DELETE FROM pams_ohlcv_daily WHERE symbol IN ('sz000001', 'sh600519', 'sz000999') AND trade_date IN ('2024-01-02', '2024-01-03', '2024-01-04')")
    cur.execute("DELETE FROM pit_trade_cal WHERE exchange = 'SSE' AND cal_date >= '2024-01-02' AND cal_date <= '2024-01-07'")
    cur.execute("DELETE FROM pit_security_master WHERE ts_code IN ('000001.SZ', '600519.SH', '000999.SZ', '600100.SH')")
    cur.close()
    c.close()


class TestGetOhlcv:
    """get_ohlcv() 测试。"""

    def test_returns_correct_symbols(self, conn):
        from datahub.pit_reader import get_ohlcv

        data = get_ohlcv(
            ["sz000001", "sh600519"],
            "2024-01-02", "2024-01-04",
            adjust=False, conn=conn,
        )
        assert "sz000001" in data
        assert "sh600519" in data
        assert len(data["sz000001"]) == 3  # 3 days
        assert len(data["sh600519"]) == 2  # 2 days

    def test_pit_truncation(self, conn):
        """不返回 > end_date 的数据。"""
        from datahub.pit_reader import get_ohlcv

        data = get_ohlcv(
            ["sz000001"], "2024-01-02", "2024-01-03",
            adjust=False, conn=conn,
        )
        assert len(data["sz000001"]) == 2
        dates = [b["trade_date"] for b in data["sz000001"]]
        assert "2024-01-04" not in dates

    def test_missing_symbol_raises(self, conn):
        """缺数据默认 raise RuntimeError。"""
        from datahub.pit_reader import get_ohlcv

        with pytest.raises(RuntimeError, match="PIT data missing"):
            get_ohlcv(
                ["sz000001", "szNONEXIST"],
                "2024-01-02", "2024-01-04",
                allow_missing=False, conn=conn,
            )

    def test_allow_missing(self, conn):
        """allow_missing=True 不 raise。"""
        from datahub.pit_reader import get_ohlcv

        data = get_ohlcv(
            ["sz000001", "szNONEXIST"],
            "2024-01-02", "2024-01-04",
            allow_missing=True, conn=conn,
        )
        assert "sz000001" in data
        assert "szNONEXIST" not in data

    def test_ohlcv_fields(self, conn):
        """验证返回字段格式。"""
        from datahub.pit_reader import get_ohlcv

        data = get_ohlcv(
            ["sz000001"], "2024-01-02", "2024-01-02",
            adjust=False, conn=conn,
        )
        bar = data["sz000001"][0]
        assert bar["trade_date"] == "2024-01-02"
        assert bar["open"] == 10.0
        assert bar["close"] == 10.3
        assert bar["volume"] == 1000000
        assert "symbol" in bar
        assert "market" in bar

    def test_adjust_forward(self, conn):
        """前复权: adj_factor 变化时价格应被调整。"""
        from datahub.pit_reader import get_ohlcv

        data = get_ohlcv(
            ["sz000001"], "2024-01-02", "2024-01-04",
            adjust=True, conn=conn,
        )
        bars = data["sz000001"]
        # Day 1-2 adj=120.5, Day 3 adj=121.0
        # latest_adj = 121.0 (last row)
        # Day 1: factor = 120.5/121.0 ≈ 0.99587
        # Day 3: factor = 121.0/121.0 = 1.0
        assert bars[2]["close"] == 10.9  # latest adj, factor=1.0
        assert bars[0]["close"] < 10.3  # earlier adj, factor < 1.0


class TestGetTradeableSymbols:
    """get_tradeable_symbols() 测试。"""

    def test_excludes_delisted(self, conn):
        """退市股不在 Universe 中。"""
        from datahub.pit_reader import get_tradeable_symbols

        symbols = get_tradeable_symbols("2024-01-02", conn=conn)
        assert "sh600100" not in symbols  # delisted 2020-06-30

    def test_excludes_suspended(self, conn):
        """停牌股排除。"""
        from datahub.pit_reader import get_tradeable_symbols

        symbols = get_tradeable_symbols(
            "2024-01-02", exclude_st=False, conn=conn,
        )
        # sz000999 is suspended on 2024-01-02
        assert "sz000999" not in symbols

    def test_excludes_st(self, conn):
        """ST 股排除 (PIT: 2024-01-02 时 000999 是 ST)。"""
        from datahub.pit_reader import get_tradeable_symbols

        symbols = get_tradeable_symbols(
            "2024-01-02", exclude_st=True, exclude_suspended=False, conn=conn,
        )
        assert "sz000999" not in symbols

    def test_includes_active(self, conn):
        """正常上市股在 Universe 中。"""
        from datahub.pit_reader import get_tradeable_symbols

        symbols = get_tradeable_symbols(
            "2024-01-02", exclude_st=False, exclude_suspended=False, conn=conn,
        )
        assert "sz000001" in symbols
        assert "sh600519" in symbols


class TestBenchmarkSeries:
    """get_benchmark_series() 测试。"""

    def test_returns_index_data(self, conn):
        from datahub.pit_reader import get_benchmark_series

        series = get_benchmark_series(
            "000300.SH", "2024-01-02", "2024-01-03", conn=conn,
        )
        assert len(series) == 2
        assert series[0]["trade_date"] == "2024-01-02"
        assert series[0]["close"] == 3430.0

    def test_accepts_internal_format(self, conn):
        """也接受内部格式 sh000300。"""
        from datahub.pit_reader import get_benchmark_series

        series = get_benchmark_series(
            "sh000300", "2024-01-02", "2024-01-03", conn=conn,
        )
        assert len(series) == 2


class TestStFlags:
    """get_st_flags() 测试。"""

    def test_during_st_period(self, conn):
        from datahub.pit_reader import get_st_flags

        flags = get_st_flags(
            ["sz000001", "sz000999"], "2024-01-02", conn=conn,
        )
        assert flags["sz000001"] is False
        assert flags["sz000999"] is True

    def test_after_st_period(self, conn):
        """ST 结束后不再标记。"""
        from datahub.pit_reader import get_st_flags

        flags = get_st_flags(
            ["sz000999"], "2024-03-01", conn=conn,
        )
        assert flags["sz000999"] is False


class TestTradeDates:
    """get_trade_dates() 测试。"""

    def test_returns_trading_days_only(self, conn):
        from datahub.pit_reader import get_trade_dates

        dates = get_trade_dates("2024-01-02", "2024-01-07", conn=conn)
        assert "2024-01-06" not in dates  # Saturday
        assert "2024-01-07" not in dates  # Sunday
        assert "2024-01-02" in dates
        assert len(dates) == 4  # Mon-Fri (4 trading days)


class TestSecurityNames:
    """get_security_names() 测试。"""

    def test_batch_names(self, conn):
        from datahub.pit_reader import get_security_names

        names = get_security_names(["sz000001", "sh600519"], conn=conn)
        assert names["sz000001"] == "平安银行"
        assert names["sh600519"] == "贵州茅台"

    def test_missing_symbol_returns_empty(self, conn):
        from datahub.pit_reader import get_security_names

        names = get_security_names(["szNONEXIST"], conn=conn)
        assert "szNONEXIST" not in names
