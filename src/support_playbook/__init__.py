"""Linux, PostgreSQL & Container Support Field Playbook package."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from support_playbook.log_correlator import (
        AnomalySpike,
        BucketSummary,
        LogCorrelator,
        LogEntry,
    )
    from support_playbook.pg_advisor import (
        AutovacuumAuditResult,
        IndexAuditResult,
        IndexTelemetryRow,
        LiveTuningRecommendation,
        LiveWorkloadTelemetry,
        MemoryAuditResult,
        MemoryTelemetry,
        MountTelemetryRow,
        NetworkInterfaceTelemetry,
        PgAdvisor,
        StorageAuditResult,
        TableTelemetryRow,
        calculate_live_workload_tunings,
        format_live_tuning_markdown,
    )
    from support_playbook.sql_pushdown_demo import (
        BenchmarkResult,
        DepartmentSummary,
        run_sql_pushdown_benchmark,
    )

__all__ = [
    "LogCorrelator",
    "LogEntry",
    "BucketSummary",
    "AnomalySpike",
    "PgAdvisor",
    "MemoryTelemetry",
    "MemoryAuditResult",
    "IndexTelemetryRow",
    "IndexAuditResult",
    "TableTelemetryRow",
    "AutovacuumAuditResult",
    "LiveWorkloadTelemetry",
    "LiveTuningRecommendation",
    "calculate_live_workload_tunings",
    "format_live_tuning_markdown",
    "MountTelemetryRow",
    "NetworkInterfaceTelemetry",
    "StorageAuditResult",
    "run_sql_pushdown_benchmark",
    "BenchmarkResult",
    "DepartmentSummary",
]


def __getattr__(name: str) -> object:
    if name in ("LogCorrelator", "LogEntry", "BucketSummary", "AnomalySpike"):
        import support_playbook.log_correlator as log_mod
        return getattr(log_mod, name)
    if name in (
        "PgAdvisor",
        "MemoryTelemetry",
        "MemoryAuditResult",
        "IndexTelemetryRow",
        "IndexAuditResult",
        "TableTelemetryRow",
        "AutovacuumAuditResult",
        "MountTelemetryRow",
        "NetworkInterfaceTelemetry",
        "StorageAuditResult",
        "LiveWorkloadTelemetry",
        "LiveTuningRecommendation",
        "calculate_live_workload_tunings",
        "format_live_tuning_markdown",
    ):
        import support_playbook.pg_advisor as pg_mod
        return getattr(pg_mod, name)
    if name in ("BenchmarkResult", "DepartmentSummary", "run_sql_pushdown_benchmark"):
        import support_playbook.sql_pushdown_demo as sql_mod
        return getattr(sql_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

