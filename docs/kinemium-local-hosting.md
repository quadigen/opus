# Hosting Opus locally

Opus is not tied to `https://opus.quadigen.com`. A local or self-hosted
deployment has three parts:

- a Synapse homeserver with the Kinemium module (`kinemium_module.KinemiumModule`);
- the Opus web client in `opus-client/`;
- optionally, a reverse proxy that provides TLS.

For local-only use, run Synapse on `http://localhost:8008` and the client on
`http://localhost:8080`. Keep both on HTTP or put both behind HTTPS. Browsers
block an HTTPS client from calling an HTTP homeserver, so the hosted HTTPS
client cannot connect directly to a local HTTP server.

## 1. Create a local Synapse configuration

Run these commands from the repository root. Do not reuse the production
`homeserver.yaml` or its secrets.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Building Synapse from source requires a Rust toolchain. If the repository
already has a working `.venv`, skip the install step.

Generate a new configuration in an isolated directory:

```sh
mkdir -p local
.venv/bin/synapse_homeserver --generate-config \
  -H localhost \
  -c local/homeserver.yaml \
  --config-directory local \
  --data-directory local/data \
  --report-stats no
```

The generated configuration does not set `public_baseurl`. Add it explicitly,
along with the local Kinemium module and registration settings:

```yaml
server_name: "localhost"
public_baseurl: "http://localhost:8008/"

# Local development only. Use register_new_matrix_user instead on a shared
# or public server.
enable_registration: true
enable_registration_without_verification: true

modules:
  - module: kinemium_module.KinemiumModule
    config:
      # Must be an absolute, writable path. Replace this example.
      storage_path: /absolute/path/to/opus-local/kinemium
      enable_cors: true
      cors_origins:
        - "http://localhost:8080"
```

`storage_path` holds uploaded package archives. It is separate from Synapse's
media store and database.

The generated log config writes to `homeserver.log` in the current directory.
Change the `filename` in `local/localhost.log.config` if you want separate
local logs.

The generated listener binds to `127.0.0.1` and `::1` on port `8008`. That is
the right default for local use. Do not expose port `8008` directly to the
internet.

## 2. Start Synapse and create an account

```sh
.venv/bin/synapse_homeserver -c local/homeserver.yaml -n
```

`-n` keeps logs on the terminal. On first start, Synapse applies the Kinemium
schema deltas (95 through 106). Wait for the homeserver to finish starting
before continuing.

Create the first account. Use `-a` for an administrator, which is required for
the Kinemium review page:

```sh
.venv/bin/register_new_matrix_user \
  -c local/homeserver.yaml \
  -a \
  http://localhost:8008
```

Verify the server is responding:

```sh
curl -fsS http://localhost:8008/_matrix/client/versions
curl -fsS http://localhost:8008/_kinemium/v1/capabilities
```

The Kinemium administration page is then available at:

```text
http://localhost:8008/_kinemium/admin/review
```

## 3. Run the Opus client

For development:

```sh
cd opus-client
npm install
npm start
```

Open `http://localhost:8080`. In the Homeserver field, enter the full URL:

```text
http://localhost:8008
```

Include the `http://` scheme. A bare hostname such as `localhost:8008` makes
the client try HTTPS first.

With an HTTP-only server, sign in with the localpart (for example `alice`), not
the full MXID `@alice:localhost`. Full-MXID login performs discovery against
`https://localhost`, which fails unless you have configured HTTPS and a
`.well-known/matrix/client` response for that name.

To make the local server the default in a client build, replace
`opus-client/config.json` before building, or replace `dist/config.json` after
building:

```json
{
  "defaultHomeserver": 0,
  "homeserverList": ["http://localhost:8008"],
  "allowCustomHomeservers": true,
  "featuredCommunities": {
    "openAsDefault": false,
    "spaces": [],
    "rooms": [],
    "servers": []
  },
  "hashRouter": {
    "enabled": false,
    "basename": "/"
  }
}
```

Then build and serve the static client:

```sh
npm run build
```

The build copies `config.json` into `dist/`. Serve `dist/` with any static web
server. `index.html` and `public/manifest.json` contain the hosted deployment's
branding; `index.html` also has absolute `opus.quadigen.com` metadata. Update
them if you deploy under a different name or domain.

## 4. LAN or public hosting

For anything beyond localhost:

- Choose `server_name` before the first start. It becomes part of every user
  ID and cannot be changed later.
- Set `public_baseurl` to the URL clients actually use.
- Put Synapse behind a reverse proxy with TLS. Bind Synapse to `127.0.0.1` and
  expose only the proxy.
- Keep `x_forwarded: true` on the Synapse listener and make sure the proxy
  sends `X-Forwarded-For` and `X-Forwarded-Proto`.
- Set `cors_origins` to the exact client origin instead of relying on `*`.
- Allow long-lived `/sync` requests through the proxy. Opus uses HTTP long
  polling for Matrix sync and HTTP polling for Studio collaboration; it does
  not require WebSockets.
- Use PostgreSQL for production. SQLite is appropriate for local testing only.
- Disable open registration and create users deliberately.

If `server_name` differs from the hostname serving the client, serve a Matrix
client discovery document at `https://<server_name>/.well-known/matrix/client`:

```json
{
  "m.homeserver": {
    "base_url": "https://opus.example.com"
  }
}
```

The response needs CORS headers. See `docs/reverse_proxy.md` for Synapse proxy
details, and the example SPA configs in `opus-client/contrib/nginx/` and
`opus-client/contrib/caddy/`. Add proxy passes for `/_matrix/`, `/_kinemium/`,
and the discovery path when using those examples.

## 5. Local-only limitations

- Webhook URLs are checked for SSRF safety. Loopback, private, link-local, and
  reserved addresses are rejected, so a local webhook receiver needs a public
  tunnel or a non-local deployment.
- Federation with `server_name: localhost` is not practical. Use a real domain
  if you need federation.
- The production `homeserver.yaml` contains MatrixRTC/LiveKit URLs for
  `quadigen.com`. Remove or replace those settings in a local configuration.
- Do not commit `local/homeserver.yaml`, signing keys, secrets, the database,
  the media store, or `kinemium-data`. The repository ignores `local/` and the
  other local data paths used in this guide.

## 6. Troubleshooting

- **Failed to find homeserver:** enter `http://localhost:8008` explicitly for
  local HTTP, or configure HTTPS.
- **Full MXID login fails:** use the localpart while on HTTP, or set up HTTPS
  and client discovery for the `server_name`.
- **CORS errors on Kinemium API calls:** set `cors_origins` to the exact origin
  of the client.
- **Kinemium routes return 404:** check that the `modules` entry is loaded and
  that `kinemium_module.py` is importable from the working directory.
- **Schema errors on startup:** check the log for the delta 95-106 migration
  and take a backup before retrying.
- **Mixed-content errors:** serve the client and homeserver over the same
  scheme, or put both behind HTTPS.

## Related documentation

- [Kinemium module and API](kinemium.md)
- [Synapse installation](setup/installation.md)
- [Reverse proxy](reverse_proxy.md)
- [PostgreSQL](postgres.md)
- The `opus-client` checkout and its README
