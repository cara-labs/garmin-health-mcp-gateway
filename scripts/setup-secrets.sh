#!/usr/bin/env bash
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
secret_dir="${project_dir}/secrets"
mkdir -p "${secret_dir}"
chmod 700 "${secret_dir}"

read -r -p "Garmin account email: " garmin_email
read -r -s -p "Garmin account password: " garmin_password
printf '\n'
read -r -s -p "OpenAI tunnel runtime API key: " tunnel_key
printf '\n'

umask 077
printf '%s' "${garmin_email}" > "${secret_dir}/garmin_email.txt"
printf '%s' "${garmin_password}" > "${secret_dir}/garmin_password.txt"
printf '%s' "${tunnel_key}" > "${secret_dir}/openai_tunnel_api_key.txt"

if [[ ! -s "${secret_dir}/postgres_password.txt" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 36 | tr -d '\n' > "${secret_dir}/postgres_password.txt"
  else
    read -r -s -p "PostgreSQL password: " postgres_password
    printf '\n'
    printf '%s' "${postgres_password}" > "${secret_dir}/postgres_password.txt"
  fi
fi

if [[ ! -s "${secret_dir}/mcp_reader_password.txt" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 36 | tr -d '\n' > "${secret_dir}/mcp_reader_password.txt"
  else
    read -r -s -p "MCP read-only PostgreSQL password: " mcp_reader_password
    printf '\n'
    printf '%s' "${mcp_reader_password}" > "${secret_dir}/mcp_reader_password.txt"
  fi
fi

chmod 600 "${secret_dir}"/*.txt
printf 'Secret files created in %s\n' "${secret_dir}"
