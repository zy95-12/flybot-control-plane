# botawaken.com deployment inputs

The user selected `botawaken.com`, managed at Wix, for server `${SERVER_IP}`. These files contain non-secret site settings and concrete public ingress/router definitions. They do not change DNS, start containers, obtain certificates, or open any host ports.

## DNS status (2026-10-20)

Registration remains at Wix; DNS hosting is now Cloudflare. Authoritative nameservers are `eloise.ns.cloudflare.com` and `johnathan.ns.cloudflare.com`.

| Type | Host | Value | Proxy |
| --- | --- | --- | --- |
| A | panel | ${SERVER_IP} | DNS only |
| A | social | ${SERVER_IP} | DNS only |
| A | * | ${SERVER_IP} | DNS only |

The user confirmed gray-cloud mode. Authoritative A-record lookups for panel, social and a random wildcard hostname all return the origin IP; panel has no AAAA record.

Unknown hosts still have no application route: ingress rules use exact authorized hostnames. The spelling `botawaken.com` was confirmed from the user's screenshot and DNS; the earlier `bowawaken.com` was a typo.

## Public TLS

Use individual hostname certificates with ACME TLS-ALPN-01 through the dedicated HTTPS listener on TCP 443. Traefik can manage acquisition and renewal for configured routers without a Wix DNS API credential. This avoids depending on a wildcard certificate/DNS-01 automation; the wildcard above is a **DNS record**, not a wildcard certificate. Do not request a `*.botawaken.com` certificate with TLS-ALPN-01.

The supplied static config deliberately points at the Let's Encrypt staging CA. Its test certificates are not browser-trusted. After DNS, external port reachability, and isolated container validation pass, switch `caServer` to `https://acme-v02.api.letsencrypt.org/directory`, record the account, and use a distinct production ACME state file. Keep that state writable only by the ingress UID (10001), persistent across image upgrades, and backed up. Do not share it with user containers. An operations contact email can be added when available; no mailbox is assumed to exist at this domain.

TCP 443 must actually reach this dedicated ingress for issuance and renewal. Host listener inventory previously found it free; cloud security-group/firewall reachability is not yet verified or changed. No port 80 listener is required by this challenge type. ACME issuance limits constrain new runtime certificates, so keep onboarding invite-only and control issuance batches before enabling large public registration. A future wildcard DNS-01 setup can use delegated validation if scale requires it.

The supplied public config is separate from `compose.staging.yaml`, which uses loopback HTTPS and manually supplied validation certificates. A deployable public overlay still needs persistent ACME storage/network mounts and the actual application image digests; do not substitute this file into the staging command blindly.

Generate ready-user route files with:

```sh
python3 deploy/ingress/generate_routes.py \
  --domain botawaken.com \
  --network-cidr <reviewed-runtime-network-cidr> \
  --inventory <trusted-ready-runtime-inventory.json> \
  --output <private-dynamic-directory>/runtimes.toml \
  --cert-resolver letsencrypt
```

The supplied `dynamic/runtimes.toml` starts with an empty runtime inventory and owns the shared `flybot-security` middleware. Replace that file with the renderer output once instances are ready. TLS from ingress to each runtime still uses its separate platform-issued leaf key and verified runtime CA.

## Intended URLs after deployment

- Panel: `https://panel.botawaken.com/dashboard/console.html`
- Bot Social: `https://social.botawaken.com`
- User runtime: `https://u-<instance-id>.botawaken.com/`

## Sources

- [Wix: Managing DNS records](https://support.wix.com/en/article/managing-dns-records-in-your-wix-account)
- [Let's Encrypt: Challenge types](https://letsencrypt.org/docs/challenge-types/)
- [Traefik: TLS certificate resolvers](https://doc.traefik.io/traefik/reference/routing-configuration/http/tls/overview/)
- [Wix: Changing nameservers](https://support.wix.com/en/article/changing-nameserver-ns-records-for-a-wix-domain)
- [Cloudflare: Wildcard DNS](https://developers.cloudflare.com/dns/manage-dns-records/reference/wildcard-dns-records/)
