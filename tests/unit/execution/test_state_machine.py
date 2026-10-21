import pytest

from trading.execution.state_machine import (
    ALLOWED,
    OPEN_AT_BROKER,
    TERMINAL,
    IllegalTransition,
    OrderStatus,
    assert_transition,
    can_transition,
)

S = OrderStatus
EXPECTED: dict[OrderStatus, set[OrderStatus]] = {
    S.CREATED: {S.RISK_APPROVED, S.REJECTED},
    S.RISK_APPROVED: {S.SUBMITTING, S.REJECTED, S.CANCELED},
    S.SUBMITTING: {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.REJECTED, S.UNKNOWN},
    S.ACKNOWLEDGED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED, S.UNKNOWN},
    S.PARTIALLY_FILLED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED, S.UNKNOWN},
    S.UNKNOWN: {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED},
    S.FILLED: set(),
    S.REJECTED: set(),
    S.CANCELED: set(),
    S.EXPIRED: set(),
}


def test_expected_table_covers_every_status() -> None:
    assert set(EXPECTED) == set(OrderStatus)
    assert len(OrderStatus) == 10


@pytest.mark.parametrize("a", list(OrderStatus))
@pytest.mark.parametrize("b", list(OrderStatus))
def test_transition_table(a: OrderStatus, b: OrderStatus) -> None:
    assert can_transition(a, b) is (b in EXPECTED[a])
    if b in EXPECTED[a]:
        assert_transition(a, b)
    else:
        with pytest.raises(IllegalTransition):
            assert_transition(a, b)


def test_allowed_mapping_matches_expected() -> None:
    assert {k: set(v) for k, v in ALLOWED.items()} == EXPECTED


def test_terminal_states_have_no_exits() -> None:
    assert {S.FILLED, S.REJECTED, S.CANCELED, S.EXPIRED} == TERMINAL
    assert all(not ALLOWED[t] for t in TERMINAL)


def test_open_at_broker_states() -> None:
    assert {S.ACKNOWLEDGED, S.PARTIALLY_FILLED} == OPEN_AT_BROKER


def test_illegal_transition_message_names_both_states() -> None:
    with pytest.raises(IllegalTransition, match=r"FILLED.*CREATED"):
        assert_transition(S.FILLED, S.CREATED)


def test_rejected_order_can_never_become_filled() -> None:
    assert not can_transition(S.REJECTED, S.FILLED)
    assert not can_transition(S.CREATED, S.FILLED)
