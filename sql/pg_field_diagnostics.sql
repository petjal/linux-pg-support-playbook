-- ============================================================================
-- Linux, PostgreSQL & Container Support Field Playbook
-- PostgreSQL Field Diagnostics (Strictly Read-Only Snapshot, PG 12-17+)
-- Author: Emil Walter "Pete" Jalajas
-- ============================================================================
-- Strictly enforce error-stop and read-only session mode to guarantee zero mutation
\set ON_ERROR_STOP on
SET default_transaction_read_only = on;

-- ----------------------------------------------------------------------------
-- Section 1: Active, Long-Running, and Blocked Queries (pg_stat_activity)
-- Uses pg_blocking_pids() kernel lock-wait graph to prevent false waiter-waiter
-- cross-joins and Cartesian lock-mode duplicates.
-- ----------------------------------------------------------------------------
SELECT DISTINCT ON (blocked_locks.pid, blocking_locks.pid)
    blocked_locks.pid AS blocked_pid,
    blocked_activity.usename AS blocked_user,
    blocked_activity.client_addr AS blocked_client,
    now() - blocked_activity.xact_start AS blocked_xact_duration,
    now() - blocked_activity.query_start AS blocked_query_duration,
    blocked_locks.mode AS blocked_lock_mode,
    blocking_locks.pid AS blocking_pid,
    blocking_activity.usename AS blocking_user,
    blocking_activity.client_addr AS blocking_client,
    blocking_activity.state AS blocking_state,
    now() - blocking_activity.xact_start AS blocking_xact_duration,
    now() - blocking_activity.query_start AS blocking_query_duration,
    blocking_locks.mode AS blocking_lock_mode,
    blocked_activity.query AS blocked_statement,
    blocking_activity.query AS blocking_statement
FROM pg_catalog.pg_locks blocked_locks
JOIN pg_catalog.pg_stat_activity blocked_activity
    ON blocked_activity.pid = blocked_locks.pid
JOIN pg_catalog.pg_locks blocking_locks
    ON blocking_locks.pid = ANY(pg_catalog.pg_blocking_pids(blocked_activity.pid))
    AND blocking_locks.granted
    AND blocking_locks.locktype = blocked_locks.locktype
    AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
    AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
    AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
    AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
    AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
    AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
    AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
    AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
    AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
JOIN pg_catalog.pg_stat_activity blocking_activity
    ON blocking_activity.pid = blocking_locks.pid
WHERE NOT blocked_locks.granted
ORDER BY blocked_locks.pid, blocking_locks.pid;

-- Active queries or open transactions running longer than 10 seconds
SELECT
    pid,
    usename,
    client_addr,
    state,
    wait_event_type,
    wait_event,
    now() - xact_start AS xact_duration,
    now() - query_start AS duration,
    query
FROM pg_catalog.pg_stat_activity
WHERE state != 'idle'
  AND pid != pg_catalog.pg_backend_pid()
  AND (
      (now() - query_start) > interval '10 seconds'
      OR (xact_start IS NOT NULL AND (now() - xact_start) > interval '10 seconds')
  )
ORDER BY COALESCE(now() - xact_start, now() - query_start) DESC
LIMIT 25;

-- ----------------------------------------------------------------------------
-- Section 2: Top Slow Queries, Planner Inversion & Temp Spills (pg_stat_statements)
-- Guards execution via \gset + \if so psql never fails when pg_stat_statements
-- is either uninstalled or awaiting a PostgreSQL restart for shared_preload_libraries.
-- ----------------------------------------------------------------------------
SELECT (
    EXISTS (SELECT 1 FROM pg_catalog.pg_extension WHERE extname = 'pg_stat_statements')
    AND position('pg_stat_statements' in current_setting('shared_preload_libraries')) > 0
    AND current_setting('server_version_num')::int >= 130000
) AS has_pgss_ready \gset

\if :has_pgss_ready
SELECT
    queryid,
    calls,
    round(total_exec_time::numeric, 2) AS total_exec_time_ms,
    round(mean_exec_time::numeric, 3) AS mean_exec_time_ms,
    round(stddev_exec_time::numeric, 3) AS stddev_exec_time_ms,
    round(total_plan_time::numeric, 2) AS total_plan_time_ms,
    round(mean_plan_time::numeric, 3) AS mean_plan_time_ms,
    rows,
    shared_blks_hit,
    shared_blks_read,
    temp_blks_read,
    temp_blks_written,
    round((100.0 * shared_blks_hit / nullif(shared_blks_hit + shared_blks_read, 0))::numeric, 2) AS cache_hit_pct,
    substr(query, 1, 150) AS query_snippet
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
\else
SELECT
    'pg_stat_statements is not installed or shared_preload_libraries restart is pending' AS pg_stat_statements_status;
\endif

-- ----------------------------------------------------------------------------
-- Section 3: Unused Zero-Scan Indexes (pg_stat_user_indexes)
-- Detects write-amplification and query-planner tax: indexes that slow down
-- INSERT/UPDATE/DELETE and query planning without servicing SELECT queries.
-- Excludes primary keys, unique/exclusion constraints, replica identity indexes,
-- partitioned parent indexes (relkind = 'I'), and attached partition child
-- indexes (pg_inherits) where standalone DROP INDEX CONCURRENTLY is invalid.
-- NOTE: Always sample across a full 30-day production cycle across primary and
-- all read replicas before executing DROP INDEX CONCURRENTLY.
-- ----------------------------------------------------------------------------
SELECT
    s.schemaname,
    s.relname AS table_name,
    s.indexrelname AS index_name,
    s.idx_scan,
    s.idx_tup_read,
    s.idx_tup_fetch,
    pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size,
    pg_relation_size(s.indexrelid) AS index_bytes,
    'DROP INDEX CONCURRENTLY ' || quote_ident(s.schemaname) || '.' || quote_ident(s.indexrelname) || ';' AS remediation_sql
FROM pg_catalog.pg_stat_user_indexes s
JOIN pg_catalog.pg_index idx ON idx.indexrelid = s.indexrelid
JOIN pg_catalog.pg_class ic ON ic.oid = s.indexrelid
WHERE s.idx_scan = 0
  AND ic.relkind = 'i'
  AND NOT idx.indisunique
  AND NOT idx.indisprimary
  AND NOT idx.indisexclusion
  AND NOT idx.indisreplident
  AND NOT EXISTS (
      SELECT 1 FROM pg_catalog.pg_constraint con
      WHERE con.conindid = s.indexrelid
  )
  AND NOT EXISTS (
      SELECT 1 FROM pg_catalog.pg_inherits inh
      WHERE inh.inhrelid = s.indexrelid
  )
ORDER BY pg_relation_size(s.indexrelid) DESC
LIMIT 30;

-- ----------------------------------------------------------------------------
-- Section 4: Dead-Tuple Bloat & Effective Autovacuum Thresholds (pg_stat_user_tables)
-- Filters to physical heap tables and leaf partitions (relkind = 'r') because
-- declarative parent tables (relkind = 'p') cannot store autovacuum reloptions.
-- Computes each table's effective autovacuum trigger threshold.
-- ----------------------------------------------------------------------------
SELECT
    u.schemaname,
    u.relname AS table_name,
    u.n_live_tup,
    u.n_dead_tup,
    round(100.0 * u.n_dead_tup / nullif(u.n_live_tup + u.n_dead_tup, 0), 2) AS dead_tuple_ratio_pct,
    COALESCE(
        (SELECT split_part(opt, '=', 2)::numeric FROM unnest(c.reloptions) AS opt WHERE opt LIKE 'autovacuum_vacuum_scale_factor=%'),
        current_setting('autovacuum_vacuum_scale_factor')::numeric
    ) AS effective_vacuum_scale_factor,
    round(
        COALESCE(
            (SELECT split_part(opt, '=', 2)::numeric FROM unnest(c.reloptions) AS opt WHERE opt LIKE 'autovacuum_vacuum_threshold=%'),
            current_setting('autovacuum_vacuum_threshold')::numeric
        )
        + COALESCE(
            (SELECT split_part(opt, '=', 2)::numeric FROM unnest(c.reloptions) AS opt WHERE opt LIKE 'autovacuum_vacuum_scale_factor=%'),
            current_setting('autovacuum_vacuum_scale_factor')::numeric
        ) * u.n_live_tup
    ) AS vacuum_trigger_threshold,
    u.last_vacuum,
    u.last_autovacuum,
    u.last_analyze,
    u.last_autoanalyze,
    c.reloptions AS table_storage_options
FROM pg_catalog.pg_stat_user_tables u
JOIN pg_catalog.pg_class c ON c.oid = u.relid
WHERE c.relkind = 'r'
  AND (u.n_live_tup + u.n_dead_tup) > 10000
ORDER BY u.n_dead_tup DESC
LIMIT 25;

-- ----------------------------------------------------------------------------
-- Section 5: Buffer Pool Eviction vs. Checkpoint Writes (PG 12-16 vs. PG 17+)
-- In PostgreSQL 17+, checkpoint columns were moved to pg_stat_checkpointer and
-- client backend writes/fsyncs were moved to pg_stat_io. Uses \gset + \if so
-- the exact version-matched catalog view runs with zero SQL errors.
-- ----------------------------------------------------------------------------
SELECT current_setting('server_version_num')::int >= 170000 AS is_pg_17_or_newer \gset

\if :is_pg_17_or_newer
-- PostgreSQL 17+ Unified Checkpointer, Bgwriter & Client Backend I/O Telemetry
SELECT
    cp.num_timed AS checkpoints_timed,
    cp.num_requested AS checkpoints_req,
    cp.write_time AS checkpoint_write_time,
    cp.sync_time AS checkpoint_sync_time,
    cp.buffers_written AS buffers_checkpoint,
    bg.buffers_clean,
    bg.maxwritten_clean,
    COALESCE(io.writes, 0) AS buffers_backend,
    COALESCE(io.fsyncs, 0) AS buffers_backend_fsync,
    bg.buffers_alloc,
    cp.stats_reset
FROM pg_catalog.pg_stat_checkpointer cp
CROSS JOIN pg_catalog.pg_stat_bgwriter bg
LEFT JOIN (
    SELECT
        sum(writes) AS writes,
        sum(fsyncs) AS fsyncs
    FROM pg_catalog.pg_stat_io
    WHERE backend_type = 'client backend'
      AND object = 'relation'
      AND context = 'normal'
) io ON true;
\else
-- PostgreSQL 12-16 Buffer Pool & Checkpoint Telemetry
SELECT
    checkpoints_timed,
    checkpoints_req,
    checkpoint_write_time,
    checkpoint_sync_time,
    buffers_checkpoint,
    buffers_clean,
    maxwritten_clean,
    buffers_backend,
    buffers_backend_fsync,
    buffers_alloc,
    stats_reset
FROM pg_catalog.pg_stat_bgwriter;
\endif

-- ----------------------------------------------------------------------------
-- Section 6: Database-Wide Temp Sort Spills, Deadlocks & Cache Hit Ratio (Layer 1)
-- Detects external disk sort spills (temp_files / temp_bytes) when work_mem is
-- undersized, alongside database-wide buffer cache efficiency.
-- ----------------------------------------------------------------------------
SELECT
    datname AS database_name,
    xact_commit,
    xact_rollback,
    blks_read,
    blks_hit,
    round((100.0 * blks_hit / nullif(blks_hit + blks_read, 0))::numeric, 2) AS cache_hit_pct,
    temp_files,
    temp_bytes,
    pg_size_pretty(temp_bytes) AS temp_spill_size,
    deadlocks,
    stats_reset
FROM pg_catalog.pg_stat_database
WHERE datname = current_database();

-- ----------------------------------------------------------------------------
-- Section 7: Per-Table Index Write-Amplification & Catalog Bloat Ratio (Layer 5)
-- Identifies tables where total index count and index storage dwarf the table
-- heap (e.g. 30+ indexes > 200% of heap size), causing severe INSERT/UPDATE
-- write amplification and Query Planner catalog inspection latency.
-- ----------------------------------------------------------------------------
SELECT
    u.schemaname,
    u.relname AS table_name,
    count(s.indexrelid) AS total_indexes,
    count(s.indexrelid) FILTER (WHERE s.idx_scan = 0) AS zero_scan_indexes,
    u.n_tup_ins + u.n_tup_upd + u.n_tup_del AS total_write_ops,
    u.n_tup_hot_upd AS hot_updates,
    pg_size_pretty(pg_relation_size(u.relid)) AS heap_size,
    pg_size_pretty(COALESCE(sum(pg_relation_size(s.indexrelid)), 0)) AS total_indexes_size,
    round(
        100.0 * COALESCE(sum(pg_relation_size(s.indexrelid)), 0)
        / nullif(pg_relation_size(u.relid), 0),
        1
    ) AS index_to_heap_pct
FROM pg_catalog.pg_stat_user_tables u
LEFT JOIN pg_catalog.pg_stat_user_indexes s
    ON s.relid = u.relid
WHERE pg_relation_size(u.relid) > 0
GROUP BY u.schemaname, u.relname, u.relid, u.n_tup_ins, u.n_tup_upd, u.n_tup_del, u.n_tup_hot_upd
HAVING count(s.indexrelid) >= 5
ORDER BY COALESCE(sum(pg_relation_size(s.indexrelid)), 0) DESC
LIMIT 20;
