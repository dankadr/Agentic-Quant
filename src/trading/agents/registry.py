"""What each agent is for, what it may touch, and which of its settings the dashboard may edit.

Static descriptions only: the dashboard shows these as "Rules this agent follows". The editable
fields are exactly the agent's config model fields (a test pins the two together); everything in
``LOCKED_SCOPES`` can never be changed from the panel.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import ModuleType

from trading.agents import consensus, evaluator, fundamental, macro, technical
from trading.core.config import AgentsConfig

LOCKED_SCOPES: tuple[str, ...] = (
    "risk",
    "paper_broker",
    "system",
    "universe",
    "strategies",
    "llm",
)

_NEVER = (
    "place or cancel orders",
    "change risk limits or switch mode",
    "apply any change by itself",
)


@dataclass(frozen=True)
class AgentInfo:
    name: str
    job: str
    can_affect: tuple[str, ...]
    cannot: tuple[str, ...]
    uses_llm: bool
    prompt_version: str
    prompt_text: str | None
    valid_output: str
    on_failure: str
    editable: tuple[str, ...]


def _fields(name: str) -> tuple[str, ...]:
    return tuple(type(getattr(AgentsConfig(), name)).model_fields)


def _prompt(module: ModuleType) -> str | None:
    text = getattr(module, "SYSTEM_PROMPT", None)
    return text if isinstance(text, str) else None


AGENT_REGISTRY: Mapping[str, AgentInfo] = {
    "macro": AgentInfo(
        name="macro",
        job="Reads index bars (SPY, QQQ, IWM) and breadth, labels the market regime and outputs "
        "a size multiplier for new entries.",
        can_affect=("new-entry size of the dependent strategies (orb_v1, vwap_v1)",),
        cannot=("touch exits", *_NEVER),
        uses_llm=True,
        prompt_version=macro.PROMPT_VERSION,
        prompt_text=_prompt(macro),
        valid_output="regime label, multiplier between 0 and 1, confidence, evidence and "
        "limitations lists",
        on_failure="A missing or stale view is ignored (multiplier 1) unless `required` is true; "
        "it is recorded either way.",
        editable=_fields("macro"),
    ),
    "fundamental": AgentInfo(
        name="fundamental",
        job="Reads recent Alpaca news headlines and proposes a watchlist and catalysts.",
        can_affect=("the research watchlist shown to the owner",),
        cannot=("trade on headlines", *_NEVER),
        uses_llm=True,
        prompt_version=fundamental.PROMPT_VERSION,
        prompt_text=_prompt(fundamental),
        valid_output="catalysts with kind, symbol, date and source ids; headlines are untrusted "
        "text",
        on_failure="The run is recorded as REJECTED_INVALID or FAILED and nothing else changes.",
        editable=_fields("fundamental"),
    ),
    "evaluator": AgentInfo(
        name="evaluator",
        job="Reviews one finished session and writes observations and suggestions.",
        can_affect=("the end-of-day review text",),
        cannot=_NEVER,
        uses_llm=True,
        prompt_version=evaluator.PROMPT_VERSION,
        prompt_text=_prompt(evaluator),
        valid_output="a structured review whose lists respect the schema's length limits",
        on_failure="A reply that breaks the schema is stored as REJECTED_INVALID with the error.",
        editable=_fields("evaluator"),
    ),
    "consensus": AgentInfo(
        name="consensus",
        job="Replays each day's entries against the macro view and records what would have "
        "happened with a veto (a counterfactual).",
        can_affect=("nothing: shadow mode only",),
        cannot=("veto or resize real orders", *_NEVER),
        uses_llm=False,
        prompt_version=consensus.PROMPT_VERSION,
        prompt_text=None,
        valid_output="agreement counts and counterfactual P&L, computed deterministically",
        on_failure="The run is recorded as FAILED.",
        editable=_fields("consensus"),
    ),
    "technical": AgentInfo(
        name="technical",
        job="Reads recorded walk-forward and sensitivity experiments and may propose a strategy "
        "parameter revision.",
        can_affect=("strategy parameter proposals awaiting human approval",),
        cannot=("apply a proposal", *_NEVER),
        uses_llm=True,
        prompt_version=technical.PROMPT_VERSION,
        prompt_text=_prompt(technical),
        valid_output="a proposal only if the walk-forward verdict is SUPPORTED, the base is "
        "sensitivity-robust, and every value was actually tested",
        on_failure="No proposal is filed; the reason is recorded.",
        editable=_fields("technical"),
    ),
}
