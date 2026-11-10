#!/usr/bin/env bash
# /usr/local/bin/check_user_changes.sh
# Compares current MD5 checksum of user:homedir list against /var/log/current_users.
# Logs "DATE TIME changes occurred" (UTC/Zulu with milliseconds) to /var/log/user_changes
# per the assessment spec, AND records the exact added/removed user:homedir diff in
# /var/log/user_changes.diff.log whenever a change is detected.

set -euo pipefail

# Ensure standard system binaries are found when executed by cron
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

LIST_SCRIPT="${LIST_SCRIPT:-${LIST_USERS_SCRIPT:-/usr/local/bin/list_users.sh}}"
CURRENT_USERS_FILE="${CURRENT_USERS_FILE:-/var/log/current_users}"
SNAPSHOT_FILE="${SNAPSHOT_FILE:-${CURRENT_USERS_SNAPSHOT_FILE:-${CURRENT_USERS_FILE}.snapshot}}"
CHANGES_LOG_FILE="${CHANGES_LOG_FILE:-${USER_CHANGES_FILE:-/var/log/user_changes}}"
DIFF_LOG_FILE="${DIFF_LOG_FILE:-${USER_CHANGES_DIFF_FILE:-${CHANGES_LOG_FILE}.diff.log}}"

# Capture current user:homedir list and calculate MD5 checksum (32-char hex string)
current_list="$("$LIST_SCRIPT")"
new_md5="$(printf '%s\n' "$current_list" | /usr/bin/md5sum | /usr/bin/awk '{print $1}')"

# First run: if /var/log/current_users does not exist, is empty, or contains only whitespace, initialize baseline
stored_md5="$(/usr/bin/awk '{print $1}' "$CURRENT_USERS_FILE" 2>/dev/null || true)"
if [[ ! -s "$CURRENT_USERS_FILE" || -z "$stored_md5" ]]; then
    printf '%s\n' "$new_md5" > "$CURRENT_USERS_FILE"
    printf '%s\n' "$current_list" > "$SNAPSHOT_FILE"
    exit 0
fi

# Compare checksums; if different, log exact required line to /var/log/user_changes,
# log actionable +/- user:homedir diff to /var/log/user_changes.diff.log, and update baseline
if [[ "$new_md5" != "$stored_md5" ]]; then
    ts="$(/usr/bin/date -u '+%Y-%m-%d %H:%M:%S.%3NZ')"
    printf '%s changes occurred\n' "$ts" >> "$CHANGES_LOG_FILE"
    if [[ -f "$SNAPSHOT_FILE" ]]; then
        {
            printf '=== %s (MD5: %s -> %s) ===\n' "$ts" "$stored_md5" "$new_md5"
            /usr/bin/diff -u --label "previous_users" --label "current_users" "$SNAPSHOT_FILE" <(printf '%s\n' "$current_list") | /usr/bin/grep -E '^[+-][^+-]' || true
        } >> "$DIFF_LOG_FILE"
    fi
    printf '%s\n' "$new_md5" > "$CURRENT_USERS_FILE"
    printf '%s\n' "$current_list" > "$SNAPSHOT_FILE"
fi
