# ============================================================================
# Linux, PostgreSQL & Container Support Field Playbook
# Megan's Rule Reference Verification Container
# "Never give a customer a script that you haven't run successfully yourself"
# ============================================================================
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash cron coreutils gawk ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create baseline service account
RUN useradd -m -s /bin/bash appuser

# Install scripts into /usr/local/bin
COPY list_users.sh /usr/local/bin/list_users.sh
COPY check_user_changes.sh /usr/local/bin/check_user_changes.sh

RUN chmod 0755 /usr/local/bin/list_users.sh /usr/local/bin/check_user_changes.sh

# Install root hourly crontab entry
RUN printf '0 * * * * /usr/local/bin/check_user_changes.sh\n' | crontab -

# Execute Megan's Rule automated verification harness:
# Run 1 (baseline) -> Run 2 (idempotent no change) -> Add user -> Run 3 (change detected)
RUN echo "=== VERIFY: SAMPLE list_users.sh OUTPUT ===" && \
    /usr/local/bin/list_users.sh | grep -E '^(appuser|nobody):' && \
    echo "=== VERIFY: RUN 1 (INITIAL BASELINE) ===" && \
    /usr/local/bin/check_user_changes.sh && \
    cat /var/log/current_users && \
    test ! -f /var/log/user_changes && \
    echo "=== VERIFY: RUN 2 (IDEMPOTENT NO-CHANGE) ===" && \
    /usr/local/bin/check_user_changes.sh && \
    test ! -f /var/log/user_changes && \
    echo "=== VERIFY: RUN 3 (AFTER ADDING USER) ===" && \
    useradd -m -s /bin/bash support_test && \
    /usr/local/bin/check_user_changes.sh && \
    echo "Updated /var/log/current_users:" && cat /var/log/current_users && \
    echo "Contents of /var/log/user_changes:" && cat /var/log/user_changes && \
    echo "Verified root crontab:" && crontab -l
