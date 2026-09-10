# Security model

This gateway handles health data and long-lived account access. Treat its volumes and backups as sensitive.

- PostgreSQL has no host port and exists only on Compose's internal `data` network.
- Only the collector receives Garmin credentials and the Garmin token volume.
- The MCP server uses a dedicated PostgreSQL role whose transactions default to read-only. It has only `SELECT`, has no Garmin credentials, has no FIT mount, and publishes no host port.
- The `request_sync` tool writes a bounded request to the dedicated shared `sync_requests` volume. Only the collector fetches Garmin data and writes health tables. Pending requests are coalesced; a five-minute cooldown and worker lock limit repeated/concurrent refreshes. Users allowed to call this tool can trigger external Garmin reads and updates to the local cache. The shared volume is trusted local control state, not a boundary against a compromised MCP container.
- Remote access uses OpenAI Secure MCP Tunnel over outbound HTTPS. No router port-forward is needed.
- The tunnel runtime key should have only **Tunnels Read + Use**. Do not use an admin API key.
- Application containers drop Linux capabilities, use a read-only root filesystem, and enable `no-new-privileges`.
- Secret files are ignored by Git and should remain mode `0600`; the secret directory should remain `0700`.

The tunnel and ChatGPT workspace association are the remote authorization boundary. Do not attach the tunnel to a workspace or organization whose members should not be able to query this data.

If a Garmin credential or token may have leaked, change the Garmin password, revoke sessions in Garmin, stop the collector, and recreate the `garmin_tokens` volume. If a tunnel runtime key leaks, revoke it in OpenAI Platform and replace `secrets/openai_tunnel_api_key.txt`.

The optional `compose.local.yaml` binds MCP only to host loopback for MCP Inspector testing. Never change that mapping to `0.0.0.0` on an untrusted network.

## Container provenance

Release images are built from version tags by GitHub Actions for `linux/amd64` and `linux/arm64`. The workflow publishes an SBOM and GitHub artifact attestation. Production deployments should use a versioned image tag or immutable digest rather than a branch tag.
