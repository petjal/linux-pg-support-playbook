"""Automated test suite for Linux, PostgreSQL & Container Support Field Playbook."""

import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from support_playbook.log_correlator import LogCorrelator
from support_playbook.pg_advisor import (
    IndexTelemetryRow,
    MemoryTelemetry,
    MountTelemetryRow,
    NetworkInterfaceTelemetry,
    PgAdvisor,
    TableTelemetryRow,
)
from support_playbook.sql_pushdown_demo import run_sql_pushdown_benchmark


def test_log_correlator_timestamp_parsing() -> None:
    """Verify parsing of heterogeneous timestamp formats."""
    correlator = LogCorrelator(default_year=2026)

    # ISO-8601 UTC
    iso_line = "2026-11-10T01:15:30Z [web] INFO request completed"
    p1 = correlator.parse_timestamp(iso_line)
    assert p1 is not None
    dt1, msg1 = p1
    assert dt1 == datetime(2026, 11, 10, 1, 15, 30, tzinfo=timezone.utc)
    assert msg1 == "[web] INFO request completed"

    # PostgreSQL style timestamp
    pg_line = "2026-11-10 01:16:05.123 UTC [38421]: [1-1] LOG: statement: SELECT 1"
    p2 = correlator.parse_timestamp(pg_line)
    assert p2 is not None
    dt2, _ = p2
    assert dt2.year == 2026 and dt2.minute == 16 and dt2.second == 5

    # Syslog style
    syslog_line = "Nov 10 01:20:00 server kernel: [123.456] eth0: link up"
    p3 = correlator.parse_timestamp(syslog_line)
    assert p3 is not None
    dt3, _ = p3
    assert dt3 == datetime(2026, 11, 10, 1, 20, 0, tzinfo=timezone.utc)


def test_log_correlator_normalization_and_bucketing() -> None:
    """Verify multi-service interlacing and 5-minute heartbeat aggregation."""
    correlator = LogCorrelator(default_year=2026)

    web_logs = [
        "2026-11-10T01:10:15Z INFO GET /api/v1/health 200",
        "2026-11-10T01:12:00Z WARN GET /api/v1/data 429 Rate limited",
        "2026-11-10T01:15:30Z ERROR GET /api/v1/orders 500 Internal error",
    ]
    db_logs = [
        "2026-11-10 01:11:00 UTC [100]: LOG connection received",
        "2026-11-10 01:14:45 UTC [101]: ERROR deadlock detected process 101",
        "2026-11-10 01:15:10 UTC [102]: ERROR lock timeout on relation orders",
    ]

    correlator.ingest_stream("web", web_logs)
    correlator.ingest_stream("db", db_logs)

    interlaced = correlator.emit_interlaced_with_heartbeats()
    assert len(interlaced) > 6

    # Verify presence of synthetic 5-minute heartbeat boundaries
    heartbeat_lines = [line for line in interlaced if "HEARTBEAT_5M" in line]
    assert len(heartbeat_lines) >= 2
    assert any("2026-11-10T01:10:00.000Z" in hb for hb in heartbeat_lines)
    assert any("2026-11-10T01:15:00.000Z" in hb for hb in heartbeat_lines)

    buckets = correlator.slice_heartbeat_buckets()
    assert len(buckets) == 2

    # Bucket 1: 01:10 to 01:14:59 (contains web: 1 info, 1 warn; db: 1 log, 1 error)
    b1 = buckets[0]
    assert b1.window_label == "2026-11-10T01:10:00Z"
    assert b1.error_count == 1
    assert b1.warning_count == 1

    # Bucket 2: 01:15 to 01:19:59 (contains web: 1 error; db: 1 error)
    b2 = buckets[1]
    assert b2.window_label == "2026-11-10T01:15:00Z"
    assert b2.error_count == 2


def test_log_correlator_spike_detection_look_left() -> None:
    """Verify 'Look Left on the Timeline' identifies the earliest trigger window."""
    correlator = LogCorrelator(default_year=2026)

    # Timeline:
    # 01:00:00 - Baseline normal
    # 01:05:00 - Database starts throwing lock errors (Root failure window)
    # 01:10:00 - Application connection pool fills up, app logs errors
    # 01:15:00 - Web proxy times out and returns HTTP 502/504 errors
    correlator.ingest_line("web", "2026-11-10T01:01:00Z INFO HTTP 200 OK")
    correlator.ingest_line("db", "2026-11-10T01:02:00Z INFO checkpoint complete")

    correlator.ingest_line("db", "2026-11-10T01:06:10Z ERROR relation lock timeout")
    correlator.ingest_line("db", "2026-11-10T01:07:30Z ERROR connection limit exceeded")

    correlator.ingest_line("app", "2026-11-10T01:11:00Z ERROR pool exhausted")
    correlator.ingest_line("web", "2026-11-10T01:16:00Z ERROR upstream gateway timeout 504")

    spike = correlator.find_earliest_spike(min_error_threshold=1)
    assert spike is not None
    assert spike.window_label == "2026-11-10T01:05:00Z"
    assert spike.trigger_service == "db"
    assert spike.error_count == 2
    assert "Initial escalation driven by service 'db'" in spike.diagnosis


def test_pg_advisor_memory_split_and_swap() -> None:
    """Verify single-box memory audit flags starved page cache and active swap."""
    advisor = PgAdvisor()

    # 8 GB RAM box, 2 CPU cores
    # MVC workers: 4 * 512 MB = 2048 MB
    # shared_buffers: 4096 MB (50% of RAM)
    # OS base: 1024 MB
    # Allocated: 7168 MB -> Leaves only 1024 MB (12.5% < 25%) for OS page cache
    telemetry = MemoryTelemetry(
        total_ram_mb=8192.0,
        cpu_cores=2,
        shared_buffers_mb=4096.0,
        mvc_worker_count=4,
        mvc_worker_ram_mb=512.0,
        os_base_ram_mb=1024.0,
        swap_in_kb=450.0,
        swap_out_kb=890.0,
        swap_used_mb=512.0,
    )

    result = advisor.audit_memory_split(telemetry)
    assert result.is_page_cache_starved is True
    assert result.page_cache_pct == 12.5
    assert result.is_swap_active is True

    # Ensure critical warning on swap is present
    swap_findings = [f for f in result.findings if "Swap is a scourge" in f]
    assert len(swap_findings) == 1

    # Healthy profile: 16 GB RAM, shared_buffers 4 GB (25%), 4 workers @ 512MB = 2GB, OS 1GB
    healthy_telem = MemoryTelemetry(
        total_ram_mb=16384.0,
        cpu_cores=4,
        shared_buffers_mb=4096.0,
        mvc_worker_count=4,
        mvc_worker_ram_mb=512.0,
        os_base_ram_mb=1024.0,
        swap_in_kb=0.0,
        swap_out_kb=0.0,
        swap_used_mb=0.0,
    )
    healthy_res = advisor.audit_memory_split(healthy_telem)
    assert healthy_res.is_page_cache_starved is False
    assert healthy_res.is_swap_active is False
    assert healthy_res.page_cache_pct > 50.0


def test_pg_advisor_unused_index_detection() -> None:
    """Verify detection of zero-scan indexes and write-amplification penalty calculation."""
    advisor = PgAdvisor()

    index_rows = [
        IndexTelemetryRow(
            schemaname="public",
            relname="customer_orders",
            indexrelname="idx_orders_created_at",
            idx_scan=145000,
            idx_tup_read=145000,
            idx_tup_fetch=142000,
            index_bytes=45 * 1024 * 1024,
        ),
        IndexTelemetryRow(
            schemaname="public",
            relname="customer_orders",
            indexrelname="idx_orders_legacy_ref",
            idx_scan=0,
            idx_tup_read=0,
            idx_tup_fetch=0,
            index_bytes=120 * 1024 * 1024,  # 120 MB wasted
        ),
        IndexTelemetryRow(
            schemaname="public",
            relname="audit_events",
            indexrelname="idx_audit_unused_metadata",
            idx_scan=0,
            idx_tup_read=0,
            idx_tup_fetch=0,
            index_bytes=80 * 1024 * 1024,  # 80 MB wasted
        ),
    ]

    result = advisor.audit_unused_indexes(index_rows)
    assert result.total_indexes == 3
    assert result.unused_indexes_count == 2
    assert result.wasted_index_mb == 200.0  # 120 + 80

    assert "DROP INDEX CONCURRENTLY public.idx_orders_legacy_ref;" in result.sql_remediations
    assert "DROP INDEX CONCURRENTLY public.idx_audit_unused_metadata;" in result.sql_remediations
    assert "Write-Amplification & Cache Pollution Impact" in result.write_amplification_explanation


def test_pg_advisor_autovacuum_tuning() -> None:
    """Verify table-specific autovacuum tuning recommendations for high-churn tables."""
    advisor = PgAdvisor()

    tables = [
        TableTelemetryRow(
            schemaname="public",
            relname="transaction_logs",
            n_live_tup=2_500_000,
            n_dead_tup=350_000,
            vacuum_scale_factor=0.20,
            analyze_scale_factor=0.10,
            has_table_override=False,
        ),
        TableTelemetryRow(
            schemaname="public",
            relname="lookup_status",
            n_live_tup=50,
            n_dead_tup=2,
            vacuum_scale_factor=0.20,
            analyze_scale_factor=0.10,
            has_table_override=False,
        ),
        TableTelemetryRow(
            schemaname="public",
            relname="events_optimized",
            n_live_tup=4_000_000,
            n_dead_tup=40_000,
            vacuum_scale_factor=0.01,
            analyze_scale_factor=0.005,
            has_table_override=True,  # Already tuned
        ),
    ]

    result = advisor.audit_autovacuum_tuning(tables)
    assert len(result.high_churn_tables) == 1
    assert result.high_churn_tables[0].relname == "transaction_logs"

    expected_sql = (
        "ALTER TABLE public.transaction_logs SET ("
        "autovacuum_vacuum_scale_factor = 0.01, "
        "autovacuum_analyze_scale_factor = 0.005);"
    )
    assert expected_sql in result.sql_remediations


def test_pg_advisor_storage_and_network_traps() -> None:
    """Verify detection of NFS storage mounts for WAL/data and MTU 1500 network bottlenecks."""
    advisor = PgAdvisor()

    mounts = [
        MountTelemetryRow(
            mount_point="/var/lib/postgresql/16/main/pg_wal",
            fs_type="nfs4",
            purpose="pg_wal write-ahead logs",
            mount_options="rw,sync,hard",
        ),
        MountTelemetryRow(
            mount_point="/backup/pg_dumps",
            fs_type="nfs4",
            purpose="nightly backup archives",
            mount_options="rw,nosuid",
        ),
    ]

    interfaces = [
        NetworkInterfaceTelemetry(
            interface_name="eth1",
            mtu=1500,
            role="san_nas_storage",
        ),
        NetworkInterfaceTelemetry(
            interface_name="eth0",
            mtu=1500,
            role="client_traffic",
        ),
    ]

    result = advisor.audit_storage_and_network(mounts, interfaces)
    assert len(result.mount_risks) == 1
    assert "pg_wal" in result.mount_risks[0]
    assert len(result.network_risks) == 1
    assert "eth1" in result.network_risks[0]


def test_sql_pushdown_benchmark() -> None:
    """Verify deterministic execution and speedup of SQL pushdown vs. MVC N+1 queries."""
    result = run_sql_pushdown_benchmark(department_count=100, employees_per_department=50)

    # Assert deterministic query counts and dataset properties
    assert result.anti_pattern_query_count == 101  # 1 initial + 100 per department
    assert result.pushdown_query_count == 1
    assert result.department_count == 100
    assert result.employee_count == 5_000
    assert result.records_match is True
    assert result.speedup_factor > 1.0
    assert len(result.sample_records) == 3


def test_linux_user_audit_scripts_in_tempdir() -> None:
    """Verify Megan's Rule shell scripts across baseline, idempotent, empty file, and change runs.

    Runs list_users.sh and check_user_changes.sh in a clean environment (env -i)
    simulating non-interactive cron execution.
    """
    repo_root = Path(__file__).resolve().parent.parent
    scripts_dir = repo_root / "labs" / "linux_user_audit_container"
    list_users_orig = scripts_dir / "list_users.sh"
    check_user_changes_orig = scripts_dir / "check_user_changes.sh"

    assert list_users_orig.exists()
    assert check_user_changes_orig.exists()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        mock_list_script = tmp_path / "mock_list_users.sh"
        current_users_file = tmp_path / "current_users"
        current_users_snapshot_file = tmp_path / "current_users.snapshot"
        user_changes_file = tmp_path / "user_changes"
        user_changes_diff_file = tmp_path / "user_changes.diff.log"

        # 1. Initial user state
        mock_list_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'appuser:/home/appuser\\nnobody:/nonexistent\\n'\n"
        )
        mock_list_script.chmod(mock_list_script.stat().st_mode | stat.S_IEXEC)

        cron_env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LIST_USERS_SCRIPT": str(mock_list_script),
            "CURRENT_USERS_FILE": str(current_users_file),
            "CURRENT_USERS_SNAPSHOT_FILE": str(current_users_snapshot_file),
            "USER_CHANGES_FILE": str(user_changes_file),
            "USER_CHANGES_DIFF_FILE": str(user_changes_diff_file),
        }

        # Run 1: Initial Baseline
        proc1 = subprocess.run(
            ["bash", str(check_user_changes_orig)],
            env=cron_env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc1.returncode == 0
        assert current_users_file.exists()
        initial_hash = current_users_file.read_text().strip()
        assert len(initial_hash) == 32
        assert not user_changes_file.exists()

        # Run 2: Idempotent no-change
        proc2 = subprocess.run(
            ["bash", str(check_user_changes_orig)],
            env=cron_env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc2.returncode == 0
        assert current_users_file.read_text().strip() == initial_hash
        assert not user_changes_file.exists()

        # Edge Case: Empty file (0 bytes) recovers cleanly without logging false change
        current_users_file.write_text("")
        assert current_users_file.stat().st_size == 0
        proc_empty = subprocess.run(
            ["bash", str(check_user_changes_orig)],
            env=cron_env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc_empty.returncode == 0
        assert current_users_file.read_text().strip() == initial_hash
        assert not user_changes_file.exists()

        # Edge Case: Whitespace-only file (spaces/newlines) recovers cleanly as Run 1
        current_users_file.write_text("   \n\t  \n  ")
        assert current_users_file.stat().st_size > 0
        proc_ws = subprocess.run(
            ["bash", str(check_user_changes_orig)],
            env=cron_env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc_ws.returncode == 0
        assert current_users_file.read_text().strip() == initial_hash
        assert not user_changes_file.exists()

        # Run 3: User added
        mock_list_script.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'appuser:/home/appuser\\nsupport_test:/home/support_test\\nnobody:/nonexistent\\n'\n"
        )
        proc3 = subprocess.run(
            ["bash", str(check_user_changes_orig)],
            env=cron_env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc3.returncode == 0
        new_hash = current_users_file.read_text().strip()
        assert new_hash != initial_hash
        assert user_changes_file.exists()
        changes_content = user_changes_file.read_text()
        assert "changes occurred" in changes_content
        assert user_changes_diff_file.exists()
        diff_content = user_changes_diff_file.read_text()
        assert "+support_test:/home/support_test" in diff_content


def test_pg_advisor_zero_ram_guard() -> None:
    """Verify PgAdvisor handles total_ram_mb <= 0 without ZeroDivisionError."""
    advisor = PgAdvisor()
    telemetry = MemoryTelemetry(
        total_ram_mb=0.0,
        cpu_cores=1,
        shared_buffers_mb=128.0,
        mvc_worker_count=2,
        mvc_worker_ram_mb=64.0,
        os_base_ram_mb=512.0,
    )
    result = advisor.audit_memory_split(telemetry)
    assert result.total_ram_mb == 0.0
    assert result.page_cache_pct == 0.0
    assert result.is_page_cache_starved is True


def test_table_telemetry_negative_tuple_guard() -> None:
    """Verify TableTelemetryRow handles negative live/dead tuples safely."""
    row1 = TableTelemetryRow(
        schemaname="public",
        relname="test_neg",
        n_live_tup=-10,
        n_dead_tup=-5,
    )
    assert row1.dead_tuple_ratio == 0.0

    row2 = TableTelemetryRow(
        schemaname="public",
        relname="test_part_neg",
        n_live_tup=-5,
        n_dead_tup=10,
    )
    assert row2.dead_tuple_ratio == 1.0

    row3 = TableTelemetryRow(
        schemaname="public",
        relname="test_zero",
        n_live_tup=0,
        n_dead_tup=0,
    )
    assert row3.dead_tuple_ratio == 0.0


def test_log_correlator_extended_timestamp_formats_and_precision() -> None:
    """Verify comma fractional seconds, space-separated timezone offsets, and millisecond formatting."""
    correlator = LogCorrelator(default_year=2026)

    # Comma fractional seconds: 2026-11-10 01:15:30,123Z
    line_comma = "2026-11-10 01:15:30,123Z [app] INFO job started"
    p1 = correlator.parse_timestamp(line_comma)
    assert p1 is not None
    dt1, msg1 = p1
    assert dt1 == datetime(2026, 11, 10, 1, 15, 30, 123000, tzinfo=timezone.utc)
    assert msg1 == "[app] INFO job started"

    # Space-separated -0500 offset: 2026-11-10 01:15:30 -0500
    line_offset = "2026-11-10 01:15:30 -0500 [worker] WARN retry attempt"
    p2 = correlator.parse_timestamp(line_offset)
    assert p2 is not None
    dt2, msg2 = p2
    # 01:15:30 -0500 is 06:15:30 UTC
    assert dt2 == datetime(2026, 11, 10, 6, 15, 30, tzinfo=timezone.utc)
    assert msg2 == "[worker] WARN retry attempt"

    # LogEntry.iso_timestamp preserves millisecond precision
    entry = correlator.ingest_line("app", line_comma)
    assert entry is not None
    assert entry.iso_timestamp == "2026-11-10T01:15:30.123Z"


def test_log_correlator_hyphenated_hostname_level_priority() -> None:
    """Verify hyphenated hostname log-server01 is not misclassified as LOG, prioritizing ERROR."""
    correlator = LogCorrelator(default_year=2026)
    line = "2026-11-10T01:15:30Z log-server01 kernel: [12345] ERROR: deadlock detected"
    entry = correlator.ingest_line("sys", line)
    assert entry is not None
    assert entry.level == "ERROR"


def test_log_correlator_sparse_bucket_memory_guard() -> None:
    """Verify clock-skewed logs (1970 to 2026) do not allocate millions of empty buckets."""
    correlator = LogCorrelator(default_year=2026)
    correlator.ingest_line("sys", "1970-01-01T00:00:00Z WARN clock epoch reset")
    correlator.ingest_line("sys", "2026-11-10T01:00:00Z ERROR normal service error")

    buckets = correlator.slice_heartbeat_buckets()
    # Materializes only the 2 active windows without 5+ million intermediate empty buckets
    assert len(buckets) == 2
    assert buckets[0].window_label == "1970-01-01T00:00:00Z"
    assert buckets[1].window_label == "2026-11-10T01:00:00Z"


def test_log_correlator_min_error_threshold_zero_guard() -> None:
    """Verify min_error_threshold <= 0 is clamped to 1 and does not report a 0-error window as a spike."""
    correlator = LogCorrelator(default_year=2026)
    correlator.ingest_line("web", "2026-11-10T01:00:00Z INFO HTTP 200 OK")
    correlator.ingest_line("web", "2026-11-10T01:01:00Z INFO HTTP 200 OK")

    spike = correlator.find_earliest_spike(min_error_threshold=0)
    assert spike is None


def test_sql_pushdown_benchmark_negative_and_zero_guards() -> None:
    """Verify run_sql_pushdown_benchmark clamps non-positive department/employee counts."""
    res_zero = run_sql_pushdown_benchmark(department_count=0, employees_per_department=0)
    assert res_zero.department_count == 0
    assert res_zero.employee_count == 0
    assert res_zero.records_match is True

    res_neg = run_sql_pushdown_benchmark(department_count=-5, employees_per_department=-10)
    assert res_neg.department_count == 0
    assert res_neg.employee_count == 0
    assert res_neg.records_match is True


def test_list_users_script_execution() -> None:
    """Verify direct execution of list_users.sh script output format (user:homedir)."""
    repo_root = Path(__file__).resolve().parent.parent
    list_users_path = repo_root / "labs" / "linux_user_audit_container" / "list_users.sh"
    assert list_users_path.exists()

    proc = subprocess.run(
        ["bash", str(list_users_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.returncode == 0
    lines = [line.strip() for line in proc.stdout.strip().splitlines() if line.strip()]
    assert len(lines) > 0
    for line in lines:
        parts = line.split(":")
        assert len(parts) >= 2, f"Line '{line}' not in user:homedir format"


def test_repository_no_pii_or_path_bleed() -> None:
    """Verify tracked repository files contain no personal phone numbers, customer names, or local workstation paths."""
    import re

    repo_root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_files = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert len(tracked_files) > 0

    banned_phone = re.compile(r"\b\d{3}[-.]\d{3}[-.]\d{4}\b")
    banned_customers = re.compile(
        r"\b(cisco|citigroup|netapp|comcast|broadcom|synopsys|black\s*duck|tebuco)\b",
        re.IGNORECASE,
    )
    banned_paths = re.compile(
        r"(/home/(?!appuser\b|support_test\b|gitlab_support\b|corpuser_|user\b|developer\b)[a-zA-Z0-9_-]+|/dev/shm|antigravity-cli/brain)"
    )

    for rel_path in tracked_files:
        if rel_path == "tests/test_support_playbook.py":
            continue
        content = (repo_root / rel_path).read_text(encoding="utf-8", errors="ignore")
        assert banned_phone.search(content) is None, f"Phone number found in {rel_path}"
        assert banned_customers.search(content) is None, f"Customer/internal name found in {rel_path}"
        assert banned_paths.search(content) is None, f"Local workstation path found in {rel_path}"

