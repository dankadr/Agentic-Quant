from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.core.config import load_config
from trading.execution.replay import ReplayResult, replay_session

CFG = load_config(Path("configs"))
DAY = date(2026, 10, 19)
FIXTURES = Path("tests/fixtures/replay")
SESSION = FIXTURES / "2026-10-19"
GAPS = FIXTURES / "gaps"
BASIC = FIXTURES / "scripts" / "basic.yaml"
EXPIRED = FIXTURES / "scripts" / "expired.yaml"


def write_script(tmp_path: Path, *signals: str, name: str = "script.yaml") -> Path:
    path = tmp_path / name
    path.write_text("signals:\n" + "".join(f"  - {s}\n" for s in signals))
    return path


def signal(at: str, symbol: str = "SPY", side: str = "BUY", qty: str = "0.15", limit: str = "560.00",
           strategy: str = "orb_v1", ttl: int = 60) -> str:  # fmt: skip
    return (f"{{at: '2026-10-19T{at}:00Z', symbol: {symbol}, side: {side}, quantity: '{qty}', order_type: LIMIT, "
            f"limit_price: '{limit}', strategy_id: {strategy}, ttl_seconds: {ttl}}}")  # fmt: skip


def run(
    sf: sessionmaker[Session], script: Path | None, fixtures: Path = SESSION, **kw
) -> ReplayResult:  # type: ignore[no-untyped-def]
    return replay_session(
        sf, fixtures_dir=fixtures, session_date=DAY, script=script, config=CFG, **kw
    )


def decision_reasons(sf: sessionmaker[Session]) -> list[list[tuple[str, str]]]:
    with sf() as s:
        rows = (
            s.execute(text("select reasons_json from risk_decisions order by ts, dedupe_key"))
            .scalars()
            .all()
        )
    return [[(r["code"], r["detail"]) for r in reasons] for reasons in rows]


def scalar(sf: sessionmaker[Session], sql: str):  # type: ignore[no-untyped-def]
    with sf() as s:
        return s.execute(text(sql)).scalar_one()


def rows_of(sf: sessionmaker[Session], sql: str):  # type: ignore[no-untyped-def]
    with sf() as s:
        return [tuple(r) for r in s.execute(text(sql)).all()]
