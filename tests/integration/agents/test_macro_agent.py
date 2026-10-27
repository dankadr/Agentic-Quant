import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0
from trading.agents.llm import LlmError, LlmInvalidOutput, LlmResult
from trading.agents.macro import MacroOutput, latest_macro_view, run_macro_agent
from trading.core.clock import ManualClock
from trading.core.config import load_config
from trading.data.calendar import MarketCalendar
from trading.data.instruments import upsert_universe
from trading.db.models.market import MarketBar
from trading.quant.registry import seed_strategies

CFG = load_config(Path("configs"))
D = Decimal
OPEN = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
NOW = OPEN + timedelta(minutes=40, seconds=10)


@dataclass
class FakeLlm:
    reply: object  # MacroOutput | Exception
    calls: int = 0
    last_user: str = ""

    def complete_json(self, system: str, user: str, schema):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.last_user = user
        if isinstance(self.reply, Exception):
            raise self.reply
        return LlmResult(
            value=self.reply, model="fake-model", prompt_tokens=10, completion_tokens=5
        )


def output(**over) -> MacroOutput:  # type: ignore[no-untyped-def]
    base = dict(regime="RANGING", risk_multiplier=D("0.6"), confidence=0.7,
                evidence=["index efficiency is low"], limitations=["only 40 minutes of data"])  # fmt: skip
    base.update(over)
    return MacroOutput(**base)


@pytest.fixture
def seeded(db: Session) -> None:
    instruments = upsert_universe(db, CFG.universe)
    seed_strategies(db, CFG, now=T0)
    for inst in instruments.values():
        for m in range(40):
            px = D("100") + D(m) / 10
            db.add(MarketBar(instrument_id=inst.id, interval="1m", ts=OPEN + timedelta(minutes=m), provider="replay",
                             open=px, high=px + D("0.05"), low=px - D("0.05"), close=px, volume=D("1000"),
                             ingested_at=OPEN + timedelta(minutes=m + 1, seconds=5), session_date=OPEN.date(),
                             source_seq=None, quality_flags=[]))  # fmt: skip
    db.commit()


def run(
    sf: sessionmaker[Session], calendar: MarketCalendar, llm: FakeLlm, now: datetime = NOW
) -> str:
    return run_macro_agent(  # type: ignore[arg-type]
        sf, llm, config=CFG, calendar=calendar, provider_name="replay", clock=ManualClock(now)
    )


def test_a_valid_answer_is_stored_with_the_full_contract_and_becomes_the_current_view(
    seeded: None, db: Session, session_factory: sessionmaker[Session], calendar: MarketCalendar
) -> None:
    llm = FakeLlm(output())
    assert run(session_factory, calendar, llm) == "COMPLETED"
    row = db.execute(
        text(
            "select agent_name, model, prompt_version, status, result_json, cost_usd from agent_runs"
        )
    ).one()
    assert (row.agent_name, row.model, row.prompt_version, row.status) == (
        "macro",
        "fake-model",
        "macro-v1",
        "COMPLETED",
    )
    r = row.result_json
    assert r["regime"] == "RANGING"
    assert D(str(r["risk_multiplier"])) == D("0.6")
    assert r["valid_until"] == "2026-10-19T15:10:10Z"  # now + 60 min TTL
    assert {
        "agent_name",
        "run_id",
        "model_id",
        "prompt_version",
        "created_at",
        "evidence",
        "conclusion",
        "confidence",
        "limitations",
        "input_hash",
    } <= set(r)
    view = latest_macro_view(db, NOW + timedelta(minutes=5))
    assert view is not None
    assert (view.regime, view.risk_multiplier) == ("RANGING", D("0.6"))
    assert latest_macro_view(db, NOW + timedelta(minutes=61)) is None  # stale after its TTL


def test_the_prompt_carries_numbers_only(
    seeded: None, session_factory: sessionmaker[Session], calendar: MarketCalendar
) -> None:
    llm = FakeLlm(output())
    run(session_factory, calendar, llm)
    payload = json.loads(llm.last_user)
    assert set(payload) == {"bars", "breadth_above_vwap", "indices"}


@pytest.mark.parametrize("bad", [D("1.5"), D("-0.1")])
def test_a_multiplier_outside_zero_to_one_is_rejected_by_the_schema(bad: Decimal) -> None:
    with pytest.raises(ValueError):
        output(risk_multiplier=bad)


def test_unknown_regime_or_extra_fields_are_invalid() -> None:
    with pytest.raises(ValueError):
        output(regime="MOON")
    with pytest.raises(ValueError):
        MacroOutput.model_validate({**output().model_dump(), "tool": "place_order"})


def test_invalid_model_output_is_recorded_and_never_becomes_a_view(
    seeded: None, db: Session, session_factory: sessionmaker[Session], calendar: MarketCalendar
) -> None:
    assert (
        run(session_factory, calendar, FakeLlm(LlmInvalidOutput("nonsense"))) == "REJECTED_INVALID"
    )
    assert latest_macro_view(db, NOW) is None
    assert db.execute(text("select status from agent_runs")).scalar_one() == "REJECTED_INVALID"


def test_an_unreachable_model_is_recorded_as_failed(
    seeded: None, db: Session, session_factory: sessionmaker[Session], calendar: MarketCalendar
) -> None:
    assert run(session_factory, calendar, FakeLlm(LlmError("down"))) == "FAILED"
    assert latest_macro_view(db, NOW) is None


def test_too_little_data_skips_the_model_entirely(
    db: Session, session_factory: sessionmaker[Session], calendar: MarketCalendar
) -> None:
    upsert_universe(db, CFG.universe)
    db.commit()
    llm = FakeLlm(output())
    assert run(session_factory, calendar, llm) == "FAILED"
    assert llm.calls == 0
    assert (
        "insufficient_data"
        in db.execute(text("select result_json::text from agent_runs")).scalar_one()
    )


def test_the_newest_valid_run_wins(
    seeded: None, db: Session, session_factory: sessionmaker[Session], calendar: MarketCalendar
) -> None:
    run(session_factory, calendar, FakeLlm(output(risk_multiplier=D("0.9"))))
    # Inside the fixture's data window: a later run needs fresh bars to be a valid input at all.
    run(
        session_factory,
        calendar,
        FakeLlm(output(risk_multiplier=D("0.3"))),
        now=NOW + timedelta(minutes=1),
    )
    view = latest_macro_view(db, NOW + timedelta(minutes=2))
    assert view is not None
    assert view.risk_multiplier == D("0.3")


def test_evaluator_writes_a_narrative_and_has_no_effect_on_trading(
    seeded: None, db: Session, session_factory: sessionmaker[Session], calendar: MarketCalendar
) -> None:
    from trading.agents.evaluator import EvaluatorOutput, run_evaluator_agent
    from trading.portfolio.ledger import open_paper_account

    open_paper_account(db, name="paper-primary", starting_cash=D("1000"), now=T0)
    db.commit()
    llm = FakeLlm(
        EvaluatorOutput(
            summary="Quiet day, no trades.", findings=["no signals"], suggestions=["none"]
        )
    )  # type: ignore[arg-type]
    status = run_evaluator_agent(
        session_factory, llm, config=CFG, session_date=OPEN.date(), provider_name="replay", now=NOW
    )  # type: ignore[arg-type]
    assert status == "COMPLETED"
    row = db.execute(
        text("select agent_name, result_json from agent_runs where agent_name = 'evaluator'")
    ).one()
    assert row.result_json["summary"] == "Quiet day, no trades."
    assert row.result_json["input_hash"]
    assert json.loads(llm.last_user)["date"] == "2026-10-19"
    assert latest_macro_view(db, NOW) is None  # the evaluator never produces a trading view
    assert db.execute(text("select count(*) from strategy_config_proposals")).scalar_one() == 0


def _enabled_cfg(**over):  # type: ignore[no-untyped-def]
    from trading.core.config import AgentsConfig

    agents = AgentsConfig.model_validate(
        {"macro": {"enabled": True}, "evaluator": {"enabled": True}, **over}
    )
    return CFG.model_copy(update={"agents": agents})


def test_job_wrappers_respect_enablement_missing_llm_and_staleness(
    seeded: None, db: Session, session_factory: sessionmaker[Session], calendar: MarketCalendar
) -> None:
    from trading.agents.jobs import macro_job
    from trading.core.clock import ManualClock
    from trading.core.config import AgentsConfig

    clock = ManualClock(NOW)
    payload = {"at": (NOW - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}

    def runs() -> list[tuple[str, str | None]]:
        """(status, skip reason) of every macro run, oldest first."""
        db.rollback()
        rows = db.execute(
            text("select status, result_json::text from agent_runs order by started_at")
        ).all()
        out: list[tuple[str, str | None]] = []
        for status, body in rows:
            reason = json.loads(body).get("skip_reason") if body else None
            out.append((status, reason))
        return out

    # disabled in config -> nothing happens, even with a model available
    macro_job(
        session_factory,
        FakeLlm(output()),
        config=CFG.model_copy(update={"agents": AgentsConfig()}),
        calendar=calendar,
        provider_name="replay",
        clock=clock,
        payload=payload,
    )  # type: ignore[arg-type]
    assert runs() == []
    # enabled but no model configured -> nothing happens (and no crash)
    macro_job(
        session_factory,
        None,
        config=_enabled_cfg(),
        calendar=calendar,
        provider_name="replay",
        clock=clock,
        payload=payload,
    )
    assert runs() == [("SKIPPED", "no_llm_key")]
    # a job that fires far too late is skipped, like a stale tick
    late = {"at": (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    macro_job(
        session_factory,
        FakeLlm(output()),
        config=_enabled_cfg(),
        calendar=calendar,
        provider_name="replay",
        clock=clock,
        payload=late,
    )  # type: ignore[arg-type]
    assert runs()[-1] == ("SKIPPED", "stale_job")
    macro_job(
        session_factory,
        FakeLlm(output()),
        config=_enabled_cfg(),
        calendar=calendar,
        provider_name="replay",
        clock=clock,
        payload=payload,
    )  # type: ignore[arg-type]
    assert [status for status, _ in runs()][-1] in {"COMPLETED", "FAILED"}
