#!/usr/bin/env bash
# Create random database passwords under ./secrets. Never overwrites existing files.
# The directory is 700 (host-private); the files are 644 so the non-root container user
# (uid 10001) can read the bind-mounted copies. They must never be committed (see .gitignore).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p secrets
chmod 700 secrets
for name in db_superuser_password db_owner_password db_app_password; do
  file="secrets/$name"
  if [ -e "$file" ]; then
    echo "keeping existing $file"
  else
    umask 077
    openssl rand -hex 32 | tr -d '\n' > "$file"
    chmod 644 "$file"
    echo "created $file"
  fi
done
# Alpaca MARKET DATA keys (data-only; never put trading keys here) and the research-agent LLM key. Empty placeholders keep Compose
# happy; an empty file means "not configured" and the worker then runs without a data provider.
for name in alpaca_key_id alpaca_secret llm_api_key; do
  file="secrets/$name"
  if [ -e "$file" ]; then
    echo "keeping existing $file"
  else
    : > "$file"
    chmod 644 "$file"
    echo "created empty $file (paste your Alpaca data key in it)"
  fi
done
