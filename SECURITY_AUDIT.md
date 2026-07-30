# Security Audit & Hardening — Geely Global Home Assistant integration

Base project: `nitaybz/geely-global-ha` @ v0.6.3
Hardened build: **0.6.3-hardened.1**
Audited: 2026-07-29

This is a line-by-line security review of the community integration for the
Geely EX5, plus the fixes applied. The goal was to make sure the code is safe
to run on your Home Assistant: no malicious code, no data leaving to anyone but
Geely, and no easy way for someone on your network to steal your credentials or
control your car.

---

## Summary — is it safe?

**The integration is not malicious.** Every network request goes only to
Geely / ECARX servers (`*.geely.com`, `*.ecloudeu.com`). There is no telemetry,
no analytics, no "phone home", no third-party endpoint, no `eval`/`exec`, no
obfuscated code, no data collection. Dependencies are the mainstream
`cryptography` and `pycryptodome` libraries.

It did, however, have **one serious real-world weakness**: it disabled TLS
certificate verification on every call, which would let an attacker on your
network impersonate Geely and steal your login token and car-control
certificate. That is now fixed, along with several smaller hardening
improvements. Details below.

---

## Findings

### 1. CRITICAL — TLS server verification was completely disabled  *(FIXED)*

Original `api.py` built every HTTPS connection like this:

```python
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
```

This means the client accepted **any** certificate from **any** server, on
every request — including the ones that carry your `cidpsso` login token, the
rotating session JWT, and the mutual-TLS client key that authorizes locking,
unlocking and climate control. Anyone able to intercept your traffic (a rogue
Wi-Fi access point, a DNS spoof, a compromised router, a malicious ISP node)
could transparently impersonate `apis.ecloudeu.com`, capture those
credentials, and then issue commands to your car.

**Why the author disabled it:** some of Geely's gateways are fronted by a
private / self-signed certificate authority that doesn't chain to a public CA,
so naive verification fails. Disabling verification was the lazy fix; it throws
away all protection.

**The fix — a fail-closed, two-tier transport (`_secure_tls_connect`):**

1. **Strict validation first.** Full certificate-chain + hostname validation
   against the operating-system trust store (public CAs). If Geely uses public
   certificates, this simply works and is the strongest possible option.
2. **Public-key pinning fallback.** *Only* if strict validation fails because
   the chain isn't publicly trusted, the code records the server's public-key
   fingerprint (SHA-256 of its SubjectPublicKeyInfo) on first contact —
   trust-on-first-use, exactly like SSH's `known_hosts` — and then **requires
   an exact match on every subsequent call.** A man-in-the-middle presenting a
   different key is rejected with `GeelyTLSPinError` and no credentials are
   sent.

At no point does the code silently trust an arbitrary certificate on an ongoing
session — which is the property the original lacked. The one residual caveat is
the very first connection to a privately-signed host: do the initial setup on a
network you trust so the first-use pin is captured cleanly. Pins are stored at
`.storage/geely_global/<VIN>/server_pins.json` (mode 0600).

This was verified with an automated test harness (self-signed origin server +
a second "attacker" server with a different key on the same hostname):
first-use pinning, genuine-reconnect acceptance, MITM rejection, and public-CA
success all pass.

### 2. MEDIUM — Token-bearing calls bypassed even the weak context  *(FIXED)*

The `getCode` call and both certificate-provisioning POSTs used bare
`urllib.request.urlopen`. These have been rerouted through the same
verified/pinned transport (`_raw_https`), so the login token and CSR exchange
get the same protection as the main data path.

### 3. MEDIUM — Private key & certificate written world-readable  *(FIXED)*

The mTLS private key (which authorizes remote control of the car) was written
with default permissions. It is now written via `os.open(..., 0o600)` inside a
`0700` directory, with `chmod` fallbacks, so other local users can't read it.

### 4. LOW — Secrets could reach the Home Assistant log  *(FIXED)*

Several error paths folded raw server responses into exception messages that
land in the HA log (often shared when asking for help). A `_redact()` helper
now masks token / JWT / certificate / password fields in any response before it
is logged or raised.

### 5. LOW — Process-wide TLS warning suppression  *(FIXED)*

`geetest_solver.py` called `urllib3.disable_warnings()`, which would hide
insecure-connection warnings for **every** integration in your HA process, not
just this one. Removed; the captcha session now verifies certificates like the
rest.

### 6. MEDIUM — Unvalidated VIN / user_id from the backend  *(FIXED)*

The VIN and `user_id` come from Geely's server JSON and were used verbatim in
filesystem paths (cert/key/pin storage) and in the hand-built raw HTTP request
line. A malicious or compromised backend could return a VIN like
`../../config/...` (path traversal / arbitrary file write) or one containing
CR/LF (HTTP request-line injection / smuggling). Both are now blocked: the VIN
is validated against `^[A-Za-z0-9]{8,20}$` and the user_id against
`^[A-Za-z0-9._-]{1,64}$` at every entry point, and the transport layer rejects
any path/header containing CR/LF (`_no_crlf`).

### 7. INFORMATIONAL — Hardcoded `APP_SECRET` (not a leak, left as-is)

`const.py` contains `APP_ID` / `APP_SECRET`. This is **not** your personal
secret — it is the app-level HMAC signing key baked into every copy of the
Geely mobile app, identical for all users, required to sign requests the way
the app does. It cannot be removed without breaking the integration and does
not expose anything user-specific. Noted for transparency.

---

## What was NOT changed

The functional behavior — entities, controls, polling, capability discovery —
is untouched. Only the transport-security layer, file permissions, and logging
were modified. If Geely's servers use public certificates, you will not notice
any difference; if they use a private CA, you'll see one "pinned on first use"
line in the log the first time each host is contacted.

## Verifying it yourself

- All traffic destinations: `grep -rE "https?://" custom_components/geely_global`
  → only `*.geely.com` and `*.ecloudeu.com`.
- No dangerous calls: `grep -rE "eval\(|exec\(|subprocess|os.system"` → none.
- TLS is enforced: `_secure_tls_connect` in `api.py` fails closed.
