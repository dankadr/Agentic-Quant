from trading.agents import consensus, evaluator, fundamental, macro, technical
from trading.agents.registry import AGENT_REGISTRY, LOCKED_SCOPES
from trading.core.config import AgentsConfig

_MODULES = {
    "macro": macro,
    "fundamental": fundamental,
    "evaluator": evaluator,
    "consensus": consensus,
    "technical": technical,
}


def test_registry_covers_every_agent_config_field() -> None:
    assert set(AGENT_REGISTRY) == set(_MODULES)
    for name, info in AGENT_REGISTRY.items():
        fields = set(type(getattr(AgentsConfig(), name)).model_fields)
        assert set(info.editable) == fields, name


def test_prompt_versions_match_modules() -> None:
    for name, info in AGENT_REGISTRY.items():
        assert info.prompt_version == _MODULES[name].PROMPT_VERSION


def test_consensus_has_no_llm() -> None:
    info = AGENT_REGISTRY["consensus"]
    assert info.uses_llm is False
    assert info.prompt_text is None


def test_llm_agents_expose_their_prompt_text() -> None:
    assert AGENT_REGISTRY["macro"].prompt_text == macro.SYSTEM_PROMPT
    assert AGENT_REGISTRY["evaluator"].prompt_text == evaluator.SYSTEM_PROMPT


def test_risk_and_trading_scopes_are_locked() -> None:
    assert {"risk", "paper_broker", "system", "universe", "strategies", "llm"} <= set(LOCKED_SCOPES)
    assert not any(a in LOCKED_SCOPES for a in AGENT_REGISTRY)
