# The anchor — a disposable front door

For nodes without a reachable IP (`front_door: guided-vps` or `byo-anchor`
in the placement manifest): a cheap VPS that is nothing but a pipe.

The contract (DESIGN.md, "The front door is pluggable"):

- **L4 SNI passthrough, not TLS termination.** The anchor reads the SNI of
  the still-encrypted stream and forwards it down the WireGuard tunnel.
  TLS terminates ON THE NODE; the anchor never holds certificates, never
  sees plaintext. Compromise yields traffic metadata only.
- **The node dials OUTBOUND.** Zero ports open at home; CGNAT is
  irrelevant. The anchor only ever *answers*.
- **Stateless by construction.** Everything here is re-derivable from this
  directory plus two keys minted at provision time. Destroying and
  re-provisioning the anchor must be a 20-minute, zero-loss event — that
  is the M2 exit criterion, and the property that keeps a rented box from
  ever becoming a hostage.

## Files

| File | What |
|---|---|
| `cloud-init.yaml` | the whole anchor: nginx stream + WireGuard, from first boot |
| `wg-node.conf.example` | node side: the outbound tunnel (`wg-quick`), copy to `/etc/wireguard/wg-anchor.conf` |
| `Corefile.example` | node-run CoreDNS for DNS-01 — wildcard certs without lending registrar tokens |

## Bring-up (operator, ~20 minutes, twice: once for real, once to prove it)

1. Mint two WireGuard keypairs (node + anchor): `wg genkey | tee k | wg pubkey`.
2. Provision any Debian/Ubuntu VPS with `cloud-init.yaml`, substituting the
   `${...}` values. Point an `A` record for `anchor.<domain>` at it; point
   the service records (`git`, `auth`, `llm`, ...) at the SAME address —
   or `NS`-delegate the zone to the node's CoreDNS (Corefile.example).
3. Node side: fill `wg-node.conf.example`, `wg-quick up wg-anchor`.
   The node dials out; the tunnel is up when `wg show` lists a handshake.
4. Caddy needs no change: streams arrive at the node's :443 exactly as if
   the box were public. For DNS-01 wildcard issuance, delegate ACME
   challenges to CoreDNS per the Corefile comments.

M1's installer-agent automates 1–3, with the operator approving the two
payment moments (VPS, domain). The drill — destroy the VPS, re-run this,
zero loss — belongs in your build-log with a date next to it.

The anchor holds: its own WG private key, an nginx config, this README's
claims. It does not hold: certificates, data, sessions, identity, secrets.
Fire it whenever you like.
