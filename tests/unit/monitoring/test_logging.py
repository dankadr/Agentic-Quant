import json
from collections.abc import Iterator

import pytest

from trading.monitoring.logging import (
    bind_request_id,
    clear_context,
    configure_logging,
    get_logger,
    redact,
)


@pytest.fixture(autouse=True)
def _clean_context() -> Iterator[None]:
    clear_context()
    yield
    clear_context()


def test_redacts_sensitive_keys() -> None:
    out = redact(
        None,
        "info",
        {
            "event": "x",
            "password": "p",
            "api_key": "k",
            "db_password_file": "/run/secrets/x",
            "nested": {"token": "t"},
        },
    )
    assert out["password"] == out["api_key"] == out["nested"]["token"] == "***"
    assert out["db_password_file"] == "/run/secrets/x"


def test_redacts_bearer_in_strings() -> None:
    out = redact(None, "info", {"event": "Authorization: Bearer trk_abc"})
    assert "trk_abc" not in out["event"]


def test_redacts_api_key_pattern_in_nested_lists() -> None:
    out = redact(None, "info", {"event": "x", "items": ["use trk_AbC-123_x now"]})
    assert "trk_AbC" not in out["items"][0]


def test_json_output_includes_request_id_and_redacts(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json_output=True)
    bind_request_id("r1")
    get_logger("test").info("hello", password="hunter2")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["request_id"] == "r1"
    assert record["event"] == "hello"
    assert record["password"] == "***"
    assert record["timestamp"].endswith("Z")
    assert "hunter2" not in line


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING", json_output=True)
    get_logger("test").info("quiet")
    assert capsys.readouterr().out == ""


class _Holder:
    def __repr__(self) -> str:
        return "Holder(key=trk_leakme123)"


def test_redacts_secret_inside_arbitrary_object() -> None:
    out = redact(
        None, "info", {"event": "x", "obj": _Holder(), "err": ValueError("Bearer abc.def")}
    )
    assert "trk_leakme123" not in str(out["obj"])
    assert "abc.def" not in str(out["err"])


def test_redacts_secret_inside_set_and_nested_tuple() -> None:
    out = redact(None, "info", {"event": "x", "s": {"trk_inset1"}, "t": (("trk_deep2",),)})
    assert "trk_inset1" not in str(out["s"])
    assert "trk_deep2" not in str(out["t"])


def test_non_string_keys_do_not_crash_and_are_scrubbed() -> None:
    out = redact(None, "info", {"event": "x", "m": {1: "ok", ("password",): "p"}})
    assert set(out["m"]) == {"1", "('password',)"}
    assert out["m"]["1"] == "ok"


def test_primitives_pass_through() -> None:
    out = redact(None, "info", {"event": "x", "n": 3, "f": 1.5, "b": True, "none": None})
    assert (out["n"], out["f"], out["b"], out["none"]) == (3, 1.5, True, None)


def test_typed_scalars_survive_redaction_unchanged() -> None:
    from datetime import UTC, date, datetime
    from decimal import Decimal
    from uuid import UUID

    values = {
        "d": Decimal("0.10000000"),
        "t": datetime(2026, 10, 19, 13, 30, tzinfo=UTC),
        "day": date(2026, 10, 19),
        "u": UUID(int=7),
    }
    out = redact(None, "info", {"event": "x", **values})
    for key, value in values.items():
        assert out[key] == value
        assert type(out[key]) is type(value)


def test_json_logs_render_typed_scalars_as_text(capsys: pytest.CaptureFixture[str]) -> None:
    from decimal import Decimal

    configure_logging("INFO", json_output=True)
    get_logger("test").info("priced", price=Decimal("1.50"))
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["price"] == "1.50"


def test_logging_survives_a_stream_closed_after_configuration() -> None:
    """Order-dependent flake: configure under a captured stream, close it, log from another test."""
    import contextlib
    import io

    first = io.StringIO()
    with contextlib.redirect_stdout(first):
        configure_logging("INFO", json_output=True)
    first.close()
    get_logger("test").info("still-works")  # must not raise "I/O operation on closed file"
