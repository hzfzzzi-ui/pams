"""Business dates are source facts, not portfolio display dates."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend" / "api"))
from backend.api.collectors import ibkr_flex as collector  # noqa: E402


@pytest.mark.parametrize("requested,latest,align", [
    (None, None, False), (None, date(2024, 2, 7), False),
    ("20240206", None, False), (date(2024, 2, 6), None, False),
    (None, date(2024, 2, 6), True), (None, None, True),
])
def test_matching_report_date_preserved(monkeypatch, requested, latest, align):
    monkeypatch.setattr(collector, "_latest_portfolio_as_of", lambda conn: latest)
    assert collector._target_as_of(None, requested_as_of=requested,
        statement_as_of=date(2024, 2, 6), align_to_latest_snapshot=align) == date(2024, 2, 6)


@pytest.mark.parametrize("target", [date(2024, 2, 5), date(2024, 2, 7)])
@pytest.mark.parametrize("explicit", [False, True])
def test_earlier_and_future_date_relabelling_rejected(monkeypatch, target, explicit):
    monkeypatch.setattr(collector, "_latest_portfolio_as_of", lambda conn: target)
    with pytest.raises(collector.IBKRFlexError, match="does not match IBKR report date"):
        collector._target_as_of(None, requested_as_of=target.isoformat() if explicit else None,
            statement_as_of=date(2024, 2, 6), align_to_latest_snapshot=not explicit)


def test_invalid_override_still_rejected():
    with pytest.raises(collector.IBKRFlexError, match="Invalid as_of override"):
        collector._target_as_of(None, requested_as_of="not-a-date",
            statement_as_of=date(2024, 2, 6), align_to_latest_snapshot=False)


def test_current_projection_does_not_share_mutable_history_rows():
    xml = '''<FlexQueryResponse><FlexStatements>
      <FlexStatement accountId="U123" toDate="20240205"><OpenPositions>
        <OpenPosition accountId="U123" symbol="AAA" description="AAA" currency="USD"
          reportDate="20240205" position="2" markPrice="10" marketValue="20" />
      </OpenPositions></FlexStatement>
      <FlexStatement accountId="U123" toDate="20240206"><OpenPositions>
        <OpenPosition accountId="U123" symbol="AAA" description="AAA" currency="USD"
          reportDate="20240206" position="3" markPrice="11" marketValue="33" />
      </OpenPositions></FlexStatement>
    </FlexStatements></FlexQueryResponse>'''
    parsed = collector.parse_flex_report(xml)
    history = deepcopy(parsed.history_rows)
    assert parsed.rows[0] == parsed.history_rows[-1]
    assert parsed.rows[0] is not parsed.history_rows[-1]
    parsed.rows[0]["as_of"] = date(2024, 2, 5)
    parsed.rows[0]["market_value"] = 99
    assert parsed.history_rows == history


@pytest.mark.parametrize("row_date", [None, date(2024, 2, 5), date(2024, 2, 7)])
def test_writer_rejects_date_mismatch_before_any_database_write(monkeypatch, row_date):
    monkeypatch.setattr(collector, "_ensure_reference_data", lambda *args: pytest.fail("Write gate too late"))
    rows = [{"as_of": row_date, "symbol": "AAA"}]
    before = deepcopy(rows)
    with pytest.raises(collector.IBKRFlexError, match="row date does not match"):
        collector._write_holdings(None, rows, account_code="example", as_of=date(2024, 2, 6))
    assert rows == before


def test_writer_leaves_input_and_values_unchanged(monkeypatch):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

    captured = []
    monkeypatch.setattr(collector, "_ensure_reference_data", lambda *args: None)
    monkeypatch.setattr(collector, "_column_exists", lambda *args: False)
    monkeypatch.setattr(collector.psycopg2.extras, "execute_batch",
        lambda cur, statement, rows, **kwargs: captured.extend(rows))
    rows = [{"as_of": date(2024, 2, 6), "market_value": 33, "quantity": 3}]
    before = deepcopy(rows)
    assert collector._write_holdings(Connection(), rows, account_code="example", as_of=date(2024, 2, 6)) == 1
    assert rows == before == captured
    assert rows[0] is not captured[0]
