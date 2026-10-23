from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from tests.factories import T0
from tests.integration.portfolio.helpers import open_account
from tests.unit.risk.helpers import portfolio
from trading.control.state import RiskState, set_risk_state
from trading.portfolio.valuation import PortfolioState

D = Decimal
DAY = date(2026, 10, 19)
NEXT = date(2026, 10, 20)


def pf_for(account_id: UUID, equity: str, **kw: object) -> PortfolioState:
    return replace(portfolio(equity=equity, **kw), account_id=account_id)  # type: ignore[arg-type]


def make_normal(db: Session) -> UUID:
    """An account with a NORMAL risk state, as after a clean reconciliation."""
    account_id = open_account(db)
    set_risk_state(db, risk_state=RiskState.NORMAL, reason="test", actor="t", now=T0)
    return account_id
