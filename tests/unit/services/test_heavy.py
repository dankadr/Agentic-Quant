from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from trading.core.config import load_config
from trading.services import heavy


def test_scratch_guard_refuses_production_and_same_database_alias() -> None:
    with pytest.raises(ValueError, match="scratch"):
        heavy.validate_scratch_url(
            "postgresql+psycopg://u@host/trading", "postgresql+psycopg://u@host/trading"
        )
    with pytest.raises(ValueError, match="same"):
        heavy.validate_scratch_url(
            "postgresql+psycopg://other@host/test_db", "postgresql+psycopg://u@host/test_db"
        )
    assert (
        make_url(
            heavy.validate_scratch_url(
                "postgresql+psycopg://u@host/test_scratch", "postgresql+psycopg://u@host/trading"
            )
        ).database
        == "test_scratch"
    )


def test_fingerprint_changes_when_same_size_data_changes(tmp_path: Path) -> None:
    for name in ("bars.csv", "quotes.csv"):
        (tmp_path / name).write_text("a")
    first = heavy.fixture_fingerprint(tmp_path)
    (tmp_path / "bars.csv").write_text("b")
    assert first != heavy.fixture_fingerprint(tmp_path)


def test_pair_changes_only_entry_limit_preserves_risk_and_financing(tmp_path: Path) -> None:
    config = load_config(Path("configs"))
    seen = []

    def run(sf, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            result_hash="hash",
            account=SimpleNamespace(
                net_pnl=Decimal("2"),
                trades=SimpleNamespace(trades=2),
                costs=SimpleNamespace(fees=Decimal("0.1"), slippage=Decimal("0.2")),
                max_drawdown_pct=Decimal("0.01"),
            ),
        )

    result = heavy.compare_entries(
        None, fixtures=tmp_path, config=config, day=date(2026, 11, 13), runner=run
    )
    assert len(seen) == 2
    assert seen[0]["config"].strategies["orb_v1"].params["max_entries_per_symbol_per_day"] == 1
    assert seen[1]["config"].strategies["orb_v1"].params["max_entries_per_symbol_per_day"] == 2
    for call in seen:
        assert call["config"].risk == config.risk
        assert call["config"].system.starting_cash_usd == config.system.starting_cash_usd
        assert call["reset"] is True
        assert call["start"] == call["end"] == date(2026, 11, 13)
    assert result["baseline"]["trades"] == 2
    assert result["candidate"]["fees"] == "0.1"
    assert config.strategies["orb_v1"].params["max_entries_per_symbol_per_day"] == 1


def test_retrospective_compares_normalized_returns_not_different_funding():
    metrics = heavy.retrospective(
        [
            {
                "starting_equity": "1000",
                "baseline": {"net_pnl": "10", "trades": 1},
                "candidate": {"net_pnl": "20", "trades": 2},
            },
            {
                "starting_equity": "100000",
                "baseline": {"net_pnl": "1000", "trades": 1},
                "candidate": {"net_pnl": "0", "trades": 2},
            },
        ]
    )
    assert metrics["sessions"] == 2
    assert Decimal(metrics["mean_return_difference"]) == 0
    assert metrics["extra_trades"] == 2


def test_empty_quote_collection_is_not_usable_research(tmp_path: Path):
    (tmp_path / "bars.csv").write_text(
        "symbol,ts,open,high,low,close,volume\nSPY,2026-11-13T14:30:00Z,1,1,1,1,1\n"
    )
    (tmp_path / "quotes.csv").write_text("symbol,ts,bid,ask\n")
    assert not heavy.research_data_ready(tmp_path, [date(2026, 11, 13)], ["SPY"])


def test_tick_timeline_uses_durable_job_minute_not_snapshot_floor():
    minute = datetime(2026, 11, 13, 14, 31, tzinfo=UTC)
    observed = minute.replace(minute=32, second=2)
    completed = observed.replace(second=3)
    assert heavy.recorded_timeline([observed], [(minute, completed)]) == {minute: observed}
    assert heavy.recorded_timeline([observed], []) is None


def test_reordered_completed_job_minutes_are_not_replayed():
    first = datetime(2026, 11, 13, 14, 31, tzinfo=UTC)
    second = first.replace(minute=32)
    assert (
        heavy.recorded_timeline(
            [second.replace(second=10), second.replace(second=20)],
            [(second, second.replace(second=11)), (first, second.replace(second=21))],
        )
        is None
    )
