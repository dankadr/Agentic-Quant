"""Records that an agent run did not happen, and why.

A skipped run is still an event an operator must be able to see. Silently returning would make a
missing research input indistinguishable from a healthy agent that had nothing to say.
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from trading.db.models.research import AgentRun


def record_skip(
    session_factory: sessionmaker[Session],
    *,
    agent_name: str,
    model: str,
    prompt_version: str,
    reason: str,
    now: datetime,
) -> None:
    with session_factory.begin() as session:
        session.add(
            AgentRun(
                id=uuid4(),
                agent_name=agent_name,
                model=model,
                prompt_version=prompt_version,
                started_at=now,
                completed_at=now,
                status="SKIPPED",
                result_json={"skip_reason": reason},
                cost_usd=None,
                available_at=None,  # a skipped run produced no output, so nothing is available
                attempt=1,
            )
        )
