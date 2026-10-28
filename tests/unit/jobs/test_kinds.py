from datetime import UTC, date, datetime

from trading.core.config import AgentsConfig
from trading.data.calendar import SessionInfo
from trading.jobs.kinds import ALL_KINDS, EXECUTION_KINDS, OPERATIONS_KINDS, RESEARCH_KINDS
from trading.jobs.schedule import plan_jobs, plan_session_jobs


def test_role_sets_are_disjoint() -> None:
    assert not EXECUTION_KINDS & OPERATIONS_KINDS
    assert not EXECUTION_KINDS & RESEARCH_KINDS
    assert not OPERATIONS_KINDS & RESEARCH_KINDS


def test_every_planned_kind_is_assigned_to_a_role() -> None:
    """A kind no role claims would sit in the queue forever; a kind in two roles is ambiguous."""
    session = SessionInfo(
        session_date=date(2026, 10, 19),
        open_at=datetime(2026, 10, 19, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 10, 19, 20, 0, tzinfo=UTC),
        is_half_day=False,
    )
    agents = AgentsConfig.model_validate(
        {
            "macro": {"enabled": True},
            "fundamental": {"enabled": True},
            "consensus": {"enabled": True},
            "evaluator": {"enabled": True},
        }
    )
    planned = {j.kind for j in plan_session_jobs(session, agents=agents)}
    planned |= {j.kind for j in plan_jobs(datetime(2026, 10, 19, 13, 31, tzinfo=UTC), None)}
    assert planned <= ALL_KINDS
    # document_import is enqueued on demand by the API, not by the schedule.
    scheduled_research = RESEARCH_KINDS - {"document_import"}
    assert planned >= scheduled_research | {
        "session_start",
        "paper_tick",
        "session_end",
        "heartbeat",
    }


def test_research_kinds_are_exactly_the_agent_kinds() -> None:
    assert {k for k in ALL_KINDS if k.startswith("agent_")} == RESEARCH_KINDS - {"document_import"}
