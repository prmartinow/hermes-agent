# Hindsight dashboard webhook inbox

The dashboard accepts `POST /api/webhooks/hindsight` using an independent shared
secret. This is not the general agent-triggering gateway webhook adapter. It
records signed retain, consolidation, and memory-defense events for inspection;
it does not launch agents, execute payload content, or mutate memory caches.

## Deployment (operator action required)

- Deploy this code to the dashboard process; no live deployment is performed by tests.
- Set `HERMES_HINDSIGHT_WEBHOOK_SECRET` in that process's secret environment (or
  its profile `.env`). Use a high-entropy random secret, not the Hindsight API key
  or dashboard token. Set the identical value as the Hindsight subscription's
  `secret`; never put it in the URL, ordinary YAML, logs, or version control.
- Configure the subscription for POST to the exact endpoint, without a trailing
  slash. The Hindsight sender must emit `X-Hindsight-Signature-V2`; older
  body-only senders are intentionally rejected. The current sender signs each
  attempt, so normal delayed retries remain valid.
- Preserve raw request bytes and the signature header through the proxy. Use
  HTTPS outside a trusted private network and synchronize both clocks.
- Preserve the dashboard's existing Host restrictions. If an upstream proxy has
  its own interactive login gate, configure only this exact POST for independent
  webhook authentication; do not exempt `/api/` or `/api/webhooks/` wholesale.
- Persist and restrict the dashboard profile's `HERMES_HOME` volume. Receipts live
  in `hindsight-webhooks/inbox.sqlite3` (new directory 0700, file 0600). All workers
  receiving this subscription must share that local SQLite store; independent
  replicas do not share deduplication. Do not delete the store during deployment.

## Protocol and security

The required header is `t=<unix_seconds>,v1=<lowercase_sha256_hex>`. The HMAC key
is the shared secret encoded as UTF-8. Signed bytes are the literal timestamp,
a period, and the exact raw HTTP body. Comparison is constant-time. Timestamp
skew is limited to 300 seconds in either direction; duplicate or malformed
signature headers and body-only signatures fail closed. Bodies are capped at
256 KiB, including streaming bodies. Only the exact signed POST receives webhook
authority; ordinary dashboard credentials cannot substitute for the signature.
Other methods, suffix paths, dashboard APIs, and Host checks remain protected.

Successful ACKs follow a SQLite commit. A SHA-256 body digest primary key makes
retries (including newly signed attempts), concurrent deliveries, and process
restarts idempotent. Duplicates return success without recording another event.
Receipts older than 30 days are removed on the next accepted delivery. The
freshness check prevents an old captured signature from being replayed after
receipt expiry. Storage is not a general notification queue or memory cache;
its useful artifact is the durable event audit trail, including the full signed
JSON envelope. Treat payloads as sensitive, untrusted data when reading them.

An operator can inspect the inbox read-only with SQLite, for example:

```sql
SELECT received_at, event, bank_id, operation_id, status
FROM events ORDER BY received_at DESC LIMIT 20;
```

Responses: 200 `recorded` / `duplicate`; 401 invalid signature or freshness;
400 malformed JSON/event (or invalid Host); 413 oversized body; 503 secret
unconfigured or inbox unavailable. Persistence failure is never acknowledged as
success, so Hindsight can retry. There is no unauthenticated inbox read endpoint.
