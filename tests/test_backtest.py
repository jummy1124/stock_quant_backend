"""Backtest engine + endpoints.

The arithmetic is checked against hand-computed numbers rather than against the
implementation's own output, and the fixtures deliberately include the awkward
cases the engine exists to get right: a market holiday inside the holding
period, a stock with no price on its exit day, and a screening day that has not
yet had N trading days happen.
"""
from datetime import date, datetime, timezone

import pytest

from app.backtest import nth_trading_day, normalize_horizons

INGEST_TOKEN = "test-ingest-token"


@pytest.fixture(autouse=True)
def _configure_ingest(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "INGEST_TOKEN", INGEST_TOKEN, raising=False)


def _headers():
    return {"X-Ingest-Token": INGEST_TOKEN}


def post_snapshot(client, trade_date, session, items):
    body = {
        "trade_date": trade_date,
        "session": session,
        "generated_at": datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc).isoformat(),
        "source": "live" if session == "intraday_1300" else "eod",
        "items": [
            {"rank": i, "symbol": sym, "name": f"股{sym}", "market": "上市",
             "market_code": "TWSE", "close": close}
            for i, (sym, close) in enumerate(items, start=1)
        ],
    }
    resp = client.post("/userapi/ingest/snapshot", json=body, headers=_headers())
    assert resp.status_code == 200, resp.text
    return resp.json()


def post_prices(client, trade_date, rows):
    body = {
        "trade_date": trade_date,
        "source": "backfill",
        "items": [
            {"symbol": sym, "name": f"股{sym}", "market_code": "TWSE", "close": close}
            for sym, close in rows
        ],
    }
    resp = client.post("/userapi/ingest/prices", json=body, headers=_headers())
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# Trading-day arithmetic
# --------------------------------------------------------------------------


def test_nth_trading_day_skips_market_closures():
    # Mon, Tue, then a gap (public holiday Wed), Thu, Fri.
    cal = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 4), date(2026, 6, 5)]
    assert nth_trading_day(cal, date(2026, 6, 1), 0) == date(2026, 6, 1)
    assert nth_trading_day(cal, date(2026, 6, 1), 1) == date(2026, 6, 2)
    # 2 trading days after Monday is Thursday, not Wednesday.
    assert nth_trading_day(cal, date(2026, 6, 1), 2) == date(2026, 6, 4)
    assert nth_trading_day(cal, date(2026, 6, 1), 3) == date(2026, 6, 5)
    # Past the end of the calendar: unknown, not clamped to the last day.
    assert nth_trading_day(cal, date(2026, 6, 1), 4) is None


def test_nth_trading_day_when_entry_day_missing_from_calendar():
    """An entry day with no uploaded prices still resolves its successors."""
    cal = [date(2026, 6, 1), date(2026, 6, 4), date(2026, 6, 5)]
    missing_day = date(2026, 6, 2)
    assert nth_trading_day(cal, missing_day, 0) is None
    assert nth_trading_day(cal, missing_day, 1) == date(2026, 6, 4)
    assert nth_trading_day(cal, missing_day, 2) == date(2026, 6, 5)


def test_normalize_horizons_sorts_dedups_and_bounds():
    assert normalize_horizons([5, 1, 5, 2]) == [1, 2, 5]
    with pytest.raises(ValueError):
        normalize_horizons([])
    with pytest.raises(ValueError):
        normalize_horizons([-1])
    with pytest.raises(ValueError):
        normalize_horizons([999])
    with pytest.raises(ValueError):
        normalize_horizons(list(range(1, 30)))


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------


def test_price_ingest_is_idempotent_and_additive(client):
    first = post_prices(client, "2026-06-01", [("2330", 100.0), ("2317", 50.0)])
    assert (first["inserted"], first["updated"]) == (2, 0)

    # Re-uploading the same symbol overwrites it ...
    again = post_prices(client, "2026-06-01", [("2330", 101.0)])
    assert (again["inserted"], again["updated"]) == (0, 1)

    # ... and leaves the symbol it didn't mention alone. A "delete the day then
    # insert" strategy would have destroyed 2317 here.
    cov = client.get("/backtestapi/coverage").json()["prices"]
    assert cov["total_rows"] == 2
    assert cov["symbols"] == 2


def test_price_ingest_skips_bars_without_a_close(client):
    body = {
        "trade_date": "2026-06-01",
        "items": [
            {"symbol": "2330", "close": 100.0},
            {"symbol": "9999", "close": None},  # suspended: nothing to record
        ],
    }
    resp = client.post("/userapi/ingest/prices", json=body, headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {
        "trade_date": "2026-06-01", "received": 2,
        "inserted": 1, "updated": 0, "skipped": 1,
    }


def test_price_ingest_requires_token(client):
    resp = client.post("/userapi/ingest/prices", json={"trade_date": "2026-06-01", "items": []})
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Backtest maths
# --------------------------------------------------------------------------


@pytest.fixture(name="seeded")
def seeded_fixture(client):
    """Three trading days; two stocks screened on day one.

    Prices are chosen so every expected percentage is exact in binary floating
    point, and so the two stocks disagree at N=1 (one up, one down) — a win rate
    that is neither 0 nor 1 is the only kind that can catch an off-by-one.

        2330: 100 -> 110 (+10%) -> 105 (+5%)
        2317:  50 ->  45 (-10%) ->  60 (+20%)
    """
    post_prices(client, "2026-06-01", [("2330", 100.0), ("2317", 50.0)])
    post_prices(client, "2026-06-02", [("2330", 110.0), ("2317", 45.0)])
    # 6/3 is a market holiday — absent from the calendar entirely.
    post_prices(client, "2026-06-04", [("2330", 105.0), ("2317", 60.0)])
    post_snapshot(client, "2026-06-01", "eod", [("2330", 100.0), ("2317", 50.0)])
    return client


def test_close_to_close_win_rate_and_returns(seeded):
    resp = seeded.get(
        "/backtestapi/run",
        params={"mode": "close_to_close", "horizons": "1,2", "detail_n": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entries"] == 2
    assert body["trading_days"] == 1

    n1, n2 = body["summary"]
    assert n1["n"] == 1 and n1["samples"] == 2
    assert (n1["wins"], n1["losses"], n1["flat"]) == (1, 1, 0)
    assert n1["win_rate"] == 0.5
    assert n1["avg_return_pct"] == 0.0          # (+10 + -10) / 2
    assert n1["median_return_pct"] == 0.0
    assert n1["best_return_pct"] == 10.0
    assert n1["worst_return_pct"] == -10.0

    # N=2 crosses the 6/3 holiday: the exit is 6/4, not 6/3.
    assert n2["samples"] == 2 and n2["win_rate"] == 1.0
    assert n2["avg_return_pct"] == 12.5         # (+5 + +20) / 2
    assert {r["exit_date"] for r in body["detail"]} == {"2026-06-04"}


def test_intraday_mode_uses_the_1300_price_and_allows_n_zero(client):
    """N=0 marks the 13:00 entry against the same day's close."""
    post_prices(client, "2026-06-01", [("2330", 110.0)])
    post_snapshot(client, "2026-06-01", "intraday_1300", [("2330", 100.0)])

    body = client.get(
        "/backtestapi/run",
        params={"mode": "intraday_to_close", "horizons": "0"},
    ).json()
    stat = body["summary"][0]
    assert stat["samples"] == 1 and stat["wins"] == 1
    assert stat["avg_return_pct"] == 10.0       # 13:00 的 100 -> 收盤 110


def test_intraday_and_eod_snapshots_do_not_bleed_into_each_other(client):
    """Same day, same stock, two sessions with different entry prices."""
    post_prices(client, "2026-06-01", [("2330", 110.0)])
    post_prices(client, "2026-06-02", [("2330", 121.0)])
    post_snapshot(client, "2026-06-01", "intraday_1300", [("2330", 100.0)])
    post_snapshot(client, "2026-06-01", "eod", [("2330", 110.0)])

    intraday = client.get(
        "/backtestapi/run", params={"mode": "intraday_to_close", "horizons": "1"}
    ).json()["summary"][0]
    eod = client.get(
        "/backtestapi/run", params={"mode": "close_to_close", "horizons": "1"}
    ).json()["summary"][0]

    assert intraday["avg_return_pct"] == 21.0   # 100 -> 121
    assert eod["avg_return_pct"] == 10.0        # 110 -> 121


def test_unsettled_horizon_counts_as_missing_not_as_a_flat_trade(seeded):
    """N=5 has not happened yet — those entries must not dilute the win rate."""
    body = seeded.get(
        "/backtestapi/run", params={"mode": "close_to_close", "horizons": "5"}
    ).json()
    stat = body["summary"][0]
    assert stat["samples"] == 0
    assert stat["missing"] == 2
    assert stat["win_rate"] is None
    assert stat["flat"] == 0
    assert "尚未經過" in (body["warning"] or "")


def test_missing_exit_price_for_one_stock_only_drops_that_stock(client):
    post_prices(client, "2026-06-01", [("2330", 100.0), ("2317", 50.0)])
    post_prices(client, "2026-06-02", [("2330", 110.0)])  # 2317 has no bar
    post_snapshot(client, "2026-06-01", "eod", [("2330", 100.0), ("2317", 50.0)])

    stat = client.get(
        "/backtestapi/run", params={"mode": "close_to_close", "horizons": "1"}
    ).json()["summary"][0]
    assert (stat["samples"], stat["missing"]) == (1, 1)
    assert stat["win_rate"] == 1.0


def test_detail_rows_carry_both_ends_of_the_trade(seeded):
    body = seeded.get(
        "/backtestapi/run",
        params={"mode": "close_to_close", "horizons": "1", "detail_n": 1},
    ).json()
    assert body["detail_total"] == 2
    row = next(r for r in body["detail"] if r["symbol"] == "2330")
    assert row == {
        "trade_date": "2026-06-01", "symbol": "2330", "name": "股2330",
        "market": "上市", "market_code": "TWSE",
        "entry_price": 100.0, "exit_date": "2026-06-02", "exit_price": 110.0,
        "change": 10.0, "return_pct": 10.0,
    }


def test_detail_limit_caps_rows_but_reports_the_true_total(seeded):
    body = seeded.get(
        "/backtestapi/run",
        params={"mode": "close_to_close", "horizons": "1", "detail_limit": 1},
    ).json()
    assert body["detail_total"] == 2
    assert len(body["detail"]) == 1


def test_date_range_filters_entries(seeded):
    body = seeded.get(
        "/backtestapi/run",
        params={"mode": "close_to_close", "horizons": "1",
                "start": "2026-06-02", "end": "2026-06-04"},
    ).json()
    assert body["entries"] == 0
    assert body["summary"][0]["samples"] == 0


def test_omitting_dates_covers_the_whole_database(seeded):
    body = seeded.get(
        "/backtestapi/run", params={"mode": "close_to_close", "horizons": "1"}
    ).json()
    assert body["start"] == "2026-06-01" and body["end"] == "2026-06-01"


# --------------------------------------------------------------------------
# Validation + empty states
# --------------------------------------------------------------------------


def test_close_to_close_rejects_n_zero(client):
    resp = client.get(
        "/backtestapi/run", params={"mode": "close_to_close", "horizons": "0"}
    )
    assert resp.status_code == 422
    assert "N=0" in resp.json()["detail"]


@pytest.mark.parametrize(
    "params",
    [
        {"mode": "nonsense"},
        {"horizons": "abc"},
        {"horizons": "999"},
        {"start": "2026-06-10", "end": "2026-06-01"},
    ],
)
def test_invalid_parameters_are_rejected(client, params):
    resp = client.get("/backtestapi/run", params={"mode": "close_to_close", **params})
    assert resp.status_code == 422


def test_empty_database_explains_itself(client):
    body = client.get("/backtestapi/run", params={"horizons": "1"}).json()
    assert body["entries"] == 0
    assert "查無篩選紀錄" in body["warning"]


def test_snapshots_without_prices_say_so(client):
    post_snapshot(client, "2026-06-01", "eod", [("2330", 100.0)])
    body = client.get("/backtestapi/run", params={"horizons": "1"}).json()
    assert body["summary"][0]["missing"] == 1
    assert "收盤價" in body["warning"]


def test_coverage_reports_both_tables(seeded):
    body = seeded.get("/backtestapi/coverage").json()
    assert body["snapshots"]["trading_days"] == 1
    assert body["prices"] == {
        "min_date": "2026-06-01", "max_date": "2026-06-04",
        "trading_days": 3, "total_rows": 6, "symbols": 2,
    }


def test_xlsx_export_returns_a_workbook(seeded):
    resp = seeded.get(
        "/backtestapi/backtest.xlsx",
        params={"mode": "close_to_close", "horizons": "1,2", "detail_n": 1},
    )
    assert resp.status_code == 200
    assert resp.content[:2] == b"PK"  # zip magic — a real .xlsx
    assert "backtest_" in resp.headers["content-disposition"]


def test_xlsx_export_is_still_a_workbook_when_empty(client):
    resp = client.get("/backtestapi/backtest.xlsx", params={"horizons": "1"})
    assert resp.status_code == 200
    assert resp.content[:2] == b"PK"
