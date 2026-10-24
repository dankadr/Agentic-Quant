"""Strategies, signals, performance and the strategy-config proposal workflow.

Reads are viewer-level. A proposal never edits live parameters: it is stored PENDING_REVIEW,
reviewed by a *different* operator, and applied by the executor at the next session start.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, tuple_
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, require_operator, require_viewer
from trading.api.deps import get_clock, get_config, get_session
from trading.api.idempotency import run_idempotent
from trading.api.pagination import decode_cursor, encode_cursor
from trading.api.problems import ApiProblem
from trading.api.schemas import Money, Qty
from trading.core.clock import Clock
from trading.core.config import AppConfig
from trading.core.time import UtcDatetime
from trading.db.models.market import Instrument
from trading.db.models.portfolio import Account, EquitySnapshot
from trading.db.models.research import Signal, Strategy, StrategyConfigProposal
from trading.quant.performance import PerformanceReport, load_report
from trading.quant.proposals import (
    APPROVED,
    PENDING,
    REJECTED,
    ProposalError,
    propose_config,
    review_proposal,
)

router = APIRouter(prefix="/v1", tags=["quant"])

SIGNAL_STATUSES = frozenset({"PROPOSED", "ALLOCATED", "REJECTED", "EXPIRED", "IGNORED"})
PROPOSAL_STATUSES = frozenset({PENDING, APPROVED, REJECTED, "APPLIED", "SUPERSEDED"})
_HTTP_STATUS = {"NOT_FOUND": 404, "CONFLICT": 409, "INVALID_PARAMETERS": 422, "FORBIDDEN": 403}


def _problem(exc: ProposalError) -> ApiProblem:
    return ApiProblem(
        _HTTP_STATUS.get(exc.code, 400),
        exc.code,
        "Strategy config proposal refused",
        detail=str(exc),
    )


# ---- strategies ----------------------------------------------------------------------------------


class StrategyOut(BaseModel):
    id: str
    name: str
    version: str
    enabled: bool
    capital_budget: Money
    params: dict[str, Any]


class StrategyList(BaseModel):
    items: list[StrategyOut]


@router.get("/strategies", response_model=StrategyList)
def get_strategies(
    _: AuthPrincipal = Depends(require_viewer), session: Session = Depends(get_session)
) -> StrategyList:
    rows = session.scalars(select(Strategy).order_by(Strategy.id)).all()
    return StrategyList(
        items=[
            StrategyOut(
                id=r.id,
                name=r.name,
                version=r.version,
                enabled=r.enabled,
                capital_budget=r.capital_budget,
                params=dict(r.params_json),
            )
            for r in rows
        ]
    )


# ---- signals -------------------------------------------------------------------------------------


class SignalOut(BaseModel):
    id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    side: str
    generated_at: UtcDatetime
    expires_at: UtcDatetime
    target_notional: Money
    stop_price: Qty | None
    limit_price: Qty | None
    reason_codes: list[str]
    status: str


class SignalPage(BaseModel):
    items: list[SignalOut]
    next_cursor: str | None


@router.get("/signals", response_model=SignalPage)
def get_signals(
    strategy_id: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> SignalPage:
    if status is not None and status not in SIGNAL_STATUSES:
        raise ApiProblem(422, "INVALID_STATUS", "Unknown signal status", detail=status)
    stmt = (
        select(Signal, Instrument.symbol)
        .join(Instrument, Instrument.id == Signal.instrument_id)
        .order_by(Signal.generated_at.desc(), Signal.id.desc())
        .limit(limit + 1)
    )
    if strategy_id is not None:
        stmt = stmt.where(Signal.strategy_id == strategy_id)
    if status is not None:
        stmt = stmt.where(Signal.status == status)
    if cursor:
        stmt = stmt.where(tuple_(Signal.generated_at, Signal.id) < tuple_(*decode_cursor(cursor)))
    rows = session.execute(stmt).all()
    page = rows[:limit]
    return SignalPage(
        items=[
            SignalOut(
                id=str(s.id),
                strategy_id=s.strategy_id,
                strategy_version=s.strategy_version,
                symbol=symbol,
                side=s.side,
                generated_at=s.generated_at,
                expires_at=s.expires_at,
                target_notional=s.target_notional,
                stop_price=s.stop_price,
                limit_price=s.limit_price,
                reason_codes=list(s.reason_codes),
                status=s.status,
            )
            for s, symbol in page
        ],
        next_cursor=(
            encode_cursor(page[-1][0].generated_at, page[-1][0].id) if len(rows) > limit else None
        ),
    )


# ---- performance ---------------------------------------------------------------------------------


class TradeStatsOut(BaseModel):
    trades: int
    wins: int
    win_rate: Qty | None
    expectancy: Qty | None
    avg_win: Qty | None
    avg_loss: Qty | None
    profit_factor: Qty | None


class CostsOut(BaseModel):
    fees: Money
    slippage: Money
    total: Money


class BenchmarkOut(BaseModel):
    symbol: str
    start_price: Qty
    end_price: Qty
    return_pct: Qty


class PerformanceOut(BaseModel):
    scope: str
    curve_kind: str
    start: UtcDatetime
    end: UtcDatetime
    start_equity: Money | None
    end_equity: Money | None
    net_pnl: Money
    return_pct: Qty | None
    max_drawdown_abs: Money
    max_drawdown_pct: Qty
    trades: TradeStatsOut
    fill_ratio: Qty | None
    costs: CostsOut
    benchmark: BenchmarkOut | None
    excess_return_pct: Qty | None
    notes: list[str]


def _performance_out(r: PerformanceReport) -> PerformanceOut:
    return PerformanceOut(
        scope=r.scope,
        curve_kind=r.curve_kind,
        start=r.start,
        end=r.end,
        start_equity=r.start_equity,
        end_equity=r.end_equity,
        net_pnl=r.net_pnl,
        return_pct=r.return_pct,
        max_drawdown_abs=r.max_drawdown_abs,
        max_drawdown_pct=r.max_drawdown_pct,
        trades=TradeStatsOut(**vars(r.trades)),
        fill_ratio=r.fill_ratio,
        costs=CostsOut(fees=r.costs.fees, slippage=r.costs.slippage, total=r.costs.total),
        benchmark=BenchmarkOut(**vars(r.benchmark)) if r.benchmark else None,
        excess_return_pct=r.excess_return_pct,
        notes=list(r.notes),
    )


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


@router.get("/performance", response_model=PerformanceOut)
def get_performance(
    start: date | None = None,
    end: date | None = None,
    strategy_id: str | None = None,
    benchmark: str = Query(default="SPY", min_length=1, max_length=10),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> PerformanceOut:
    """Net of fees and slippage, against a buy-and-hold benchmark. ``end`` is inclusive."""
    if start is not None and end is not None and end < start:
        raise ApiProblem(422, "INVALID_RANGE", "end must not be before start")
    if strategy_id is not None and session.get(Strategy, strategy_id) is None:
        raise ApiProblem(404, "NOT_FOUND", "Unknown strategy", detail=strategy_id)
    primary = (
        select(EquitySnapshot)
        .join(Account, Account.id == EquitySnapshot.account_id)
        .where(Account.name.not_like("replay-%"), Account.name.not_like("backtest%"))
        .limit(1)
    )
    first = session.scalars(primary.order_by(EquitySnapshot.ts)).first()
    last = session.scalars(primary.order_by(EquitySnapshot.ts.desc())).first()
    if first is None or last is None:
        raise ApiProblem(
            503, "STATE_UNAVAILABLE", "Performance unavailable", detail="no equity snapshot yet"
        )
    provider = (
        session.execute(text("SELECT provider FROM market_bars ORDER BY ts DESC LIMIT 1")).scalar()
        or "replay"
    )
    report = load_report(
        session,
        account_id=last.account_id,
        start=_day_start(start) if start else first.ts,
        end=_day_start(end) + timedelta(days=1) if end else last.ts + timedelta(seconds=1),
        strategy_id=strategy_id,
        benchmark_symbol=benchmark.upper(),
        provider=provider,
    )
    return _performance_out(report)


# ---- config proposals ----------------------------------------------------------------------------


class ProposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_version: str = Field(min_length=1, max_length=32)
    parameters: dict[str, Any]
    rationale: str = Field(min_length=1, max_length=2000)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str | None = Field(default=None, max_length=500)


class ProposalOut(BaseModel):
    id: str
    strategy_id: str
    base_version: str
    parameters: dict[str, Any]
    rationale: str
    status: str
    proposed_by: str
    created_at: UtcDatetime
    reviewed_by: str | None
    reviewed_at: UtcDatetime | None
    applied_at: UtcDatetime | None
    applied_version: str | None


class ProposalPage(BaseModel):
    items: list[ProposalOut]
    next_cursor: str | None


def _proposal_out(p: StrategyConfigProposal) -> ProposalOut:
    return ProposalOut(
        id=str(p.id),
        strategy_id=p.strategy_id,
        base_version=p.base_version,
        parameters=dict(p.parameters_json),
        rationale=p.rationale,
        status=p.status,
        proposed_by=p.proposed_by,
        created_at=p.created_at,
        reviewed_by=p.reviewed_by,
        reviewed_at=p.reviewed_at,
        applied_at=p.applied_at,
        applied_version=p.applied_version,
    )


@router.get("/config-proposals", response_model=ProposalPage)
def get_proposals(
    status: str | None = None,
    strategy_id: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> ProposalPage:
    if status is not None and status not in PROPOSAL_STATUSES:
        raise ApiProblem(422, "INVALID_STATUS", "Unknown proposal status", detail=status)
    stmt = (
        select(StrategyConfigProposal)
        .order_by(StrategyConfigProposal.created_at.desc(), StrategyConfigProposal.id.desc())
        .limit(limit + 1)
    )
    if status is not None:
        stmt = stmt.where(StrategyConfigProposal.status == status)
    if strategy_id is not None:
        stmt = stmt.where(StrategyConfigProposal.strategy_id == strategy_id)
    if cursor:
        stmt = stmt.where(
            tuple_(StrategyConfigProposal.created_at, StrategyConfigProposal.id)
            < tuple_(*decode_cursor(cursor))
        )
    rows = session.scalars(stmt).all()
    page = rows[:limit]
    return ProposalPage(
        items=[_proposal_out(p) for p in page],
        next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None,
    )


@router.post("/strategies/{strategy_id}/propose-config", status_code=201)
def post_propose_config(
    strategy_id: str,
    body: ProposeRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthPrincipal = Depends(require_operator),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
    config: AppConfig = Depends(get_config),
) -> JSONResponse:
    """Stores a PENDING_REVIEW proposal. Live parameters are never edited here."""

    def action() -> tuple[int, dict[str, Any]]:
        try:
            row = propose_config(
                session,
                strategy_id=strategy_id,
                base_version=body.base_version,
                parameters=body.parameters,
                rationale=body.rationale,
                proposed_by=principal.name,
                universe={u.symbol for u in config.universe},
                now=clock.now(),
            )
        except ProposalError as exc:
            raise _problem(exc) from exc
        return 201, _proposal_out(row).model_dump(mode="json")

    return run_idempotent(
        session,
        principal=principal,
        route=f"POST /v1/strategies/{strategy_id}/propose-config",
        key=idempotency_key,
        body=body,
        action=action,
        now=clock.now(),
    )


def _review(
    proposal_id: UUID,
    approve: bool,
    body: ReviewRequest,
    idempotency_key: str | None,
    principal: AuthPrincipal,
    session: Session,
    clock: Clock,
) -> JSONResponse:
    def action() -> tuple[int, dict[str, Any]]:
        try:
            row = review_proposal(
                session, proposal_id, approve=approve, reviewer=principal.name, now=clock.now()
            )
        except ProposalError as exc:
            raise _problem(exc) from exc
        return 200, _proposal_out(row).model_dump(mode="json")

    verb = "approve" if approve else "reject"
    return run_idempotent(
        session,
        principal=principal,
        route=f"POST /v1/config-proposals/{proposal_id}/{verb}",
        key=idempotency_key,
        body=body,
        action=action,
        now=clock.now(),
    )


@router.post("/config-proposals/{proposal_id}/approve")
def approve_proposal(
    proposal_id: UUID,
    body: ReviewRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthPrincipal = Depends(require_operator),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    """Marks the proposal APPROVED (by someone other than its proposer). Applied next session."""
    return _review(proposal_id, True, body, idempotency_key, principal, session, clock)


@router.post("/config-proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: UUID,
    body: ReviewRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthPrincipal = Depends(require_operator),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    return _review(proposal_id, False, body, idempotency_key, principal, session, clock)
