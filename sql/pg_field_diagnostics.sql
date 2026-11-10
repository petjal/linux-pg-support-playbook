-- ============================================================================
-- Linux, PostgreSQL & Container Support Field Playbook
-- PostgreSQL Field Diagnostics (Strictly Read-Only Snapshot)
-- Author: Emil Walter "Pete" Jalajas
-- ============================================================================
-- Strictly enforce read-only session mode to guarantee zero mutation
\set ON_ERROR_STOP off
SET default_transaction_read_only = on;

-- ----------------------------------------------------------------------------
-- Section 1: Active, Long-Running, and Blocked Queries (pg_stat_activity)
-- Identifies blocking queries, lock wait chains, and connection states.
-- ----------------------------------------------------------------------------
SELECT
    blocked_locks.pid AS blocked_pid,
    blocked_activity.usename AS blocked_user,
    blocked_activity.client_addr AS blocked_client,
    now() - blocked_activity.query_start AS blocked_duration,
    blocking_locks.pid AS blocking_pid,
    blocking_activity.usename AS blocking_user,
    blocking_activity.client_addr AS blocking_client,
    now() - blocking_activity.query_start AS blocking_duration,
    blocked_activity.query AS blocked_statement,
    blocking_activity.query AS blocking_statement
FROM pg_catalog.pg_locks blocked_locks
JOIN pg_catalog.pg_stat_activity blocked_activity
    ON blocked_activity.pid = blocked_locks.pid
JOIN pg_catalog.pg_locks blocking_locks
    ON blocking_locks.locktype = blocked_locks.locktype
    AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
    AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
    AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
    AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
    AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
    AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
    AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
    AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
    AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
    AND blocking_locks.pid != blocked_locks.pid
JOIN pg_catalog.pg_stat_activity blocking_activity
    ON blocking_activity.pid = blocking_locks.pid
WHERE NOT blocked_locks.granted;

-- Active queries running longer than 10 seconds
SELECT
    pid,
    usename,
    client_addr,
    now() - query_start AS duration,
    wait_event_type,
    wait_event,
    state,
    query
FROM pg_catalog.pg_stat_activity
WHERE state != 'idle'
  AND (now() - query_start) > interval '10 seconds'
ORDER BY duration DESC
LIMIT 25;

-- ----------------------------------------------------------------------------
-- Section 2: Top Slow Queries by Total & Mean Exec Time (pg_stat_statements)
-- Requires shared_preload_libraries = 'pg_stat_statements'
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'
    ) THEN
        RAISE NOTICE 'pg_stat_statements extension is installed. Querying statements telemetry...';
    ELSE
        RAISE NOTICE 'pg_stat_statements is not installed in current database.';
    END IF;
END $$;

SELECT
    queryid,
    calls,
    round(total_exec_time::numeric, 2) AS total_exec_time_ms,
    round(mean_exec_time::numeric, 2) AS mean_exec_time_ms,
    round(stddev_exec_time::numeric, 2) AS stddev_exec_time_ms,
    rows,
    round((100.0 * shared_blks_hit / nullif(shared_blks_hit + shared_blks_read, 0))::numeric, 2) AS cache_hit_pct,
    substr(query, 1, 150) AS query_snippet
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;

-- ----------------------------------------------------------------------------
-- Section 3: Unused Zero-Scan Indexes (pg_stat_user_indexes)
-- Detects write-amplification tax: indexes that slow down INSERT/UPDATE/DELETE
-- without servicing any SELECT queries.
-- Excludes primary keys and unique indexes (both constraints and standalone CREATE UNIQUE INDEX).
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
WHERE s.idx_scan = 0
  AND NOT idx.indisunique
  AND NOT idx.indisprimary
  AND s.indexrelid NOT IN (
      SELECT conindid FROM pg_catalog.pg_constraint
  )
ORDER BY pg_relation_size(s.indexrelid) DESC
LIMIT 30;

-- ----------------------------------------------------------------------------
-- Section 4: Dead-Tuple Bloat & Table Autovacuum Overrides (pg_stat_user_tables)
-- Identifies tables with high churn needing table-specific autovacuum tuning.
-- ----------------------------------------------------------------------------
SELECT
    schemaname,
    relname AS table_name,
    n_live_tup,
    n_dead_tup,
    round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 2) AS dead_tuple_ratio_pct,
    last_vacuum,
    last_autovacuum,
    last_analyze,
    last_autoanalyze,
    c.reloptions AS table_storage_options
FROM pg_catalog.pg_stat_user_tables u
JOIN pg_catalog.pg_class c ON c.oid = u.relid
WHERE (n_live_tup + n_dead_tup) > 10000
ORDER BY n_dead_tup DESC
LIMIT 25;

-- ----------------------------------------------------------------------------
-- Section 5: Buffer Pool Eviction vs. Backend Writes (pg_stat_bgwriter / pg_stat_checkpointer)
-- Evaluates checkpointer and background writer health. High buffers_backend
-- indicates shared_buffers is undersized or bgwriter is not keeping up.
--
-- NOTE: In PostgreSQL 17+, checkpoint statistics were separated from pg_stat_bgwriter
-- into the dedicated pg_stat_checkpointer view (num_timed, num_requested, write_time,
-- sync_time, buffers_written). In PG 12-16, query pg_stat_bgwriter; in PG 17+, query both.
-- ----------------------------------------------------------------------------
-- PostgreSQL 12-16 Buffer Pool & Checkpoint Telemetry (also bgwriter in PG 17+)
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

-- PostgreSQL 17+ Dedicated Checkpointer Telemetry
SELECT
    num_timed AS checkpoints_timed,
    num_requested AS checkpoints_req,
    write_time AS checkpoint_write_time,
    sync_time AS checkpoint_sync_time,
    buffers_written AS buffers_checkpoint,
    stats_reset
FROM pg_catalog.pg_stat_checkpointer;
