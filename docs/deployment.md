# Remote deployment

Runs the server on a small VPS behind a Cloudflare Tunnel, so it can be added as a custom
connector in claude.ai and used from the Claude mobile app. Read [security.md](security.md) first.

## Prerequisites

- A Garmin Personal API key (one key at a time; choose a 1-year lifetime).
- A domain on Cloudflare and a Cloudflare Tunnel (Zero Trust -> Networks -> Tunnels) with a
  public hostname, e.g. `garmin.example.com` -> service `http://app:8000`.
- A VPS, e.g. Hetzner Cloud CX22 in the EU with Ubuntu LTS.

## 1. Server

- Hetzner Cloud Firewall: **no inbound ports** except SSH (or none at all if you use Tailscale for
  SSH). The tunnel is outbound-only.
- SSH with keys only (`PasswordAuthentication no`), or only via Tailscale.
- `unattended-upgrades` enabled.
- Docker Engine with the compose plugin.

## 2. Configure

```bash
git clone https://github.com/EmilbMadsen/garmin-health-mcp && cd garmin-health-mcp
install -d -m 700 secrets
printf '%s' 'YOUR-GARMIN-KEY' > secrets/garmin_api_key   # or use an editor; avoid shell history
sudo chown 10001 secrets/garmin_api_key && sudo chmod 400 secrets/garmin_api_key
cp .env.example .env && chmod 600 .env                  # then edit .env
```

`.env` needs `GARMIN_HEALTH_MCP_PUBLIC_URL=https://garmin.example.com`, `TUNNEL_TOKEN` and
`CLOUDFLARED_IMAGE` pinned to a current release of `cloudflare/cloudflared`.

## 3. Set up the login and start

```bash
docker compose build
docker compose run --rm app auth init      # username, password, scan the TOTP QR code
docker compose up -d
docker compose logs -f app
```

`auth init` prints the OAuth client ID and secret once. Check from anywhere:

```bash
curl -i https://garmin.example.com/mcp     # expect 401 with WWW-Authenticate
```

## 4. Add the connector in claude.ai

Customize -> Connectors -> Add custom connector:

- URL: `https://garmin.example.com/mcp`
- Advanced: **Use your own OAuth client** -> client ID and secret from `auth init`
- Connect -> sign in with username, password and TOTP -> Allow.

The connector is then available in the Claude mobile app too. Auth settings cannot be changed on
an existing connector: remove it and add it again (e.g. after `auth rotate-client`).

## Operations

| Task | Command |
|---|---|
| Emergency: cut off access now | `docker compose exec app garmin-health-mcp auth revoke-all` |
| New OAuth client | `docker compose exec app garmin-health-mcp auth rotate-client` |
| Unlock after too many failed logins | `docker compose exec app garmin-health-mcp auth unlock` |
| Status | `docker compose exec app garmin-health-mcp auth status` |
| Audit log | `docker compose exec app tail /data/audit.log` |
| New Garmin key | replace `secrets/garmin_api_key`, then `docker compose restart app` |
| Update | `git pull && docker compose build && docker compose up -d` |

Optional, after everything works: a Cloudflare WAF rule (or `GARMIN_HEALTH_MCP_ALLOWED_CIDRS`)
limiting `/mcp` and `/token` to `160.79.104.0/21`. Leave `/authorize`, `/login` and `/consent`
open, since they load in your own browser.

## Running the HTTP server locally

For testing on your own machine (no tunnel):

```bash
uv run garmin-health-mcp auth init
GARMIN_HEALTH_MCP_PUBLIC_URL=http://localhost:8000 uv run garmin-health-mcp serve --http
```

claude.ai cannot reach `localhost`; use this with a local MCP client or the MCP Inspector.
