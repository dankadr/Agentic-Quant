"""Session state and persistent halts.

Halts live in ``system_state`` (not memory), so they survive restarts. A daily-loss halt clears
only when a NEW session starts; a drawdown halt clears only by an explicit, audited operator action.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trading.control.state import RiskState, SystemState, get_system_state, set_risk_state
from trading.core.config import RiskPolicy
from trading.core.errors import TradingError
from trading.db.models.portfolio import Account, EquitySnapshot, TradingSession
from trading.jobs.outbox import emit_event
from trading.monitoring.audit import record_audit
from trading.portfolio.valuation import PortfolioState


class NotInHaltError(TradingError):
    """The operation requires the HALTED_DRAWDOWN risk state."""


def start_session(
    session: Session, *, account_id: UUID, session_date: date, start_equity: Decimal, now: datetime
) -> bool:
    """Open the trading session row. Idempotent: a restart on the same day changes nothing.

    Returns True only when a NEW session row was opened.

    Only a genuinely NEW session clears a daily-loss halt (decision C6).
    """
    inserted = session.execute(
        insert(TradingSession)
        .values(account_id=account_id, session_date=session_date, start_equity=start_equity)
        .on_conflict_do_nothing()
        .returning(TradingSession.session_date)
    ).first()
    if inserted is None:
        return False
    if get_system_state(session).risk_state is RiskState.HALTED_DAILY_LOSS:
        set_risk_state(
            session, risk_state=RiskState.NORMAL, reason="new_session", actor="system", now=now
        )
    return True


def evaluate_halts(
    session: Session,
    *,
    account_id: UUID,
    state: PortfolioState,
    session_date: date,
    policy: RiskPolicy,
    now: datetime,
) -> RiskState:
    """Update peak equity and trigger daily-loss / drawdown halts. Returns the resulting state.

    A breach at exactly the limit halts (decision C4). Halts never auto-recover within a session
    and never override UNKNOWN / RECONCILIATION_REQUIRED.
    """
    account = session.get(Account, account_id, with_for_update=True)
    if account is None:
        raise LookupError(f"account {account_id} not found")
    equity = state.equity
    account.peak_equity = max(account.peak_equity, equity)
    peak = account.peak_equity
    current = get_system_state(session).risk_state
    if current in (RiskState.UNKNOWN, RiskState.RECONCILIATION_REQUIRED):
        return current
    row = session.scalars(
        select(TradingSession)
        .where(TradingSession.account_id == account_id, TradingSession.session_date == session_date)
        .with_for_update()
    ).one_or_none()
    if row is None:
        if current in (RiskState.NORMAL, RiskState.HALTED_DAILY_LOSS):
            set_risk_state(
                session,
                risk_state=RiskState.UNKNOWN,
                reason="no_session_row",
                actor="system",
                now=now,
            )
            return RiskState.UNKNOWN
        return current

    if current is not RiskState.HALTED_DRAWDOWN and peak - equity >= policy.max_drawdown_pct * peak:
        account.drawdown_halted_at = account.drawdown_halted_at or now  # survives reconciliation
        _halt(session, RiskState.HALTED_DRAWDOWN, "DRAWDOWN", equity, row.start_equity, peak, now)
        return RiskState.HALTED_DRAWDOWN
    if (
        current is RiskState.NORMAL
        and row.start_equity - equity >= policy.max_daily_loss_pct * row.start_equity
    ):
        row.daily_loss_halted_at = row.daily_loss_halted_at or now
        _halt(
            session, RiskState.HALTED_DAILY_LOSS, "DAILY_LOSS", equity, row.start_equity, peak, now
        )
        return RiskState.HALTED_DAILY_LOSS
    return current


def _halt(
    session: Session,
    target: RiskState,
    kind: str,
    equity: Decimal,
    start_equity: Decimal,
    peak: Decimal,
    now: datetime,
) -> None:
    set_risk_state(
        session,
        risk_state=target,
        reason=f"{kind.lower()}: equity {equity} start {start_equity} peak {peak}",
        actor="system",
        now=now,
    )
    emit_event(
        session,
        event_type="risk_alert",
        payload={"kind": kind, "equity": equity, "start_equity": start_equity, "peak_equity": peak},
        now=now,
    )


def restore_halt_state(session: Session, account_id: UUID) -> RiskState | None:
    """The halt that must still be in force, from PERSISTED markers (never from equity alone).

    Reconciliation and unknown-state handling temporarily replace a HALTED_* state; when they
    clear, the halt must come back. A drawdown halt is lifted only by ``clear_drawdown_halt``;
    a daily-loss halt only by the start of a new session.
    """
    account = session.get(Account, account_id)
    if account is not None and account.drawdown_halted_at is not None:
        return RiskState.HALTED_DRAWDOWN
    latest = session.scalars(
        select(TradingSession)
        .where(TradingSession.account_id == account_id)
        .order_by(TradingSession.session_date.desc())
        .limit(1)
    ).first()
    if latest is not None and latest.daily_loss_halted_at is not None:
        return RiskState.HALTED_DAILY_LOSS
    return None


def clear_drawdown_halt(
    session: Session, *, actor: str, reason: str, request_id: str | None, now: datetime
) -> SystemState:
    """Operator action: leave HALTED_DRAWDOWN and reset peak equity to the latest snapshot."""
    if get_system_state(session, for_update=True).risk_state is not RiskState.HALTED_DRAWDOWN:
        raise NotInHaltError("risk state is not HALTED_DRAWDOWN")
    session.execute(update(Account).values(drawdown_halted_at=None))
    latest = session.scalars(
        select(EquitySnapshot).order_by(EquitySnapshot.ts.desc()).limit(1)
    ).first()
    if latest is not None:
        account = session.get(Account, latest.account_id, with_for_update=True)
        if account is not None:
            account.peak_equity = latest.equity
    result = set_risk_state(
        session,
        risk_state=RiskState.NORMAL,
        reason=f"drawdown halt cleared by {actor}",
        actor=actor,
        now=now,
    )
    record_audit(
        session,
        actor=actor,
        action="risk.clear_drawdown_halt",
        target="system_state",
        request_id=request_id,
        before={"risk_state": RiskState.HALTED_DRAWDOWN},
        after={"risk_state": RiskState.NORMAL},
        result="ok",
        now=now,
        details={"reason": reason},
    )
    return result
