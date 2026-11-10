# syntax=docker/dockerfile:1
# Megan's Rule Reference Verification Container
# Verifies Question 1 (Linux User Audit + Smart Diff under 520-user load & cron env -i)
# and Question 3 (Git Commit Graph Reproduction from an empty directory).

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash coreutils gawk passwd cron git ca-certificates diffutils grep \
    && rm -rf /var/lib/apt/lists/*

COPY list_users.sh /usr/local/bin/list_users.sh
COPY check_user_changes.sh /usr/local/bin/check_user_changes.sh

RUN chmod 0755 /usr/local/bin/list_users.sh /usr/local/bin/check_user_changes.sh

# Simulate an enterprise host with 500+ local accounts
RUN for i in $(seq 1 500); do \
      useradd -M -d "/home/corpuser_${i}" -s /bin/bash "corpuser_${i}"; \
    done

# Verify Question 1 under cron's stripped environment (env -i) and Question 3 Git graph
RUN /usr/bin/env -i /usr/local/bin/check_user_changes.sh && \
    /usr/bin/env -i /usr/local/bin/check_user_changes.sh && \
    useradd -m -d /home/gitlab_support -s /bin/bash gitlab_support && \
    /usr/bin/env -i /usr/local/bin/check_user_changes.sh && \
    userdel -r gitlab_support && \
    /usr/bin/env -i /usr/local/bin/check_user_changes.sh && \
    echo "=== /var/log/user_changes ===" && \
    cat /var/log/user_changes && \
    echo "=== /var/log/user_changes.diff.log ===" && \
    cat /var/log/user_changes.diff.log && \
    mkdir -p /tmp/q3_verify && cd /tmp/q3_verify && \
    git init -b main && \
    git config user.name "GitLab Support" && \
    git config user.email "support@example.com" && \
    echo "first" > file.txt && git add file.txt && git commit -m "first commit" && \
    echo "second" >> file.txt && git add file.txt && git commit -m "second commit" && \
    git checkout -b feature-branch && \
    echo "feature" > feature.txt && git add feature.txt && git commit -m "awesome feature" && \
    git checkout main && \
    echo "third" >> file.txt && git add file.txt && git commit -m "third commit" && \
    git merge feature-branch -m "Merge" && \
    echo "fourth" >> file.txt && git add file.txt && git commit -m "fourth commit" && \
    git log --all --decorate --oneline --graph
