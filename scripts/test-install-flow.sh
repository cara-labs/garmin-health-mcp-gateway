#!/usr/bin/env bash
set -euo pipefail

mode="${1:-published}"
if [[ "${mode}" != "published" && "${mode}" != "build" ]]; then
  printf 'Usage: %s [published|build]\n' "$0" >&2
  exit 2
fi

for command_name in docker mktemp; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "${command_name}" >&2
    exit 1
  fi
done

project_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
test_dir="$(mktemp -d "${TMPDIR:-/tmp}/garmin-health-install-test.XXXXXX")"
project_name="garmin-health-install-test-${mode}-$$"
compose_files=(-f compose.yaml)
if [[ "${mode}" == "build" ]]; then
  compose_files+=(-f compose.build.yaml)
fi

cleanup() {
  exit_status=$?
  if [[ -d "${test_dir}" ]]; then
    (
      cd "${test_dir}"
      if [[ "${exit_status}" != "0" ]]; then
        printf '\nInstallation smoke test failed; service status and logs follow.\n' >&2
        docker compose --project-name "${project_name}" \
          --env-file .env "${compose_files[@]}" ps -a >&2 || true
        docker compose --project-name "${project_name}" \
          --env-file .env "${compose_files[@]}" \
          logs --no-color postgres migrate mcp volume-init >&2 || true
      fi
      docker compose --project-name "${project_name}" \
        --env-file .env "${compose_files[@]}" \
        down --volumes --remove-orphans >/dev/null 2>&1 || true
    )
    rm -rf -- "${test_dir}"
  fi
}
trap cleanup EXIT

cp "${project_dir}/compose.yaml" "${project_dir}/.env.example" "${test_dir}/"
if [[ "${mode}" == "build" ]]; then
  cp "${project_dir}/compose.build.yaml" "${project_dir}/Dockerfile" \
    "${project_dir}/pyproject.toml" "${project_dir}/README.md" \
    "${project_dir}/LICENSE" "${project_dir}/THIRD_PARTY_NOTICES.md" "${test_dir}/"
  cp -R "${project_dir}/src" "${project_dir}/migrations" "${test_dir}/"
fi

cd "${test_dir}"
cp .env.example .env
sed -i "s/^GATEWAY_UID=.*/GATEWAY_UID=$(id -u)/" .env
sed -i "s/^GATEWAY_GID=.*/GATEWAY_GID=$(id -g)/" .env
mkdir -m 700 secrets
printf '%s' 'install-test@example.invalid' > secrets/garmin_email.txt
printf '%s' 'not-a-real-garmin-password' > secrets/garmin_password.txt
printf '%s' 'not-a-real-openai-key' > secrets/openai_tunnel_api_key.txt
printf '%s' 'install-test-postgres-password' > secrets/postgres_password.txt
printf '%s' 'install-test-reader-password' > secrets/mcp_reader_password.txt
chmod 600 secrets/*.txt

compose=(docker compose --project-name "${project_name}" --env-file .env "${compose_files[@]}")

"${compose[@]}" config --quiet

if [[ "${mode}" == "published" ]]; then
  "${compose[@]}" pull postgres volume-init migrate mcp
else
  "${compose[@]}" pull postgres volume-init
  "${compose[@]}" build --pull migrate
fi

"${compose[@]}" up --no-deps volume-init
"${compose[@]}" up -d --wait --wait-timeout 180 mcp

volume_init_id="$("${compose[@]}" ps -aq volume-init)"
migrate_id="$("${compose[@]}" ps -aq migrate)"
if [[ "$(docker inspect --format '{{.State.ExitCode}}' "${volume_init_id}")" != "0" ]]; then
  printf 'volume-init did not exit successfully\n' >&2
  exit 1
fi
if [[ "$(docker inspect --format '{{.State.ExitCode}}' "${migrate_id}")" != "0" ]]; then
  printf 'migrate did not exit successfully\n' >&2
  exit 1
fi

"${compose[@]}" exec -T mcp python -c \
  "import socket; connection = socket.create_connection(('127.0.0.1', 8000), 5); connection.close()"

host_arch="$(uname -m)"
case "${host_arch}" in
  aarch64 | arm64) expected_arch=arm64 ;;
  x86_64 | amd64) expected_arch=amd64 ;;
  *) expected_arch="${host_arch}" ;;
esac
mcp_image_id="$("${compose[@]}" images -q mcp)"
image_arch="$(docker image inspect --format '{{.Architecture}}' "${mcp_image_id}")"
if [[ "${image_arch}" != "${expected_arch}" ]]; then
  printf 'Expected %s image on %s host, got %s\n' \
    "${expected_arch}" "${host_arch}" "${image_arch}" >&2
  exit 1
fi

"${compose[@]}" ps -a
printf 'Installation smoke test passed (%s image, %s).\n' "${mode}" "${image_arch}"
