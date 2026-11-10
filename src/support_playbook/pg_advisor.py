"""Linux & PostgreSQL enterprise configuration and field telemetry advisor.

Analyzes single-box memory splits, unused index write-amplification, table autovacuum
churn, and storage/network mount configurations based on field-tested escalation heuristics.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import List, Literal, Optional, Sequence


@dataclass
class LiveWorkloadTelemetry:
    """Metrics collected from a live running PostgreSQL instance under heavy load."""

    ram_gb: int
    vcpus: int
    storage_type: Literal["nvme", "ssd", "hdd"] = "ssd"
    max_connections: int = 200
    active_workers: int = 4
    temp_bytes_written_per_hour: int = 0
    top_sort_spill_mb: int = 0
    dead_tuple_ratio_max: float = 0.0
    hot_table_names: tuple[str, ...] = ()
    checkpoints_req_per_hour: int = 0
    zero_scan_index_ratio: float = 0.0
    network_rtt_ms: float = 0.5


@dataclass
class LiveTuningRecommendation:
    """Workload-driven configuration recommendations and remediation actions."""

    gitlab_rb_overrides: dict[str, str | int | float]
    table_storage_sql: list[str]
    hardware_and_network_actions: list[str]
    rationale: list[str]


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

    def calculate_live_workload_tunings(
        self, telemetry: LiveWorkloadTelemetry
    ) -> LiveTuningRecommendation:
        """Calculate workload-driven recommendations from live telemetry."""
        return calculate_live_workload_tunings(telemetry)


def calculate_live_workload_tunings(
    telemetry: LiveWorkloadTelemetry,
) -> LiveTuningRecommendation:
    """Calculate production-grade PostgreSQL and GitLab Omnibus tunings from live telemetry.

    Synthesizes single-box memory splits, sort spill metrics, high-churn autovacuum needs,
    and storage/network characteristics into concrete /etc/gitlab/gitlab.rb overrides
    and table storage parameters.
    """
    overrides: dict[str, str | int | float] = {}
    table_storage_sql: list[str] = []
    actions: list[str] = []
    rationale: list[str] = []

    # 1. shared_buffers & effective_cache_size (25% and 75% memory rules)
    shared_mb = int(telemetry.ram_gb * 1024 * 0.25)
    if shared_mb >= 1024:
        sb_val = f"{shared_mb // 1024}GB"
    else:
        sb_val = f"{shared_mb}MB"
    overrides["postgresql['shared_buffers']"] = sb_val
    rationale.append(
        f"postgresql['shared_buffers'] set to 25% of physical RAM ({sb_val}) "
        "to cache active hot working sets while preserving headroom for OS page cache and worker processes."
    )

    eff_cache_mb = int(telemetry.ram_gb * 1024 * 0.75)
    if eff_cache_mb >= 1024:
        overrides["postgresql['effective_cache_size']"] = f"{eff_cache_mb // 1024}GB"
    else:
        overrides["postgresql['effective_cache_size']"] = f"{eff_cache_mb}MB"

    # 2. work_mem computation with safe OOM ceiling
    base_work_mem_mb = 16
    safe_cap_mb = max(16, (telemetry.ram_gb * 1024) // max(1, telemetry.active_workers * 4))

    if telemetry.top_sort_spill_mb > 0:
        target_mb = max(base_work_mem_mb, int(telemetry.top_sort_spill_mb * 2))
        p = 1
        while p < target_mb:
            p <<= 1
        work_mem_mb = min(p, safe_cap_mb)
        rationale.append(
            f"Observed peak sort spill of {telemetry.top_sort_spill_mb} MB to disk. Scaled work_mem to {work_mem_mb}MB "
            f"(target: {target_mb}MB, safety cap: {safe_cap_mb}MB) to convert external merge sorts to in-memory operations."
        )
    elif telemetry.temp_bytes_written_per_hour > 0:
        work_mem_mb = min(base_work_mem_mb * 2, safe_cap_mb)
        rationale.append(
            f"Observed {telemetry.temp_bytes_written_per_hour:,} temp bytes/hr written to disk. Increased work_mem to "
            f"{work_mem_mb}MB (baseline: {base_work_mem_mb}MB) to suppress temporary file generation."
        )
    else:
        work_mem_mb = min(base_work_mem_mb, safe_cap_mb)
        rationale.append(
            f"No sort spills or temp disk writes observed. Keeping baseline work_mem at {work_mem_mb}MB."
        )

    overrides["postgresql['work_mem']"] = f"{work_mem_mb}MB"

    # 3. Storage I/O concurrency & random_page_cost
    if telemetry.storage_type in ("nvme", "ssd"):
        overrides["postgresql['random_page_cost']"] = 1.1
        overrides["postgresql['effective_io_concurrency']"] = 200
        rationale.append(
            f"Flash storage ({telemetry.storage_type.upper()}) detected: set random_page_cost = 1.1 "
            "and effective_io_concurrency = 200 for non-rotational random seek parity."
        )
    else:  # hdd
        overrides["postgresql['random_page_cost']"] = 4.0
        overrides["postgresql['effective_io_concurrency']"] = 2
        actions.append(
            "CRITICAL STORAGE WARNING: Database is running on rotational HDD storage. "
            "Migrate PostgreSQL data and WAL to dedicated enterprise SSD or NVMe storage."
        )
        rationale.append(
            "Rotational HDD detected: random_page_cost set to 4.0 and effective_io_concurrency to 2."
        )

    # 4. Checkpoints & WAL volume
    if telemetry.checkpoints_req_per_hour > 4:
        overrides["postgresql['max_wal_size']"] = "16GB"
        overrides["postgresql['checkpoint_completion_target']"] = 0.9
        actions.append(
            f"High forced WAL checkpoint rate ({telemetry.checkpoints_req_per_hour}/hr > 4/hr). "
            "Ensure WAL volume (/var/opt/gitlab/postgresql/data/pg_wal) has >= 32 GB disk space and dedicated IOPS."
        )
        rationale.append(
            f"Forced checkpoint rate ({telemetry.checkpoints_req_per_hour}/hr) exceeds 4/hr threshold. "
            "Increased max_wal_size to 16GB and smoothed completion target to 0.9 to prevent disk sync spikes."
        )
    else:
        overrides["postgresql['max_wal_size']"] = "4GB"
        overrides["postgresql['checkpoint_completion_target']"] = 0.9

    # 5. Dead tuple churn & leaf table autovacuum
    if telemetry.dead_tuple_ratio_max >= 0.05 and telemetry.hot_table_names:
        for t in telemetry.hot_table_names:
            table_storage_sql.append(
                f"ALTER TABLE {t} SET ("
                f"autovacuum_vacuum_scale_factor = 0.01, "
                f"autovacuum_analyze_scale_factor = 0.005, "
                f"autovacuum_vacuum_cost_limit = 2000);"
            )
        rationale.append(
            f"Peak dead tuple ratio of {telemetry.dead_tuple_ratio_max * 100:.1f}% on hot tables "
            f"exceeds 5% threshold. Generated aggressive per-table autovacuum parameters (1% vacuum, 0.5% analyze, "
            f"2000 cost limit) targeting {len(telemetry.hot_table_names)} physical leaf relations to halt table bloat."
        )

    # 6. Zero-scan index ratio & write amplification
    if telemetry.zero_scan_index_ratio >= 0.30:
        actions.append(
            f"High write amplification detected: {telemetry.zero_scan_index_ratio * 100:.1f}% of index disk footprint "
            "has zero scans (idx_scan = 0). Audit pg_stat_user_indexes and drop redundant indexes concurrently to "
            "recover shared buffer cache and eliminate useless B-tree write penalties."
        )
        rationale.append(
            f"Zero-scan index ratio ({telemetry.zero_scan_index_ratio * 100:.1f}%) exceeds 30% threshold. "
            "Redundant indexes double disk write I/O and evict active table pages from Linux page cache."
        )

    # 7. Network RTT latency & round-trips
    if telemetry.network_rtt_ms >= 10.0:
        actions.append(
            f"High network RTT ({telemetry.network_rtt_ms:.1f} ms >= 10.0 ms) detected between application and database tier. "
            "Deploy local PgBouncer connection pooling, enable local Redis read-through caching, and audit chatty "
            "N+1 Gitaly/PostgreSQL RPC round-trips."
        )
        rationale.append(
            f"Network RTT ({telemetry.network_rtt_ms:.1f} ms) amplifies multi-query transactions. Each sequential database "
            "round-trip incurs severe latency overhead."
        )
    else:
        rationale.append(
            f"Network RTT ({telemetry.network_rtt_ms:.1f} ms) is within acceptable LAN/local boundary (<10.0 ms)."
        )

    return LiveTuningRecommendation(
        gitlab_rb_overrides=overrides,
        table_storage_sql=table_storage_sql,
        hardware_and_network_actions=actions,
        rationale=rationale,
    )


def format_live_tuning_markdown(rec: LiveTuningRecommendation) -> str:
    """Format live tuning recommendations as readable markdown report."""
    lines: list[str] = []
    lines.append("# Live Workload-Driven PostgreSQL & GitLab Tuning Advisory")
    lines.append("")
    lines.append("## 1. /etc/gitlab/gitlab.rb Configuration Overrides")
    lines.append("```ruby")
    for k, v in rec.gitlab_rb_overrides.items():
        if isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {v}")
    lines.append("```")
    lines.append("")
    lines.append("## 2. Leaf Partition Autovacuum Storage Parameters")
    if rec.table_storage_sql:
        lines.append("```sql")
        for sql in rec.table_storage_sql:
            lines.append(sql)
        lines.append("```")
    else:
        lines.append("*No per-table autovacuum overrides required at current tuple churn rates.*")
    lines.append("")
    lines.append("## 3. Hardware, Storage & Network Actions")
    if rec.hardware_and_network_actions:
        for action in rec.hardware_and_network_actions:
            lines.append(f"- {action}")
    else:
        lines.append("*No hardware or network bottlenecks flagged.*")
    lines.append("")
    lines.append("## 4. Engineering Rationale")
    for r in rec.rationale:
        lines.append(f"- {r}")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entrypoint for support-pg-advisor."""
    parser = argparse.ArgumentParser(
        prog="support-pg-advisor",
        description="PostgreSQL & Linux workload-driven live configuration and tuning advisor.",
    )
    parser.add_argument("--ram-gb", type=int, default=32, help="Host RAM in GB (default: 32)")
    parser.add_argument("--vcpus", type=int, default=8, help="Host vCPUs (default: 8)")
    parser.add_argument(
        "--storage-type",
        choices=["nvme", "ssd", "hdd"],
        default="ssd",
        help="Storage medium: nvme, ssd, hdd (default: ssd)",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=200,
        help="Max PostgreSQL connections (default: 200)",
    )
    parser.add_argument(
        "--active-workers",
        type=int,
        default=4,
        help="Concurrent active query worker threads (default: 4)",
    )
    parser.add_argument(
        "--temp-bytes-written-per-hour",
        type=int,
        default=0,
        help="Temp bytes written per hour from pg_stat_database (default: 0)",
    )
    parser.add_argument(
        "--top-sort-spill-mb",
        type=int,
        default=0,
        help="Peak disk sort spill in MB from EXPLAIN (ANALYZE, BUFFERS) (default: 0)",
    )
    parser.add_argument(
        "--dead-tuple-ratio",
        type=float,
        default=0.0,
        help="Max dead tuple ratio on hot tables (default: 0.0)",
    )
    parser.add_argument(
        "--hot-table",
        action="append",
        dest="hot_tables",
        default=None,
        help="Hot table name requiring aggressive autovacuum (repeatable)",
    )
    parser.add_argument(
        "--checkpoints-req-per-hour",
        type=int,
        default=0,
        help="Forced WAL checkpoints per hour from pg_stat_bgwriter (default: 0)",
    )
    parser.add_argument(
        "--zero-scan-index-ratio",
        type=float,
        default=0.0,
        help="Fraction of database size consumed by zero-scan indexes (default: 0.0)",
    )
    parser.add_argument(
        "--network-rtt-ms",
        type=float,
        default=0.5,
        help="Network RTT between app and DB tier in ms (default: 0.5)",
    )
    parser.add_argument(
        "--live-example",
        action="store_true",
        help="Run Project 77K empirical benchmark telemetry scenario (32GB, 8vCPUs, 16MB spill, 53.5% zero-scan indexes)",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "text", "json"],
        default="markdown",
        help="Output format: markdown, text, json (default: markdown)",
    )

    args = parser.parse_args(argv)

    if args.live_example:
        telemetry = LiveWorkloadTelemetry(
            ram_gb=32,
            vcpus=8,
            storage_type="ssd",
            max_connections=200,
            active_workers=4,
            temp_bytes_written_per_hour=820_838_400,
            top_sort_spill_mb=16,
            dead_tuple_ratio_max=0.145,
            hot_table_names=("issues", "notes", "gitlab_partitions_dynamic.ci_builds_100"),
            checkpoints_req_per_hour=6,
            zero_scan_index_ratio=0.535,
            network_rtt_ms=0.5,
        )
    else:
        hot_tables: tuple[str, ...] = tuple(args.hot_tables) if args.hot_tables else ()
        storage_type: Literal["nvme", "ssd", "hdd"] = args.storage_type
        telemetry = LiveWorkloadTelemetry(
            ram_gb=args.ram_gb,
            vcpus=args.vcpus,
            storage_type=storage_type,
            max_connections=args.max_connections,
            active_workers=args.active_workers,
            temp_bytes_written_per_hour=args.temp_bytes_written_per_hour,
            top_sort_spill_mb=args.top_sort_spill_mb,
            dead_tuple_ratio_max=args.dead_tuple_ratio,
            hot_table_names=hot_tables,
            checkpoints_req_per_hour=args.checkpoints_req_per_hour,
            zero_scan_index_ratio=args.zero_scan_index_ratio,
            network_rtt_ms=args.network_rtt_ms,
        )

    rec = calculate_live_workload_tunings(telemetry)

    if args.format == "json":
        data = asdict(rec)
        print(json.dumps(data, indent=2))
    elif args.format == "text":
        for k, v in rec.gitlab_rb_overrides.items():
            print(f"{k} = {v}")
        if rec.table_storage_sql:
            print("\nStorage SQL:")
            for sql in rec.table_storage_sql:
                print(sql)
        if rec.hardware_and_network_actions:
            print("\nHardware & Network Actions:")
            for action in rec.hardware_and_network_actions:
                print(f"- {action}")
        print("\nRationale:")
        for r in rec.rationale:
            print(f"- {r}")
    else:  # markdown
        print(format_live_tuning_markdown(rec))

    return 0


if __name__ == "__main__":
    sys.exit(main())
