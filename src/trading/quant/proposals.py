"""Strategy parameter proposals: propose -> review (different principal) -> applied next session.

A proposal never edits live parameters. Approval only marks it; ``apply_approved`` is called by
``start_session`` (and nowhere else), so parameters cannot change mid-session. Only strategy
parameters are tunable: enablement, sleeve size and risk limits are not reachable from here.
"""

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.core.errors import TradingError
from trading.db.models.research import Strategy, StrategyConfigProposal
from trading.monitoring.audit import record_audit
from trading.quant.source import DEFAULT_FACTORIES

PENDING, APPROVED, REJECTED, APPLIED, SUPERSEDED = (
    "PENDING_REVIEW",
    "APPROVED",
    "REJECTED",
    "APPLIED",
    "SUPERSEDED",
)
# The end-of-day flatten is a risk control: its timing is not tunable through proposals.
# Replay/backtest accounts share the global strategies table, so they never apply live proposals.
SCRATCH_ACCOUNT_PREFIXES = ("replay-", "backtest")
NOT_TUNABLE = frozenset({"eod_flatten"})


class ProposalError(TradingError):
    code = "PROPOSAL_ERROR"


class ProposalNotFound(ProposalError):
    code = "NOT_FOUND"


class ProposalConflict(ProposalError):
    code = "CONFLICT"


class ProposalInvalid(ProposalError):
    code = "INVALID_PARAMETERS"


class ProposalForbidden(ProposalError):
    code = "FORBIDDEN"


def bump_version(version: str) -> str:
    head, _, tail = version.rpartition(".")
    if not tail.isdigit():
        raise ProposalInvalid(f"cannot bump version {version!r}")
    return f"{head}.{int(tail) + 1}" if head else f"{int(tail) + 1}"


def _reject_floats(value: object, path: str) -> None:
    if isinstance(value, float):
        raise ProposalInvalid(f"{path}: floats are not allowed; send a decimal string")
    if isinstance(value, Mapping):
        for k, v in value.items():
            _reject_floats(v, f"{path}.{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            _reject_floats(v, f"{path}[{i}]")


def _validate(strategy: Strategy, merged: Mapping[str, Any], universe: Collection[str]) -> None:
    factory = DEFAULT_FACTORIES.get(strategy.id)
    if factory is None or strategy.id in NOT_TUNABLE:
        raise ProposalInvalid(f"strategy {strategy.id!r} has no tunable parameters")
    symbols = merged.get("symbols", ())
    if isinstance(symbols, list | tuple) and any(s not in universe for s in symbols):
        raise ProposalInvalid("symbols must all be in the configured universe")
    try:
        factory(strategy_id=strategy.id, version=strategy.version, params=merged)
    except ValidationError as exc:
        raise ProposalInvalid(f"invalid parameters: {exc.errors()[0]['msg']}") from exc
    except ValueError as exc:  # cross-field checks inside a strategy constructor
        raise ProposalInvalid(f"invalid parameters: {exc}") from exc


def propose_config(
    session: Session,
    *,
    strategy_id: str,
    base_version: str,
    parameters: Mapping[str, Any],
    rationale: str,
    proposed_by: str,
    universe: Collection[str],
    now: datetime,
) -> StrategyConfigProposal:
    strategy = session.scalar(select(Strategy).where(Strategy.id == strategy_id).with_for_update())
    if strategy is None:
        raise ProposalNotFound(f"unknown strategy {strategy_id!r}")
    if strategy.version != base_version:
        raise ProposalConflict(
            f"base_version {base_version!r} is stale; current version is {strategy.version!r}"
        )
    if not parameters:
        raise ProposalInvalid("parameters must not be empty")
    _reject_floats(parameters, "parameters")
    unknown = set(parameters) - set(strategy.params_json)
    if unknown:
        raise ProposalInvalid(f"unknown parameters {sorted(unknown)}")
    _validate(strategy, {**strategy.params_json, **parameters}, universe)

    for older in session.scalars(
        select(StrategyConfigProposal).where(
            StrategyConfigProposal.strategy_id == strategy_id,
            StrategyConfigProposal.status == PENDING,
        )
    ):
        older.status = SUPERSEDED
    row = StrategyConfigProposal(
        id=uuid4(),
        strategy_id=strategy_id,
        base_version=base_version,
        parameters_json=dict(parameters),
        rationale=rationale,
        status=PENDING,
        proposed_by=proposed_by,
        created_at=now,
    )
    session.add(row)
    session.flush()
    record_audit(
        session,
        actor=proposed_by,
        action="strategy_config.propose",
        target=f"strategy:{strategy_id}",
        request_id=None,
        before=None,
        after=dict(parameters),
        result="PENDING_REVIEW",
        now=now,
        details={"proposal_id": str(row.id), "base_version": base_version},
    )
    return row


def review_proposal(
    session: Session, proposal_id: UUID, *, approve: bool, reviewer: str, now: datetime
) -> StrategyConfigProposal:
    row = session.scalar(
        select(StrategyConfigProposal)
        .where(StrategyConfigProposal.id == proposal_id)
        .with_for_update()
    )
    if row is None:
        raise ProposalNotFound(f"unknown proposal {proposal_id}")
    if row.proposed_by == reviewer:
        raise ProposalForbidden("a proposal must be reviewed by a different principal")
    if row.status != PENDING:
        raise ProposalConflict(f"proposal is {row.status}; only PENDING_REVIEW can be reviewed")
    row.status = APPROVED if approve else REJECTED
    row.reviewed_by = reviewer
    row.reviewed_at = now
    session.flush()
    record_audit(
        session,
        actor=reviewer,
        action="strategy_config.approve" if approve else "strategy_config.reject",
        target=f"strategy:{row.strategy_id}",
        request_id=None,
        before={"status": PENDING},
        after={"status": row.status},
        result=row.status,
        now=now,
        details={"proposal_id": str(row.id)},
    )
    return row


def apply_approved(session: Session, *, now: datetime) -> list[StrategyConfigProposal]:
    """Apply approved proposals. Call ONLY from session start. Stale bases are superseded."""
    applied: list[StrategyConfigProposal] = []
    approved = session.scalars(
        select(StrategyConfigProposal)
        .where(StrategyConfigProposal.status == APPROVED)
        .order_by(StrategyConfigProposal.created_at, StrategyConfigProposal.id)
        .with_for_update()
    ).all()
    for row in approved:
        strategy = session.scalar(
            select(Strategy).where(Strategy.id == row.strategy_id).with_for_update()
        )
        if strategy is None or strategy.version != row.base_version:
            row.status = SUPERSEDED
            outcome = "SUPERSEDED"
        else:
            merged = {**strategy.params_json, **row.parameters_json}
            before = {"version": strategy.version, "params": dict(strategy.params_json)}
            strategy.params_json = merged
            strategy.version = bump_version(strategy.version)
            row.status = APPLIED
            row.applied_at = now
            row.applied_version = strategy.version
            applied.append(row)
            outcome = "APPLIED"
            record_audit(
                session,
                actor="system",
                action="strategy_config.apply",
                target=f"strategy:{row.strategy_id}",
                request_id=None,
                before=before,
                after={"version": strategy.version, "params": merged},
                result=outcome,
                now=now,
                details={"proposal_id": str(row.id)},
            )
    session.flush()
    return applied
