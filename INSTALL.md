# Installation

This guide installs Garmin Health Gateway on a 64-bit Docker host and connects it to ChatGPT through an outbound-only OpenAI Secure MCP Tunnel. No inbound firewall rule, router port forwarding, public hostname, or TLS certificate is required.

The installation starts six Compose services:

- `postgres`: private PostgreSQL database
- `collector`: scheduled Garmin synchronization and FIT-file archiving
- `mcp`: read-only MCP server
- `tunnel-client`: outbound connection to OpenAI
- `volume-init` and `migrate`: one-shot initialization jobs

## 1. Check the host

Use a 64-bit `arm64` or `amd64` machine with at least 2 GB RAM and durable storage. A native Linux host is recommended for continuous operation. Raspberry Pi users should install a 64-bit operating system.

Install [Docker Engine](https://docs.docker.com/engine/install/) and the [Docker Compose plugin](https://docs.docker.com/compose/install/) on Linux. On macOS or Windows, install [Docker Desktop](https://docs.docker.com/desktop/); Windows users should run the project commands in WSL or Git Bash with Linux containers enabled.

Confirm the prerequisites:

```bash
uname -m
git --version
docker --version
docker compose version
```

`uname -m` should normally report `aarch64`, `arm64`, or `x86_64`. This project does not support 32-bit ARM hosts.

The host must be able to reach Garmin Connect and `api.openai.com:443` over outbound HTTPS. Do not open port 8000 or any PostgreSQL port on the router or firewall.

## 2. Download the project

```bash
git clone https://github.com/cara-labs/garmin-health-mcp-gateway.git
cd garmin-health-mcp-gateway
```

Run all remaining commands from this directory.

## 3. Create the OpenAI tunnel

Open [OpenAI Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels) in a browser.

1. Select the Platform organization that will own the tunnel.
2. Create a tunnel and associate it with the ChatGPT workspace or personal account that will use the gateway.
3. Copy its `tunnel_...` identifier.
4. Create a restricted runtime API key for the tunnel client with **Tunnels Read + Use**.

Creating or editing a tunnel requires **Tunnels Read + Manage**. Running the client and selecting the tunnel in ChatGPT requires **Tunnels Read + Use**. ChatGPT developer-mode access is a separate workspace permission. See the official [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

Treat the runtime API key like a password. Do not paste it into `.env`, commit it, or share it in an issue or chat.

## 4. Configure non-secret settings

```bash
cp .env.example .env
```

Open `.env` in a text editor and set:

```dotenv
TZ=America/Los_Angeles
OPENAI_TUNNEL_ID=tunnel_replace_me
```

Replace the timezone with your [IANA timezone name](https://www.iana.org/time-zones) and replace `tunnel_replace_me` with the tunnel ID from the previous step. The backfill and scheduler defaults are suitable for a first installation.

Leave `TUNNEL_CLIENT_IMAGE` pinned to the tested version unless you are intentionally performing an upgrade.

## 5. Create secret files

Run the included setup script:

```bash
chmod +x scripts/setup-secrets.sh
./scripts/setup-secrets.sh
```

It prompts locally for:

- Garmin account email
- Garmin account password
- OpenAI tunnel runtime API key

It also generates separate PostgreSQL administrator and MCP reader passwords. Secret files are stored under `secrets/` with restrictive permissions and are excluded by `.gitignore`. Do not print or commit their contents.

Validate the Compose configuration:

```bash
docker compose config --quiet
```

## 6. Build the application containers

```bash
docker compose build --pull collector mcp migrate
```

The first build downloads the pinned Python base image and dependencies and can take several minutes on a Raspberry Pi.

## 7. Authenticate with Garmin

Create the renewable Garmin session before starting the scheduler:

```bash
docker compose run --rm collector auth
```

If Garmin requests multi-factor authentication, enter the code at the prompt. The resulting Garmin tokens are stored in a private Docker volume, not in the repository. Rerun this command if Garmin later invalidates the session.

## 8. Start the gateway

```bash
docker compose up -d
docker compose ps -a
```

Expected state:

- `postgres`, `collector`, `mcp`, and `tunnel-client` are running.
- `postgres`, `mcp`, and eventually `collector` report healthy.
- `volume-init` and `migrate` exit successfully with status `0`; they are one-shot jobs.

Watch startup and synchronization:

```bash
docker compose logs -f collector tunnel-client
```

Press `Ctrl+C` to stop following the logs; the containers continue running. The initial synchronization backfills 90 days of daily health and 365 days of activities, so it may take a while. Later cycles synchronize recent corrections every hour.

After the initial sync finishes, verify collector freshness:

```bash
docker compose exec collector garmin-health check --max-age-hours 3
```

## 9. Connect the tunnel in ChatGPT

Use ChatGPT on the web for the one-time connection setup:

1. Open **Settings → Security and login** and enable **Developer mode**.
2. Open [ChatGPT Plugins](https://chatgpt.com/#settings/Connectors/Advanced).
3. Select the plus button to create a developer-mode app.
4. Enter a name such as **Garmin Health Gateway** and a short description.
5. Under **Connection**, choose **Tunnel**.
6. Select the tunnel created earlier, or paste its `tunnel_...` ID.
7. Create the connection and review the discovered tools.

The connection should discover 12 tools. Every tool should be marked read-only; there is no raw SQL or mutation tool. OpenAI's current workflow is documented in [Connect and test your plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt).

If the tunnel is missing from the list, verify that it is associated with the correct ChatGPT workspace, the current user has **Tunnels Read + Use**, developer mode is enabled, and `docker compose ps` shows `tunnel-client` running.

## 10. Use it from the ChatGPT phone app

Sign in to the phone app with the same ChatGPT account and workspace used during setup. Start a new conversation, open the tools/apps menu, and select **Garmin Health Gateway**.

Example requests:

- “Show my sleep and HRV trend for the last 14 days.”
- “What were my five most recent runs?”
- “Compare this week's resting heart rate with last week.”
- “When did the Garmin collector last synchronize successfully?”

The Docker host and tunnel client must remain online for ChatGPT to retrieve fresh data.

## Routine operation

Check service status and recent logs:

```bash
docker compose ps -a
docker compose logs --since=2h collector tunnel-client
```

Run an immediate incremental sync:

```bash
docker compose exec collector garmin-health sync
```

Repeat the configured historical backfill windows:

```bash
docker compose exec collector garmin-health sync --backfill
```

Stop and restart without removing containers:

```bash
docker compose stop
docker compose start
```

## Upgrade

Back up the PostgreSQL, FIT archive, and Garmin token volumes before upgrading. Then run:

```bash
git pull --ff-only
docker compose pull postgres volume-init tunnel-client
docker compose up -d --build --remove-orphans
docker compose ps -a
```

Review Garmin client and OpenAI tunnel-client release notes before changing pinned dependency or image versions.

## Remove the containers

To remove containers and project networks while retaining all health data and tokens:

```bash
docker compose down
```

Do not add `--volumes` unless you intentionally want to permanently delete the PostgreSQL database, FIT archive, and Garmin authentication tokens.

## Troubleshooting

### Docker permission denied

Run Docker with an account allowed to access the Docker daemon. On Linux, follow Docker's official [post-install guidance](https://docs.docker.com/engine/install/linux-postinstall/). Membership in the `docker` group is effectively root-level access.

### A secret file is missing

Rerun `./scripts/setup-secrets.sh`, then confirm the filenames exist without printing their contents:

```bash
find secrets -maxdepth 1 -type f -print
```

### Garmin authentication or MFA fails

Confirm the host clock and timezone are correct, then rerun:

```bash
docker compose run --rm collector auth
```

### Collector is unhealthy during the first run

The initial backfill can exceed the health-check freshness window on a slow host. Inspect `docker compose logs collector`; wait for a successful sync before treating this as a failure.

### The tunnel is not connected

```bash
docker compose ps tunnel-client mcp
docker compose logs --since=30m tunnel-client mcp
```

Confirm outbound HTTPS to `api.openai.com:443`, the `OPENAI_TUNNEL_ID` value, the runtime key, the tunnel's workspace association, and **Tunnels Read + Use** permission. No inbound port should be opened as a workaround.

### ChatGPT shows an old tool list

Restart the services, open the developer-mode connection in ChatGPT, and select **Refresh**:

```bash
docker compose restart mcp tunnel-client
```
