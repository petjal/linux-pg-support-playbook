# Linux, PostgreSQL & Container Support Field Playbook

**Author:** Emil Walter "Pete" Jalajas  
**Links:** https://linkedin.com/in/ewpj | https://github.com/petjal  
**Repository:** https://github.com/petjal/linux-pg-support-playbook  

---

## 1. Overview and Why This Repository Exists

In enterprise technical support and designated systems engineering, production outages rarely stem from esoteric theoretical bugs. They almost always result from mundane, compounding operational friction:

- Single-box memory misallocations where application worker processes starve the Linux OS page cache (`buff/cache`), forcing active database pages onto swap disk.
- Zero-scan database indexes silently imposing a write-amplification penalty on every `INSERT`, `DELETE`, and non-HOT `UPDATE` while polluting shared memory buffers.
- Default relational database autovacuum thresholds (`0.20`) that fail to trigger on million-row tables until hundreds of thousands of dead tuples have already degraded table scans.
- Write-ahead logs (`pg_wal`) mounted across network filesystems (`nfs`), turning sub-millisecond local `fsync` flushes into high-latency blocking network calls.
- MVC application architectures repeatedly executing `N+1` round-trip query loops and in-memory application filtering instead of pushing aggregation into the relational database engine.

This repository codifies 16 years of enterprise Designated Support Engineering experience diagnosing large-scale production escalations across Linux servers, PostgreSQL clusters, and containerized deployments. The field triage principles, database tuning patterns, and escalation lessons originate from hands-on production support experience, with the Python 3.11+ standard-library tools and automated test suite synthesized and verified via human-directed AI coding agents.

---

## 2. Five Core Field Triage Principles

### Principle 1: Megan's Rule (Container-Verified Scripts)
> *"Never give a customer a script that you haven't run successfully yourself."*

When an enterprise customer system is degraded, handing them an untested shell snippet or diagnostic script is unacceptable. A script that fails due to missing PATH definitions, unhandled `set -e` traps, or subtle environment differences damages customer trust and burns critical downtime.

Under Megan's Rule, every customer-facing diagnostic script must first run cleanly inside an isolated reference container (e.g., Debian or Alpine) simulating the target environment. The `labs/linux_user_audit_container/` directory demonstrates this practice with an automated, container-verified user audit daemon running under non-interactive cron environments (`env -i`).

### Principle 2: Look Left on the Timeline and Check Internal Prior Art
Customer escalation reports almost always focus on the end symptom: *"Web portal threw HTTP 504 Gateway Timeout at 01:15 UTC."* 

Triage engineers who inspect only the 01:15 UTC window waste hours analyzing web proxy logs. The actual root trigger occurred minutes earlier:
1. At 01:05 UTC, a background batch job acquired an exclusive table lock in the database.
2. At 01:08 UTC, application database connection pools saturated waiting on the lock.
3. At 01:15 UTC, the front-end web tier ran out of available backend worker sockets.

**Rule:** Always look left on the timeline to locate the initial inflection point. Before diving into novel troubleshooting, search internal team ticket history and known-error databases for curated error strings. Most "new" production fires are recurring variants of previously solved edge cases.

### Principle 3: Bottom-Up Linux and Network Survey
Before suspecting application logic, establish ground truth at the operating system, storage, and networking layers:

- **Per-Core CPU Wait (`top` -> press `1`):** High aggregate `%cpu` can mask single-core saturation, while elevated `%wa` (I/O wait) indicates storage bottlenecks rather than compute exhaustion.
- **Memory Split and Page Cache (`free -m`):** PostgreSQL relies on double buffering: shared buffers in database memory and cached relation pages in the Linux OS page cache (`buff/cache`). If application processes starve the page cache below 25% of total system RAM, read latency climbs dramatically.
- **Swap Activity (`vmstat 1` or `sar -W`):** Swap is a scourge on database and application servers. When memory pressure forces active database buffer pages or application workers into swap disk (`swap_in_kb > 0` or `swap_out_kb > 0`), query latencies surge from microseconds to hundreds of milliseconds.
- **Disk Saturation & SSD Wear Leveling (`iostat -xz 1`, `smartctl`, `fstrim`):** Check `%util`, `r_await`, and `w_await`. Sustained `%util > 85%` indicates disk queue saturation, while elevated `w_await` on an SSD with ample free space often points to internal SSD wear-leveling garbage collection, missing periodic `fstrim` (`systemctl status fstrim.timer`), or exhausted cloud block-storage IOPS burst credits.
- **Filesystem Capacity & Mount Types (`df -Th` & `df -i`):** Check both byte usage, inode exhaustion (`df -i`), and mount filesystem types (confirming database directories are not silently mounted on `nfs`).
- **Network Throughput (`iftop -t`):** Run `iftop -t` (text/log mode) to identify saturated interfaces, asymmetric traffic flows, or workers hanging on synchronous external endpoints.

### Principle 4: The 4-Tier Log Sweep ("Malpractice Insurance")
When triaging complex multi-tier incidents, pulling logs from only one layer leads to flawed diagnoses. A complete diagnostic snapshot requires collecting and interlacing four synchronized tiers:

1. **Client / Edge Tier:** Browser network waterfalls, API gateway status codes, and HTTP client connection timeouts.
2. **Application Tier:** Application server logs, uncaught exceptions, and database pool connection wait times.
3. **Database Tier:** PostgreSQL error logs (`log_min_duration_statement`, lock timeouts, autovacuum completion logs, and deadlocks).
4. **Operating System Tier:** `/var/log/messages`, `dmesg -T` (OOM-killer invocations, filesystem driver warnings), and `journalctl -u <service>`.

### Principle 5: Putting AI Directly on the Box ("One-Hoss Shay" / `tbintdb` Replica Lab & Case Queue Automation)
Modern AI models excel at synthesizing patterns across dense, multi-service log dumps and live server telemetry when given deterministic, UTC-normalized inputs:
- **Strict UTC/Zulu (`Z`) Sub-Second Timestamps:** Mismatched local timezones are the bane of distributed systems support. Normalize all disparate log streams and audit scripts to **ISO-8601 UTC (`date -u` with trailing `Z`)** with **millisecond (`.%3NZ`)** or **nanosecond (`.%9NZ`)** precision so sub-second cross-container events order deterministically.
- **Chronological Interlacing & 5-Minute Heartbeats (`HEARTBEAT_5M`):** Interlace logs across all four tiers and emit synthetic 5-minute UTC heartbeat boundaries summarizing error and warning counts to point the AI directly at the earliest anomaly spike ("Look Left").
- **The Terabyte-Scale "AI-on-the-Box" Replica Lab (`tbintdb` / "One-Hoss Shay"):** Rather than guessing at customer bottlenecks from static text tickets, stand up a production-scale lab server (`tbintdb`, an internal benchmark project hosted on our self-managed GitLab instance and backed by a 1.1 TB to 1.25 TB database replica populated with complex, high-scale repositories) and place an AI diagnostic agent directly on the box with read access to live Linux telemetry, container cgroups, PostgreSQL catalogs, and product source code.
- **End-to-End Case Queue Velocity (`2X` Capacity):** Pairing the lab replica with AI case-queue automation (prioritizing incoming tickets by SLA/MBO metrics, pulling case attachments, cross-searching historical tickets, KnowledgeBase articles, Jira, Confluence, and source code, reproducing on the lab VM, and filing engineering defect tickets with exact source filenames and line numbers) transforms support throughput while uncovering deep architectural fixes.

---

## 3. Four PostgreSQL and MVC Architecture Bottlenecks

### 1. Unused Index Write-Amplification and Cache Pollution
Every index defined on a PostgreSQL table carries an operational maintenance tax:
- Every `INSERT` must write entries to every index on the table.
- Every `DELETE` must remove entries from every index on the table.
- Every `UPDATE` modifying indexed columns (or non-HOT updates) must update index trees.

When an index has `idx_scan = 0` in `pg_stat_user_indexes`, it provides zero query read benefit while continuously:
1. Dirtying shared buffer pages and consuming write-ahead log (WAL) bandwidth.
2. Evicting active table and index blocks from shared buffers and OS page cache.
3. Inflating database backup times and storage costs.

**Remediation:** Identify zero-scan indexes and drop them safely using `DROP INDEX CONCURRENTLY <schemaname>.<indexname>;`.

### 2. High-Churn Table Autovacuum Starvation
PostgreSQL's default global vacuum settings:
```ini
autovacuum_vacuum_scale_factor = 0.20
autovacuum_analyze_scale_factor = 0.10
```
On a small table of 1,000 rows, autovacuum triggers after 200 dead tuples. But on an enterprise transaction table with 2,500,000 live rows, a 20% scale factor requires **500,000 dead rows** before autovacuum initiates cleanup.

During high-churn workloads, this delay causes table bloat, table scan degradation, and index bloat.

**Remediation:** Apply table-specific autovacuum tuning to high-volume tables:
```sql
ALTER TABLE public.transaction_logs SET (
    autovacuum_vacuum_scale_factor = 0.01,
    autovacuum_analyze_scale_factor = 0.005
);
```
This forces autovacuum to run incrementally at 1% tuple churn (25,000 dead tuples instead of 500,000), preventing table bloat.

### 3. NFS Storage Mount Traps and Jumbo Frames
Two frequent infrastructure configuration traps in enterprise database deployments:

1. **`pg_wal` or `PGDATA` on Network Filesystems (`nfs`/`nfs4`):**
   PostgreSQL relies on synchronous, deterministic `fsync` flushes to ensure write-ahead log entries are safely committed before reporting success to clients. Mounting `pg_wal` over NFS introduces network jitter, locking hazards, and potential corruption during network splits. Database storage and WAL must reside on local high-performance NVMe/SSD or dedicated block volumes.
2. **Standard 1500 MTU on Dedicated Storage Networks:**
   Enterprise storage networks carrying SAN/NAS iSCSI or NFS traffic should be configured with 9000 MTU (Jumbo Frames) end-to-end. Standard 1500 MTU forces 6x more packet framing, increasing CPU interrupt load during bulk database writes and checkpoint syncs.

### 4. Pushing Data Closer to Code (SQL Pushdown, Materialized Views & Local Cache Containers)
A common anti-pattern in MVC web frameworks is retrieving parent entities in one query, then looping through children with `N` separate database queries while performing joins, filtering, and aggregation in application memory, or making synchronous external HTTP/API lookups during the web request cycle.

Three architectural patterns resolve these bottlenecks on constrained servers:
1. **Relational SQL Pushdown (Dimitri Fontaine's *The Art of PostgreSQL*):** Push filtering, joins, and aggregations directly into SQL so the database returns only the final computed rows rather than thousands of raw ORM objects.
2. **PostgreSQL Materialized Views (`CREATE MATERIALIZED VIEW` / `REFRESH MATERIALIZED VIEW CONCURRENTLY`):** Pre-compute expensive multi-table aggregations and index the materialized view so dashboard and report pages load in milliseconds.
3. **Local Cache Containers for External Lookups:** When an application repeatedly queries a slow external metadata or knowledge-base service during request processing, deploying a lightweight local cache container eliminates synchronous external network latency.

#### Verified Benchmark Results (`src/support_playbook/sql_pushdown_demo.py`):
- **Audited Dataset:** 100 Departments, 5,000 Employees.
- **Pattern A (MVC N+1 Queries + App-Side Filtering):** 101 queries executed in **7.099 ms**.
- **Pattern B (Relational SQL Pushdown):** 1 query executed in **1.731 ms**.
- **Measured Speedup:** **4.1x faster execution** with 100% data verification match.

#### Verified Containerized Single-Box Sabotage & Autonomous AI Triage PoC (`labs/single_box_mvc_sabotage_poc/run_sabotage_and_heal_lab.py`):
- **Container Environment:** `--cpus="2.0" --memory="2g" --cap-add=NET_ADMIN` on `postgres:17-alpine` (`PostgreSQL 17.11` + `iproute2-tc`).
- **Sabotaged State (4 Compounding Faults):** `tc qdisc add dev lo root netem delay 5ms` on TCP loopback + 41-query MVC `N+1` loop + `60.0%` dead-tuple bloat (`60,000` dead rows with `autovacuum_enabled = false`) + 2 zero-scan redundant indexes (`idx_scan = 0`) -> **2,808.59 ms** (`1,425` buffers per query = `57,000` buffer hits).
- **Post-Remediation State (Autonomous AI Diagnosis & Fix):** Removed `tc-netem` delay, switched to Unix domain socket, rewrote 41-query `N+1` loop to single SQL `JOIN` pushdown, created partial covering index `idx_sbom_proj_unarchived_sev` (`Index Only Scan`, `Heap Fetches: 0`), dropped zero-scan indexes, and ran `VACUUM (ANALYZE)` with `autovacuum_vacuum_scale_factor = 0.01` -> **12.63 ms** (`1` query, `92` shared buffer hits, **222.4x speedup**).

---

## 4. Live GitLab Source-Code Triage Demonstration (November 9, 2026)

To demonstrate how human-directed AI source-code and database schema correlation works on real-world problems in a codebase I had never worked in prior to this assessment, `docs/LIVE_GITLAB_ISSUE_TRIAGE_2026_11.md` documents the live root-cause triage, immediate self-managed customer workarounds, and upstream patches for three currently open/unanswered GitLab problems found on `gitlab.com` and `forum.gitlab.com`:

1. **Case 1 (Hard - PostgreSQL Constraint & CE/EE Packaging Split, `gitlab-org/gitlab#638577`, opened Nov 5, 2026, 0 comments):** Isolated why deleting a user who linked a child work item silently hangs forever (`PG::NotNullViolation` on `resource_link_events.user_id`). Traced the failure to a self-contradictory PostgreSQL schema (`user_id bigint NOT NULL` paired with `CONSTRAINT fk_bd4ae15ce4 FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL` in `db/migrate/20230329085754_add_foreign_key_to_resource_link_events_on_user.rb`) combined with `migrate_resource_link_events` living inside the EE-only override (`ee/app/services/ee/users/migrate_records_to_ghost_user_service.rb`) while `WorkItems::ResourceLinkEvent` lives in CE (`app/models/work_items/resource_link_event.rb`).
2. **Case 2 (Medium - Self-Managed Cryptography & Rake Task Scope Mismatch, `forum.gitlab.com/t/135881`, posted Oct 29, 2026, 0 replies):** Isolated why `gitlab-rake gitlab:two_factor:rotate_key:apply` leaves `gitlab:doctor:secrets` failing on users with 2FA disabled (`lib/gitlab/otp_key_rotator.rb` line 39 scopes to `User.with_two_factor` instead of `User.where.not(encrypted_otp_secret: nil)`, skipping users who generated a pre-enrollment `encrypted_otp_secret` on `/profile/two_factor_auth` in `app/controllers/profiles/two_factor_auths_controller.rb` line 145).
3. **Case 3 (Medium - Enterprise Ultimate Security Policy Hierarchy Truncation, `forum.gitlab.com/t/135642`, posted Oct 12, 2026, 0 replies):** Isolated why a 6th inherited Pipeline Execution Policy (`inject_policy`) silently disappears across group hierarchies even after raising the 18.3 Admin UI limit (`ee/app/services/security/security_orchestration_policies/limit_service.rb` lines 44-46 hardcodes `:pipeline => 5` without reading `CurrentSettings`, and `ee/lib/gitlab/security/orchestration/project_pipeline_execution_policies.rb` lines 20-22 silently slices `.first(5)` before reversing hierarchy precedence).

See `docs/LIVE_GITLAB_ISSUE_TRIAGE_2026_11.md` for the full technical write-up, file/line citations, `gitlab-rails console` workarounds, and upstream patches.

---

## 5. Repository Structure

```
linux-pg-support-playbook/
├── pyproject.toml                         # Project metadata, linting and type configuration
├── LICENSE                                # MIT License (c) 2026 Emil Walter "Pete" Jalajas
├── README.md                              # Operational field triage playbook and documentation
├── docs/
│   └── LIVE_GITLAB_ISSUE_TRIAGE_2026_11.md # Live root-cause triage of 3 open GitLab issues (Nov 2026)
├── sql/
│   └── pg_field_diagnostics.sql           # Strictly read-only PostgreSQL snapshot inspection script
├── src/
│   └── support_playbook/
│       ├── __init__.py                    # Public API exports and lazy attribute routing
│       ├── log_correlator.py              # Multi-service log normalizer and 5m heartbeat slicer
│       ├── pg_advisor.py                  # Linux and PostgreSQL configuration and telemetry advisor
│       └── sql_pushdown_demo.py           # Runnable SQLite benchmark comparing N+1 vs. SQL pushdown
├── labs/
│   ├── linux_user_audit_container/
│   │   ├── Dockerfile                     # Megan's Rule reference verification container (Q1 & Q3)
│   │   ├── list_users.sh                  # Portable Linux user audit script
│   │   └── check_user_changes.sh          # Idempotent MD5 change detection + unified diff logger
│   └── single_box_mvc_sabotage_poc/
│       └── run_sabotage_and_heal_lab.py   # Live PostgreSQL 17 + tc-netem sabotage & AI triage PoC
└── tests/
    └── test_support_playbook.py           # Comprehensive pytest suite verifying all components
```

---

## 6. Quickstart and Verification Commands

### 1. Install Development Dependencies
```bash
python3 -m pip install pytest mypy ruff
```

### 2. Run Test Suite
```bash
PYTHONPATH=src pytest -v
```
Verified output:
```
============================== test session starts ==============================
collected 18 items

tests/test_support_playbook.py::test_log_correlator_timestamp_parsing PASSED [  5%]
tests/test_support_playbook.py::test_log_correlator_normalization_and_bucketing PASSED [ 11%]
tests/test_support_playbook.py::test_log_correlator_spike_detection_look_left PASSED [ 16%]
tests/test_support_playbook.py::test_pg_advisor_memory_split_and_swap PASSED [ 22%]
tests/test_support_playbook.py::test_pg_advisor_unused_index_detection PASSED [ 27%]
tests/test_support_playbook.py::test_pg_advisor_autovacuum_tuning PASSED [ 33%]
tests/test_support_playbook.py::test_pg_advisor_storage_and_network_traps PASSED [ 38%]
tests/test_support_playbook.py::test_sql_pushdown_benchmark PASSED       [ 44%]
tests/test_support_playbook.py::test_linux_user_audit_scripts_in_tempdir PASSED [ 50%]
tests/test_support_playbook.py::test_pg_advisor_zero_ram_guard PASSED    [ 55%]
tests/test_support_playbook.py::test_table_telemetry_negative_tuple_guard PASSED [ 61%]
tests/test_support_playbook.py::test_log_correlator_extended_timestamp_formats_and_precision PASSED [ 66%]
tests/test_support_playbook.py::test_log_correlator_hyphenated_hostname_level_priority PASSED [ 72%]
tests/test_support_playbook.py::test_log_correlator_sparse_bucket_memory_guard PASSED [ 77%]
tests/test_support_playbook.py::test_log_correlator_min_error_threshold_zero_guard PASSED [ 83%]
tests/test_support_playbook.py::test_sql_pushdown_benchmark_negative_and_zero_guards PASSED [ 88%]
tests/test_support_playbook.py::test_list_users_script_execution PASSED  [ 94%]
tests/test_support_playbook.py::test_repository_no_pii_or_path_bleed PASSED [100%]

============================== 18 passed in 0.16s ===============================
```

### 3. Run Strict Type Checking and Linting
```bash
PYTHONPATH=src mypy --strict src tests
ruff check src tests
```
Verified output:
```
Success: no issues found in 5 source files
All checks passed!
```

### 4. Execute SQL Pushdown Benchmark CLI
```bash
PYTHONPATH=src python3 -m support_playbook.sql_pushdown_demo
```

### 5. Execute Multi-Service Log Correlator
```bash
PYTHONPATH=src python3 -m support_playbook.log_correlator --service web /var/log/nginx/access.log --service app /var/log/app/app.log
```

---

## 7. Scope and Limitations

1. **PostgreSQL Diagnostics Script:** The queries in `sql/pg_field_diagnostics.sql` are strictly read-only (`SET default_transaction_read_only = on;`). They do not modify tables or configuration settings. In production, execute diagnostics during low-traffic windows or read replicas when investigating high lock contention.
2. **Container Lab Verification:** The reference container in `labs/linux_user_audit_container/Dockerfile` requires Docker or Podman. The test suite executes the shell logic directly using temporary directories and clean process environments (`env -i`) so that tests pass in standard CI/CD environments without container socket access.
3. **Telemetry Heuristics:** The advisors in `src/support_playbook/pg_advisor.py` codify standard production best practices for single-box and primary-standby architectures. Highly specialized distributed setups (e.g., Citus sharding or active-active multi-region replication) may require custom threshold tuning.
