"""CSV-backed provider for deterministic replay. Timestamps must carry a UTC offset."""

import csv
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

from trading.core.time import ensure_utc
from trading.data.models import Bar, CorporateActionRecord, Quote
from trading.data.provider import DataFormatError

_BAR_COLUMNS = ("symbol", "ts", "open", "high", "low", "close", "volume")
_QUOTE_COLUMNS = ("symbol", "ts", "bid", "ask")
_ACTION_COLUMNS = ("symbol", "action_type", "ex_date", "announced_at")


class CsvReplayProvider:
    def __init__(self, root: Path, name: str = "replay") -> None:
        self._root = root
        self.name = name

    def _rows(self, filename: str, required: Sequence[str]) -> Iterator[tuple[str, dict[str, str]]]:
        path = self._root / filename
        if not path.exists():
            return
        header: list[str] | None = None
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = next(csv.reader([line]))
            where = f"{filename}:{number}"
            if header is None:
                header = [f.strip() for f in fields]
                for column in required:
                    if column not in header:
                        raise DataFormatError(f"{filename}: missing column {column!r}")
                continue
            if len(fields) != len(header):
                raise DataFormatError(f"{where}: expected {len(header)} fields, got {len(fields)}")
            yield where, dict(zip(header, (f.strip() for f in fields), strict=True))

    @staticmethod
    def _ts(value: str, where: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise DataFormatError(f"{where}: invalid timestamp {value!r}") from exc
        if parsed.tzinfo is None:
            raise DataFormatError(f"{where}: naive timestamp {value!r} (UTC offset required)")
        return ensure_utc(parsed)

    @staticmethod
    def _dec(value: str, where: str, column: str) -> Decimal:
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise DataFormatError(f"{where}: invalid number {value!r} in {column}") from exc

    def _opt_dec(self, row: dict[str, str], column: str, where: str) -> Decimal | None:
        value = row.get(column, "")
        return self._dec(value, where, column) if value else None

    def fetch_bars(self, symbols: Sequence[str], start: datetime, end: datetime) -> Iterator[Bar]:
        wanted = set(symbols)
        for where, row in self._rows("bars.csv", _BAR_COLUMNS):
            if row["symbol"] not in wanted:
                continue
            ts = self._ts(row["ts"], where)
            if not start <= ts < end:
                continue
            yield Bar(
                symbol=row["symbol"],
                interval="1m",
                ts=ts,
                open=self._dec(row["open"], where, "open"),
                high=self._dec(row["high"], where, "high"),
                low=self._dec(row["low"], where, "low"),
                close=self._dec(row["close"], where, "close"),
                volume=self._dec(row["volume"], where, "volume"),
                provider=self.name,
                source_seq=row.get("source_seq") or None,
            )

    def fetch_quotes(
        self, symbols: Sequence[str], start: datetime, end: datetime
    ) -> Iterator[Quote]:
        wanted = set(symbols)
        for where, row in self._rows("quotes.csv", _QUOTE_COLUMNS):
            if row["symbol"] not in wanted:
                continue
            ts = self._ts(row["ts"], where)
            if not start <= ts < end:
                continue
            yield Quote(
                symbol=row["symbol"],
                ts=ts,
                bid=self._dec(row["bid"], where, "bid"),
                ask=self._dec(row["ask"], where, "ask"),
                bid_size=self._opt_dec(row, "bid_size", where),
                ask_size=self._opt_dec(row, "ask_size", where),
                provider=self.name,
            )

    def fetch_corporate_actions(
        self, symbols: Sequence[str], start: date, end: date
    ) -> Iterator[CorporateActionRecord]:
        wanted = set(symbols)
        for where, row in self._rows("corporate_actions.csv", _ACTION_COLUMNS):
            if row["symbol"] not in wanted:
                continue
            try:
                ex_date = date.fromisoformat(row["ex_date"])
            except ValueError as exc:
                raise DataFormatError(f"{where}: invalid ex_date {row['ex_date']!r}") from exc
            if not start <= ex_date <= end:
                continue
            try:
                yield CorporateActionRecord(
                    symbol=row["symbol"],
                    action_type=row["action_type"],
                    ex_date=ex_date,
                    ratio_from=self._opt_dec(row, "ratio_from", where),
                    ratio_to=self._opt_dec(row, "ratio_to", where),
                    cash_amount=self._opt_dec(row, "cash_amount", where),
                    provider=self.name,
                    announced_at=self._ts(row["announced_at"], where),
                )
            except ValidationError as exc:
                raise DataFormatError(f"{where}: {exc.errors()[0]['msg']}") from exc
