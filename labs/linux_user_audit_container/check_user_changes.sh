#!/usr/bin/env bash
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

LIST_USERS_SCRIPT="${LIST_USERS_SCRIPT:-/usr/local/bin/list_users.sh}"
CURRENT_USERS_FILE="${CURRENT_USERS_FILE:-/var/log/current_users}"
USER_CHANGES_FILE="${USER_CHANGES_FILE:-/var/log/user_changes}"

current_md5="$("$LIST_USERS_SCRIPT" | md5sum | awk '{print $1}')"
stored_md5="$(awk '{print $1}' "$CURRENT_USERS_FILE" 2>/dev/null || true)"

if [[ ! -s "$CURRENT_USERS_FILE" || -z "$stored_md5" ]]; then
    printf '%s\n' "$current_md5" > "$CURRENT_USERS_FILE"
    exit 0
fi

if [[ "$current_md5" != "$stored_md5" ]]; then
    timestamp="$(date -u '+%Y-%m-%d %H:%M:%S.%3NZ')"
    printf '%s changes occurred\n' "$timestamp" >> "$USER_CHANGES_FILE"
    printf '%s\n' "$current_md5" > "$CURRENT_USERS_FILE"
fi
