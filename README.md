# Linux, PostgreSQL & Container Support Field Playbook

**Author:** Emil Walter "Pete" Jalajas  
**Links:** https://linkedin.com/in/ewpj | https://github.com/petjal  
**Repository:** https://github.com/petjal/linux-pg-support-playbook  

---

## Executive Summary: Live Self-Managed GitLab CE Omnibus Bug, Schema & Performance Discovery Matrix (Nov 9-10, 2026)

To demonstrate how enterprise Designated Support Engineering and bottom-up Linux/PostgreSQL diagnostic discipline operate on production infrastructure, this repository pairs automated unit tests with live empirical research conducted on an isolated **GCP Spot VM (`n2-standard-8`, 8 vCPUs, 32 GB RAM, 200 GB SSD)** running official **Self-Managed GitLab CE Omnibus** ("Project One-Hoss Shay / 77K").

Across **1,703 PostgreSQL tables**, **7,111 default indexes**, and **2.2 million high-churn enterprise rows (plus 275,000 dead tuples)**, we stress-tested the platform under concurrent 4-worker load, peeled 14 cascading bottlenecks across the entire systems stack from 10,663 ms down to 0.0126 ms (**846,270x speedup**), codified the 7-Layer Bottleneck-Peeling Troubleshooting Protocol, and executed an exhaustive catalog audit identifying 5 live production defects and schema contradictions in GitLab's codebase.

### Table 1: 5 Live Self-Managed GitLab Bugs & Schema Contradictions Isolated in Production Source & Omnibus PostgreSQL (1,703 Tables Audited)

| # | Component & Defect | Production Source / Schema Citation | Mechanism & Production Failure Impact | Actionability & Remediation |
|---|---|---|---|---|
| **1** | **`user_permission_export_upload_upload_states.uploaded_by_user_id`**<br>`fk_cda50402b9`<br>*(NEW Unfiled Bug Discovered via 1,703-Table Omnibus Audit)* | `FOREIGN KEY (uploaded_by_user_id) REFERENCES users(id) ON DELETE SET NULL`<br>Column: `uploaded_by_user_id bigint NOT NULL`<br>`app/services/users/migrate_records_to_ghost_user_service.rb` | **100% missing from Ghost User migration in both CE and EE.** `MigrateRecordsToGhostUserService` migrates 20+ user associations to `Users::Internal.ghost`, but completely omits this table. When `user.destroy` executes `DELETE FROM users`, PostgreSQL's internal C trigger (`ri_triggers.c`) attempts `SET uploaded_by_user_id = NULL`, instantly crashing with `PG::NotNullViolation`. Deleting any user who has uploaded a permission export permanently fails and blocks user deletion indefinitely. | **Immediate Workaround (`gitlab-rails console`):**<br>`UserPermissionExportUpload::UploadState.where(uploaded_by_user_id: user_id).update_all(uploaded_by_user_id: Users::Internal.ghost.id)`<br>**Upstream Patch:** Add `batched_migrate(UserPermissionExportUpload::UploadState, :uploaded_by_user_id)` to `migrate_records` in CE `MigrateRecordsToGhostUserService`, and relax column nullability to align with `ON DELETE SET NULL`. |
| **2** | **`resource_link_events.user_id`**<br>`fk_bd4ae15ce4`<br>*(Open Issue `gitlab-org/gitlab#638577`, filed Nov 5, 2026, 0 comments)* | `FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL`<br>Column: `user_id bigint NOT NULL`<br>`ee/app/services/ee/users/migrate_records_to_ghost_user_service.rb:14-15,69-71`<br>`app/models/work_items/resource_link_event.rb` | **CE/EE Packaging Split + Schema Contradiction.** `WorkItems::ResourceLinkEvent` lives in CE, allowing all CE/EE users to link child tasks to issues. However, `migrate_resource_link_events` was placed in the EE-only service override (`ee/app/services/ee/users/...`). In CE instances (or any path bypassing the EE override), rows are never re-pointed to Ghost User before `DELETE FROM users`. PostgreSQL's `ON DELETE SET NULL` fires against `user_id NOT NULL`, throwing `PG::NotNullViolation` and trapping deleted users in `blocked` state with background retries every 30 minutes forever. | **Immediate Workaround (`gitlab-rails console`):**<br>`WorkItems::ResourceLinkEvent.where(user_id: stuck_ids).update_all(user_id: Users::Internal.ghost.id)`<br>**Upstream Patch:** Move `migrate_resource_link_events` from `ee/` into base CE service `app/services/users/migrate_records_to_ghost_user_service.rb`, and drop `NOT NULL` constraint via migration. |
| **3** | **`resource_iteration_events.user_id`**<br>`fk_rails_501fa15d69`<br>*(NEW Schema Contradiction Discovered via 1,703-Table Omnibus Audit)* | `FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL`<br>Column: `user_id bigint NOT NULL`<br>`app/models/resource_iteration_event.rb` | **Direct Schema Contradiction.** `resource_iteration_events` defines an `ON DELETE SET NULL` foreign key referencing `users(id)`, but keeps `user_id` marked `NOT NULL`. While EE's `migrate_resource_iteration_events` handles standard UI ghost migrations, any direct database `hard_delete`, cascade bypass, or CE-level deletion immediately crashes with `ActiveRecord::NotNullViolation: PG::NotNullViolation`. | **Upstream Patch:** Harmonize database schema via migration:<br>`change_column_null :resource_iteration_events, :user_id, true` to ensure PostgreSQL foreign-key cascade consistency. |
| **4** | **`Gitlab::OtpKeyRotator#rotate!`**<br>*(Unanswered Thread `forum.gitlab.com/t/135881`, posted Oct 29, 2026, 0 replies)* | `lib/gitlab/otp_key_rotator.rb:39`<br>`app/controllers/profiles/two_factor_auths_controller.rb:144-152`<br>`lib/gitlab/doctor/secrets.rb:68` | **Query Scope Mismatch Breaking `gitlab:doctor:secrets`.** When any user visits `/profile/two_factor_auth` (even without completing 2FA enrollment), GitLab pre-generates and persists `encrypted_otp_secret` while leaving `otp_required_for_login = false`. `OtpKeyRotator#rotate!` iterates strictly over `User.with_two_factor`, skipping all users with 2FA disabled. After rotating `otp_key_base`, those unrotated rows cause `gitlab:doctor:secrets` to fail with `OpenSSL::Cipher::CipherError: bad decrypt`. | **Immediate Workaround (`gitlab-rails console`):**<br>`User.where(otp_required_for_login: false).where.not(encrypted_otp_secret: nil).update_all(encrypted_otp_secret: nil, encrypted_otp_secret_iv: nil, encrypted_otp_secret_salt: nil)`<br>**Upstream Patch:** In `lib/gitlab/otp_key_rotator.rb:39`, change iteration scope from `User.with_two_factor` to `User.where.not(encrypted_otp_secret: nil)`. |
| **5** | **Pipeline Execution Policies Hierarchy Truncation**<br>*(Unanswered Thread `forum.gitlab.com/t/135642`, posted Oct 12, 2026, 0 replies)* | `ee/app/services/security/security_orchestration_policies/limit_service.rb:44-46`<br>`ee/lib/gitlab/security/orchestration/project_pipeline_execution_policies.rb:45-55` | **Hardcoded Constant Ignoring Admin UI Limit.** Administrators raising the 18.3 Admin UI limit *"Maximum number of pipeline execution policies allowed"* find the 6th policy still fails to run. `limit_service.rb:44-46` hardcodes `:pipeline => 5` without checking `Gitlab::CurrentSettings`. Furthermore, `project_pipeline_execution_policies.rb` evaluates `applicable_execution_policies_by_hierarchy.first(5).reverse`, silently dropping subgroup policies without emitting any warnings or error logs. | **Immediate Workaround:** Consolidate subgroup policies into single policy files utilizing `include: [{ local: ... }]` within the same Security Policy Project.<br>**Upstream Patch:** Connect `pipeline_execution_policies_per_pipeline_limit` to `Gitlab::CurrentSettings`, or emit structured `AppJsonLogger` warnings when hierarchy policies exceed the limit. |

---

### Table 2: Live GitLab CE Omnibus "One-Hoss Shay / Project 77K" Bottleneck-Peeling Waterfall (8 vCPUs, 32 GB RAM, 200 GB SSD, 1,703 Tables, 7,111 Default Indexes, 2.2M Enterprise Rows + 275k Dead Tuples)

All benchmarks executed on GCP Spot VM (`n2-standard-8`, 8 vCPUs, 32 GB RAM, 200 GB SSD) under GitLab CE Omnibus with official GitLab Reference Architecture PostgreSQL tunings applied (`shared_buffers = 8GB`, `effective_cache_size = 24GB`, `work_mem = 16MB`, `random_page_cost = 1.1`, `effective_io_concurrency = 200`, down from 107 ms untuned default). Workload comprises **180 heavy composite queries** running across **4 concurrent workers** against 2.2M rows and 275k dead tuples.

> **Why Both Cumulative Waterfall & Isolated Target Speedups Are Shown (Order-Dependence vs. True Magnitude):** In a sequential bottleneck-peeling waterfall, the apparent millisecond savings of a later fix depends on where it sits in the sequence—for example, Fix #10 (`Pass 9B`) appears to save "only" `65.5 ms` (`71 ms -> 5.46 ms`) because Fixes #1–#9 already removed `10,592 ms` of database work. Conversely, if an architectural cache (`Pass 12B`) or connection pool (`Pass 9B`) is applied first, it saves seconds upfront. To make both perspectives clear, the table below reports **4-Worker Suite Wall-Clock**, **Step-Relative Speedup**, **Cumulative Speedup vs. State 1**, and the **Order-Independent Isolated Target Speedup** on the specific query, write path, planner phase, or IPC boundary each fix addresses.

| Stage / Pass | 4-Worker Suite Wall-Clock | Step Speedup | Cumulative vs. State 1 | Isolated Target Speedup (Order-Independent) | Core Bottleneck Broken, Diagnostic Mechanism & Telemetry Metric |
|---|---|---|---|---|---|
| **00: State 0 (Default Empty)** | **107 ms -> 59 ms** | **1.81x** | Baseline *(Empty DB)* | **1.81x** *(Empty DB baseline)* | **Untuned Default -> Reference Architecture Baseline.** 1,703 tables and 7,111 default indexes. Official GitLab Reference Architecture memory tuning (`shared_buffers = 8GB`, `effective_cache_size = 24GB`, `work_mem = 16MB`, `random_page_cost = 1.1`, `effective_io_concurrency = 200`) improved empty-table query suite from 107 ms down to 59 ms baseline. |
| **01: State 1 (Max-Load Collapse)** | **10,663 ms** | **1.00x** | **1.00x** *(Collapse)* | Baseline *(Collapse under 2.2M rows)* | **Disk Sort Spills & Autovacuum Starvation.** Loaded 2.2M enterprise rows + 275k dead tuples. Default autovacuum scale factor (`0.20`) starved cleanup. Stock `work_mem = 16MB` caused external merge disk spills (16 MB temp files hitting GCP `pd-ssd` Direct I/O throughput ceiling of 339.2 MB/s). Workload C nested loop join burned 1,019,439 shared buffer hits. |
| **02: Pass 1 (Targeted Leaf Autovacuum + work_mem = 64MB)** | **8,781 ms** | **1.21x** | **1.21x** | **41.2x buffer reduction** on CI join (`1.02M -> 24.7k` buffers); **100% disk spill elimination** (`16 MB -> 0 B`) | **Autovacuum Starvation & Temp Disk Spills Broken.** Executed `VACUUM ANALYZE` and set aggressive table autovacuum (`autovacuum_vacuum_scale_factor = 0.01`) on `issues`, `notes`, and physical leaf partition `gitlab_partitions_dynamic.ci_builds_100`. Raised `work_mem` to 64MB, eliminating all disk sort spills (`temp_blks_written = 0`) via in-memory `HashAggregate` (18.4 MB) and replacing nested loop with hash join. |
| **03: Pass 2 (Partial Covering Indexes with INCLUDE)** | **4,268 ms** | **2.06x** | **2.50x** | **9.86x buffer reduction** on `notes` (`29.2k -> 2.96k` buffers, `Heap Fetches: 0`) | **Heap Fetch Latency Broken.** Created partial covering index `idx_shay_notes_issue_nosys_cov` on `notes(project_id, noteable_id) INCLUDE (updated_at) WHERE NOT system AND noteable_type = 'Issue'` and covering index on `ci_builds_100(project_id, commit_id, partition_id, name) WHERE status = 'failed'`. Converted heap-fetching scans into pure `Index Only Scan` (`Heap Fetches: 0`). |
| **04: Pass 3 (Naive pg_trgm GIN Index on issues.description)** | **4,520 ms** | **0.94x** *(Regression)* | **2.36x** *(Regression Trap)* | **0.03x (32x slower** on Workload A: `0.69 ms -> 22.4 ms`) | **PostgreSQL GIN-vs-LIMIT Sort-Order Trap Exposed.** Created partial GIN trigram index on `issues(description)` with `WHERE state_id = 1`. Because `'autovacuum'` matched 4% of rows (8,000 issues) and GIN posting lists are ordered by physical `ctid` rather than `updated_at DESC`, PostgreSQL could not stop early: it fetched and sorted all 8,000 candidate rows (22.4 ms) instead of stopping early after 50 sorted B-tree tuples (0.69 ms). |
| **05: Pass 4 (Materialized View Roll-Up mv_shay_ci_failure_rollup)** | **1,313 ms** | **3.44x** | **8.12x** | **5,952x on Workload C** (`238.08 ms -> 0.040 ms`; `7,606 -> 2` buffers) | **Relational Join Aggregation Floor Broken.** Created pre-computed `MATERIALIZED VIEW mv_shay_ci_failure_rollup` with covering index `(project_id, fail_count DESC) INCLUDE (ref, name)`. Collapsed Workload C's 250,000-row `p_ci_builds JOIN p_ci_pipelines` hash join (238.08 ms) into a 0.040 ms lookup, exposing Workload B (600,000-row `notes` `GroupAggregate` at 130.17 ms/call) as **99.1% of remaining runtime**. |
| **06: Pass 5 (Sidecar Counter Cache Table shay_issue_note_counts)** | **95 ms** | **13.82x** | **112.2x** | **1,669x on Workload B** (`130.17 ms -> 0.078 ms`; `2,964 -> 5` buffers) | **Read-Time 600k-Row Grouping Broken.** Replaced read-time `600,000`-row `notes` `GroupAggregate` (130.17 ms, 2,964 buffers) with an indexed sidecar counter table `shay_issue_note_counts(project_id, comment_cnt DESC, noteable_id) INCLUDE (last_user_note_at)`. Dropped Workload B to an `Index Only Scan` taking **0.078 ms** (5 buffers, `Heap Fetches: 0`), dropping 4-worker wall-clock from 1,313 ms to **95 ms**. |
| **07: Pass 6 (Inverted B-Tree Token Index on (project_id, token, updated_at DESC))** | **84 ms** | **1.13x** *(1.64x SQL)* | **126.9x** | **191x vs. Pass 3 GIN** (`22.4 ms -> 0.117 ms`); **4.4x vs. Pass 2 B-tree** (`84 -> 4` buffers) | **GIN-vs-LIMIT Sort-Order Trap Solved.** Replaced un-ordered GIN scan with an inverted token B-Tree table indexed on `(project_id, token, updated_at DESC) INCLUDE (issue_id, title, state_id)`. Gave PostgreSQL exact index ordering matching `ORDER BY updated_at DESC LIMIT 50`, dropping Workload A from 0.516 ms (84 buffers, 1,184 rows filtered) to **0.117 ms** (4 buffers, 0 rows filtered). |
| **08: Pass 7 (Zero-Scan Index Pruning + Single Prepared Statement)** | **74 ms** | **1.14x** *(2.03x SQL)* | **144.1x** | **5.68x faster bulk `UPDATE` writes** (`727 ms -> 128 ms`); **4.51x faster Query Planning** (`5.00 ms -> 1.11 ms`) | **Planner Catalog Contention & Write Amplification Broken.** Pruned 35 unused zero-scan indexes on `issues` (37 -> 2 indexes, 158 MB -> 20 MB). Slashed Query Planner catalog reads from 876 buffers (5.00 ms) to 155 buffers (1.11 ms). Slashed 5,000-row non-HOT `UPDATE` write latency from 727 ms to 128 ms. Consolidated Workloads A, B, and C into a single-roundtrip prepared statement (`PREPARE`). |
| **09: Pass 8A (Pre-Assembled JSONB Generation Table PK Lookup)** | **71 ms** outer / **2.44 ms** SQL | **1.04x** outer / **3.77x** SQL | **150.2x** outer / **4,370x** SQL | **3.77x SQL reduction** (`9.21 ms -> 2.44 ms` per worker) | **Process & Connection Spawn Overhead Exposed.** Pre-assembled dashboard widgets into single JSONB payload table `shay_project_cache_gen` keyed by `project_id`. SQL execution time dropped to 2.44 ms per worker across all 15 loops. Exposed that **68.5 ms (96.5%) of outer wall-clock time was connection/process spawn tax** (`docker exec` + `psql` startup). |
| **10: Pass 9B (Visibility Map Fix + Warm Persistent Connection Pool)** | **5.463 ms** | **13.00x** | **1,951.9x** | **13.00x client wall-clock speedup** (`71 ms -> 5.463 ms`); **5.9x buffer reduction** (`3,188 -> 540`) | **Connection Spawn Tax & Visibility Map Fetches Broken.** Ran `VACUUM ANALYZE` to set Visibility Map all-visible bits (`Heap Fetches: 20 -> 0`, shared buffers dropped 3,188 -> 540). Deployed a warm persistent connection pool stripping the 68.5 ms process-spawn overhead, dropping 4-worker wall-clock time to **5.463 ms**. |
| **11: Pass 10B (Untoasted STORAGE PLAIN Inline Cache Row)** | **1.811 ms** outer / **0.508 ms** SQL | **3.02x** outer / **4.74x** SQL | **5,887.9x** outer / **20,990x** SQL | **4.74x SQL speedup** (`2.41 ms -> 0.508 ms`); **11.5x buffer reduction** (`688 -> 60` = `1.0` buffer/query) | **TOAST Chunk Traversal & pglz Decompression Broken.** Pass 10A exposed that reading a 12 KB JSONB row 60 times required 688 buffer hits (`11.5` buffers/query) due to out-of-line `pg_toast` chunk fetches and `pglz` CPU decompression. Pass 10B used `STORAGE PLAIN` (<1.8 KB inline), slashing buffer hits from 688 to **60 (literally 1.0 buffer pin per query)**. |
| **12: Pass 11 (Vectorized Single-Executor-Pass Batch Query)** | **1.845 ms** outer / **0.0466 ms (46.6 us)** SQL | **10.90x** SQL | **228,820x** SQL speedup | **10.90x SQL speedup** (`0.508 ms -> 0.0466 ms`); **15x buffer reduction** (`60 -> 4` = `1` buffer/worker) | **PL/pgSQL SPI Loop Overhead Broken.** Replaced 15-iteration PL/pgSQL `FOR i IN 1..15 LOOP` SPI calls with a single vectorized query. PostgreSQL executed all 15 lookups in **0.0466 ms (46.6 microseconds)** touching **4 shared buffers total across all 4 workers (1 buffer pin per worker)**, hitting the physical 1.80 ms Docker / Unix-socket pipe IPC round-trip floor. |
| **13: Pass 12A (Warm 4-Worker Pool + /dev/shm tmpfs RAM Cache with json.loads)** | **1.032 ms** | **26.42x** vs. Pass 8B | **10,332.4x** | **26.42x over cold fork** (`27.26 ms -> 1.032 ms`); **100% DB offload** (`0` DB socket hits) | **Database Socket IPC & Process Fork Tax Bypassed.** Bypassed PostgreSQL socket IPC on unmutated project generations (`(project_id, gen_seq)`) via a mutation-gated POSIX shared memory (`tmpfs` RAM) local read-through cache. Stripped the 25 ms `ProcessPoolExecutor` `os.fork()` overhead using a warm 4-worker pool. |
| **14: Pass 12B (Warm 4-Worker Pool + Zero-Copy /dev/shm Binary Header & Memoryview Slice)** | **0.0126 ms (12.60 us)** *(70.0 ns / query)* | **81.90x** | **846,270x** speedup | **81.90x over `json.loads()`** (`1,032 us -> 12.60 us`); operates at **27.54 GB/s RAM bus limit** | **Zero-Copy Memory-Bus Ceiling Reached.** Replaced ASCII `json.loads()` deserialization (`1.02 ms`) with a 24-byte binary header (`struct.unpack_from`) and zero-copy `memoryview` byte slicing in `/dev/shm`. All 180 queries across 4 concurrent workers completed in **0.0126 ms (12.60 microseconds total = 70.0 nanoseconds per query)** at DDR4 memory bus speed (`28,198 MB/s`). |

---

### The 7-Layer Bottleneck-Peeling Troubleshooting Protocol

When enterprise systems collapse under load, inexperienced teams guess at application code or throw compute at the symptom. Designated Support Engineering applies a systematic, bottom-up 7-layer isolation protocol that peels each bottleneck down to physical hardware limits:

1. **Layer 1: Storage Subsystem, Filesystem & Garbage Collection**
   - *Failure Signatures:* Cloud block-storage hypervisor I/O limits (e.g. GCP `pd-ssd` 339.2 MB/s cap), elevated disk `%util`, `w_await`, write-ahead log (`pg_wal`) `fsync` queueing, dead-tuple table bloat, and autovacuum starvation.
   - *Diagnostic Tools:* `iostat -xz 1`, `pg_stat_database` (`temp_bytes`, `temp_files`), `pg_stat_user_tables` (`n_dead_tup`, `n_live_tup`).
   - *Intervention:* Expand `work_mem` to contain sorts in RAM (`temp_blks_written = 0`); apply aggressive table autovacuum scale factors (`scale_factor = 0.01`) directly to physical leaf partitions (`ALTER TABLE gitlab_partitions_dynamic.ci_builds_100 SET (...)`).

2. **Layer 2: Heap Access & Index Traversal**
   - *Failure Signatures:* High buffer hits on index scans, random disk reads, and table page fetches for columns not present in index b-trees (`Heap Fetches > 0`).
   - *Diagnostic Tools:* `EXPLAIN (ANALYZE, BUFFERS)` inspecting `Index Scan` vs. `Index Only Scan` nodes; Visibility Map status.
   - *Intervention:* Construct partial covering B-tree indexes using `INCLUDE (...)` and selective `WHERE` clauses; maintain Visibility Map all-visible bits via `VACUUM` to achieve pure `Index Only Scan` (`Heap Fetches: 0`), cutting buffer pins by 70% to 90%.

3. **Layer 3: Query Access-Path & Planner Costing (Sort-Order Traps)**
   - *Failure Signatures:* The GIN-vs-`LIMIT` sort-order trap. Naive full-text or trigram GIN indexes returning posting lists ordered by physical `ctid` rather than query sort order, forcing the executor to fetch and sort every candidate row (e.g. 8,000 matches) instead of stopping early on an ordered B-tree index.
   - *Diagnostic Tools:* `EXPLAIN (ANALYZE, BUFFERS)` showing `Bitmap Index Scan` followed by `Sort` and `Limit` with high execution times relative to candidate count.
   - *Intervention:* Replace un-ordered GIN scans with inverted token B-Tree tables indexed on `(project_id, token, updated_at DESC) INCLUDE (...)`, allowing PostgreSQL to stop scanning immediately upon satisfying the `LIMIT` (0.516 ms -> 0.117 ms).

4. **Layer 4: Relational Join & Aggregation Computation**
   - *Failure Signatures:* Repeated large-table hash joins, nested loops, and `GroupAggregate` / `HashAggregate` computations across hundreds of thousands of rows at read time, consuming 90%+ of total query latency.
   - *Diagnostic Tools:* `EXPLAIN (ANALYZE, BUFFERS)` showing large `GroupAggregate` nodes and high buffer counts on read queries; `pg_stat_statements`.
   - *Intervention:* Pre-compute complex joins via Materialized Views (`CREATE MATERIALIZED VIEW ... UNIQUE INDEX`) and replace heavy read-time groupings with indexed sidecar counter-cache tables (`shay_issue_note_counts`) updated asynchronously or on write, turning $O(N)$ table scans into $O(1)$ single-key index lookups (130 ms -> 0.078 ms).

5. **Layer 5: Catalog Metadata, Planner Contention & Write-Amplification**
   - *Failure Signatures:* Bloated system catalogs (`pg_class`, `pg_index`, `pg_attribute`) where Query Planner time exceeds execution time (e.g. 5 ms planning vs 0.17 ms execution); write-amplification on `INSERT`/`UPDATE` operations caused by dozens of unused zero-scan indexes.
   - *Diagnostic Tools:* `pg_stat_user_indexes` (`idx_scan = 0`), `pg_statio_user_indexes`, `pg_stat_statements` comparing `mean_plan_time_ms` vs. `mean_exec_time_ms`.
   - *Intervention:* Prune zero-scan indexes aggressively (e.g. 37 -> 2 indexes on `issues`, 158 MB -> 20 MB), slashing planner catalog buffer hits by 4.5x (876 -> 155 buffers) and speeding up 5,000-row non-HOT `UPDATE` write latency by 5.68x (727 ms -> 128 ms); bind composite queries via prepared statements (`PREPARE`) for reusable generic execution plans.

6. **Layer 6: Serialization, Storage Strategy & Database Engine IPC**
   - *Failure Signatures:* Client wall-clock time dominated by connection/process spawn tax (`docker exec` + `psql` startup taking 68.5 ms of a 71 ms turn); single-row queries touching dozens of shared buffers due to out-of-line `pg_toast` chunk traversal and `pglz` decompression; PL/pgSQL SPI iterative loop transitions.
   - *Diagnostic Tools:* Comparing application client latency against `pg_stat_statements` execution times; inspecting buffer hits on single-row lookups (688 buffers vs. 1 buffer).
   - *Intervention:* Deploy warm, persistent connection pools (slashing 68.5 ms spawn tax); enforce `STORAGE PLAIN` inlining (<2 KB rows) to eliminate TOAST table reads (688 -> 60 buffers = 1.0 buffer pin per query); vectorize batch queries (`generate_series` + `CROSS JOIN LATERAL`) to execute in a single executor pass (46.6 microseconds SQL time, 1 buffer hit per worker).

7. **Layer 7: Kernel Socket IPC, Process Boundaries & Memory-Bus Floor**
   - *Failure Signatures:* Query execution dropping to microseconds while multi-worker wall-clock is bottlenecked on TCP loopback/Unix domain socket syscall context switches, process `fork()` overhead, and CPU cycles spent in ASCII JSON parsing (`json.loads`).
   - *Diagnostic Tools:* `perf top` showing `__memcpy_avx_unaligned` or socket syscall overhead; client-side CPU profiling identifying serialization bottlenecks.
   - *Intervention:* Implement mutation-gated POSIX shared memory (`tmpfs /dev/shm`) read-through caching with warm worker pools and zero-copy 16-byte binary headers with `memoryview` byte slicing, operating directly at DDR4 memory bus bandwidth limits (28.2 GB/s, 70 nanoseconds per query = 846,270x speedup).

---

### Table 3: Operational Gotchas & Write-Amplification Footprint Across GitLab's 7,111 Default Indexes

Analysis of live Omnibus PostgreSQL catalog telemetry following the 2.2M-row stress workload (`06_index_bloat_and_waterfall_summary.txt` and `07_cold_storage_archive_size.txt`):

#### 1. Zero-Scan Index Bloat & Write Amplification Footprint
- **Total Database Size:** 1,641 MB across 1,704 user tables and 7,119 user indexes.
- **Zero-Scan Index Waste:** **7,054 indexes (99.09% of all indexes) had `idx_scan = 0`**, consuming **878 MB (53.5%)** of total database disk space!
- **High-Write Table Penalties:**
  - `notes`: 18 total indexes (15 zero-scan). Heap size: 228 MB, All indexes: 348 MB (**Indexes are 153% the size of the table!**). Zero-scan indexes absorbing 1,000,000 write ops each include `index_notes_on_discussion_id` (64 MB), `index_notes_on_noteable_id_noteable_type_and_id` (54 MB), and `index_notes_on_noteable_id_and_noteable_type_system_author_id` (54 MB).
  - `ci_builds_100`: 24 total indexes (21 zero-scan). Heap size: 150 MB, All indexes: 266 MB (**Indexes are 177% the size of the table!**). Zero-scan indexes absorbing 1,000,000 write ops each include `ci_builds_100_runner_id_id_idx` (54 MB) and `ci_builds_100_project_id_id_idx` (30 MB).
  - `issues`: 37 total indexes (32 zero-scan). Heap size: 74 MB, All indexes: 224 MB (**Indexes are 303% the size of the table!**).
  - `ci_pipelines_100`: 25 total indexes (22 zero-scan). Heap size: 41 MB, All indexes: 131 MB (**Indexes are 320% the size of the table!**).

#### 2. Three Critical Production Operational Findings
1. **`gitlab-ctl reconfigure` Does Not Restart PostgreSQL for Shared Preload Libraries:**
   Adding `postgresql['shared_preload_libraries'] = 'pg_stat_statements'` to `/etc/gitlab/gitlab.rb` followed by `gitlab-ctl reconfigure` executes only `pg_ctl reload` (SIGHUP). PostgreSQL silently ignores shared preload library changes on SIGHUP. A full `gitlab-ctl restart postgresql` is strictly required before the library activates.
2. **PostgreSQL Partitioned Table Storage Parameter Trap:**
   Executing `ALTER TABLE p_ci_builds SET (autovacuum_vacuum_scale_factor = 0.01)` immediately fails with `ERROR: cannot set storage parameters on partitioned table "p_ci_builds"`. In PostgreSQL partitioned architectures, storage parameters cannot be defined on declarative parent tables; autovacuum thresholds must be applied directly to active leaf partitions (e.g., `gitlab_partitions_dynamic.ci_builds_100`).
3. **Cold Storage Archive Efficiency:**
   Piping the full 2.2M-row database dump through multi-threaded zstandard (`zstd -T0 -3`) compresses the 1.64 GB database down to **40 MB** (`gitlabhq_production_shay_77k.sql.zst`), providing a 41x compression ratio for fast archival to cloud `NEARLINE` storage.

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

## 4. Live GitLab Source-Code & Omnibus Schema Triage Demonstration (November 9-10, 2026)

To demonstrate how human-directed AI source-code and database schema correlation works on real-world problems in an enterprise codebase, `docs/LIVE_GITLAB_ISSUE_TRIAGE_2026_11.md` documents the live root-cause triage, immediate self-managed customer workarounds, upstream patches, and the live 2.2M-row GCP VM benchmark waterfall for five production GitLab problems:

1. **Case 1 (Hard - PostgreSQL Constraint & CE/EE Packaging Split, `gitlab-org/gitlab#638577`, opened Nov 5, 2026, 0 comments):** Isolated why deleting a user who linked a child work item silently hangs forever (`PG::NotNullViolation` on `resource_link_events.user_id`). Traced the failure to a self-contradictory PostgreSQL schema (`user_id bigint NOT NULL` paired with `CONSTRAINT fk_bd4ae15ce4 FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL` in `db/migrate/20230329085754_add_foreign_key_to_resource_link_events_on_user.rb`) combined with `migrate_resource_link_events` living inside the EE-only override (`ee/app/services/ee/users/migrate_records_to_ghost_user_service.rb`) while `WorkItems::ResourceLinkEvent` lives in CE (`app/models/work_items/resource_link_event.rb`).
2. **Case 2 (Medium - Self-Managed Cryptography & Rake Task Scope Mismatch, `forum.gitlab.com/t/135881`, posted Oct 29, 2026, 0 replies):** Isolated why `gitlab-rake gitlab:two_factor:rotate_key:apply` leaves `gitlab:doctor:secrets` failing on users with 2FA disabled (`lib/gitlab/otp_key_rotator.rb` line 39 scopes to `User.with_two_factor` instead of `User.where.not(encrypted_otp_secret: nil)`, skipping users who generated a pre-enrollment `encrypted_otp_secret` on `/profile/two_factor_auth` in `app/controllers/profiles/two_factor_auths_controller.rb` line 145).
3. **Case 3 (Medium - Enterprise Ultimate Security Policy Hierarchy Truncation, `forum.gitlab.com/t/135642`, posted Oct 12, 2026, 0 replies):** Isolated why a 6th inherited Pipeline Execution Policy (`inject_policy`) silently disappears across group hierarchies even after raising the 18.3 Admin UI limit (`ee/app/services/security/security_orchestration_policies/limit_service.rb` lines 44-46 hardcodes `:pipeline => 5` without reading `CurrentSettings`, and `ee/lib/gitlab/security/orchestration/project_pipeline_execution_policies.rb` lines 20-22 silently slices `.first(5)` before reversing hierarchy precedence).
4. **Case 4 (NEW Unfiled Bug Discovered via 1,703-Table Omnibus Audit):** Isolated `user_permission_export_upload_upload_states.uploaded_by_user_id` (`fk_cda50402b9`), where `uploaded_by_user_id bigint NOT NULL` is paired with `ON DELETE SET NULL` and 100% missing from `MigrateRecordsToGhostUserService` in both CE and EE, permanently crashing user deletion for any user who uploaded a permission export.
5. **Case 5 (NEW Schema Contradiction Discovered via 1,703-Table Omnibus Audit):** Isolated `resource_iteration_events.user_id` (`fk_rails_501fa15d69`), where `user_id bigint NOT NULL` is paired with `ON DELETE SET NULL`, crashing on `hard_delete` or whenever EE Ghost User migration is bypassed.

See `docs/LIVE_GITLAB_ISSUE_TRIAGE_2026_11.md` for the full technical write-up, file/line citations, `gitlab-rails console` workarounds, upstream patches, and the live GCP Spot VM "One-Hoss Shay / 77K" waterfall benchmark.

---

## 5. Repository Structure

```
linux-pg-support-playbook/
├── pyproject.toml                         # Project metadata, linting and type configuration
├── LICENSE                                # MIT License (c) 2026 Emil Walter "Pete" Jalajas
├── README.md                              # Operational field triage playbook and documentation
├── docs/
│   └── LIVE_GITLAB_ISSUE_TRIAGE_2026_11.md # Live root-cause triage of 5 GitLab issues & GCP VM benchmark (Nov 2026)
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
collected 22 items

tests/test_support_playbook.py::test_log_correlator_timestamp_parsing PASSED [  4%]
tests/test_support_playbook.py::test_log_correlator_normalization_and_bucketing PASSED [  9%]
tests/test_support_playbook.py::test_log_correlator_spike_detection_look_left PASSED [ 13%]
tests/test_support_playbook.py::test_pg_advisor_memory_split_and_swap PASSED [ 18%]
tests/test_support_playbook.py::test_pg_advisor_unused_index_detection PASSED [ 22%]
tests/test_support_playbook.py::test_pg_advisor_autovacuum_tuning PASSED [ 27%]
tests/test_support_playbook.py::test_pg_advisor_storage_and_network_traps PASSED [ 31%]
tests/test_support_playbook.py::test_sql_pushdown_benchmark PASSED       [ 36%]
tests/test_support_playbook.py::test_linux_user_audit_scripts_in_tempdir PASSED [ 40%]
tests/test_support_playbook.py::test_pg_advisor_zero_ram_guard PASSED    [ 45%]
tests/test_support_playbook.py::test_table_telemetry_negative_tuple_guard PASSED [ 50%]
tests/test_support_playbook.py::test_log_correlator_extended_timestamp_formats_and_precision PASSED [ 54%]
tests/test_support_playbook.py::test_log_correlator_hyphenated_hostname_level_priority PASSED [ 59%]
tests/test_support_playbook.py::test_log_correlator_sparse_bucket_memory_guard PASSED [ 63%]
tests/test_support_playbook.py::test_log_correlator_min_error_threshold_zero_guard PASSED [ 68%]
tests/test_support_playbook.py::test_sql_pushdown_benchmark_negative_and_zero_guards PASSED [ 72%]
tests/test_support_playbook.py::test_list_users_script_execution PASSED  [ 77%]
tests/test_support_playbook.py::test_repository_no_pii_or_path_bleed PASSED [ 81%]
tests/test_support_playbook.py::test_calculate_live_workload_tunings_project_77k PASSED [ 86%]
tests/test_support_playbook.py::test_calculate_live_workload_tunings_storage_types_and_network_rtt PASSED [ 90%]
tests/test_support_playbook.py::test_calculate_live_workload_tunings_oom_cap_and_temp_bytes PASSED [ 95%]
tests/test_support_playbook.py::test_support_pg_advisor_cli_main PASSED  [100%]

============================== 22 passed in 0.16s ===============================
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

### 6. Execute Live Workload-Driven PostgreSQL Tuning Advisor CLI
```bash
# Run with empirical Project 77K benchmark telemetry (32GB RAM, 8 vCPUs, 16MB sort spill, 53.5% zero-scan indexes):
PYTHONPATH=src python3 -m support_playbook.pg_advisor --live-example

# Or pass custom live telemetry directly:
PYTHONPATH=src python3 -m support_playbook.pg_advisor \
  --ram-gb 32 \
  --vcpus 8 \
  --storage-type ssd \
  --top-sort-spill-mb 16 \
  --dead-tuple-ratio 0.145 \
  --hot-table issues \
  --hot-table notes \
  --hot-table gitlab_partitions_dynamic.ci_builds_100 \
  --zero-scan-index-ratio 0.535 \
  --checkpoints-req-per-hour 6 \
  --format markdown
```
Example Output:
```ruby
# /etc/gitlab/gitlab.rb Configuration Overrides
postgresql['shared_buffers'] = "8GB"
postgresql['effective_cache_size'] = "24GB"
postgresql['work_mem'] = "32MB"
postgresql['random_page_cost'] = 1.1
postgresql['effective_io_concurrency'] = 200
postgresql['max_wal_size'] = "16GB"
postgresql['checkpoint_completion_target'] = 0.9
```
```sql
-- Leaf Partition Autovacuum Storage Parameters
ALTER TABLE issues SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_analyze_scale_factor = 0.005, autovacuum_vacuum_cost_limit = 2000);
ALTER TABLE notes SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_analyze_scale_factor = 0.005, autovacuum_vacuum_cost_limit = 2000);
ALTER TABLE gitlab_partitions_dynamic.ci_builds_100 SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_analyze_scale_factor = 0.005, autovacuum_vacuum_cost_limit = 2000);
```

---

## 7. Scope and Limitations

1. **PostgreSQL Diagnostics Script:** The queries in `sql/pg_field_diagnostics.sql` are strictly read-only (`SET default_transaction_read_only = on;`). They do not modify tables or configuration settings. In production, execute diagnostics during low-traffic windows or read replicas when investigating high lock contention.
2. **Container Lab Verification:** The reference container in `labs/linux_user_audit_container/Dockerfile` requires Docker or Podman. The test suite executes the shell logic directly using temporary directories and clean process environments (`env -i`) so that tests pass in standard CI/CD environments without container socket access.
3. **Telemetry Heuristics:** The advisors in `src/support_playbook/pg_advisor.py` codify standard production best practices for single-box and primary-standby architectures. Highly specialized distributed setups (e.g., Citus sharding or active-active multi-region replication) may require custom threshold tuning.
