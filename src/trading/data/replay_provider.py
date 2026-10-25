"""CSV-backed provider for deterministic replay. Timestamps must carry a UTC offset."""

import csv
from bisect import bisect_left
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
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


@dataclass
class _Table:
    """A CSV file parsed once. ``ts`` is None (with ``error`` set) for a bad timestamp; such a row
    only raises if its symbol is actually requested, exactly as an uncached read would."""

    rows: list[tuple[str, datetime | None, str | None, dict[str, str], str]]
    times: list[datetime] | None  # set only when every ts parsed and the file is in time order


class CsvReplayProvider:
    def __init__(self, root: Path, name: str = "replay") -> None:
        self._root = root
        self.name = name
        self._cache: dict[str, tuple[tuple[int, int], _Table]] = {}

    def _table(self, filename: str, required: Sequence[str]) -> _Table:
        path = self._root / filename
        if not path.exists():
            return _Table([], [])
        stat = path.stat()
        stamp = (stat.st_size, stat.st_mtime_ns)
        cached = self._cache.get(filename)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        rows: list[tuple[str, datetime | None, str | None, dict[str, str], str]] = []
        ordered = True
        previous: datetime | None = None
        for where, row in self._rows(filename, required):
            try:
                ts: datetime | None = self._ts(row["ts"], where)
                error = None
            except DataFormatError as exc:
                ts, error = None, str(exc)
            if ts is None or (previous is not None and ts < previous):
                ordered = False
            previous = ts if ts is not None else previous
            rows.append((row["symbol"], ts, error, row, where))
        table = _Table(rows, [r[1] for r in rows] if ordered else None)  # type: ignore[misc]
        self._cache[filename] = (stamp, table)
        return table

    def _window(
        self,
        filename: str,
        required: Sequence[str],
        wanted: set[str],
        start: datetime,
        end: datetime,
    ) -> Iterator[tuple[datetime, dict[str, str], str]]:
        table = self._table(filename, required)
        lo, hi = 0, len(table.rows)
        if table.times is not None:  # time-ordered file: jump straight to the window
            lo, hi = bisect_left(table.times, start), bisect_left(table.times, end)
        for symbol, ts, error, row, where in table.rows[lo:hi]:
            if symbol not in wanted:
                continue
            if error is not None:
                raise DataFormatError(error)
            if ts is None or not start <= ts < end:
                continue
            yield ts, row, where

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
        for ts, row, where in self._window("bars.csv", _BAR_COLUMNS, set(symbols), start, end):
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
        for ts, row, where in self._window("quotes.csv", _QUOTE_COLUMNS, set(symbols), start, end):
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
