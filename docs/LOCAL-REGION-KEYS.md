# Original-login regions with local app keys

The original Geely app (`com.geely.android.intl`) talks to a separate backend
per area, and each area signs its requests with its own app ID and secret. The
integration ships those for the EU, North-American and APAC backends. For the
United Arab Emirates the servers are known, but the app ID and secret are not
public: the official app decrypts them at run time from a white-box native
library (#105). An installation owner who has them may supply them locally.

Nothing here ships those values, an APK, a native library or an extractor.
Obtain them yourself from an app copy you are entitled to analyse, keep them
private, and note that supplying them yourself does not by itself authorise
every use of the app or the service.

## Which cars

A car is read this way when its vehicle record names the area `AE` - the
international app picks the UAE servers from a market string containing AE -
and only then. Every other car, and every existing entry for another area, is
untouched, with or without the file.

| Area | Cert server | Control server | Login code from |
| --- | --- | --- | --- |
| `AE` | `tsp-geely-api-uae.xcloudsvc.com` | `tsp-geely-apis-uae.xcloudsvc.com` | `m-lcmsam-ae.geely.com` |

The servers come from the integration's code, never from the file. The control
server presents a certificate from Geely's own PKI; its key is pinned in the
integration, and when the pin was captured its certificate was checked to come
from the same Geely CA that issued the EU control server's. The other two
present public certificates and are validated normally.

## Local file

Create a JSON object with exactly these three string fields:

| Field | Meaning |
| --- | --- |
| `region` | The area code, `AE` |
| `app_id` | The area's app ID, as the app sends it in `X-APP-ID` |
| `app_secret` | The area's request-signing secret |

There are no working example values. Values are printable ASCII without
spaces, at most 256 characters each, and the file at most 4 KiB.

Save it as `geely_region_keys.json` beside `configuration.yaml` (normally
`/config/geely_region_keys.json`) and restrict access to the Home Assistant
user. Never paste it, or its values, into an issue, a pull request, a log
excerpt or a diagnostics report.

## Setup

Set the car up with the original login, as for any other area. When the
vehicle record names `AE`, setup reads the file and provisions the car's
certificate against the UAE cert server. Without the file, or with one that is
invalid or names another area, setup stops with the region message, before
contacting any Geely server for the certificate, and the log line says which
of those it was. It never falls back to signing the car for the EU, which is
what produced the `1501 geelyos verify error` before.

An existing UAE entry reads the file again at every start and waits, rather
than starting, while it is missing or invalid. After replacing the file,
reload the integration or restart Home Assistant.

## Storage and diagnostics

The values stay in the local file and in memory. They are not written into
the config entry or a diagnostics report, and both log and diagnostics
redaction cover the `app_secret` name. The file itself is not encrypted, and
Home Assistant backups may include it, so protect the backups too.

## Validation limits

Not yet tried on a car. What is known: the cert and control servers are the
pair the international app builds for the UAE by the same rule that gives the
EU servers this integration already uses, and the login-code server resolves
and presents Geely's public certificate. If setup reaches the certificate step
and stops there, the app ID or secret is the first thing to check, and the log
line says so. If the
certificate is issued but the first status read fails with `8500`, the login
code may have to come from another server; the log line and the issue thread
are the place to say so.
