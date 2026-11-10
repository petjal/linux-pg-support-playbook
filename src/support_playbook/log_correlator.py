"""Multi-service log correlator and 5-minute heartbeat slicer.

Normalizes heterogeneous timestamps across web, app, database, and system logs,
interlaces them chronologically, inserts synthetic 5-minute heartbeat boundaries,
and detects the earliest anomaly window ('Look Left on the Timeline').
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

# Regex patterns for heterogeneous log timestamps
ISO_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:\s*(?:Z|[+-]\d{2}:?\d{2}|UTC))?)"
)
SYSLOG_PATTERN = re.compile(
    r"^([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
)
COMMON_LOG_PATTERN = re.compile(
    r"\[(\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2}\s+[+-]\d{4})\]"
)

# Common level tokens avoiding hyphenated hostname matches (e.g. log-server01)
LEVEL_PATTERN = re.compile(
    r"(?<![\w-])(FATAL|PANIC|CRITICAL|SEVERE|ERROR|ERR|WARN|WARNING|NOTICE|INFO|LOG|DEBUG|TRACE)(?![\w-])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LogEntry:
    """Normalized log record across any service stream."""

    timestamp: datetime
    service: str
    level: str
    message: str
    raw_line: str
    source_line_num: int = 0

    @property
    def iso_timestamp(self) -> str:
        """Return formatted ISO-8601 UTC timestamp with millisecond precision."""
        utc_dt = self.timestamp.astimezone(timezone.utc)
        return f"{utc_dt.strftime('%Y-%m-%dT%H:%M:%S')}.{utc_dt.microsecond // 1000:03d}Z"


@dataclass
class BucketSummary:
    """Aggregated telemetry metrics for a 5-minute observation window."""

    window_start: datetime
    total_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    counts_by_service: Dict[str, Dict[str, int]] = field(default_factory=dict)

    @property
    def window_label(self) -> str:
        """Canonical ISO-8601 UTC boundary label."""
        utc_dt = self.window_start.astimezone(timezone.utc)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:00Z")

    def record_entry(self, entry: LogEntry) -> None:
        """Update window metrics with an incoming log entry."""
        self.total_count += 1
        service = entry.service
        if service not in self.counts_by_service:
            self.counts_by_service[service] = {"total": 0, "error": 0, "warn": 0, "info": 0}

        self.counts_by_service[service]["total"] += 1

        lvl = entry.level.upper()
        if lvl in ("FATAL", "PANIC", "CRITICAL", "SEVERE", "ERROR", "ERR"):
            self.error_count += 1
            self.counts_by_service[service]["error"] += 1
        elif lvl in ("WARN", "WARNING"):
            self.warning_count += 1
            self.counts_by_service[service]["warn"] += 1
        else:
            self.counts_by_service[service]["info"] += 1


@dataclass(frozen=True)
class AnomalySpike:
    """Earliest detected error or warning spike identifying root incident window."""

    window_start: datetime
    trigger_service: str
    error_count: int
    warning_count: int
    previous_window_errors: int
    diagnosis: str

    @property
    def window_label(self) -> str:
        """Formatted window boundary label."""
        utc_dt = self.window_start.astimezone(timezone.utc)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:00Z")


class LogCorrelator:
    """Interlaces multi-service streams and slices into 5-minute heartbeat intervals."""

    def __init__(self, default_year: int = 2026) -> None:
        self.default_year = default_year
        self._entries: List[LogEntry] = []

    def parse_timestamp(self, text: str) -> Optional[Tuple[datetime, str]]:
        """Extract and parse timestamp from raw line prefix.

        Returns (parsed_utc_datetime, remaining_text) if found, else None.
        """
        # 1. ISO-8601 / RFC-3339
        iso_match = ISO_PATTERN.match(text)
        if iso_match:
            raw_ts = iso_match.group(1)
            # Normalize comma to dot for fractional seconds
            clean_ts = raw_ts.replace(",", ".")
            # Normalize trailing UTC to Z
            clean_ts = re.sub(r"\s*UTC$", "Z", clean_ts)
            # Remove space before timezone offset (e.g., ' -0500' -> '-0500', ' Z' -> 'Z')
            clean_ts = re.sub(r"\s+([+-]\d{2}:?\d{2}|Z)$", r"\1", clean_ts)
            # Replace space between date and time with T
            if " " in clean_ts:
                clean_ts = clean_ts.replace(" ", "T", 1)
            if clean_ts.endswith("Z"):
                clean_ts = clean_ts[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(clean_ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
                remainder = text[iso_match.end():].lstrip(": -")
                return dt, remainder
            except ValueError:
                pass

        # 2. Apache / Common Log style [10/Nov/2026:01:15:30 +0000]
        clf_match = COMMON_LOG_PATTERN.search(text)
        if clf_match:
            ts_str = clf_match.group(1)
            try:
                dt = datetime.strptime(ts_str, "%d/%b/%Y:%H:%M:%S %z")
                dt = dt.astimezone(timezone.utc)
                remainder = (text[:clf_match.start()] + text[clf_match.end():]).strip()
                return dt, remainder
            except ValueError:
                pass

        # 3. Syslog style: Nov 10 01:15:30
        syslog_match = SYSLOG_PATTERN.match(text)
        if syslog_match:
            ts_str = syslog_match.group(1)
            try:
                naive_dt = datetime.strptime(
                    f"{self.default_year} {ts_str}", "%Y %b %d %H:%M:%S"
                )
                dt = naive_dt.replace(tzinfo=timezone.utc)
                remainder = text[syslog_match.end():].lstrip(": -")
                return dt, remainder
            except ValueError:
                pass

        return None

    def detect_level(self, text: str) -> str:
        """Identify log severity level token, prioritizing highest severity."""
        matches = [m.upper() for m in LEVEL_PATTERN.findall(text)]
        if not matches:
            return "INFO"
        if any(m in ("FATAL", "PANIC", "CRITICAL", "SEVERE", "ERROR", "ERR") for m in matches):
            return "ERROR"
        if any(m in ("WARN", "WARNING") for m in matches):
            return "WARN"
        if any(m in ("DEBUG", "TRACE") for m in matches):
            return "DEBUG"
        return "INFO"

    def ingest_line(
        self,
        service: str,
        line: str,
        line_num: int = 0,
        fallback_dt: Optional[datetime] = None,
    ) -> Optional[LogEntry]:
        """Ingest a single log line, normalizing timestamp and severity."""
        cleaned = line.strip()
        if not cleaned:
            return None

        parsed = self.parse_timestamp(cleaned)
        if parsed:
            dt, msg = parsed
        else:
            if fallback_dt is None:
                return None
            dt = fallback_dt
            msg = cleaned

        level = self.detect_level(msg)
        entry = LogEntry(
            timestamp=dt,
            service=service,
            level=level,
            message=msg,
            raw_line=cleaned,
            source_line_num=line_num,
        )
        self._entries.append(entry)
        return entry

    def ingest_stream(
        self,
        service: str,
        lines: Iterable[str],
    ) -> int:
        """Ingest an iterable stream of lines for a service."""
        count = 0
        last_dt: Optional[datetime] = None
        for i, line in enumerate(lines, 1):
            entry = self.ingest_line(service, line, line_num=i, fallback_dt=last_dt)
            if entry:
                last_dt = entry.timestamp
                count += 1
        return count

    @staticmethod
    def floor_5m(dt: datetime) -> datetime:
        """Floor a UTC datetime to its 5-minute window boundary."""
        utc_dt = dt.astimezone(timezone.utc)
        floored_minute = (utc_dt.minute // 5) * 5
        return utc_dt.replace(minute=floored_minute, second=0, microsecond=0)

    def slice_heartbeat_buckets(self) -> List[BucketSummary]:
        """Group sorted log entries into 5-minute chronological buckets.

        Guards against clock-skew memory bloat by capping contiguous empty bucket
        materialization between distant entry windows to at most 288 buckets (24 hours).
        """
        if not self._entries:
            return []

        sorted_entries = sorted(self._entries, key=lambda e: e.timestamp)
        MAX_CONTIGUOUS_EMPTY_BUCKETS = 288

        distinct_windows = sorted({self.floor_5m(e.timestamp) for e in sorted_entries})
        buckets: Dict[datetime, BucketSummary] = {}

        for i, win in enumerate(distinct_windows):
            buckets[win] = BucketSummary(window_start=win)
            if i + 1 < len(distinct_windows):
                next_win = distinct_windows[i + 1]
                gap_seconds = (next_win - win).total_seconds()
                gap_buckets = int(gap_seconds // 300)
                if 1 < gap_buckets <= MAX_CONTIGUOUS_EMPTY_BUCKETS:
                    curr = datetime.fromtimestamp(win.timestamp() + 300, tz=timezone.utc)
                    while curr < next_win:
                        buckets[curr] = BucketSummary(window_start=curr)
                        curr = datetime.fromtimestamp(curr.timestamp() + 300, tz=timezone.utc)

        for e in sorted_entries:
            w = self.floor_5m(e.timestamp)
            if w in buckets:
                buckets[w].record_entry(e)

        return [buckets[k] for k in sorted(buckets.keys())]

    def emit_interlaced_with_heartbeats(self) -> List[str]:
        """Interlace multi-service logs chronologically with synthetic 5m heartbeat markers."""
        if not self._entries:
            return []

        sorted_entries = sorted(self._entries, key=lambda e: e.timestamp)
        output_lines: List[str] = []
        current_window: Optional[datetime] = None

        buckets = {b.window_start: b for b in self.slice_heartbeat_buckets()}

        for e in sorted_entries:
            entry_win = self.floor_5m(e.timestamp)
            if current_window is None or entry_win != current_window:
                current_window = entry_win
                b = buckets.get(entry_win)
                err_summary = "errors=0"
                if b:
                    per_svc = [
                        f"{svc}={counts.get('error', 0)}e"
                        for svc, counts in sorted(b.counts_by_service.items())
                    ]
                    err_summary = f"errors={b.error_count} ({', '.join(per_svc)})"

                marker = (
                    f"=== HEARTBEAT_5M: {entry_win.strftime('%Y-%m-%dT%H:%M:00.000Z')} "
                    f"[{err_summary}] ==="
                )
                output_lines.append(marker)

            line_str = (
                f"{e.iso_timestamp} [{e.service.upper():<6}] [{e.level:<5}] {e.message}"
            )
            output_lines.append(line_str)

        return output_lines

    def find_earliest_spike(
        self,
        min_error_threshold: int = 1,
    ) -> Optional[AnomalySpike]:
        """Look left on the timeline to isolate the exact 5-minute window where errors first surged.

        Scans 5-minute buckets chronologically. Identifies the initial window where
        errors or warnings escalated above baseline, pointing to the root failure origin.
        """
        buckets = self.slice_heartbeat_buckets()
        if not buckets:
            return None

        effective_threshold = max(1, min_error_threshold)
        prev_errors = 0
        for b in buckets:
            if b.error_count >= effective_threshold:
                # Find the primary service contributing to the surge in this window
                lead_service = "unknown"
                max_svc_errors = -1
                for svc, stats in b.counts_by_service.items():
                    svc_err = stats.get("error", 0)
                    if svc_err > max_svc_errors:
                        max_svc_errors = svc_err
                        lead_service = svc

                diagnosis = (
                    f"Cross-service error surge detected at {b.window_label}. "
                    f"Initial escalation driven by service '{lead_service}' "
                    f"with {max_svc_errors} errors (total window errors: {b.error_count})."
                )
                return AnomalySpike(
                    window_start=b.window_start,
                    trigger_service=lead_service,
                    error_count=b.error_count,
                    warning_count=b.warning_count,
                    previous_window_errors=prev_errors,
                    diagnosis=diagnosis,
                )
            prev_errors = b.error_count

        return None


def main() -> None:
    """CLI driver for log correlator."""
    parser = argparse.ArgumentParser(
        description="Correlate multi-service logs and slice into 5-minute heartbeat buckets."
    )
    parser.add_argument(
        "--service",
        action="append",
        nargs=2,
        metavar=("SERVICE_NAME", "FILE_PATH"),
        help="Map service name to log file (can be specified multiple times)",
    )
    args = parser.parse_args()

    correlator = LogCorrelator()
    if args.service:
        for svc_name, fpath in args.service:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                correlator.ingest_stream(svc_name, f)
    else:
        # Default read from stdin as app service
        correlator.ingest_stream("stdin", sys.stdin)

    for line in correlator.emit_interlaced_with_heartbeats():
        print(line)

    spike = correlator.find_earliest_spike()
    if spike:
        print("\n--- ROOT CAUSE TIMELINE ANALYSIS (LOOK LEFT) ---")
        print(f"Trigger Window : {spike.window_label}")
        print(f"Trigger Service: {spike.trigger_service}")
        print(f"Window Errors  : {spike.error_count}")
        print(f"Diagnosis      : {spike.diagnosis}")


if __name__ == "__main__":
    main()
