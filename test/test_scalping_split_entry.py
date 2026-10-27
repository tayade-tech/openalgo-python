"""Scalping entry auto-split: unlimited lots, freeze-sized order slicing.

The /scalping/api/order endpoint must NOT cap entries at the freeze limit.
It validates the whole-lot multiple, then routes any quantity above the
exchange freeze through split_order_service so a single entry of N lots
becomes ceil(N / freeze_lots) orders, each within the freeze.

The freeze limit itself is dynamic (admin-managed qty_freeze table / SymToken
lot size) - nothing is hardcoded per product:
    NIFTY:     freeze 1800 / lot 65  -> 27 lots = 1755 qty per order
    BANKNIFTY: freeze  600 / lot 30  -> 20 lots =  600 qty per order

Run: uv run pytest test/test_scalping_split_entry.py -v
"""

from types import SimpleNamespace
from unittest import mock

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import blueprints.scalping as scalping_bp_module
import database.symbol as symbol_db

# restx_api first: importing services.place_order_service directly runs into
# the app's import graph (place_order_service -> restx_api.schemas ->
# restx_api/__init__ -> options_multiorder -> options_multiorder_service ->
# place_order_service). Importing restx_api first resolves the cycle, the same
# order app.py and the other service-layer tests use.
import restx_api  # noqa: F401
import utils.session as session_utils
from blueprints.scalping import (
    DEFAULT_FREEZE_LOT_CAP,
    MAX_ORDER_QUANTITY,
    _entry_split_chunk,
    _lot_rules,
    _validate_quantity,
    scalping_bp,
)

# ---------------------------------------------------------------------------
# Fixtures: in-memory SymToken + qty_freeze caches, stubbed order services.
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    # The order routes carry @track_latency, which persists each request to the
    # latency DB off-thread; create its table (conftest already points the URL
    # at the test database).
    from database.latency_db import init_latency_db

    init_latency_db()

    application = Flask(__name__)
    application.config.update(TESTING=True, SECRET_KEY="test")
    application.register_blueprint(scalping_bp)
    monkeypatch.setattr(session_utils, "is_session_valid", lambda: True)
    return application


@pytest.fixture
def client(app, monkeypatch):  # noqa: ARG001 - fixture dependency
    # _resolve_session_auth reads session["user"] and resolves a TradingView
    # API key from it; give the session a user and stub the key lookup.
    monkeypatch.setattr(scalping_bp_module, "get_api_key_for_tradingview", lambda u: "test-key")
    with app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            sess["user"] = "tester"
        return test_client


@pytest.fixture
def symbol_rows():
    """NIFTY (freeze 1800 / lot 65 -> chunk 1755) and BANKNIFTY (600/30 -> 600)."""
    return [
        ("NIFTY26DEC2424000CE", "NFO", 65),
        ("BANKNIFTY26DEC2451000CE", "NFO", 30),
        ("NIFTY26DEC24FUT", "NFO", 65),
    ]


@pytest.fixture
def freeze_map():
    return {"NFO:NIFTY": 1800, "NFO:BANKNIFTY": 600}


@pytest.fixture
def symbol_db_session(symbol_rows):
    engine = create_engine("sqlite:///:memory:")
    symbol_db.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for symbol, exchange, lotsize in symbol_rows:
        db.add(
            symbol_db.SymToken(
                symbol=symbol,
                brsymbol=symbol,
                name=symbol.split("26DEC24")[0],
                exchange=exchange,
                expiry="26-DEC-24",
                lotsize=lotsize,
                instrumenttype="CE" if "CE" in symbol else "FUT",
                tick_size=0.05,
            )
        )
    db.commit()
    return db


@pytest.fixture(autouse=True)
def patch_lookups(monkeypatch, symbol_db_session, freeze_map):
    """Point the blueprint's lookups at in-memory data.

    _lot_rules imports get_freeze_qty_for_option lazily from
    database.qty_freeze_db, so patch the source module's function (the lazy
    import then resolves the patched attribute). SymToken/db_session are
    imported lazily too; patch them on database.symbol. Analyze mode is forced
    off so the routing assertions are deterministic (the routes read it).
    """
    import database.qty_freeze_db as qty_freeze_db

    def fake_freeze(symbol, exchange):
        # Same underlying extraction the real function performs; test data is
        # shaped so the plain prefix rules suffice.
        for name in ("BANKNIFTY", "NIFTY"):
            if symbol.upper().startswith(name):
                return freeze_map.get(f"{exchange}:{name}", 1)
        return 1

    def fake_freeze_by_underlying(underlying, exchange):
        return freeze_map.get(f"{exchange}:{underlying}", 1)

    monkeypatch.setattr(qty_freeze_db, "get_freeze_qty_for_option", fake_freeze)
    monkeypatch.setattr(qty_freeze_db, "get_freeze_qty", fake_freeze_by_underlying)
    monkeypatch.setattr(symbol_db, "db_session", symbol_db_session)
    # The blueprint imports get_analyze_mode directly, so patch its reference;
    # the order services hold their own references, patched there too — the
    # settings table does not exist in the test database.
    monkeypatch.setattr(scalping_bp_module, "get_analyze_mode", lambda: False)
    import services.place_order_service as pos
    import services.split_order_service as sos

    monkeypatch.setattr(pos, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(sos, "get_analyze_mode", lambda: False)
    return symbol_db_session


# ---------------------------------------------------------------------------
# Pure helpers - the chunk math the order path and exits share.
# ---------------------------------------------------------------------------


class TestLotRules:
    def test_chunk_is_the_whole_lot_freeze_quantity(self):
        # NIFTY: floor(1800 / 65) * 65 = 27 lots = 1755 qty per order
        assert _lot_rules("NIFTY26DEC2424000CE", "NFO") == (65, 1755)

    def test_chunk_adapts_per_product(self):
        # BANKNIFTY: floor(600 / 30) * 30 = 20 lots = 600 qty per order
        assert _lot_rules("BANKNIFTY26DEC2451000CE", "NFO") == (30, 600)

    def test_unknown_symbol_returns_zero_lotsize(self):
        assert _lot_rules("NOSUCH26DEC2424000CE", "NFO") == (0, 0)

    def test_unconfigured_freeze_falls_back_to_conservative_chunk(self, monkeypatch):
        import database.qty_freeze_db as qty_freeze_db

        monkeypatch.setattr(qty_freeze_db, "get_freeze_qty_for_option", lambda s, e: 1)
        lotsize, chunk = _lot_rules("NIFTY26DEC2424000CE", "NFO")
        assert lotsize == 65
        assert chunk == DEFAULT_FREEZE_LOT_CAP * 65

    def test_non_multiple_freeze_rounds_down_to_whole_lots(self, monkeypatch):
        # A freeze of 1801 must not produce a fractional-lot chunk.
        import database.qty_freeze_db as qty_freeze_db

        monkeypatch.setattr(qty_freeze_db, "get_freeze_qty_for_option", lambda s, e: 1801)
        assert _lot_rules("NIFTY26DEC2424000CE", "NFO") == (65, 1755)


class TestValidateQuantity:
    def test_above_freeze_is_no_longer_rejected(self):
        # 28 lots x 65 = 1820 > 1755 freeze: valid, because it will be split.
        assert _validate_quantity("NIFTY26DEC2424000CE", "NFO", 1820) is None

    def test_huge_whole_lot_quantity_is_accepted(self):
        assert _validate_quantity("NIFTY26DEC2424000CE", "NFO", 27 * 65 * 50) is None

    def test_non_lot_multiple_is_still_rejected(self):
        err = _validate_quantity("NIFTY26DEC2424000CE", "NFO", 65 + 1)
        assert err is not None and "whole number of lots" in err

    def test_unknown_symbol_is_rejected(self):
        assert _validate_quantity("NOSUCH26DEC2424000CE", "NFO", 65) is not None


class TestEntrySplitChunk:
    def test_below_freeze_is_a_single_order(self):
        assert _entry_split_chunk("NIFTY26DEC2424000CE", "NFO", 1755) == 0

    def test_exactly_the_freeze_is_a_single_order(self):
        assert _entry_split_chunk("BANKNIFTY26DEC2451000CE", "NFO", 600) == 0

    def test_above_freeze_returns_the_freeze_chunk(self):
        # 28 lots -> sliced at 27 lots = 1755 per order (27 + 1).
        assert _entry_split_chunk("NIFTY26DEC2424000CE", "NFO", 1820) == 1755

    def test_multiple_of_the_chunk_still_splits(self):
        assert _entry_split_chunk("NIFTY26DEC2424000CE", "NFO", 1755 * 2) == 1755


# ---------------------------------------------------------------------------
# Endpoint: routing between place_order and split_order, and the guards.
# ---------------------------------------------------------------------------


@pytest.fixture
def placed(monkeypatch):
    """Capture place_order calls; default success."""
    calls: list[dict] = []

    def fake_place_order(order_data, api_key=None, auth_token=None, broker=None, **_):
        calls.append(dict(order_data))
        return True, {"status": "success", "orderid": f"ord{len(calls)}"}, 200

    import services.place_order_service as pos

    monkeypatch.setattr(pos, "place_order", fake_place_order)
    return calls


@pytest.fixture
def split(monkeypatch):
    """Capture split_order calls; default success."""
    calls: list[dict] = []

    def fake_split_order(split_data=None, api_key=None, auth_token=None, broker=None, **_):
        calls.append(dict(split_data))
        return True, {"status": "success", "split_size": split_data["splitsize"]}, 200

    import services.split_order_service as sos

    monkeypatch.setattr(sos, "split_order", fake_split_order)
    return calls


def _post_order(client, **body):
    return client.post(
        "/scalping/api/order",
        json={"product": "MIS", "action": "BUY", **body},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )


class TestOrderEndpointRouting:
    def test_within_freeze_goes_through_place_order(self, client, placed, split):
        resp = _post_order(client, symbol="NIFTY26DEC2424000CE", exchange="NFO", quantity=1755)

        assert resp.status_code == 200
        assert len(placed) == 1
        assert placed[0]["quantity"] == 1755
        assert split == []

    def test_above_freeze_is_sliced_not_rejected(self, client, placed, split):
        """28 NIFTY lots (1820) must split into 1755-sized orders, not 400."""
        resp = _post_order(client, symbol="NIFTY26DEC2424000CE", exchange="NFO", quantity=1820)

        assert resp.status_code == 200
        assert placed == []
        assert len(split) == 1
        assert split[0]["quantity"] == 1820
        assert split[0]["splitsize"] == 1755  # 27 lots + 1 lot

    def test_banknifty_cap_example_now_splits(self, client, placed, split):
        """The reported defect: 21 BANKNIFTY lots was rejected at the 20-lot cap.

        21 lots = 630 qty > 600 freeze -> one split at 600 (20 + 1 lots).
        """
        resp = _post_order(client, symbol="BANKNIFTY26DEC2451000CE", exchange="NFO", quantity=630)

        assert resp.status_code == 200
        assert split[0]["splitsize"] == 600
        assert split[0]["quantity"] == 630

    def test_split_carries_the_scalping_strategy_tag(self, client, split):
        _post_order(client, symbol="NIFTY26DEC2424000CE", exchange="NFO", quantity=1820)

        assert split[0]["strategy"] == "Scalping"
        assert split[0]["pricetype"] == "MARKET"
        assert split[0]["action"] == "BUY"
        assert split[0]["product"] == "MIS"

    def test_non_lot_multiple_is_still_rejected(self, client, placed, split):
        resp = _post_order(client, symbol="NIFTY26DEC2424000CE", exchange="NFO", quantity=66)

        assert resp.status_code == 400
        assert "whole number of lots" in resp.get_json()["message"]
        assert placed == [] and split == []

    def test_unknown_symbol_is_rejected(self, client, placed, split):
        resp = _post_order(client, symbol="NOSUCH26DEC2424000CE", exchange="NFO", quantity=65)

        assert resp.status_code == 400
        assert placed == [] and split == []

    def test_quantity_above_the_sanity_ceiling_is_rejected(self, client, placed, split):
        resp = _post_order(
            client, symbol="NIFTY26DEC2424000CE", exchange="NFO", quantity=MAX_ORDER_QUANTITY + 65
        )

        assert resp.status_code == 400
        assert placed == [] and split == []

    def test_lots_field_is_accepted_but_not_a_cap(self, client, placed, split):
        """`lots` may be sent with a huge value; entries are still split, not capped."""
        resp = _post_order(
            client,
            symbol="NIFTY26DEC2424000CE",
            exchange="NFO",
            quantity=1755 * 2,
            lots=54,
        )

        assert resp.status_code == 200
        assert len(split) == 1 and split[0]["splitsize"] == 1755

    def test_futures_above_freeze_split_too(self, client, placed, split):
        resp = _post_order(client, symbol="NIFTY26DEC24FUT", exchange="NFO", quantity=1755 * 3)

        assert resp.status_code == 200
        assert split[0]["splitsize"] == 1755
        assert split[0]["quantity"] == 1755 * 3

    def test_equity_bypasses_lot_rules_entirely(self, client, placed, split):
        resp = _post_order(client, symbol="RELIANCE", exchange="NSE", quantity=100000)

        assert resp.status_code == 200
        assert len(placed) == 1
        assert split == []


# ---------------------------------------------------------------------------
# Strikes / futures responses expose the freeze limit for the UI.
# ---------------------------------------------------------------------------


class TestFreezeQtyInResponses:
    def test_futures_response_carries_the_underlying_freeze(self, client):
        resp = client.get(
            "/scalping/api/futures?underlying=NIFTY&exchange=NFO",
            headers={"Accept": "application/json"},
        )

        assert resp.status_code == 200
        assert resp.get_json()["freeze_qty"] == 1800

    def test_futures_response_freeze_is_none_when_unconfigured(self, client, monkeypatch):
        import database.qty_freeze_db as qty_freeze_db

        monkeypatch.setattr(qty_freeze_db, "get_freeze_qty", lambda symbol, exchange: 1)
        resp = client.get(
            "/scalping/api/futures?underlying=NIFTY&exchange=NFO",
            headers={"Accept": "application/json"},
        )

        assert resp.status_code == 200
        assert resp.get_json()["freeze_qty"] is None


# ---------------------------------------------------------------------------
# _reducing_exit (exits) keeps the same chunk contract as entries.
# ---------------------------------------------------------------------------


class TestReducingExitChunkParity:
    def test_exit_and_entry_use_the_same_freeze_chunk(self):
        from blueprints.scalping import _reducing_exit

        # Entry chunk for 1820:
        entry_chunk = _entry_split_chunk("NIFTY26DEC2424000CE", "NFO", 1820)
        assert entry_chunk == 1755

        # Exit for the same instrument slices at the same 1755 chunk: a stub
        # split_order captures the splitsize the exit would dispatch.
        captured: dict = {}

        def fake_split_order(split_data=None, **_):
            captured.update(split_data)
            return True, {"status": "success"}, 200

        import unittest.mock as mock

        import services.split_order_service as sos

        with mock.patch.object(sos, "split_order", fake_split_order):
            ok, _resp, _code = _reducing_exit(
                "NIFTY26DEC2424000CE", "NFO", "MIS", "SELL", 1820, "tok", "zerodha", "key"
            )

        assert ok is True
        assert captured["splitsize"] == 1755
        assert captured["quantity"] == 1820

    def test_small_exit_is_a_single_order(self):
        import unittest.mock as mock

        import services.place_order_service as pos
        from blueprints.scalping import _reducing_exit

        captured: dict = {}

        def fake_place_order(order_data=None, **_):
            captured.update(order_data)
            return True, {"status": "success", "orderid": "x"}, 200

        with mock.patch.object(pos, "place_order", fake_place_order):
            ok, _resp, _code = _reducing_exit(
                "NIFTY26DEC2424000CE", "NFO", "MIS", "SELL", 65, "tok", "zerodha", "key"
            )

        assert ok is True
        assert captured["quantity"] == 65

    def test_exit_rejects_non_whole_lots(self):
        from blueprints.scalping import _reducing_exit

        ok, resp, code = _reducing_exit(
            "NIFTY26DEC2424000CE", "NFO", "MIS", "SELL", 66, "tok", "zerodha", "key"
        )

        assert ok is False and code == 400
        assert "whole number of lots" in resp["message"]


class TestReducingExitPartialFailure:
    """A split exit with failed chunks must NOT report success.

    The risk monitor clears the SL only when _reducing_exit says ok — if a
    split with 5/55 failed children still said success, the monitor deleted
    the stop and the remainder sat open with nothing watching it. The exit
    must fail closed, so the tick engine keeps protecting the remainder and
    re-exits automatically while the price stays beyond the stop (throttled,
    never hammering the broker).
    """

    def _split_stub(self, results):
        """Return a split_order stub yielding the given per-child results."""
        import services.split_order_service as sos

        def fake_split_order(split_data=None, **_):
            return (
                True,  # the service itself always reports success
                {
                    "status": "success",
                    "total_quantity": split_data["quantity"],
                    "split_size": split_data["splitsize"],
                    "results": results,
                },
                200,
            )

        return mock.patch.object(sos, "split_order", fake_split_order)

    def test_a_split_exit_with_failed_children_reports_failure(self):
        from blueprints.scalping import _reducing_exit

        results = [
            {"order_num": 1, "quantity": 1755, "status": "success", "orderid": "a"},
            {"order_num": 2, "quantity": 65, "status": "error", "message": "rejected"},
        ]
        with self._split_stub(results):
            ok, resp, code = _reducing_exit(
                "NIFTY26DEC2424000CE", "NFO", "MIS", "SELL", 1820, "tok", "zerodha", "key"
            )

        assert ok is False
        assert resp["status"] == "error"
        assert "still open" in resp["message"]
        # The exact failed quantity is reported (the 1-lot remainder).
        assert "65 of 1820" in resp["message"]
        # Per-child detail rides along so callers can surface what failed.
        assert resp["results"] == results

    def test_a_fully_successful_split_exit_still_reports_success(self):
        from blueprints.scalping import _reducing_exit

        results = [
            {"order_num": 1, "quantity": 1755, "status": "success", "orderid": "a"},
            {"order_num": 2, "quantity": 65, "status": "success", "orderid": "b"},
        ]
        with self._split_stub(results):
            ok, resp, code = _reducing_exit(
                "NIFTY26DEC2424000CE", "NFO", "MIS", "SELL", 1820, "tok", "zerodha", "key"
            )

        assert ok is True
        assert resp["status"] == "success"

    def test_a_split_rejected_by_the_service_propagates_failure(self):
        import services.split_order_service as sos
        from blueprints.scalping import _reducing_exit

        def fake_split_order(split_data=None, **_):
            return False, {"status": "error", "message": "broker down"}, 502

        with mock.patch.object(sos, "split_order", fake_split_order):
            ok, resp, _code = _reducing_exit(
                "NIFTY26DEC2424000CE", "NFO", "MIS", "SELL", 1820, "tok", "zerodha", "key"
            )

        assert ok is False
        assert resp["message"] == "broker down"


# ---------------------------------------------------------------------------
# The split service itself must slice 1820 into 27-lot + 1-lot orders.
# ---------------------------------------------------------------------------


class TestSplitServiceSlicing:
    def test_split_order_slices_into_freeze_chunks(self, monkeypatch):
        from services import split_order_service as sos

        placed: list[int] = []
        broker_module = SimpleNamespace(
            place_order_api=lambda order_data, auth_token: (
                SimpleNamespace(status=200),
                {},
                f"oid{len(placed) + 1}",
            )
        )
        # Record the quantity each child order carried.
        original = broker_module.place_order_api

        def recording(order_data, auth_token):
            placed.append(int(order_data["quantity"]))
            return original(order_data, auth_token)

        broker_module.place_order_api = recording
        monkeypatch.setattr(sos, "import_broker_module", lambda b: broker_module)
        monkeypatch.setattr(sos, "time", SimpleNamespace(sleep=lambda _s: None))
        monkeypatch.setattr(sos, "get_analyze_mode", lambda: False)
        monkeypatch.setattr(sos, "get_auth_token_broker", lambda k: ("tok", "zerodha"))

        success, response, code = sos.split_order_with_auth(
            {
                "strategy": "Scalping",
                "symbol": "NIFTY26DEC2424000CE",
                "exchange": "NFO",
                "action": "BUY",
                "quantity": 1820,
                "splitsize": 1755,
                "pricetype": "MARKET",
                "product": "MIS",
            },
            "tok",
            "zerodha",
            {"apikey": "key"},
        )

        assert success is True and code == 200
        assert placed == [1755, 65]
        assert response["total_quantity"] == 1820
        assert response["split_size"] == 1755
        assert [r["status"] for r in response["results"]] == ["success", "success"]
        assert [r["quantity"] for r in response["results"]] == [1755, 65]

    def test_split_service_analyze_mode_routes_each_child_to_the_sandbox(self, monkeypatch):
        from services import split_order_service as sos

        sandbox_orders: list[int] = []
        monkeypatch.setattr(sos, "get_analyze_mode", lambda: True)

        import services.sandbox_service as sandbox_service

        def fake_sandbox_place_order(order_data, api_key, original_data, prefetched_quote=None):
            sandbox_orders.append(int(order_data["quantity"]))
            return True, {"status": "success", "orderid": f"s{len(sandbox_orders)}"}, 200

        monkeypatch.setattr(
            sandbox_service, "sandbox_place_order", fake_sandbox_place_order, raising=True
        )
        # The service imports the function lazily inside the analyze branch.
        monkeypatch.setattr(
            "services.sandbox_service.sandbox_place_order", fake_sandbox_place_order
        )
        monkeypatch.setattr(sos, "get_auth_token_broker", lambda k: ("tok", "zerodha"))

        success, response, code = sos.split_order_with_auth(
            {
                "strategy": "Scalping",
                "symbol": "BANKNIFTY26DEC2451000CE",
                "exchange": "NFO",
                "action": "BUY",
                "quantity": 1260,  # 42 lots -> chunks of 600 + 600 + 60
                "splitsize": 600,
                "pricetype": "MARKET",
                "product": "MIS",
            },
            "tok",
            "zerodha",
            {"apikey": "key"},
        )

        assert success is True and code == 200
        assert sandbox_orders == [600, 600, 60]
        assert response["mode"] == "analyze"
        assert [r["quantity"] for r in response["results"]] == [600, 600, 60]
