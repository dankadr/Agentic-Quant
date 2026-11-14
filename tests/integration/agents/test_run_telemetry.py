"""Run timing must reflect model work; unknown costs/usage must remain unknown."""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.integration.agents.test_fundamental import NOW, FakeNews, good, news
from tests.integration.agents.test_technical import experiments, output
from trading.agents.evaluator import EvaluatorOutput, MetricFinding, run_evaluator_agent
from trading.agents.fundamental import run_fundamental_agent
from trading.agents.jobs import evaluator_job, fundamental_job
from trading.agents.llm import LlmError, LlmInvalidOutput, LlmResult
from trading.agents.technical import run_technical_agent
from trading.core.clock import ManualClock
from trading.core.config import load_config
from trading.db.models.research import AgentRun
from trading.quant.registry import seed_strategies

CFG = load_config(Path("configs"))


class AdvancingLlm:
    def __init__(self, clock, reply, usage_reported=True):
        self.clock, self.reply = clock, reply
        self.calls = 0
        self.usage_reported = usage_reported

    def complete_json(self, system, user, schema):
        self.calls += 1
        self.clock.advance(timedelta(seconds=7))
        if isinstance(self.reply, Exception):
            raise self.reply
        return LlmResult(
            value=self.reply,
            model="measured-model",
            prompt_tokens=31,
            completion_tokens=17,
            usage_reported=self.usage_reported,
        )


def invoke(agent, sf, db, llm, clock, *, with_inputs=True, wrapper=False):
    if agent == "fundamental":
        source = FakeNews([news(1, "earnings")] if with_inputs else [])
        if wrapper:
            return fundamental_job(sf, llm, source, config=CFG, clock=clock)
        return run_fundamental_agent(sf, llm, source, config=CFG, now=NOW, clock=clock)
    if agent == "evaluator":
        if wrapper:
            return evaluator_job(
                sf,
                llm,
                config=CFG,
                provider_name="test",
                clock=clock,
                payload={"session_date": NOW.date().isoformat()},
            )
        return run_evaluator_agent(
            sf, llm, config=CFG, session_date=NOW.date(), provider_name="test", now=NOW, clock=clock
        )
    seed_strategies(db, CFG, now=NOW)
    db.commit()
    if with_inputs:
        experiments(db)
    return run_technical_agent(sf, llm, config=CFG, strategy_id="orb_v1", now=NOW, clock=clock)


@pytest.mark.parametrize("agent", ["fundamental", "evaluator", "technical"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "invalid", "domain_invalid"])
def test_runtime_clock_and_returned_usage_are_persisted(db, session_factory, agent, outcome):
    replies = {
        "fundamental": good(),
        "evaluator": EvaluatorOutput(),
        "technical": output(),
    }
    if outcome == "domain_invalid":
        if agent == "fundamental":
            replies[agent] = good(
                watchlist=[{"symbol": "ZZZZ", "rank": 1, "reason": "x", "confidence": 0.5}]
            )
        elif agent == "evaluator":
            replies[agent] = EvaluatorOutput(
                findings=[MetricFinding(metric_id="invented", explanation_code="OBSERVATION")]
            )
        else:
            pytest.skip("technical proposals are gated, not domain-invalid outputs")
    reply = {"failed": LlmError("unavailable"), "invalid": LlmInvalidOutput("invalid")}.get(
        outcome, replies[agent]
    )
    clock = ManualClock(NOW)
    llm = AdvancingLlm(clock, reply)
    invoke(agent, session_factory, db, llm, clock)
    row = db.scalars(select(AgentRun).where(AgentRun.agent_name == agent)).one()
    assert row.started_at == NOW
    assert row.completed_at == NOW + timedelta(seconds=7)
    assert row.cost_usd is None
    assert row.result_json["telemetry_version"] == "run-telemetry-v1"
    assert row.result_json["completion_clock"] == "runtime"
    if outcome == "completed":
        assert row.status == "COMPLETED"
        assert row.available_at == row.completed_at
    else:
        assert row.status == ("FAILED" if outcome == "failed" else "REJECTED_INVALID")
        assert row.available_at is None
    returned = outcome in ("completed", "domain_invalid")
    assert (row.prompt_tokens, row.completion_tokens) == ((31, 17) if returned else (None, None))
    if returned:
        assert row.model == "measured-model"
    assert llm.calls == 1


@pytest.mark.parametrize("agent", ["fundamental", "technical"])
def test_no_inputs_records_no_fabricated_usage(db, session_factory, agent):
    clock = ManualClock(NOW)
    llm = AdvancingLlm(clock, good())
    invoke(agent, session_factory, db, llm, clock, with_inputs=False)
    row = db.scalars(select(AgentRun).where(AgentRun.agent_name == agent)).one()
    assert row.status == "FAILED"
    assert row.available_at is None
    assert row.cost_usd is None
    assert row.prompt_tokens is None and row.completion_tokens is None
    assert row.result_json["completion_clock"] == "runtime"
    assert llm.calls == 0


@pytest.mark.parametrize("agent", ["fundamental", "evaluator"])
def test_jobs_forward_runtime_clock(db, session_factory, agent):
    clock = ManualClock(NOW)
    llm = AdvancingLlm(clock, good() if agent == "fundamental" else EvaluatorOutput())
    invoke(agent, session_factory, db, llm, clock, wrapper=True)
    row = db.scalars(select(AgentRun).where(AgentRun.agent_name == agent)).one()
    assert row.completed_at == NOW + timedelta(seconds=7)
    assert row.result_json["completion_clock"] == "runtime"


@pytest.mark.parametrize("agent", ["fundamental", "evaluator", "technical"])
def test_unreported_usage_is_null(db, session_factory, agent):
    clock = ManualClock(NOW)
    value = {"fundamental": good(), "evaluator": EvaluatorOutput(), "technical": output()}[agent]
    invoke(agent, session_factory, db, AdvancingLlm(clock, value, usage_reported=False), clock)
    row = db.scalars(select(AgentRun).where(AgentRun.agent_name == agent)).one()
    assert row.prompt_tokens is None and row.completion_tokens is None
    assert row.result_json["usage_reported"] is False


def test_without_clock_legacy_completion_is_not_marked_measured(db, session_factory):
    llm = AdvancingLlm(ManualClock(NOW), good())
    run_fundamental_agent(
        session_factory, llm, FakeNews([news(1, "earnings")]), config=CFG, now=NOW
    )
    row = db.scalars(select(AgentRun)).one()
    assert row.completed_at == NOW
    assert "telemetry_version" not in row.result_json
    assert "completion_clock" not in row.result_json


def test_weekly_research_forwards_runtime_clock(
    db, session_factory, calendar, tmp_path, monkeypatch
):
    from trading.core.config import Settings
    from trading.services import heavy

    seed_strategies(db, CFG, now=NOW)
    db.commit()
    experiments(db)
    days = [NOW.date() - timedelta(days=n) for n in range(30, 0, -1)]
    monkeypatch.setattr(heavy, "recent_sessions", lambda *a, **kw: days)
    monkeypatch.setattr(heavy, "collect", lambda *a, **kw: None)
    monkeypatch.setattr(heavy, "merge_days", lambda *a, **kw: None)
    monkeypatch.setattr(heavy, "merged_dates", lambda *a: days)
    monkeypatch.setattr(heavy, "research_data_ready", lambda *a: True)
    monkeypatch.setattr(
        heavy, "load_research_spaces", lambda *a: {"orb_v1": {"target_r": ["1.5", "2.0"]}}
    )
    monkeypatch.setattr(heavy, "fixture_fingerprint", lambda *a: "test-data")
    monkeypatch.setattr(heavy, "run_research", lambda *a, **kw: None)
    monkeypatch.setattr(heavy, "record_experiment", lambda *a, **kw: None)
    clock = ManualClock(NOW)
    heavy.weekly_research(
        session_factory,
        session_factory,
        settings=Settings(),
        config=CFG,
        calendar=calendar,
        provider=object(),
        llm=AdvancingLlm(clock, output()),
        root=tmp_path,
        clock=clock,
    )
    row = db.scalars(
        select(AgentRun).where(AgentRun.agent_name == "technical", AgentRun.status == "COMPLETED")
    ).one()
    assert row.completed_at == NOW + timedelta(seconds=7)
    assert row.result_json["completion_clock"] == "runtime"
