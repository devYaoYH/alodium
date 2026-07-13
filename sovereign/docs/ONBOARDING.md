# Onboarding a trusted user

## TL;DR — the checklist

Operator, once per user:

1. `./scripts/invite.sh <username> <email> "Display Name"` — prints a
   single-use, 72-hour link and (with `qrencode` installed) a scannable QR.
2. Send the link / show the QR.

The new user, on the phone they always carry:

1. Open the link.
2. Tap **Create passkey** → approve with Face ID / fingerprint. There is no
   password step — Pocket ID doesn't have one.
3. Open `https://home.<domain>`, add it to the home screen. Every app is a
   tile there; tapping a tile signs them in with the same passkey.

That's the entire flow. Passkey-only is enforced by the product: there is no
password mode to fall back to, nothing to configure wrong.

## One-time operator setup (Ring 0)

1. **Initialize Pocket ID.** Visit `https://auth.<domain>/setup` and enroll
   YOUR passkey — the first account is the admin. This is the Ring 0
   identity; enroll a second passkey (backup device or hardware key) from
   the admin UI before inviting anyone.
2. **Mint an API key** (admin → API Keys) and put it in `.env` as
   `POCKET_ID_API_KEY` — `scripts/invite.sh` uses it.
3. **Federate the apps.** For each app with native OIDC, create an OIDC
   client in Pocket ID (admin → OIDC Clients) and register Pocket ID as the
   auth source on the app side. Forgejo first: *Site Administration →
   Authentication Sources → Add → OAuth2/OIDC*, issuer `https://auth.<domain>`
   (auto-discovery). Keep Forgejo's local admin password login enabled as the
   documented break-glass.
4. **Non-OIDC apps** (Radicale): create an OIDC client for `oauth2-proxy`,
   fill `OAUTH2_PROXY_*` in `.env`, enable the shim —
   `docker compose --profile authshim up -d` — and put the app's Caddy route
   behind the `(authed)` forward-auth snippet in the Caddyfile.

## Reality check for the current MVP

Until the anchor lands (M2), this box sits behind CGNAT and **is not
reachable from the internet**. Today that means:

- Trusted users must be on the same LAN (and note: if your LAN hands out
  non-RFC1918 addresses, Caddy's `private_ranges` ring guard will reject
  them — adjust the guard or wait for identity-aware auth to replace it).
- For local development with `NODE_DOMAIN=localhost`, add hosts entries
  (`127.0.0.1 auth.localhost home.localhost git.localhost llm.localhost
  cal.localhost`) or use `curl --resolve`; Caddy serves these names from its
  internal CA, so trust that CA once
  (`docker exec caddy cat /data/caddy/pki/authorities/local/root.crt`).

The permanent fix is the M2 front door: a disposable VPS running an L4 SNI
passthrough, WireGuard dialed outbound from the node, TLS terminating here.
The steps above are written against that end state and work unchanged once
the anchor exists.
