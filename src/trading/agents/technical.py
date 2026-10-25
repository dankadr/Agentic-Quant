"""Technical agent: reads recorded walk-forward/sensitivity experiments and may PROPOSE a parameter
revision. Indicators and entry rules stay deterministic code; the model only judges evidence.

A proposal is filed (PENDING_REVIEW, proposed by ``agent:technical``) only when ALL hold:
the walk-forward verdict is SUPPORTED, the base parameters are sensitivity-robust, the proposal
changes only researched parameters, and every proposed value was among the values actually tested.
Nothing here applies a change: a human still has to approve it, and it only takes effect at the
next session start.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.llm import LlmError, LlmInvalidOutput
from trading.agents.macro import JsonLlm
from trading.core.config import AppConfig
from trading.core.hashing import canonical_json, content_hash
from trading.core.time import isoformat_z
from trading.db.models.research import AgentRun, Experiment, Strategy, StrategyConfigProposal
from trading.monitoring.logging import get_logger
from trading.quant.proposals import ProposalError, propose_config

_log = get_logger(__name__)
AGENT_NAME = "technical"
PROMPT_VERSION = "technical-v1"
PROPOSER = "agent:technical"

SYSTEM_PROMPT = """You review quantitative research evidence for one trading strategy in a PAPER
trading system. You receive the strategy's current parameters, a walk-forward result (parameters
chosen on training windows, scored on later unseen windows) and a sensitivity result (does the
current setting survive nearby values). All numbers come from deterministic code.

Be sceptical: small samples, many hypotheses tested and in-sample/out-of-sample degradation all
argue against change. Only suggest changing parameters that appear in the tested space and only to
values that were tested. Decimal values must be JSON STRINGS (e.g. "2.0"), integers plain numbers.
If the evidence does not clearly support a change, return proposal null. A human reviews anything
you suggest; you cannot apply it.

Reply with ONLY this JSON object:
{"assessment": "<=1000 chars",
 "proposal": null | {"parameters": {"<name>": "<value>"}, "rationale": "<=500 chars"},
 "notes": ["..."]}"""


ParamValue = StrictStr | StrictInt | StrictBool | list[StrictStr]


class ProposalDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    parameters: dict[str, StrictStr | StrictInt | StrictBool | list[StrictStr]] = Field(
        min_length=1, max_length=6
    )
    rationale: str = Field(min_length=1, max_length=500)


class TechnicalOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    assessment: str = Field(min_length=1, max_length=1000)
    proposal: ProposalDraft | None = None
    notes: list[str] = Field(default_factory=list, max_length=5)


def _latest(session: Session, strategy_id: str, kind: str) -> Experiment | None:
    return session.scalars(
        select(Experiment)
        .where(Experiment.strategy_id == strategy_id, Experiment.kind == kind)
        .order_by(Experiment.created_at.desc())
        .limit(1)
    ).first()


def _same(a: Any, b: Any) -> bool:
    """Typed equality: "2.0" is not 2.0 and "60" is not 60."""
    return canonical_json(a) == canonical_json(b)


def _gate(
    out: TechnicalOutput,
    wf: Experiment,
    sens: Experiment,
    current: dict[str, Any],
    version: str,
) -> str | None:
    """None if the proposal may be filed, else the reason it may not."""
    if out.proposal is None:
        return "no_proposal"
    if wf.status != "COMPLETED" or wf.metrics_json.get("verdict") != "SUPPORTED":
        return "walk_forward_not_supported"
    if sens.status != "COMPLETED" or not sens.metrics_json.get("robust"):
        return "not_robust"
    # The evidence must be about the configuration that is live now.
    for experiment in (wf, sens):
        seen_version = experiment.params_json.get("strategy_version")
        if seen_version is not None and seen_version != version:
            return "evidence_for_different_config"
        base: dict[str, Any] = experiment.params_json.get("base", {})
        if any(not _same(value, current.get(key)) for key, value in base.items()):
            return "evidence_for_different_config"
    space: dict[str, list[Any]] = wf.params_json.get("space", {})
    changed = False
    for key, value in out.proposal.parameters.items():
        if key not in space:
            return "parameter_not_researched"
        if not any(_same(value, tested) for tested in space[key]):
            return "value_outside_tested_grid"
        changed = changed or not _same(value, current.get(key))
    return None if changed else "no_change"


def run_technical_agent(
    session_factory: sessionmaker[Session],
    llm: JsonLlm,
    *,
    config: AppConfig,
    strategy_id: str,
    now: datetime,
) -> str:
    run_id: UUID = uuid4()
    model = config.agents.llm.model
    with session_factory.begin() as session:
        session.add(
            AgentRun(
                id=run_id, agent_name=AGENT_NAME, model=model, prompt_version=PROMPT_VERSION,
                started_at=now, status="RUNNING",
            )
        )  # fmt: skip

    def finish(status: str, result: dict[str, object], used_model: str = model) -> str:
        with session_factory.begin() as session:
            row = session.get(AgentRun, run_id)
            assert row is not None  # noqa: S101 - inserted above
            row.status = status
            row.completed_at = now
            row.model = used_model
            row.cost_usd = Decimal(0)
            row.result_json = json.loads(canonical_json(result))
        return status

    with session_factory() as session:
        strategy = session.get(Strategy, strategy_id)
        wf = _latest(session, strategy_id, "WALK_FORWARD")
        sens = _latest(session, strategy_id, "SENSITIVITY")
        if strategy is None or wf is None or sens is None:
            return finish("FAILED", {"error": "no_experiments", "strategy_id": strategy_id})
        current_params: dict[str, Any] = dict(strategy.params_json)
        current = {"version": strategy.version, "params": current_params}
        payload = {
            "strategy_id": strategy_id,
            "current": current,
            "walk_forward": {
                **wf.metrics_json,
                "space": wf.params_json.get("space"),
                "data_range": wf.data_range_json,
            },
            "sensitivity": {**sens.metrics_json, "neighbors": sens.params_json.get("neighbors")},
        }
        base_version = strategy.version
        wf_copy, sens_copy = wf, sens
        session.expunge_all()
    user = canonical_json(payload)
    digest = content_hash(user)
    try:
        result = llm.complete_json(SYSTEM_PROMPT, user, TechnicalOutput)
    except LlmInvalidOutput as exc:
        return finish("REJECTED_INVALID", {"error": str(exc), "input_hash": digest})
    except LlmError as exc:
        _log.warning("technical_llm_failed", error=str(exc))
        return finish("FAILED", {"error": str(exc), "input_hash": digest})
    out = result.value
    reason = _gate(out, wf_copy, sens_copy, current_params, base_version)
    proposal_id: str | None = None
    if reason is None:
        with session_factory() as session:
            others = session.scalars(
                select(StrategyConfigProposal.id).where(
                    StrategyConfigProposal.strategy_id == strategy_id,
                    StrategyConfigProposal.status == "PENDING_REVIEW",
                    StrategyConfigProposal.proposed_by != PROPOSER,
                )
            ).first()
        if others is not None:
            reason = "human_proposal_pending"
    if reason is None and out.proposal is not None:
        try:
            with session_factory.begin() as session:
                row = propose_config(
                    session,
                    strategy_id=strategy_id,
                    base_version=base_version,
                    parameters=dict(out.proposal.parameters),
                    rationale=out.proposal.rationale,
                    proposed_by=PROPOSER,
                    universe={u.symbol for u in config.universe},
                    now=now,
                )
                proposal_id = str(row.id)
        except ProposalError as exc:
            reason = f"proposal_refused: {exc}"
    return finish(
        "COMPLETED",
        {
            "agent_name": AGENT_NAME,
            "run_id": str(run_id),
            "model_id": result.model,
            "prompt_version": PROMPT_VERSION,
            "created_at": isoformat_z(now),
            "strategy_id": strategy_id,
            "assessment": out.assessment,
            "notes": out.notes,
            "proposal_filed": proposal_id is not None,
            "proposal_id": proposal_id,
            "reason": reason,
            "input_hash": digest,
        },
        used_model=result.model,
    )
