# Production-grade reverse proxies for `fluid mcp output-port`

The Fluid MCP gateway's `--transport http` mode does not enforce TLS
or strong identity on its own. Pair it with a reverse proxy in front
that does.

This directory carries two ready-to-edit templates:

* [`Caddyfile`](Caddyfile) — Caddy 2.x. mTLS + bearer token + automatic
  cert renewal if you flip `auto_https on`.
* [`nginx.conf`](nginx.conf) — nginx. Equivalent setup for shops that
  already run nginx as their L7 entrypoint.

## Defence-in-depth layers (any one helps; all together is the production posture)

| Layer | What it stops | Where it lives |
|---|---|---|
| **mTLS client cert** | Random network attackers — they don't have a valid client cert. | Proxy (Caddy / nginx) |
| **Bearer token** | A leaked client cert — the attacker would also need the shared secret. | Proxy AND gateway (`FLUID_MCP_AUTH_TOKEN`) |
| **Trusted-proxy allow-list** | A client that reaches the gateway without the proxy and sets the certificate headers itself. | Gateway (`FLUID_MCP_TRUSTED_PROXIES`) |
| **`agentPolicy.allowedModels` / `allowedUseCases`** | A legitimate client running an unapproved model or use case. | Gateway (per-`tools/call`) |
| **`policy.rowFilters[]`** | A legitimate client bound to a different tenant. | Gateway (per-row WHERE clause) |
| **Snowflake row-access policy** (compiled via `fluid_build.output_ports.iam_compiler`) | Bypass-the-gateway reads — analyst querying Snowflake directly. | Cloud (warehouse-side) |

## Quick start

1. **Issue certs.** Easiest local PKI:

    ```bash
    brew install mkcert step-cli  # or use your corporate PKI
    mkcert -install
    mkcert -client mcp-client-1
    mkcert mcp.your-domain.example
    ```

2. **Place files** under `/etc/ssl/forge-mcp/`:

    ```
    server.pem          # mkcert mcp.your-domain.example.pem
    server.key          # mkcert mcp.your-domain.example-key.pem
    clients-ca.pem      # mkcert -CAROOT/rootCA.pem
    ```

3. **Set the shared bearer secret** on the gateway and in the proxy
   config (replace `your-shared-secret-here` in the templates), and
   tell the gateway which peer is the proxy:

    ```bash
    export FLUID_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
    export FLUID_MCP_TRUSTED_PROXIES=127.0.0.1
    ```

   The gateway reads the `X-Client-CN` / `X-Client-Fingerprint` headers
   the proxy sets only when the request's TCP peer is listed in
   `FLUID_MCP_TRUSTED_PROXIES` (IPs or CIDRs, comma-separated; empty by
   default). From any other peer they are ignored, so
   `${caller.client_cn}` row filters and the `clientCertCn` audit field
   work only once this is set, and only with the bearer token above: the
   gateway reads the headers only on an authenticated request, and refuses
   to start if this variable is set while auth is off. A client cannot
   supply its own CN any other way. If the proxy runs on another host or in
   another container, list that address instead of `127.0.0.1`. List
   only the proxy's own address, not its subnet: any peer inside a listed
   range can assert any client certificate.

4. **Start the gateway** (binds to `127.0.0.1:8765` so only the proxy
   can reach it):

    ```bash
    fluid mcp output-port serve ./contract.fluid.yaml \
      --transport http --host 127.0.0.1 --port 8765
    ```

5. **Start the proxy:**

    ```bash
    caddy run --config ./Caddyfile     # OR
    nginx -c $(pwd)/nginx.conf
    ```

6. **Configure your MCP client** with the client cert + the bearer
   token. (Claude Desktop's MCP config supports both via the
   `headers` field; Cursor needs a wrapper script.)

## Honest limits

- Bearer tokens are **shared secrets**: every client uses the same
  string. For per-client identity, switch to per-client API keys
  (one token per client → CN mapping at the proxy layer) or wait for
  the OAuth/SPIFFE phase.
- Caddy's automatic TLS works for public domains only. Behind a
  corporate firewall use an internal CA and `auto_https off`.
- The proxy templates default-deny anything without a cert AND
  without a bearer token. Failing closed is the right default for a
  data gateway; loosen carefully.
