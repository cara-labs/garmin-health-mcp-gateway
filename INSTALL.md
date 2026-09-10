# Raspberry Pi installation

This guide starts with a new Raspberry Pi, installs Docker, deploys Garmin Health Gateway, and connects it to ChatGPT through an outbound-only OpenAI Secure MCP Tunnel. No inbound firewall rule, router port forwarding, public hostname, or TLS certificate is required.

The installation starts six Compose services:

- `postgres`: private PostgreSQL database
- `collector`: scheduled Garmin synchronization and FIT-file archiving
- `mcp`: data-query MCP server with a bounded on-demand sync request tool
- `tunnel-client`: outbound connection to OpenAI
- `volume-init` and `migrate`: one-shot initialization jobs

## Before you begin

You need:

- A 64-bit Raspberry Pi with at least 2 GB RAM. A Pi 4 or Pi 5 with 4 GB or more is recommended.
- Raspberry Pi OS Lite (64-bit) or Raspberry Pi OS Desktop (64-bit).
- A microSD card or SSD with enough durable storage for PostgreSQL and downloaded FIT files. An SSD is preferable for continuous operation.
- Your Garmin Connect email and password, plus access to any MFA method enabled on the account.
- A ChatGPT account or workspace that can create developer-mode apps and use an OpenAI Secure MCP Tunnel.
- Outbound HTTPS access to Garmin Connect, GitHub Container Registry, and `api.openai.com:443`.

The gateway also works on other 64-bit ARM64 or AMD64 Docker machines. For those hosts, install Docker Engine and Compose v2 using the operating system's official instructions, then continue at [Step 4](#4-download-the-project).

## 1. Install the 64-bit Raspberry Pi operating system

On another computer, use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to write **Raspberry Pi OS Lite (64-bit)** to the Pi's storage.

In the Imager customization screen:

1. Set a hostname, for example `garmin-pi`.
2. Create a non-default username and a strong password.
3. Configure Wi-Fi if the Pi will not use Ethernet.
4. Enable SSH with password or public-key authentication.
5. Set the correct timezone and keyboard layout.

Boot the Pi and connect from another computer:

```bash
ssh your-user@garmin-pi.local
```

Use the Pi's IP address instead of `garmin-pi.local` if local hostname discovery is unavailable.

## 2. Verify and update the Pi

Confirm that the installed operating system is 64-bit:

```bash
uname -m
getconf LONG_BIT
```

The expected results are `aarch64` and `64`. Stop here and reinstall a 64-bit OS if `uname -m` reports `armv7l` or `getconf LONG_BIT` reports `32`; the gateway image does not support 32-bit ARM.

Update the Pi and install the command-line prerequisites:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y ca-certificates curl git openssl
sudo timedatectl set-ntp true
sudo reboot
```

Reconnect over SSH after the reboot. Check available resources:

```bash
free -h
df -h /
timedatectl status
```

Correct time is important for Garmin authentication and TLS connections.

## 3. Install Docker Engine and Compose

Raspberry Pi OS 64-bit uses Docker's Debian ARM64 packages. Add Docker's signed APT repository:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Allow your current account to run Docker. Membership in the `docker` group grants root-equivalent control of the host, so add only trusted administrators:

```bash
sudo usermod -aG docker "$USER"
exit
```

Reconnect over SSH so the new group membership takes effect, then test Docker:

```bash
docker run --rm hello-world
docker version
docker compose version
```

Use `docker compose`, with a space. The older standalone `docker-compose` command is not used by this project.

The host must be able to reach Garmin Connect and `api.openai.com:443` over outbound HTTPS. Do not open port 8000 or any PostgreSQL port on the router or firewall.

## 4. Download the project

```bash
git clone https://github.com/cara-labs/garmin-health-mcp-gateway.git
cd garmin-health-mcp-gateway
```

Run all remaining commands from this directory.

Optionally run the isolated installation smoke test before entering any real credentials:

```bash
./scripts/test-install-flow.sh published
```

The test creates temporary dummy secrets and volumes, starts PostgreSQL, applies the migration, checks the MCP service, verifies that Docker selected an ARM64 image, and removes the temporary containers and volumes. It does not contact Garmin or OpenAI and does not touch a configured production stack.

## 5. Create the OpenAI tunnel

Open [OpenAI Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels) in a browser.

1. Select the Platform organization that will own the tunnel.
2. Create a tunnel and associate it with the ChatGPT workspace or personal account that will use the gateway.
3. Copy its `tunnel_...` identifier.
4. Create a restricted runtime API key for the tunnel client with **Tunnels Read + Use**.

Creating or editing a tunnel requires **Tunnels Read + Manage**. Running the client and selecting the tunnel in ChatGPT requires **Tunnels Read + Use**. ChatGPT developer-mode access is a separate workspace permission. See the official [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

Treat the runtime API key like a password. Do not paste it into `.env`, commit it, or share it in an issue or chat.

## 6. Configure non-secret settings

```bash
cp .env.example .env
sed -i "s/^GATEWAY_UID=.*/GATEWAY_UID=$(id -u)/" .env
sed -i "s/^GATEWAY_GID=.*/GATEWAY_GID=$(id -g)/" .env
```

Open `.env` in a text editor and set:

```bash
nano .env
```

```dotenv
TZ=America/Los_Angeles
GATEWAY_UID=1000
GATEWAY_GID=1000
OPENAI_TUNNEL_ID=tunnel_replace_me
```

Replace the timezone with your [IANA timezone name](https://www.iana.org/time-zones) and replace `tunnel_replace_me` with the tunnel ID from the previous step. The two `sed` commands set `GATEWAY_UID` and `GATEWAY_GID` to the account that owns the mode-`0600` secret files; do not replace them with IDs from another account. The backfill and scheduler defaults are suitable for a first installation.

Leave `TUNNEL_CLIENT_IMAGE` pinned to the tested version unless you are intentionally performing an upgrade.

## 7. Create secret files

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

## 8. Choose an image installation method

### Recommended: pull the published image

This downloads the tested, versioned image built by GitHub Actions. It is the fastest and simplest option, especially on a Raspberry Pi.

```bash
docker compose pull
```

The gateway image is published for `linux/arm64` and `linux/amd64`. Docker automatically pulls the correct platform. The image is version-pinned through `GARMIN_GATEWAY_IMAGE` in `.env`; do not use an unpinned development image for production.

### Fallback: build the image locally

Use this option when the published image is unavailable, when you changed the source, or when you want to audit the complete build. “Build locally” means Docker executes this repository's `Dockerfile` on your host. It still downloads the pinned Python base image and installs declared third-party packages such as `garminconnect`, MCP, and Psycopg; it does not mean offline or dependency-free operation.

Build with the opt-in override:

```bash
docker compose -f compose.yaml -f compose.build.yaml build --pull
```

On a Raspberry Pi this takes longer than pulling the published image. To test the complete fallback installation in an isolated temporary stack first, run:

```bash
./scripts/test-install-flow.sh build
```

When using the local build, include both `-f` arguments in subsequent `docker compose` commands. For example:

```bash
docker compose -f compose.yaml -f compose.build.yaml run --rm collector auth
docker compose -f compose.yaml -f compose.build.yaml up -d
```

Both methods run the same gateway code. The published release also includes an SBOM and build provenance. The repository's Apache 2.0 license covers its original code; dependencies keep the licenses listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## 9. Authenticate with Garmin

Create the renewable Garmin session before starting the scheduler:

```bash
docker compose run --rm collector auth
```

If Garmin requests multi-factor authentication, enter the code at the prompt. The resulting Garmin tokens are stored in a private Docker volume, not in the repository. Rerun this command if Garmin later invalidates the session.

## 10. Start the gateway

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

## 11. Connect the tunnel in ChatGPT

Use ChatGPT on the web for the one-time connection setup:

1. Open **Settings → Security and login** and enable **Developer mode**.
2. Open [ChatGPT Plugins](https://chatgpt.com/#settings/Connectors/Advanced).
3. Select the plus button to create a developer-mode app.
4. Enter a name such as **Garmin Health Gateway** and a short description.
5. Under **Connection**, choose **Tunnel**.
6. Select the tunnel created earlier, or paste its `tunnel_...` ID.
7. Create the connection and review the discovered tools.

The connection should discover 13 tools: 12 read-only queries and `request_sync`, which requests local data updates from Garmin. There is no raw SQL tool. OpenAI's current workflow is documented in [Connect and test your plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt).

To refresh from chat, ask “Sync my latest Garmin health and activities.” The new tool queues the last three calendar days; use `get_sync_status` to check `ad_hoc.status` and only treat `success` as completion. Queued jobs wait for any active sync and initial backfill. A five-minute cooldown limits repeat requests. See [README](README.md#mcp-tools) for error and restart behavior.

When upgrading from 1.0.x, pull the updated repository/Compose file and set `GARMIN_GATEWAY_IMAGE=ghcr.io/cara-labs/garmin-health-mcp-gateway:1.1.0` in your existing `.env`. Run `docker compose pull` then `docker compose up -d`; this creates the shared `sync_requests` volume. Refresh the connection's tool list in ChatGPT to discover `request_sync`. Changing only the image without updating Compose is insufficient. Existing credentials, database, and Garmin tokens are retained.

If the tunnel is missing from the list, verify that it is associated with the correct ChatGPT workspace, the current user has **Tunnels Read + Use**, developer mode is enabled, and `docker compose ps` shows `tunnel-client` running.

## 12. Use it from the ChatGPT phone app

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
docker compose pull
docker compose up -d --remove-orphans
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

### Permission denied while reading `/run/secrets/...`

Make sure the numeric IDs in `.env` match the account that ran `setup-secrets.sh`:

```bash
id -u
id -g
grep -E '^GATEWAY_(UID|GID)=' .env
```

Correct `GATEWAY_UID` and `GATEWAY_GID` if necessary, then recreate the application containers:

```bash
docker compose up -d --force-recreate
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
