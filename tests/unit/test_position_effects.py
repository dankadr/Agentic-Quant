import pytest

from trading.core import enums
from trading.core.enums import Side


def test_legacy_sides_have_long_effects() -> None:
    assert enums.effective_effect(Side.BUY, None) is enums.PositionEffect.OPEN_LONG
    assert enums.effective_effect(Side.SELL, None) is enums.PositionEffect.CLOSE_LONG


def test_short_entry_and_cover_require_matching_sides() -> None:
    opening = enums.PositionEffect.OPEN_SHORT
    covering = enums.PositionEffect.CLOSE_SHORT
    assert opening.is_entry and opening.is_short and opening.side is Side.SELL
    assert not covering.is_entry and covering.is_short and covering.side is Side.BUY
    with pytest.raises(ValueError):
        enums.effective_effect(Side.SELL, covering)
