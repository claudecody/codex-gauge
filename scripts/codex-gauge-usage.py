#!/usr/bin/env python3
"""Summarize local Codex token usage with configurable pricing and credit rates."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_INPUT_RATE = 5.00
DEFAULT_CACHED_INPUT_RATE = 0.50
DEFAULT_OUTPUT_RATE = 30.00
DEFAULT_CREDIT_MODEL = "gpt-5.5"

CREDIT_RATE_CARDS = {
    "gpt-5.5": {
        "input": 125.0,
        "cached_input": 12.50,
        "output": 750.0,
    },
    "gpt-5.5-cyber": {
        "input": 500.0,
        "cached_input": 50.0,
        "output": 3000.0,
    },
    "gpt-5.4": {
        "input": 62.50,
        "cached_input": 6.250,
        "output": 375.0,
    },
    "gpt-5.4-mini": {
        "input": 18.75,
        "cached_input": 1.875,
        "output": 113.0,
    },
    "gpt-5.3-codex": {
        "input": 43.75,
        "cached_input": 4.375,
        "output": 350.0,
    },
    "gpt-5.2": {
        "input": 43.75,
        "cached_input": 4.375,
        "output": 350.0,
    },
}

FAST_MODE_MULTIPLIERS = {
    "gpt-5.5": 2.5,
    "gpt-5.4": 2.0,
}

USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


@dataclass
class Thread:
    id: str
    rollout_path: Path
    created_at: datetime
    updated_at: datetime
    source: str
    model: str
    title: str
    tokens_used: int


@dataclass
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    unknown_tokens: int = 0
    sessions: set[str] = field(default_factory=set)

    def add_dict(self, values: dict[str, int]) -> None:
        self.input_tokens += values.get("input_tokens", 0)
        self.cached_input_tokens += values.get("cached_input_tokens", 0)
        self.output_tokens += values.get("output_tokens", 0)
        self.reasoning_output_tokens += values.get("reasoning_output_tokens", 0)
        self.total_tokens += values.get("total_tokens", 0)
        self.unknown_tokens += values.get("unknown_tokens", 0)

    @property
    def uncached_input_tokens(self) -> int:
        return max(self.input_tokens - self.cached_input_tokens, 0)

    @property
    def visible_output_tokens(self) -> int:
        return max(self.output_tokens - self.reasoning_output_tokens, 0)

    def hypothetical_cost(
        self,
        input_rate: float,
        cached_input_rate: float,
        output_rate: float,
        unknown_rate: float,
    ) -> float:
        return (
            self.uncached_input_tokens / 1_000_000 * input_rate
            + self.cached_input_tokens / 1_000_000 * cached_input_rate
            + self.output_tokens / 1_000_000 * output_rate
            + self.unknown_tokens / 1_000_000 * unknown_rate
        )

    def estimated_credits(
        self,
        input_rate: float,
        cached_input_rate: float,
        output_rate: float,
        unknown_rate: float,
        multiplier: float,
    ) -> float:
        return (
            self.uncached_input_tokens / 1_000_000 * input_rate
            + self.cached_input_tokens / 1_000_000 * cached_input_rate
            + self.output_tokens / 1_000_000 * output_rate
            + self.unknown_tokens / 1_000_000 * unknown_rate
        ) * multiplier


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize local Codex token usage from ~/.codex sessions and "
            "estimate hypothetical cost from configurable per-million-token rates."
        )
    )
    parser.add_argument(
        "--codex-home",
        default=str(Path.home() / ".codex"),
        help="Codex home directory. Defaults to ~/.codex.",
    )
    parser.add_argument(
        "--state-db",
        help="Path to Codex state SQLite DB. Defaults to <codex-home>/state_5.sqlite.",
    )
    parser.add_argument(
        "--period",
        choices=("day", "week", "month", "session"),
        default="day",
        help="Aggregation period. Defaults to day.",
    )
    parser.add_argument("--since", help="Inclusive local date filter: YYYY-MM-DD.")
    parser.add_argument("--until", help="Inclusive local date filter: YYYY-MM-DD.")
    parser.add_argument(
        "--format",
        choices=("table", "json", "csv"),
        default="table",
        help="Output format. Defaults to table.",
    )
    parser.add_argument(
        "--view",
        choices=("cost", "credits", "both"),
        default="cost",
        help="Table view to print. JSON and CSV include both estimates. Defaults to cost.",
    )
    parser.add_argument(
        "--input-rate",
        type=float,
        default=DEFAULT_INPUT_RATE,
        help=(
            "Hypothetical uncached input dollars per 1M tokens. "
            f"Defaults to {DEFAULT_INPUT_RATE:.2f}."
        ),
    )
    parser.add_argument(
        "--credit-model",
        choices=sorted(CREDIT_RATE_CARDS),
        default=DEFAULT_CREDIT_MODEL,
        help=f"Codex credit rate card model. Defaults to {DEFAULT_CREDIT_MODEL}.",
    )
    parser.add_argument(
        "--credit-input-rate",
        type=float,
        help="Override Codex credits per 1M uncached input tokens.",
    )
    parser.add_argument(
        "--credit-cached-input-rate",
        type=float,
        help="Override Codex credits per 1M cached input tokens.",
    )
    parser.add_argument(
        "--credit-output-rate",
        type=float,
        help="Override Codex credits per 1M output tokens, including reasoning output.",
    )
    parser.add_argument(
        "--credit-unknown-rate",
        type=float,
        help="Override Codex credits per 1M fallback tokens when only SQLite totals exist.",
    )
    parser.add_argument(
        "--credit-multiplier",
        type=float,
        default=1.0,
        help="Multiplier applied to estimated credits. Defaults to 1.0.",
    )
    parser.add_argument(
        "--fast-mode",
        action="store_true",
        help="Apply documented Codex fast-mode credit multiplier for supported models.",
    )
    parser.add_argument(
        "--cached-input-rate",
        type=float,
        default=DEFAULT_CACHED_INPUT_RATE,
        help=(
            "Hypothetical cached input dollars per 1M tokens. "
            f"Defaults to {DEFAULT_CACHED_INPUT_RATE:.2f}."
        ),
    )
    parser.add_argument(
        "--output-rate",
        type=float,
        default=DEFAULT_OUTPUT_RATE,
        help=(
            "Hypothetical output dollars per 1M tokens, including reasoning output. "
            f"Defaults to {DEFAULT_OUTPUT_RATE:.2f}."
        ),
    )
    parser.add_argument(
        "--unknown-rate",
        type=float,
        default=DEFAULT_INPUT_RATE,
        help=(
            "Hypothetical dollars per 1M fallback tokens when only SQLite totals "
            f"exist. Defaults to {DEFAULT_INPUT_RATE:.2f}."
        ),
    )
    parser.add_argument(
        "--utc",
        action="store_true",
        help="Bucket token events by UTC date instead of local date.",
    )
    parser.add_argument(
        "--desc",
        action="store_true",
        help="Sort periods newest first.",
    )
    return parser.parse_args()


def local_tz() -> timezone:
    return datetime.now().astimezone().tzinfo or timezone.utc


def parse_date(value: str | None) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"Invalid date {value!r}; expected YYYY-MM-DD.") from exc


def datetime_from_epoch(seconds: int, tz: timezone) -> datetime:
    return datetime.fromtimestamp(seconds, timezone.utc).astimezone(tz)


def datetime_from_iso(value: str, tz: timezone) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz)


def load_threads(state_db: Path, tz: timezone) -> list[Thread]:
    if not state_db.exists():
        raise SystemExit(f"State DB not found: {state_db}")

    query = """
        SELECT id, rollout_path, created_at, updated_at, source, model, title, tokens_used
        FROM threads
        ORDER BY updated_at ASC
    """
    with sqlite3.connect(state_db) as connection:
        rows = connection.execute(query).fetchall()

    threads = []
    for row in rows:
        thread_id, rollout_path, created_at, updated_at, source, model, title, tokens_used = row
        threads.append(
            Thread(
                id=thread_id,
                rollout_path=Path(rollout_path),
                created_at=datetime_from_epoch(created_at, tz),
                updated_at=datetime_from_epoch(updated_at, tz),
                source=source or "",
                model=model or "",
                title=title or "",
                tokens_used=int(tokens_used or 0),
            )
        )
    return threads


def token_usage_events(path: Path, tz: timezone) -> list[tuple[datetime, dict[str, int]]]:
    events = []
    if not path.exists():
        return events

    previous = {field: 0 for field in USAGE_FIELDS}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload") or {}
            if payload.get("type") != "token_count":
                continue

            timestamp = event.get("timestamp")
            total_usage = ((payload.get("info") or {}).get("total_token_usage") or {})
            if not timestamp or not total_usage:
                continue

            current = {field: int(total_usage.get(field) or 0) for field in USAGE_FIELDS}
            if current["total_tokens"] < previous["total_tokens"]:
                delta = current.copy()
            else:
                delta = {
                    field: max(current[field] - previous.get(field, 0), 0)
                    for field in USAGE_FIELDS
                }
            previous = current
            if delta["total_tokens"] <= 0:
                continue
            events.append((datetime_from_iso(timestamp, tz), delta))

    return events


def final_token_usage(path: Path) -> dict[str, int] | None:
    if not path.exists():
        return None

    final_usage = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload") or {}
            if payload.get("type") != "token_count":
                continue

            total_usage = ((payload.get("info") or {}).get("total_token_usage") or {})
            if total_usage:
                final_usage = {
                    field: int(total_usage.get(field) or 0)
                    for field in USAGE_FIELDS
                }

    return final_usage


def period_key(value: datetime, period: str) -> str:
    if period == "day":
        return value.date().isoformat()
    if period == "week":
        iso_year, iso_week, _ = value.date().isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if period == "month":
        return f"{value.year:04d}-{value.month:02d}"
    raise ValueError(f"Unsupported period: {period}")


def in_range(value: datetime, since: datetime.date | None, until: datetime.date | None) -> bool:
    date_value = value.date()
    if since and date_value < since:
        return False
    if until and date_value > until:
        return False
    return True


def aggregate_periods(
    threads: list[Thread],
    period: str,
    since: datetime.date | None,
    until: datetime.date | None,
    tz: timezone,
) -> dict[str, Usage]:
    buckets: dict[str, Usage] = defaultdict(Usage)
    for thread in threads:
        events = token_usage_events(thread.rollout_path, tz)
        if events:
            for timestamp, delta in events:
                if not in_range(timestamp, since, until):
                    continue
                key = period_key(timestamp, period)
                buckets[key].add_dict(delta)
                buckets[key].sessions.add(thread.id)
            continue

        if not in_range(thread.updated_at, since, until):
            continue
        key = period_key(thread.updated_at, period)
        buckets[key].add_dict(
            {
                "total_tokens": thread.tokens_used,
                "unknown_tokens": thread.tokens_used,
            }
        )
        buckets[key].sessions.add(thread.id)

    return buckets


def aggregate_sessions(
    threads: list[Thread],
    since: datetime.date | None,
    until: datetime.date | None,
) -> dict[str, Usage]:
    buckets: dict[str, Usage] = {}
    for thread in threads:
        if not in_range(thread.updated_at, since, until):
            continue

        usage = Usage()
        final_usage = final_token_usage(thread.rollout_path)
        if final_usage:
            usage.add_dict(final_usage)
        else:
            usage.add_dict(
                {
                    "total_tokens": thread.tokens_used,
                    "unknown_tokens": thread.tokens_used,
                }
            )
        usage.sessions.add(thread.id)
        buckets[session_key(thread)] = usage
    return buckets


def session_key(thread: Thread) -> str:
    title = thread.title.replace("\n", " ").strip()
    if len(title) > 48:
        title = f"{title[:45]}..."
    model = f" {thread.model}" if thread.model else ""
    return f"{thread.updated_at.date().isoformat()} {thread.id[:8]}{model} {title}".rstrip()


def money(value: float) -> str:
    return f"${value:,.2f}"


def decimal(value: float) -> str:
    return f"{value:,.2f}"


def compact_int(value: int) -> str:
    if isinstance(value, str):
        return value
    return f"{value:,}"


def credit_settings(args: argparse.Namespace) -> dict[str, Any]:
    rates = CREDIT_RATE_CARDS[args.credit_model]
    input_rate = args.credit_input_rate if args.credit_input_rate is not None else rates["input"]
    cached_input_rate = (
        args.credit_cached_input_rate
        if args.credit_cached_input_rate is not None
        else rates["cached_input"]
    )
    output_rate = args.credit_output_rate if args.credit_output_rate is not None else rates["output"]
    unknown_rate = args.credit_unknown_rate if args.credit_unknown_rate is not None else input_rate
    multiplier = args.credit_multiplier

    if args.fast_mode:
        fast_multiplier = FAST_MODE_MULTIPLIERS.get(args.credit_model)
        if fast_multiplier is None:
            supported = ", ".join(sorted(FAST_MODE_MULTIPLIERS))
            raise SystemExit(
                f"--fast-mode is only defined for these credit models: {supported}."
            )
        multiplier *= fast_multiplier

    return {
        "credit_model": args.credit_model,
        "credit_input_rate": input_rate,
        "credit_cached_input_rate": cached_input_rate,
        "credit_output_rate": output_rate,
        "credit_unknown_rate": unknown_rate,
        "credit_multiplier": multiplier,
        "fast_mode": args.fast_mode,
    }


def rows_for_output(
    buckets: dict[str, Usage],
    input_rate: float,
    cached_input_rate: float,
    output_rate: float,
    unknown_rate: float,
    credit_input_rate: float,
    credit_cached_input_rate: float,
    credit_output_rate: float,
    credit_unknown_rate: float,
    credit_multiplier: float,
    desc: bool,
) -> list[dict[str, Any]]:
    rows = []
    for key, usage in buckets.items():
        rows.append(
            {
                "period": key,
                "sessions": len(usage.sessions),
                "input_tokens": usage.input_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "uncached_input_tokens": usage.uncached_input_tokens,
                "output_tokens": usage.output_tokens,
                "reasoning_output_tokens": usage.reasoning_output_tokens,
                "visible_output_tokens": usage.visible_output_tokens,
                "unknown_tokens": usage.unknown_tokens,
                "total_tokens": usage.total_tokens,
                "hypothetical_cost_usd": round(
                    usage.hypothetical_cost(
                        input_rate,
                        cached_input_rate,
                        output_rate,
                        unknown_rate,
                    ),
                    4,
                ),
                "estimated_credits": round(
                    usage.estimated_credits(
                        credit_input_rate,
                        credit_cached_input_rate,
                        credit_output_rate,
                        credit_unknown_rate,
                        credit_multiplier,
                    ),
                    4,
                ),
            }
        )
    return sorted(rows, key=lambda row: row["period"], reverse=desc)


def total_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = {
        "period": "TOTAL",
        "sessions": sum(row["sessions"] for row in rows),
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "cached_input_tokens": sum(row["cached_input_tokens"] for row in rows),
        "uncached_input_tokens": sum(row["uncached_input_tokens"] for row in rows),
        "output_tokens": sum(row["output_tokens"] for row in rows),
        "reasoning_output_tokens": sum(row["reasoning_output_tokens"] for row in rows),
        "visible_output_tokens": sum(row["visible_output_tokens"] for row in rows),
        "unknown_tokens": sum(row["unknown_tokens"] for row in rows),
        "total_tokens": sum(row["total_tokens"] for row in rows),
        "hypothetical_cost_usd": round(
            sum(row["hypothetical_cost_usd"] for row in rows),
            4,
        ),
        "estimated_credits": round(
            sum(row["estimated_credits"] for row in rows),
            4,
        ),
    }
    return total


def print_table(rows: list[dict[str, Any]], include_total: bool, view: str) -> None:
    display_rows = rows + ([total_row(rows)] if include_total and rows else [])
    if not display_rows:
        print("No Codex usage found for the selected range.")
        return

    headers = [
        ("period", "period"),
        ("sessions", "sessions"),
        ("input_tokens", "input"),
        ("cached_input_tokens", "cached"),
        ("uncached_input_tokens", "uncached"),
        ("output_tokens", "output"),
        ("reasoning_output_tokens", "reasoning"),
        ("unknown_tokens", "unknown"),
        ("total_tokens", "total"),
    ]
    if view in ("cost", "both"):
        headers.append(("hypothetical_cost_usd", "hyp cost"))
    if view in ("credits", "both"):
        headers.append(("estimated_credits", "credits"))

    table = []
    for row in display_rows:
        rendered = {}
        for key, label in headers:
            value = row[key]
            if key == "hypothetical_cost_usd":
                rendered[label] = money(value)
            elif key == "estimated_credits":
                rendered[label] = decimal(value)
            else:
                rendered[label] = compact_int(value)
        table.append(rendered)

    widths = {
        label: max(len(label), *(len(row[label]) for row in table))
        for _, label in headers
    }
    print("  ".join(label.ljust(widths[label]) for _, label in headers))
    print("  ".join("-" * widths[label] for _, label in headers))
    for row in table:
        print(
            "  ".join(
                row[label].rjust(widths[label]) if label != "period" else row[label].ljust(widths[label])
                for _, label in headers
            )
        )


def print_json(rows: list[dict[str, Any]], include_total: bool, metadata: dict[str, Any]) -> None:
    payload: dict[str, Any] = {"rows": rows, "metadata": metadata}
    if include_total:
        payload["total"] = total_row(rows) if rows else None
    print(json.dumps(payload, indent=2))


def print_csv(rows: list[dict[str, Any]], include_total: bool) -> None:
    output_rows = rows + ([total_row(rows)] if include_total and rows else [])
    if not output_rows:
        return
    writer = csv.DictWriter(sys.stdout, fieldnames=list(output_rows[0].keys()))
    writer.writeheader()
    writer.writerows(output_rows)


def main() -> int:
    args = parse_args()
    codex_home = Path(args.codex_home).expanduser()
    state_db = Path(args.state_db).expanduser() if args.state_db else codex_home / "state_5.sqlite"
    tz = timezone.utc if args.utc else local_tz()
    since = parse_date(args.since)
    until = parse_date(args.until)
    credits = credit_settings(args)

    threads = load_threads(state_db, tz)
    if args.period == "session":
        buckets = aggregate_sessions(threads, since, until)
    else:
        buckets = aggregate_periods(threads, args.period, since, until, tz)

    rows = rows_for_output(
        buckets,
        args.input_rate,
        args.cached_input_rate,
        args.output_rate,
        args.unknown_rate,
        credits["credit_input_rate"],
        credits["credit_cached_input_rate"],
        credits["credit_output_rate"],
        credits["credit_unknown_rate"],
        credits["credit_multiplier"],
        args.desc,
    )

    metadata = {
        "hypothetical_cost_rates_per_1m_tokens": {
            "uncached_input": args.input_rate,
            "cached_input": args.cached_input_rate,
            "output": args.output_rate,
            "unknown_fallback": args.unknown_rate,
        },
        "credit_rates_per_1m_tokens": {
            "model": credits["credit_model"],
            "uncached_input": credits["credit_input_rate"],
            "cached_input": credits["credit_cached_input_rate"],
            "output": credits["credit_output_rate"],
            "unknown_fallback": credits["credit_unknown_rate"],
            "multiplier": credits["credit_multiplier"],
            "fast_mode": credits["fast_mode"],
        },
    }

    if args.format == "table":
        if args.view in ("cost", "both"):
            print(
                "Hypothetical rates: "
                f"uncached input ${args.input_rate:.2f}/1M, "
                f"cached input ${args.cached_input_rate:.2f}/1M, "
                f"output ${args.output_rate:.2f}/1M, "
                f"unknown fallback ${args.unknown_rate:.2f}/1M"
            )
        if args.view in ("credits", "both"):
            if args.view == "both":
                print()
            print(
                "Credit rates: "
                f"model {credits['credit_model']}, "
                f"uncached input {credits['credit_input_rate']:g} credits/1M, "
                f"cached input {credits['credit_cached_input_rate']:g} credits/1M, "
                f"output {credits['credit_output_rate']:g} credits/1M, "
                f"unknown fallback {credits['credit_unknown_rate']:g} credits/1M, "
                f"multiplier {credits['credit_multiplier']:g}"
            )
        print_table(rows, include_total=args.period != "session", view=args.view)
    elif args.format == "json":
        print_json(rows, include_total=args.period != "session", metadata=metadata)
    else:
        print_csv(rows, include_total=args.period != "session")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
