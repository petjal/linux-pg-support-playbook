#!/usr/bin/env python3
"""Single-Box MVC + PostgreSQL 17 Performance Sabotage & Autonomous AI Triage PoC.

Simulates Question 2 of the GitLab Support Engineer Assessment inside a constrained
2-vCPU, 2 GB RAM container environment running PostgreSQL 17.11 + an MVC endpoint:
  1. Seeds a GitLab-style group/project SBOM dependency schema (200 projects,
     40,000 live sbom_occurrences rows + 60,000 dead tuples from unvacuumed churn).
  2. Injects 4 real system & database bottlenecks:
     - Fault A (Linux Kernel tc-netem Network Degradation): Injects a 5ms packet delay
       on loopback (`tc qdisc add dev lo root netem delay 5ms`) affecting every TCP
       database round-trip (`-h 127.0.0.1`).
     - Fault B (MVC N+1 Query Loop + Suboptimal Index, inspired by gitlab#638748):
       Executes 40 sequential per-project queries scanning 90,000 index entries and
       1,334 heap blocks per query.
     - Fault C (60% Dead Tuple Table Bloat): 60,000 dead rows with autovacuum disabled.
     - Fault D (Zero-Scan Redundant Indexes): Unused indexes causing write amplification
       and shared_buffers pollution.
  3. Runs the Autonomous AI-on-the-Box Diagnostic Sweep (`tc qdisc show`,
     `pg_stat_user_tables`, `pg_stat_user_indexes`, `EXPLAIN (ANALYZE, BUFFERS)`).
  4. Applies all 4 remediations (`tc qdisc del`, SQL JOIN pushdown, partial covering
     index for Index-Only Scans with `Heap Fetches: 0`, zero-scan index pruning, and
     `VACUUM (ANALYZE)` + `autovacuum_vacuum_scale_factor = 0.01`) and measures the
     verified speedup.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any


def run_cmd(cmd: list[str], input_text: str | None = None) -> str:
    proc = subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def run_psql(sql: str, host: str | None = "127.0.0.1") -> str:
    cmd = ["psql"]
    if host:
        cmd.extend(["-h", host])
    cmd.extend(["-U", "postgres", "-d", "gitlab_lab", "-At", "-c", sql])
    return run_cmd(cmd)


def setup_sabotaged_environment() -> bool:
    run_cmd(["psql", "-U", "postgres", "-c", "DROP DATABASE IF EXISTS gitlab_lab;"])
    run_cmd(["psql", "-U", "postgres", "-c", "CREATE DATABASE gitlab_lab;"])

    setup_sql = """
    CREATE TABLE projects (
        id integer PRIMARY KEY,
        group_id integer NOT NULL,
        name text NOT NULL
    );

    CREATE TABLE sbom_occurrences (
        id serial PRIMARY KEY,
        project_id integer NOT NULL,
        component_name text NOT NULL,
        package_manager text NOT NULL,
        highest_severity integer NOT NULL,
        archived boolean NOT NULL DEFAULT false,
        input_file_path text NOT NULL
    ) WITH (autovacuum_enabled = false);

    CREATE INDEX idx_unused_input_path_1 ON sbom_occurrences (input_file_path);
    CREATE INDEX idx_unused_pkg_mgr_2 ON sbom_occurrences (package_manager, input_file_path);
    CREATE INDEX idx_suboptimal_archived_3 ON sbom_occurrences (archived, input_file_path);

    INSERT INTO projects (id, group_id, name)
    SELECT g, (g % 5) + 1, 'project-' || g
    FROM generate_series(1, 200) AS g;

    INSERT INTO sbom_occurrences (project_id, component_name, package_manager, highest_severity, archived, input_file_path)
    SELECT
        (g % 200) + 1,
        'lib-component-' || (g % 500),
        CASE WHEN g % 3 = 0 THEN 'npm' WHEN g % 3 = 1 THEN 'maven' ELSE 'pip' END,
        (g % 5) + 1,
        (g % 10 = 0),
        '/app/services/module-' || (g % 50) || '/package-lock.json'
    FROM generate_series(1, 100000) AS g;

    DELETE FROM sbom_occurrences WHERE id > 40000;
    """
    run_cmd(["psql", "-U", "postgres", "-d", "gitlab_lab"], input_text=setup_sql)

    # Inject Linux kernel tc-netem 5ms delay on loopback if tc and NET_ADMIN are available
    netem_active = False
    if shutil.which("tc"):
        subprocess.run(["tc", "qdisc", "del", "dev", "lo", "root"], capture_output=True)
        res = subprocess.run(
            ["tc", "qdisc", "add", "dev", "lo", "root", "netem", "delay", "5ms"],
            capture_output=True,
        )
        netem_active = res.returncode == 0
    return netem_active


def mvc_endpoint_sabotaged(group_id: int = 1) -> dict[str, Any]:
    t0 = time.perf_counter()
    proj_rows = run_psql(f"SELECT id FROM projects WHERE group_id = {group_id};").splitlines()
    project_ids = [int(r) for r in proj_rows if r]

    total_critical = 0
    for pid in project_ids:
        cnt = run_psql(
            f"SELECT count(*) FROM sbom_occurrences "
            f"WHERE project_id = {pid} AND archived = false AND highest_severity = 5;"
        )
        total_critical += int(cnt or 0)

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "group_id": group_id,
        "tcp_queries_executed": len(project_ids) + 1,
        "critical_count": total_critical,
        "latency_ms": round(elapsed_ms, 2),
    }


def ai_on_the_box_diagnostic_sweep() -> dict[str, Any]:
    tc_qdisc = ""
    if shutil.which("tc"):
        tc_qdisc = run_cmd(["tc", "qdisc", "show", "dev", "lo"])

    bloat_row = run_psql(
        "SELECT relname, n_live_tup, n_dead_tup, "
        "round((n_dead_tup::numeric / NULLIF(n_live_tup + n_dead_tup, 0)) * 100, 1) "
        "FROM pg_stat_user_tables WHERE relname = 'sbom_occurrences';",
        host=None,
    )
    unused_indexes = run_psql(
        "SELECT indexrelname, idx_scan, pg_size_pretty(pg_relation_size(indexrelid)) "
        "FROM pg_stat_user_indexes "
        "WHERE relname = 'sbom_occurrences' AND idx_scan = 0 AND indexrelname NOT LIKE '%pkey%';",
        host=None,
    ).splitlines()
    explain_before = run_psql(
        "EXPLAIN (ANALYZE, BUFFERS) "
        "SELECT count(*) FROM sbom_occurrences WHERE project_id = 1 AND archived = false AND highest_severity = 5;",
        host=None,
    )
    return {
        "linux_tc_qdisc_lo": tc_qdisc,
        "table_bloat": bloat_row,
        "unused_indexes": unused_indexes,
        "explain_before": explain_before,
    }


def apply_remediations_and_verify(group_id: int = 1) -> dict[str, Any]:
    if shutil.which("tc"):
        subprocess.run(["tc", "qdisc", "del", "dev", "lo", "root"], capture_output=True)

    remediation_sql = """
    DROP INDEX IF EXISTS idx_unused_input_path_1;
    DROP INDEX IF EXISTS idx_unused_pkg_mgr_2;
    DROP INDEX IF EXISTS idx_suboptimal_archived_3;

    CREATE INDEX idx_sbom_proj_unarchived_sev
        ON sbom_occurrences (project_id, highest_severity)
        WHERE archived = false;

    ALTER TABLE sbom_occurrences SET (
        autovacuum_enabled = true,
        autovacuum_vacuum_scale_factor = 0.01,
        autovacuum_analyze_scale_factor = 0.005
    );
    VACUUM (ANALYZE) sbom_occurrences;
    ANALYZE projects;
    """
    run_cmd(["psql", "-U", "postgres", "-d", "gitlab_lab"], input_text=remediation_sql)

    explain_after = run_psql(
        "EXPLAIN (ANALYZE, BUFFERS) "
        "SELECT count(*) FROM sbom_occurrences s "
        "JOIN projects p ON p.id = s.project_id "
        "WHERE p.group_id = 1 AND s.archived = false AND s.highest_severity = 5;",
        host=None,
    )

    t0 = time.perf_counter()
    cnt_str = run_psql(
        f"SELECT count(*) FROM sbom_occurrences s "
        f"JOIN projects p ON p.id = s.project_id "
        f"WHERE p.group_id = {group_id} AND s.archived = false AND s.highest_severity = 5;"
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    bloat_after = run_psql(
        "SELECT relname, n_live_tup, n_dead_tup "
        "FROM pg_stat_user_tables WHERE relname = 'sbom_occurrences';",
        host=None,
    )

    return {
        "tcp_queries_executed": 1,
        "critical_count": int(cnt_str or 0),
        "latency_ms": round(elapsed_ms, 2),
        "bloat_after": bloat_after,
        "explain_after": explain_after,
    }


if __name__ == "__main__":
    setup_sabotaged_environment()
    before_metrics = mvc_endpoint_sabotaged()
    diagnostics = ai_on_the_box_diagnostic_sweep()
    after_metrics = apply_remediations_and_verify()
    speedup = round(before_metrics["latency_ms"] / max(after_metrics["latency_ms"], 0.01), 1)
    report = {
        "before_sabotaged": before_metrics,
        "ai_diagnostics": diagnostics,
        "after_remediated": after_metrics,
        "speedup_factor": f"{speedup}x",
    }
    print(json.dumps(report, indent=2))
