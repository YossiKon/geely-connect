# Geely Connect - APAC region support (complete solution)

**Status: working end-to-end, verified live (2026-08-03)**

The [YossiKon/geely-connect](https://github.com/YossiKon/geely-connect) Home
Assistant integration previously supported only **EU** and **NA**. This work
adds full **APAC** support - region resolution, certificate provisioning and
the runtime session exchange - verified live against a production account and
vehicle (Australian-market Geely EX5).

This document describes what was broken, the root causes, and the exact fix,
so the same changes can be reviewed and merged.

---

## TL;DR - three independent problems

1. **Region resolution.** APAC accounts' vehicle records carry no
   `tspInfo` / `edgeInfo` / top-level `serviceRegion`, so region resolution
   fell back to EU and the EU cert server rejected the account (`1501`).
   **Fix:** resolve the region from `saleMarket` / `tcamMarket` (`"AP"` → APAC).

2. **Request signing.** The APAC gateway enforces a different Accept header
   and **uppercase** signature headers than the EU gateway:
   - `Accept: application/json; charset=utf-8`
     (the integration sent `application/json;responseformat=3` → gateway
     rejects the signature with `1445`)
   - **uppercase** `X-SIGNATURE` / `X-TIMESTAMP`
     (the integration sent lowercase `X-signature` / `X-timestamp`)
   - HMAC-SHA1 over the canonical string:
     `Accept` + `\n` + sorted `x-api-*` headers (`name:value\n`) + `\n` +
     sorted query params (`k=v&...`) + `\n` + `Base64(MD5(body))` + `\n` +
     `timestamp_ms` + `\n` + `METHOD` + `\n` + path, signed with the
     regional `app_secret`.
   The signer was **byte-verified against the app's real SignUtil**
   (HMAC-SHA1, key = the whitebox `app_secret`).

3. **The session exchange.** APAC does **not** exchange the access code on
   the mTLS control host like EU. It runs on the **public** host
   `api.ecloudkr.com`:
   - `POST /auth-center/account/session` (no query string, `urlname: user-api`)
   - Body: `{"identityType": "geelyos", "authCode": <code>,
     "receiverId": <login email>}`
   - Success envelope differs from EU:
     `{"resultCode": "0", "resultMessage": "Success", "accessToken": <JWT>,
     "userId": <id>, "expiresIn": 7200}` - note `userId` here is the
     session-service id, which differs from the cidpsso `user_id`; that is
     normal.
   - **The access code must be minted by the same regional backend that
     exchanges it**: `POST /cidpsso/oauth2/v1/getCode` on
     `m-lcmsam-kr.geely.com` (the APAC host). Codes minted on the EU/global
     hosts do not exist in the APAC session store - the server crashes with
     `8500` (`服务器端内部异常`). This was the root cause of the runtime
     setup failure, and it is invisible from the client side: a garbage code
     produces the identical `8500`, so the failure happens **before** code
     validation.

## Resulting APAC flow

```
OTP login (region-agnostic; unchanged)
   → getCode on m-lcmsam-kr.geely.com            (regional code)
   → POST api.ecloudkr.com /auth-center/account/session
     {identityType, authCode, receiverId}        (public host, no mTLS)
   → JWT (HS256, appId=GEELYE245, env=production, operator=GEELY)
   → mTLS vehicle control on apis.ecloudkr.com   (client cert, pinned)
     vehicle_status → code 1000 ✓
```

The mTLS client certificate is provisioned during config flow against
`api.ecloudkr.com` (`/auth/cert/info` + `/auth/cert/file`) with
`identityType: "geelyos"` - the same identity the session exchange uses.
The APAC server pin for `apis.ecloudkr.com` is bundled (Geely private PKI,
issuer "Geely Trust Center / External Services Issuing EU-CA").

## Patch summary

| File | Change |
|---|---|
| `const.py` | `REGIONS["APAC"]` (app_id, hosts, pin); `MARKET_TO_REGION` (`AP`→APAC); rewritten `resolve_vehicle_region()` (tspInfo → edgeInfo → top-level serviceRegion → market code); `SUPPORTED_COUNTRIES` += AU/NZ/KR/JP/SG/TH/MY/HK/ID/PH/VN; removed APAC from `UNSUPPORTED_REGIONS` |
| `api.py` | `_get_access_code(host=...)` parameter; new `_apac_session_exchange()` (KR getCode → public-host session with `receiverId`, `resultCode` envelope); `refresh_jwt()` branches on `control_host == "apis.ecloudkr.com"`; `_PRIVATE_PKI_HOSTS` / `_BUNDLED_TLS_PINS` += `apis.ecloudkr.com` |
| `config_flow.py` | region-provisioning log raised to ERROR level (the strings file promises that line; `system_log` only retains ERROR+) |
| `__init__.py` | passes the stored login email into `GeelyApi` (used as `receiverId`) |

## Verification

- **Live end-to-end**: fresh JWT → `vehicle_status` on the mTLS control host
  → `code 1000`, real vehicle data (engine state, position, charging state).
- **HA integration**: entities populated with live values (charger voltage /
  current / power, charge socket switch), zero errors in `system_log`.
- **35/35 request-shape checks** (mock-based, against the exact bytes that
  were verified live).

## Known limitations (car-side, not integration-side)

- The vehicle's T-Box enters deep sleep ~5 minutes after the last event when
  the car is not charging or running. The server then stops receiving new
  telemetry, so "last updated" timestamps freeze until the next wake event.
  This is Geely vehicle firmware behaviour, not a limitation of this
  integration.

## Disclaimer

Interoperability research and integration work, performed for private use
with a vehicle owned by the operator. Not affiliated with or endorsed by
Geely Automobile Holdings. API details and regional app identifiers are as
embedded in the publicly distributed mobile application. If Geely objects to
any part of this work, it will be taken down on request.

---

*Write-up of the full capture/extraction work exists separately (private);
this document intentionally contains no personal data, no account tokens, and
no device identifiers.*
