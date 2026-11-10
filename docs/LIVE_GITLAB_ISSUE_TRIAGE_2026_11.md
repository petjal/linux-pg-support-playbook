# Live GitLab Source-Code Triage Demonstration (November 9, 2026)

**Author:** Emil Walter "Pete" Jalajas  
**Links:** https://linkedin.com/in/ewpj | https://github.com/petjal  
**Date:** November 9, 2026  
**Methodology:** Human-Directed AI Source-Code & Database Schema Correlation (`gitlab-org/gitlab` and `forum.gitlab.com`)

---

## Why This Document Exists

During my work on the GitLab Senior Assigned Support Engineer Take-Home Assessment on November 9, 2026, I wanted to test a simple question:

> *"Can a 16-year enterprise Linux and PostgreSQL Designated Support Engineer who is brand-new to GitLab's internal Ruby/Go codebase use his 'AI-on-the-box' source-code triage workflow to solve difficult, currently open GitLab customer problems reported live on the web today?"*

We queried `gitlab.com/api/v4` and `forum.gitlab.com/latest.json` for real-world self-managed and enterprise problems reported in late October and early November 2026 that had **zero replies** or **zero merged fixes**. By correlating the user-reported symptoms and PostgreSQL/Rake error output directly against the live `gitlab-org/gitlab` repository (`master` and `v19.4.5-ee`), we isolated the exact file and line root causes, immediate self-managed customer workarounds, and upstream code patches for three live issues across three different layers of the GitLab stack:

1. **Case 1 (Hard - PostgreSQL Constraint & CE/EE Packaging Split):** Open Issue `gitlab-org/gitlab#638577` (filed November 5, 2026, 0 comments) - User deletion silently hangs forever (`PG::NotNullViolation` on `resource_link_events.user_id` during Ghost User migration).
2. **Case 2 (Medium - Self-Managed Cryptography & Rake Task Scope Mismatch):** Unanswered Forum Thread `forum.gitlab.com/t/135881` (posted October 29, 2026, 0 replies) - `gitlab-rake gitlab:two_factor:rotate_key:apply` leaves `gitlab:doctor:secrets` failing on users with 2FA disabled.
3. **Case 3 (Medium - Enterprise Ultimate Security Policy Hierarchy Truncation):** Unanswered Forum Thread `forum.gitlab.com/t/135642` (posted October 12, 2026, 0 replies) - Why the 6th inherited Pipeline Execution Policy (`inject_policy`) silently disappears across group hierarchies even after raising the 18.3 Admin UI policy limit.

---

## Case 1: User Deletion Silently Hangs Forever on `resource_link_events.user_id` (`gitlab-org/gitlab#638577`)

- **Live Issue URL:**
  https://gitlab.com/gitlab-org/gitlab/-/work_items/638577
- **Reported Environment:** GitLab 19.4.5 Self-Managed Omnibus, PostgreSQL 17.11, Ruby 3.3.12, Rails 7.2.3.1 (filed November 5, 2026; 0 comments).
- **Reported Symptom:** Deleting a user via the Admin UI (`Admin::UsersController#destroy`) or REST API (`DELETE /api/v4/users/:id`) appears to succeed, but the user remains stuck in the `blocked` state forever. The background `Users::GhostUserMigration` job catches a `StandardError`, logs it only to Sentry, and reschedules itself every 30 minutes indefinitely (for over a year on 6 affected users). Bypassing the `rescue StandardError` wrapper in Rails console reveals:
  ```text
  ActiveRecord::NotNullViolation: PG::NotNullViolation: ERROR: null value in column "user_id" of relation "resource_link_events" violates not-null constraint
  DETAIL: Failing row contains (636, 1, null, 85015, 85016, 2024-05-27 09:22:33.919668+00, 215271, 28022).
  CONTEXT: SQL statement "UPDATE ONLY "public"."resource_link_events" SET "user_id" = NULL WHERE $1 OPERATOR(pg_catalog.=) "user_id""
  ```

### Lay Technician Triad Analysis

- **What it is:** A two-part defect combining a self-contradictory PostgreSQL foreign key schema (`NOT NULL` column paired with an `ON DELETE SET NULL` foreign key trigger) and a CE/EE packaging split where the Ghost User batch migration for `WorkItems::ResourceLinkEvent` was placed in the `ee/` directory while the model itself runs in CE (`app/`).
- **How it works in plain English:**
  1. **Why PostgreSQL executes `SET "user_id" = NULL`:** The issue reporter assumed Rails' Ghost User nullify logic was attempting to set `resource_link_events.user_id = NULL`. In fact, `app/models/user.rb` has no `has_many :resource_link_events` association at all. Instead, that `CONTEXT: SQL statement "UPDATE ONLY ... WHERE $1 OPERATOR(pg_catalog.=) "user_id""` string is emitted by PostgreSQL's internal C foreign-key trigger (`ri_triggers.c`) when `user.destroy` executes `DELETE FROM "users"`.
  2. **The Contradictory Database Migrations:** In `db/migrate/20230313031351_create_resource_link_events.rb`, `resource_link_events` was created with `t.bigint :user_id, null: false` (unlike `resource_milestone_events`, where `user_id` is nullable). Sixteen days later, `db/migrate/20230329085754_add_foreign_key_to_resource_link_events_on_user.rb` added `add_concurrent_foreign_key :resource_link_events, :users, column: :user_id, on_delete: :nullify`, creating `CONSTRAINT fk_bd4ae15ce4 FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL` in `db/structure.sql`. Because `user_id` is `NOT NULL`, PostgreSQL's `ON DELETE SET NULL` trigger is guaranteed to throw `PG::NotNullViolation` if any row still references the user being deleted.
  3. **The CE vs. EE Packaging Split (`app/` vs. `ee/app/`):** Why doesn't Ghost User migration re-point `resource_link_events.user_id` to the Ghost User before `DELETE FROM "users"` runs? Because `WorkItems::ResourceLinkEvent` lives in CE (`app/models/work_items/resource_link_event.rb`), allowing CE users to link child Tasks to Issues and populate `resource_link_events`. However, `def migrate_resource_link_events` (`batched_migrate(::WorkItems::ResourceLinkEvent, :user_id)`) was placed inside the EE-only override `ee/app/services/ee/users/migrate_records_to_ghost_user_service.rb` (lines 14-15 and 69-71) instead of the base CE service `app/services/users/migrate_records_to_ghost_user_service.rb`! On any `gitlab-ce` instance (or any code path where the EE override does not run), `resource_link_events` rows are never migrated to the Ghost User before `user.destroy`, tripping `fk_bd4ae15ce4` (`ON DELETE SET NULL`) against `user_id bigint NOT NULL` every 30 minutes forever.
- **Actionability (Customer Workaround & Upstream Patch):**
  - **Immediate Self-Managed Workaround (`gitlab-rails console`):** Reassign orphaned `resource_link_events` rows for pending ghost migrations to `Users::Internal.ghost` before the next 30-minute batch worker run:
    ```ruby
    ghost_id = Users::Internal.ghost.id
    stuck_user_ids = Users::GhostUserMigration.pluck(:user_id)
    WorkItems::ResourceLinkEvent.where(user_id: stuck_user_ids).update_all(user_id: ghost_id)
    Users::MigrateHumanRecordsToGhostUserInBatchesWorker.new.perform
    ```
  - **Upstream Code Fix:**
    1. Move `migrate_resource_link_events` (`batched_migrate(::WorkItems::ResourceLinkEvent, :user_id)`) from `ee/app/services/ee/users/migrate_records_to_ghost_user_service.rb` into `app/services/users/migrate_records_to_ghost_user_service.rb` (directly alongside `batched_migrate(ResourceLabelEvent, :user_id)` in `migrate_notes` or `migrate_records`).
    2. Add a database migration (`change_column_null :resource_link_events, :user_id, true`) so `resource_link_events.user_id` matches its `ON DELETE SET NULL` foreign key (`fk_bd4ae15ce4`) and `resource_milestone_events`.

### Container Reproduction & Fix Verification (`postgres:17-alpine` / PostgreSQL 17.11)

Applying Megan's Rule (*"Never give a customer a script or fix that you haven't run successfully yourself"*), we spun up an isolated `postgres:17-alpine` (`PostgreSQL 17.11`) container loaded with the exact DDL from GitLab `db/structure.sql` (MR `!114394`) and verified both the exact failure and both parts of the fix:

```text
                                         version                                          
------------------------------------------------------------------------------------------
 PostgreSQL 17.11 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit
(1 row)

=== REPRODUCING gitlab#638577 (DELETE FROM users without ghosting resource_link_events) ===
ERROR:  null value in column "user_id" of relation "resource_link_events" violates not-null constraint
DETAIL:  Failing row contains (636, 1, null, 85015, 85016, 2024-05-27 09:22:33.919668+00).
CONTEXT:  SQL statement "UPDATE ONLY "public"."resource_link_events" SET "user_id" = NULL WHERE $1 OPERATOR(pg_catalog.=) "user_id""

=== VERIFYING FIX PART A (Batched ghost migration before DELETE) ===
UPDATE 1
DELETE 1
 id  | action | user_id | issue_id | child_work_item_id 
-----+--------+---------+----------+--------------------
 636 |      1 |       1 |    85015 |              85016
(1 row)

=== VERIFYING FIX PART B (Drop NOT NULL on resource_link_events.user_id for hard_delete / FK safety) ===
INSERT 0 1
INSERT 0 1
ALTER TABLE
DELETE 1
 id  | action | user_id | issue_id | child_work_item_id 
-----+--------+---------+----------+--------------------
 636 |      1 |       1 |    85015 |              85016
 637 |      1 |         |    85015 |              85017
(2 rows)
```

---

## Case 2: `gitlab:two_factor:rotate_key:apply` Leaves `gitlab:doctor:secrets` Failing on 2FA-Disabled Users (`forum.gitlab.com/t/135881`)

- **Live Forum Thread URL:**
  https://forum.gitlab.com/t/otp-key-rotation-not-working-as-expected/135881
- **Reported Environment:** GitLab 19.5.1 EE Self-Managed (posted October 29, 2026; 0 replies).
- **Reported Symptom:** An administrator verified a clean state with `gitlab-rake gitlab:doctor:secrets VERBOSE=1`, rotated `otp_key_base` using `gitlab-rake gitlab:two_factor:rotate_key:apply filename=backup.csv old_key=<old> new_key=<new>`, updated `/etc/gitlab/gitlab-secrets.json`, ran `gitlab-ctl reconfigure`, and re-ran `gitlab-rake gitlab:doctor:secrets VERBOSE=1`. The Doctor reported `User` decryption failures on `otp_secret` specifically for users who had 2FA turned **off**.

### Lay Technician Triad Analysis

- **What it is:** A query-scope mismatch between `Gitlab::OtpKeyRotator#rotate!` (`lib/gitlab/otp_key_rotator.rb`, line 39) and `Gitlab::Doctor::Secrets` (`lib/gitlab/doctor/secrets.rb`, line 68), triggered by GitLab persisting pre-enrollment `encrypted_otp_secret` ciphertexts on users whose `otp_required_for_login` flag is still `false`.
- **How it works in plain English:**
  1. **How 2FA-disabled users get an `encrypted_otp_secret` in PostgreSQL:** In `app/controllers/profiles/two_factor_auths_controller.rb` (lines 144-152, `update_current_user_otp!`) and `app/models/user.rb` (lines 1578-1599, `needs_new_otp_secret?` / `update_otp_secret!`), the moment any user visits `/profile/two_factor_auth` (for example, to view the QR code or when redirected during a 2FA grace period), GitLab generates a new OTP secret (`self.otp_secret = User.generate_otp_secret(OTP_SECRET_LENGTH)`) and saves `encrypted_otp_secret`, `encrypted_otp_secret_iv`, and `encrypted_otp_secret_salt` to the `users` table **before** the user enters a confirmation code to set `otp_required_for_login = true`.
  2. **Why `OtpKeyRotator` skips them:** In `lib/gitlab/otp_key_rotator.rb` (line 39), `rotate!` iterates over:
     ```ruby
     User.with_two_factor.in_batches do |relation|
     ```
     In `app/models/user.rb` (lines 939-942), `User.with_two_factor` only matches users where `otp_required_for_login = true` or a `WebauthnRegistration` exists. It completely skips every user with `otp_required_for_login = false` who has a non-null `encrypted_otp_secret` from an incomplete 2FA setup page visit. (Even worse, for WebAuthn-only users who never generated an OTP secret, `user[:ciphertext]` is `nil`, risking `NoMethodError` on `nil.unpack("m")` in `reencrypt`).
  3. **Why `gitlab:doctor:secrets` fails (and why users break afterward):** `Gitlab::Doctor::Secrets#check_model_attributes` (`lib/gitlab/doctor/secrets.rb`, line 68) checks `User.in_batches` across **all** user rows. Because those `otp_required_for_login = false` rows are still encrypted with `old_key`, decrypting `user.otp_secret` with `new_key` raises `OpenSSL::Cipher::CipherError: bad decrypt` both in `gitlab:doctor:secrets` and whenever those users revisit `/profile/two_factor_auth` or trigger OTP flows.
- **Actionability (Customer Workaround & Upstream Patch):**
  - **Immediate Self-Managed Workaround (`gitlab-rails console`):** Because `otp_required_for_login` is `false` on those accounts, their stored `encrypted_otp_secret` is an unverified pre-enrollment secret. Clearing those stale columns immediately resolves the `gitlab:doctor:secrets` error and allows GitLab to generate a fresh secret encrypted with `new_key` the next time the user visits `/profile/two_factor_auth`:
    ```ruby
    User.where(otp_required_for_login: false)
        .where.not(encrypted_otp_secret: nil)
        .update_all(
          encrypted_otp_secret: nil,
          encrypted_otp_secret_iv: nil,
          encrypted_otp_secret_salt: nil,
          otp_secret_expires_at: nil
        )
    ```
  - **Upstream Code Fix (`lib/gitlab/otp_key_rotator.rb`, line 39):** Scope key rotation to every user row that actually holds an encrypted OTP secret rather than `User.with_two_factor`:
    ```ruby
    User.where.not(encrypted_otp_secret: nil).in_batches do |relation|
    ```

---

## Case 3: 6th Inherited Pipeline Execution Policy Silently Dropped on Self-Managed Ultimate (`forum.gitlab.com/t/135642`)

- **Live Forum Thread URL:**
  https://forum.gitlab.com/t/pipeline-execution-policies-is-the-per-pipeline-limit-of-5-configurable-on-self-managed/135642
- **Reported Environment:** GitLab 19.1 Self-Managed Ultimate (posted October 12, 2026; 0 replies).
- **Reported Symptom:** Top-level group `A` links security policy project `P1` (3 `inject_policy` pipeline execution policies). Subgroup `A/B` links security policy project `P2` (3 `inject_policy` pipeline execution policies). GraphQL shows all 6 inherited policies enabled, and CI Lint passes on all 6, but the 6th policy (the last one in `P2`) never runs in any pipeline and produces zero warnings or errors, even after increasing the Admin Area setting *"Maximum number of pipeline execution policies allowed per security policy configuration"*.

### Lay Technician Triad Analysis

- **What it is:** A hardcoded per-pipeline cap (`DEFAULT_LIMITS[:pipeline_execution_policies][:pipeline] = 5`) in `Security::SecurityOrchestrationPolicies::LimitService` that ignores the Admin UI setting, paired with a silent `.first(5)` array slice in `Gitlab::Security::Orchestration::ProjectPipelineExecutionPolicies#configs`.
- **How it works in plain English:**
  1. **Why the Admin UI setting does not increase the per-pipeline limit (`ee/app/services/security/security_orchestration_policies/limit_service.rb`, lines 6-10, 44-46, 88-90):** `LimitService` defines two separate limits under `DEFAULT_LIMITS[:pipeline_execution_policies]`: `configuration: 5` and `pipeline: 5`. While `pipeline_execution_policies_per_configuration_limit` checks `root_namespace_limit_for_policy_type` and `Gitlab::CurrentSettings.pipeline_execution_policies_per_configuration_limit` (controlling how many policies can live inside a single `policy.yml` file), `pipeline_execution_policies_per_pipeline_limit` (lines 44-46) is hardcoded to return `DEFAULT_LIMITS.dig(:pipeline_execution_policies, :pipeline)` (`5`) with **no `CurrentSettings` or namespace lookup**.
  2. **Why the 3rd policy in subgroup `A/B` is the exact one dropped (`ee/lib/gitlab/security/orchestration/project_pipeline_execution_policies.rb`, lines 19-27 and 45-55):** `configs_ordered_by_hierarchy` orders policy configurations from top-level group (`A`) down to subgroup (`A/B`) and project. `ProjectPipelineExecutionPolicies#configs` then executes:
     ```ruby
     applicable_execution_policies_by_hierarchy
       .first(policy_limit)
       .reverse
     ```
     Because `A` contributes policies 1-3 and `A/B` contributes policies 4-6, `.first(5)` keeps all 3 policies from `A` plus the first 2 from `A/B`, silently discarding the 3rd policy from `A/B` before `PipelineContext#build_policy_pipelines!` (`ee/lib/gitlab/ci/pipeline/pipeline_execution_policies/pipeline_context.rb`, line 53) ever sees it. Because truncation happens via `.first(5)` prior to pipeline creation, zero pipeline warnings, audit events, or application log entries are emitted.
  3. **Nested `include:` Support & Access Control Gotcha (`pipeline_context.rb`, lines 79-91):** Combining two scanners into one policy CI file using `include:` works reliably and stays under the 5-policy-per-pipeline cap, **provided** the included YAML files either reside inside the same Security Policy Project (`P2`) or pass `ci_config_access_allowed?` (`config_project.project_setting.allows_pipeline_execution_policy_ci_config_access?`), because policy pipelines evaluate with the triggering user's `current_user` permissions outside the granted policy management project.
- **Actionability (Customer Workaround & Upstream Improvement):**
  - **Verified Customer Workaround:** Consolidate the 3 subgroup `A/B` policies in `P2` into 1 or 2 pipeline execution policy entries in `.gitlab/security-policies/policy.yml` that `include: [{ local: ... }]` the individual scanner YAML files from within `P2` itself (avoiding cross-project `current_user` permission failures).
  - **Upstream Code Fix:** Wire `LimitService#pipeline_execution_policies_per_pipeline_limit` to an application setting (or emit a structured `AppJsonLogger` warning when `applicable_execution_policies_by_hierarchy.size > policy_limit` in `ProjectPipelineExecutionPolicies#configs`).
