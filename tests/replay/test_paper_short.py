from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from tests.replay.helpers import DAY, rows_of, scalar
from tests.replay.orb_day import write_orb_day
from tests.replay.test_replay_strategies import orb_config, replay
from trading.execution.replay import replay_session


@pytest.mark.parametrize(("rally", "reason"), [("101.2", "TARGET"), (None, "EOD_FLATTEN")])
def test_bearish_orb_through_reconciliation(db, session_factory, tmp_path, rally, reason):
    config = orb_config(max_hold_minutes=900, max_entries_per_symbol_per_day=1)
    config = config.model_copy(
        update={"risk": config.risk.model_copy(update={"allow_short": True})}
    )
    fixtures = write_orb_day(tmp_path / "day", bearish=True, rally_to=rally)
    result = replay(session_factory, fixtures, config)
    assert result.orders == 2
    fills = rows_of(
        session_factory,
        "SELECT o.position_effect, f.qty, f.price, f.fees FROM fills f "
        "JOIN orders o ON o.id=f.order_id ORDER BY f.ts",
    )
    assert [row[0] for row in fills] == ["OPEN_SHORT", "CLOSE_SHORT"]
    (_, q, entry, fee), (_, covered, exit_price, fee2) = fills
    expected = ((entry - exit_price) * q - fee - fee2).quantize(D(".0001"))
    assert covered == q and q > 0
    qty, pnl = rows_of(session_factory, "SELECT qty, realized_pnl FROM positions")[0]
    assert qty == 0 and pnl == expected
    assert (
        scalar(session_factory, "SELECT equity FROM equity_snapshots WHERE kind='SESSION_END'")
        == 1000 + pnl
    )
    assert reason in scalar(
        session_factory, "SELECT reason_codes FROM signals WHERE side='BUY' AND status='ALLOCATED'"
    )
    assert (
        scalar(
            session_factory,
            "SELECT status FROM reconciliation_runs ORDER BY started_at DESC LIMIT 1",
        )
        == "OK"
    )


def test_adverse_short_deficit_partial_cover_and_restart(db, session_factory, tmp_path):
    fixtures = write_orb_day(tmp_path / "day", bearish=True, rally_to=None, adverse_price=D("950"))
    config = orb_config(max_hold_minutes=900, max_entries_per_symbol_per_day=1)
    config = config.model_copy(
        update={"risk": config.risk.model_copy(update={"allow_short": True})}
    )
    params = dict(fixtures_dir=fixtures, session_date=DAY, script=None, config=config)
    replay_session(session_factory, **params, stop_after=datetime(2026, 10, 19, 13, 48, tzinfo=UTC))
    held, basis = rows_of(session_factory, "SELECT qty, average_cost FROM positions")[0]
    assert held < 0
    # A reconstructed runtime can cover an existing short even after disabling new shorts.
    params["config"] = config.model_copy(
        update={"risk": config.risk.model_copy(update={"allow_short": False})}
    )
    replay_session(
        session_factory,
        **params,
        resume=True,
        stop_after=datetime(2026, 10, 19, 13, 55, tzinfo=UTC),
    )
    assert (
        scalar(
            session_factory,
            "SELECT count(*) FROM incidents WHERE incident_key LIKE 'collateral_deficit:%'",
        )
        == 1
    )
    fills = rows_of(
        session_factory,
        "SELECT o.position_effect,f.qty,f.price,f.fees FROM orders o JOIN fills f ON f.order_id=o.id ORDER BY f.ts",
    )
    assert [r[0] for r in fills] == ["OPEN_SHORT", "CLOSE_SHORT", "CLOSE_SHORT"]
    assert fills[1][1] == D("0.4") and sum(r[1] for r in fills[1:]) == -held
    realized = sum(((basis - r[2]) * r[1]).quantize(D(".0001")) for r in fills[1:]) - sum(
        r[3] for r in fills
    )
    assert rows_of(session_factory, "SELECT qty, realized_pnl FROM positions") == [(D(0), realized)]
    assert (
        scalar(session_factory, "SELECT equity FROM equity_snapshots ORDER BY ts DESC LIMIT 1")
        == 1000 + realized
    )
