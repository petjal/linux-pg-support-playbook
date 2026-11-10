"""Linux & PostgreSQL enterprise configuration and field telemetry advisor.

Analyzes single-box memory splits, unused index write-amplification, table autovacuum
churn, and storage/network mount configurations based on field-tested escalation heuristics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MemoryTelemetry:
    """System memory, CPU, and worker memory distribution telemetry."""

    total_ram_mb: float
    cpu_cores: int
    shared_buffers_mb: float
    mvc_worker_count: int
    mvc_worker_ram_mb: float
    os_base_ram_mb: float = 1024.0
    swap_in_kb: float = 0.0
    swap_out_kb: float = 0.0
    swap_used_mb: float = 0.0

    @property
    def total_mvc_ram_mb(self) -> float:
        """Total memory consumed across all MVC application worker processes."""
        return self.mvc_worker_count * self.mvc_worker_ram_mb


@dataclass
class MemoryAuditResult:
    """Audit analysis of single-box memory allocation and swap health."""

    total_ram_mb: float
    allocated_ram_mb: float
    page_cache_mb: float
    page_cache_pct: float
    is_page_cache_starved: bool
    is_swap_active: bool
    findings: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


@dataclass
class IndexTelemetryRow:
    """Row from pg_stat_user_indexes joined with pg_relation_size."""

    schemaname: str
    relname: str
    indexrelname: str
    idx_scan: int
    idx_tup_read: int
    idx_tup_fetch: int
    index_bytes: int
    table_bytes: int = 0

    @property
    def index_mb(self) -> float:
        """Index size in megabytes."""
        return self.index_bytes / (1024 * 1024)


@dataclass
class IndexAuditResult:
    """Audit analysis of unused indexes and write-amplification."""

    total_indexes: int
    unused_indexes_count: int
    wasted_index_bytes: int
    wasted_index_mb: float
    unused_indexes: List[IndexTelemetryRow] = field(default_factory=list)
    findings: List[str] = field(default_factory=list)
    sql_remediations: List[str] = field(default_factory=list)
    write_amplification_explanation: str = ""


@dataclass
class TableTelemetryRow:
    """Row from pg_stat_user_tables inspecting tuple churn and vacuum scale."""

    schemaname: str
    relname: str
    n_live_tup: int
    n_dead_tup: int
    vacuum_scale_factor: float = 0.20
    analyze_scale_factor: float = 0.10
    has_table_override: bool = False

    @property
    def dead_tuple_ratio(self) -> float:
        """Ratio of dead tuples to total tuples."""
        live = max(0, self.n_live_tup)
        dead = max(0, self.n_dead_tup)
        total = live + dead
        if total <= 0:
            return 0.0
        return dead / total


@dataclass
class AutovacuumAuditResult:
    """Audit analysis of high-churn tables and vacuum scale factor tuning."""

    high_churn_tables: List[TableTelemetryRow] = field(default_factory=list)
    findings: List[str] = field(default_factory=list)
    sql_remediations: List[str] = field(default_factory=list)


@dataclass
class MountTelemetryRow:
    """Storage mount point and filesystem type information."""

    mount_point: str
    fs_type: str
    purpose: str
    mount_options: str = ""


@dataclass
class NetworkInterfaceTelemetry:
    """Network interface MTU and topology configuration."""

    interface_name: str
    mtu: int
    role: str


@dataclass
class StorageAuditResult:
    """Audit analysis of database storage mounts and network interface MTUs."""

    mount_risks: List[str] = field(default_factory=list)
    network_risks: List[str] = field(default_factory=list)
    findings: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


class PgAdvisor:
    """PostgreSQL and Linux single-box systems advisor."""

    PAGE_CACHE_MIN_THRESHOLD_PCT = 25.0
    HIGH_CHURN_ROW_THRESHOLD = 1_000_000

    def audit_memory_split(self, telemetry: MemoryTelemetry) -> MemoryAuditResult:
        """Audit memory split between OS, MVC workers, PostgreSQL shared_buffers, and page cache.

        PostgreSQL relies heavily on the Linux OS page cache for double buffering.
        When MVC workers and database shared_buffers consume too much memory, page cache
        drops below 25%, causing disk read stalls. Swap is a scourge on DB/app servers.
        """
        allocated = (
            telemetry.os_base_ram_mb
            + telemetry.total_mvc_ram_mb
            + telemetry.shared_buffers_mb
        )
        page_cache = max(0.0, telemetry.total_ram_mb - allocated)
        page_cache_pct = (page_cache / telemetry.total_ram_mb) * 100.0 if telemetry.total_ram_mb > 0 else 0.0

        is_starved = page_cache_pct < self.PAGE_CACHE_MIN_THRESHOLD_PCT
        is_swap_active = (
            telemetry.swap_in_kb > 0.0
            or telemetry.swap_out_kb > 0.0
            or telemetry.swap_used_mb > 100.0
        )

        findings: List[str] = []
        recommendations: List[str] = []

        alloc_pct = (
            (allocated / telemetry.total_ram_mb) * 100.0
            if telemetry.total_ram_mb > 0.0
            else 0.0
        )
        findings.append(
            f"Total RAM: {telemetry.total_ram_mb:.0f} MB | Allocated: {allocated:.0f} MB "
            f"({alloc_pct:.1f}%) | "
            f"Available for OS Page Cache: {page_cache:.0f} MB ({page_cache_pct:.1f}%)"
        )

        if is_starved:
            findings.append(
                f"CRITICAL: Linux OS page cache is starved ({page_cache_pct:.1f}% < "
                f"{self.PAGE_CACHE_MIN_THRESHOLD_PCT}% minimum threshold). "
                f"PostgreSQL relies on the OS page cache (buff/cache) to service query hot data."
            )
            recommendations.append(
                "Cap PostgreSQL shared_buffers at 25% of RAM or reduce MVC worker concurrency "
                "to ensure at least 25% to 40% of total RAM remains available for Linux OS page cache."
            )

        if is_swap_active:
            findings.append(
                f"CRITICAL: Active swap detected (swap_in: {telemetry.swap_in_kb:.1f} KB, "
                f"swap_out: {telemetry.swap_out_kb:.1f} KB, used: {telemetry.swap_used_mb:.1f} MB). "
                f"Swap is a scourge on database and application servers: paging out active query "
                f"buffers or MVC workers causes 100x latency spikes and cascading connection timeouts."
            )
            recommendations.append(
                "Set vm.swappiness = 1 (or 0) in /etc/sysctl.conf and rebalance shared_buffers "
                "and worker limits to prevent process paging under load."
            )

        return MemoryAuditResult(
            total_ram_mb=telemetry.total_ram_mb,
            allocated_ram_mb=allocated,
            page_cache_mb=page_cache,
            page_cache_pct=page_cache_pct,
            is_page_cache_starved=is_starved,
            is_swap_active=is_swap_active,
            findings=findings,
            recommendations=recommendations,
        )

    def audit_unused_indexes(self, rows: List[IndexTelemetryRow]) -> IndexAuditResult:
        """Detect zero-scan indexes and calculate wasted storage and write-amplification penalty.

        Every INSERT, DELETE, and non-HOT UPDATE on a table must update every index on that
        table, generating write-ahead log records, dirtying shared buffer pages, and causing
        write-amplification and cache pollution for indexes that are never read by queries.
        """
        unused: List[IndexTelemetryRow] = []
        wasted_bytes = 0

        for r in rows:
            if r.idx_scan == 0:
                unused.append(r)
                wasted_bytes += r.index_bytes

        wasted_mb = wasted_bytes / (1024 * 1024)
        findings: List[str] = []
        sql_remediations: List[str] = []

        findings.append(
            f"Audited {len(rows)} indexes: found {len(unused)} unused indexes (idx_scan = 0) "
            f"consuming {wasted_mb:.2f} MB of wasted disk and cache storage."
        )

        for u in unused:
            findings.append(
                f"Unused Index: {u.schemaname}.{u.indexrelname} on table {u.relname} "
                f"(size: {u.index_mb:.2f} MB, scans: 0)"
            )
            sql_remediations.append(
                f"DROP INDEX CONCURRENTLY {u.schemaname}.{u.indexrelname};"
            )

        explanation = (
            "Write-Amplification & Cache Pollution Impact: Every zero-scan index incurs a "
            "continuous write penalty on the database engine. Every INSERT, DELETE, and "
            "non-HOT UPDATE must modify the index tree, generating WAL records, consuming "
            "disk I/O bandwidth, and evicting hot query data from shared_buffers and the OS "
            "page cache without providing any query acceleration."
        )

        return IndexAuditResult(
            total_indexes=len(rows),
            unused_indexes_count=len(unused),
            wasted_index_bytes=wasted_bytes,
            wasted_index_mb=wasted_mb,
            unused_indexes=unused,
            findings=findings,
            sql_remediations=sql_remediations,
            write_amplification_explanation=explanation,
        )

    def audit_autovacuum_tuning(self, rows: List[TableTelemetryRow]) -> AutovacuumAuditResult:
        """Detect high-churn tables relying on default global autovacuum scale factors.

        The PostgreSQL global default autovacuum_vacuum_scale_factor = 0.20 requires 20%
        of table rows to be dead before triggering vacuum. On a table with 1,000,000+ rows,
        that requires 200,000+ dead tuples before cleanup, causing severe table bloat and
        sequential scan degradation.
        """
        high_churn: List[TableTelemetryRow] = []
        findings: List[str] = []
        sql_remediations: List[str] = []

        for r in rows:
            is_large = r.n_live_tup >= self.HIGH_CHURN_ROW_THRESHOLD
            is_bloated = r.dead_tuple_ratio >= 0.10 and r.n_dead_tup >= 100_000
            if (is_large or is_bloated) and not r.has_table_override:
                high_churn.append(r)
                findings.append(
                    f"Table {r.schemaname}.{r.relname} has {r.n_live_tup:,} live tuples and "
                    f"{r.n_dead_tup:,} dead tuples (dead ratio: {r.dead_tuple_ratio * 100:.1f}%), "
                    f"relying on global default autovacuum_vacuum_scale_factor = {r.vacuum_scale_factor}."
                )
                sql_remediations.append(
                    f"ALTER TABLE {r.schemaname}.{r.relname} SET ("
                    f"autovacuum_vacuum_scale_factor = 0.01, "
                    f"autovacuum_analyze_scale_factor = 0.005);"
                )

        if high_churn:
            findings.append(
                f"Detected {len(high_churn)} high-churn table(s) needing per-table autovacuum "
                f"tuning. Setting scale factor to 0.01 triggers vacuum at 1% tuple churn, "
                f"preventing multi-gigabyte bloat and index degradation."
            )

        return AutovacuumAuditResult(
            high_churn_tables=high_churn,
            findings=findings,
            sql_remediations=sql_remediations,
        )

    def audit_storage_and_network(
        self,
        mounts: List[MountTelemetryRow],
        interfaces: List[NetworkInterfaceTelemetry],
    ) -> StorageAuditResult:
        """Audit storage filesystem mounts and storage network MTU configurations.

        Flagging if pg_wal or PGDATA is mounted on NFS or if storage network interfaces
        carrying SAN/NAS traffic use standard 1500 MTU instead of 9000 Jumbo Frames.
        """
        mount_risks: List[str] = []
        network_risks: List[str] = []
        findings: List[str] = []
        recommendations: List[str] = []

        # Audit mounts
        for m in mounts:
            fs_lower = m.fs_type.lower()
            purpose_lower = m.purpose.lower()
            if "nfs" in fs_lower:
                if any(k in purpose_lower or k in m.mount_point.lower() for k in ("wal", "xlog", "data", "pgdata")):
                    risk_msg = (
                        f"CRITICAL STORAGE RISK: {m.purpose} ({m.mount_point}) is mounted on "
                        f"{m.fs_type.upper()}. PostgreSQL WAL and database files on NFS are prone to "
                        f"fsync flush delays, locking anomalies, and data corruption on network partitions."
                    )
                    mount_risks.append(risk_msg)
                    findings.append(risk_msg)
                    recommendations.append(
                        f"Migrate {m.purpose} ({m.mount_point}) to local NVMe/SSD or high-performance "
                        f"block storage. Write-ahead logs require deterministic local fsync guarantees."
                    )

        # Audit network interfaces
        for net in interfaces:
            role_lower = net.role.lower()
            if any(k in role_lower for k in ("san", "nas", "storage", "iscsi")):
                if net.mtu < 9000:
                    risk_msg = (
                        f"NETWORK MTU WARNING: Storage network interface '{net.interface_name}' "
                        f"({net.role}) is configured with MTU {net.mtu}. Standard 1500 MTU increases "
                        f"packet fragmentation and CPU interrupt overhead during bulk storage transfers."
                    )
                    network_risks.append(risk_msg)
                    findings.append(risk_msg)
                    recommendations.append(
                        f"Configure MTU 9000 (Jumbo Frames) end-to-end on interface '{net.interface_name}', "
                        f"switches, and SAN/NAS storage target ports."
                    )

        return StorageAuditResult(
            mount_risks=mount_risks,
            network_risks=network_risks,
            findings=findings,
            recommendations=recommendations,
        )

    def generate_full_markdown_report(
        self,
        memory_result: Optional[MemoryAuditResult] = None,
        index_result: Optional[IndexAuditResult] = None,
        autovacuum_result: Optional[AutovacuumAuditResult] = None,
        storage_result: Optional[StorageAuditResult] = None,
    ) -> str:
        """Render complete structured markdown triage advisory report."""
        lines: List[str] = []
        lines.append("# PostgreSQL & Linux Field Support Advisory Report")
        lines.append("")

        if memory_result:
            lines.append("## 1. Single-Box Memory Split & OS Page Cache")
            for f in memory_result.findings:
                lines.append(f"- {f}")
            if memory_result.recommendations:
                lines.append("")
                lines.append("**Recommendations:**")
                for r in memory_result.recommendations:
                    lines.append(f"- {r}")
            lines.append("")

        if index_result:
            lines.append("## 2. Unused Index Write-Amplification & Cache Pollution")
            for f in index_result.findings:
                lines.append(f"- {f}")
            lines.append("")
            lines.append(f"> {index_result.write_amplification_explanation}")
            lines.append("")
            if index_result.sql_remediations:
                lines.append("**Remediation SQL:**")
                lines.append("```sql")
                for sql in index_result.sql_remediations:
                    lines.append(sql)
                lines.append("```")
                lines.append("")

        if autovacuum_result:
            lines.append("## 3. High-Churn Autovacuum Tuning")
            for f in autovacuum_result.findings:
                lines.append(f"- {f}")
            if autovacuum_result.sql_remediations:
                lines.append("")
                lines.append("**Remediation SQL:**")
                lines.append("```sql")
                for sql in autovacuum_result.sql_remediations:
                    lines.append(sql)
                lines.append("```")
            lines.append("")

        if storage_result:
            lines.append("## 4. Storage Mounts & Network Interface MTUs")
            for f in storage_result.findings:
                lines.append(f"- {f}")
            if storage_result.recommendations:
                lines.append("")
                lines.append("**Recommendations:**")
                for r in storage_result.recommendations:
                    lines.append(f"- {r}")
            lines.append("")

        return "\n".join(lines)
