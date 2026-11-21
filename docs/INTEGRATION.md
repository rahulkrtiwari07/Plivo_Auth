# Integration guide: Plivo Numbers service

How Bigship's backend lets customers complete KYC and buy Indian phone numbers through this service.
Interactive reference with every field: `https://<service>/docs` → section **Integration v1**.

```
Customer ─► Bigship dashboard ─► Bigship backend ──(X-API-Key)──► this service ─► Plivo
                                       ▲                               │
                                       └────── signed webhooks ────────┘
AI dialer ──(X-API-Key)──► GET /v1/customers/{id}/caller-ids
```

## 1. Basics

| | |
|---|---|
| Base URL | `https://<service>/v1` |
| Auth | Header `X-API-Key: <INTEGRATION_API_KEY>` on every request. **Server-to-server only**: never ship the key to a browser or app. |
| Customer id | **Your** customer id, in the URL: `/v1/customers/{customer_id}/...` (1–64 chars: letters, digits, `_ . : @ -`). |
| Format | JSON, except KYC submission (`multipart/form-data`). Prices are USD. Times are ISO-8601 UTC. |
| Errors | `{"detail": "message you can show the customer"}` with status 400 invalid input · 401 bad key · 403 KYC not approved · 404 not found · 409 conflict/limit/wrong state · 422 malformed request · 502 Plivo failure · 503 API disabled |

## 2. Customer lifecycle

```
(no KYC) ──submit──► submitted ──► accepted ──► can buy numbers, any time
                         │             ├──► suspended (spam complaints: buying + calling blocked)
                         │             └──► expired   (submit a new KYC)
                         └──► rejected ──resubmit──► submitted
```

**One call tells you what to show:** `GET /v1/customers/{id}`

```json
{
  "customer_id": "bs_1001", "name": "Acme Logistics", "exists": true,
  "kyc": {"local": {"application_id": 7, "status": "accepted", "rejection_reason": null, "updated_at": "..."}},
  "can_buy": {"local": true},
  "numbers": {"held": 1, "limit": 10}
}
```
`kyc.local` is `null` when the customer has never submitted. `can_buy.local` is true when KYC is accepted and the
customer is under the number limit. Once accepted, the customer can buy at any later time: there is no expiry on
our side.

## 3. KYC

### 3.1 Optional: register the customer name
```
PUT /v1/customers/{id}          {"name": "Acme Logistics"}
```
Used to name the customer's Plivo subaccount. If you skip it, the business name from the KYC is used.

### 3.2 Get the requirements (to build your form)
```
GET /v1/config                  → cities, number types, kyc_documents_mode
GET /v1/kyc/requirements        → document_types[] from Plivo (cached 1 h)
```
Each `document_types[]` item has `document_type_id`, `name`, `description`, `proof_required` (file needed) and
`required_fields[]`.

Today Plivo returns two: **Registration Certificate** (field `business_name`, labelled "Legal Name", max 100
characters) and **GST certificate**. Plivo support confirmed (Nov 2026):
- **Both slots must be sent.** Keep `KYC_DOCUMENTS_MODE=all`.
- Accepted documents are the **GST Certificate, Certificate of Incorporation (COI) or Udyam Certificate**.
- **If the customer has only one of them, upload the same file in both slots.** Offer a "I only have one
  document" option in your UI.

### 3.3 Submit
```
POST /v1/customers/{id}/kyc     multipart/form-data
  payload = JSON (below)
  file_<document_type_id> = the file, for each document   (PDF/JPEG/PNG, ≤ 5 MB)
```
```json
{
  "number_type": "local",
  "end_user": {
    "name": "ACME LOGISTICS PRIVATE LIMITED",
    "registration_number": "U72200KA2020PTC123456",
    "email": "kyc@acme.in", "address_line1": "1 MG Road", "address_line2": "",
    "city": "Bangalore", "state": "Karnataka", "postal_code": "560001"
  },
  "documents": [
    {"document_type_id": "<Registration Certificate id>"},
    {"document_type_id": "<GST certificate id>"}
  ]
}
```
**Only one document?** Add `"same_file_for_all": true` to `payload` and attach the file once (under any of
the `file_<document_type_id>` names). The service sends it in every slot. Attaching the same file under each
name also works.

Rules enforced before anything reaches Plivo:
- `name` must match the name on the documents exactly. `business_name` fields are filled from it automatically.
- `registration_number` must be a CIN, Udyam number or GSTIN. A PAN is rejected.
- PIN code: 6 digits.
- One open application per customer and number type (409 otherwise).

Response `201`: the application with `"status": "submitted"`. Plivo usually decides in about 5 minutes and you
get a `kyc.accepted` or `kyc.rejected` webhook.

```bash
curl -X POST https://<service>/v1/customers/bs_1001/kyc \
  -H "X-API-Key: $KEY" \
  -F 'payload={"number_type":"local","end_user":{...},"documents":[{"document_type_id":"..."},{"document_type_id":"..."}]}' \
  -F "file_<reg-cert-id>=@coi.pdf;type=application/pdf" \
  -F "file_<gst-id>=@gst.pdf;type=application/pdf"
```

### 3.4 Rejected → resubmit
```
POST /v1/customers/{id}/kyc/{application_id}/resubmit     (same multipart format)
```
Show `rejection_reason` to the customer. Plivo replaces **all** documents on resubmission, so send every file
again, not only the one that was wrong.

Other: `GET /v1/customers/{id}/kyc` (all applications), `GET /v1/customers/{id}/kyc/{application_id}`.

## 4. Numbers

### 4.1 Search
```
GET /v1/customers/{id}/numbers/available?city=Bangalore&starts_with=8065&offset=0
```
- `city`: one of `/v1/config` → `cities` (currently Bangalore, Mumbai). Common spellings like Bengaluru work. Empty = any.
- `starts_with`: digits right after +91 (`80` = Bangalore, `22` = Mumbai).
- 20 results per page. Use `has_more` and `offset` for more.
- Returns 403 until KYC is accepted.

```json
{"numbers": [{"number": "918065587037", "e164": "+918065587037", "city": "Bangalore", "region": "Bangalore",
              "setup_price": 0.0, "monthly_price": 2.5, "currency": "USD", "voice_rate": 0.0, "sms_enabled": false}],
 "offset": 0, "has_more": true}
```
Results stay buyable **by this customer** for 15 minutes.

### 4.2 Buy

**Recommended order:** charge or reserve the customer's wallet → call buy → on failure, refund.

```
POST /v1/customers/{id}/numbers
Idempotency-Key: <your order id>
{"number": "+918065587037"}
```
| Result | Meaning |
|---|---|
| `201`, `status: "active"` | Rented and ready for calls. Webhook `number.purchased`. |
| `201`, `status: "pending_activation"` | Plivo accepted the order but hasn't activated it yet. Not given to the dialer. Webhook `number.activation_pending`. |
| `200` | Replay of the same `Idempotency-Key`: returns the original result. Nothing new was bought. |
| `400` | Not in this customer's recent search, or Plivo refused. Nothing rented: refund. |
| `403` | KYC not accepted. |
| `409` | Limit reached, number already taken, key reused for another number, or an earlier attempt with this key failed (use a new key to try again). |
| `502` | Plivo error. Nothing rented: refund. |

**Always send `Idempotency-Key`** (e.g. your order id). If the request times out, retry with the same key. You
can't be charged twice for one order.

### 4.3 Manage
```
GET    /v1/customers/{id}/numbers[?include_released=true]
GET    /v1/customers/{id}/numbers/{number_id}
PATCH  /v1/customers/{id}/numbers/{number_id}     {"alias": "Sales line"}
DELETE /v1/customers/{id}/numbers/{number_id}     release: irreversible, rental stops
```
Number object: `id, number, e164, number_type, city, region, alias, status, monthly_price, setup_price, currency,
created_at, released_at`. Status is one of `active`, `pending_activation` or `released`.

## 5. AI dialer

```
GET /v1/customers/{id}/caller-ids
→ {"customer_id": "...", "plivo_subaccount_id": "SA...", "numbers": [{"id": 1, "e164": "+918065587037", ...}]}

GET /v1/caller-ids/validate?customer_id=bs_1001&number=%2B918065587037
→ {"allowed": true}
```
Only **active** numbers with **accepted** KYC are returned. If Plivo suspends a customer for spam complaints,
their numbers disappear from this list immediately, matching Plivo, which blocks those calls.

India rules for your calls: caller ID must be one of these numbers; both call legs must be in India;
landline numbers are for **service/transactional** calls; and you need the recipient's consent.

## 6. Webhooks

Set `WEBHOOK_URL` (your HTTPS endpoint) and `WEBHOOK_SECRET` in the service's `.env`. Each event is a `POST`:

```http
POST /hooks/plivo-numbers
Content-Type: application/json
X-Webhook-Id: evt_3f2a...
X-Webhook-Event: kyc.accepted
X-Webhook-Timestamp: 1763366400
X-Webhook-Signature: sha256=8c1e...

{"id": "evt_3f2a...", "type": "kyc.accepted", "created_at": "2026-11-17T10:00:00+00:00",
 "customer_id": "bs_1001", "data": {"application": {...same shape as GET kyc...}}}
```

| Event | `data` | Typical action |
|---|---|---|
| `kyc.submitted` | `application` (+ `resubmission: true`) | Show "under review" |
| `kyc.accepted` | `application` | Unlock "Buy numbers", notify the customer |
| `kyc.rejected` | `application` (see `rejection_reason`) | Ask the customer to fix and resubmit |
| `kyc.suspended` | `application` | Block buying, alert ops (UCC/spam complaints) |
| `kyc.expired` | `application` | Ask the customer for a new KYC |
| `number.purchased` | `number` | Show the number, start billing |
| `number.activation_pending` | `number` | Show "activating" |
| `number.purchase_failed` | `number`, `reason` | Refund |
| `number.released` | `number` | Stop billing |
| `billing.charged` | `charge` | A rental period was deducted from the wallet |
| `billing.payment_due` | `charge`, `balance` | Renewal couldn't be paid: ask the seller to recharge |
| `number.suspended_nonpayment` | `charge` | Unpaid past the grace period: number is off the dialer |
| `number.released_nonpayment` | `charge` | Released for non-payment (only if `BILLING_RELEASE_AFTER_DAYS` > 0) |
| `billing.needs_review` | `charge` | Bigship refused a deduction or gave no clear answer: check by hand |
| `webhook.test` | `message` | From `POST /v1/webhook-events/test` |

**Your endpoint must:**
1. Verify the signature: `HMAC-SHA256(WEBHOOK_SECRET, timestamp + "." + raw_body)`, hex, prefixed `sha256=`.
   Use the **raw** body bytes. Reject timestamps older than 5 minutes.
2. Return any `2xx` within 10 seconds, and do slow work afterwards.
3. Ignore duplicates by `X-Webhook-Id`: delivery is at-least-once.

Non-2xx responses and timeouts are retried with backoff (30 s, 1 m, 2 m … up to 6 h, about 2 days in total).
Debugging: `GET /v1/webhook-events?status=failed`, `POST /v1/webhook-events/{id}/redeliver`.

**Python (Flask/Django-style):**
```python
import hashlib, hmac, time

def verify(raw_body: bytes, headers, secret: str) -> bool:
    ts = headers["X-Webhook-Timestamp"]
    if abs(time.time() - int(ts)) > 300:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers["X-Webhook-Signature"])
```

**Node.js (Express, with `express.raw({type: "application/json"})`):**
```js
const crypto = require("crypto");
function verify(rawBody, headers, secret) {
  const ts = headers["x-webhook-timestamp"];
  if (Math.abs(Date.now() / 1000 - Number(ts)) > 300) return false;
  const expected = "sha256=" + crypto.createHmac("sha256", secret).update(`${ts}.`).update(rawBody).digest("hex");
  const got = headers["x-webhook-signature"] || "";
  return got.length === expected.length && crypto.timingSafeEqual(Buffer.from(got), Buffer.from(expected));
}
```

If you'd rather not use webhooks, poll `GET /v1/customers/{id}` while a KYC is `submitted`.

## 7. Billing notes

### Wallet billing (`WALLET_BILLING_ENABLED=true`)
Number rental is deducted from the seller's **Bigship wallet** by this service, using the same Bigship wallet API
as AICall (`/api/shared/get-wallet-balance`, `/api/shared/ai-wallet-deduction`). Your frontend and Laravel do
nothing; the customer id must be the numeric Bigship customer id.

- **Price:** `NUMBER_MONTHLY_PRICE_INR` + `NUMBER_GST_PERCENT` GST per number per `BILLING_PERIOD_DAYS`
  (default ₹300 + 18% = **₹354 every 30 days**). Shown as `price_inr` in search results, numbers and `/v1/config`.
- **Buying:** the wallet must cover one period, otherwise `402` (or `503` if the wallet can't be read) and nothing
  is bought. After Plivo rents the number, the first period is deducted immediately.
- **Renewal:** each period is deducted automatically when the previous one ends (`paid_until` on the number).
- **Not enough balance:** the charge becomes `insufficient` and is retried daily (webhook `billing.payment_due`).
  After `BILLING_GRACE_DAYS` the number gets `billing_status: "overdue"` and is removed from caller IDs
  (`number.suspended_nonpayment`); it returns as soon as the charge goes through. With
  `BILLING_RELEASE_AFTER_DAYS` > 0 it is released on Plivo after that many unpaid days (irreversible).
- **Release stops billing at once:** no renewal is queued for a released number, and a renewal still waiting
  for money is `cancelled` (never deducted, even if the seller tops up later). Days already paid are not refunded.
- **No double charges:** each number+period is one row. Bigship's deduction API has no idempotency key, so a
  deduction that timed out or got a 5xx is marked `unknown` and **not retried**. Check the seller's wallet, then
  `POST /internal/billing/charges/{id}/resolve` with `{"status": "confirmed" | "failed" | "retry"}`.

```
GET  /v1/customers/{id}/charges[?number_id=]          the seller's charges (status, amount, period)
GET  /internal/billing/charges?status=unknown          (X-Admin-Key) charges that need a human
POST /internal/billing/charges/{id}/resolve            (X-Admin-Key)
POST /internal/billing/run                             (X-Admin-Key) run a billing pass now
```

### Plivo cost

- Prices returned are Plivo's cost × `PRICE_MARKUP`, in USD. Today: $0 setup, $2.50/month per Bangalore/Mumbai number.
- Plivo bills monthly rental from the rental date until the number is released. Bill your customers on the same
  basis using `number.purchased` and `number.released`.
- The service's Plivo account is prepaid. Keep it topped up, or enable auto-recharge: if it runs out, purchases and
  calls fail for **every** customer.

## 8. Settings (service `.env`)

| Setting | Purpose |
|---|---|
| `INTEGRATION_API_KEY` | The `X-API-Key`. Empty disables `/v1`. |
| `WEBHOOK_URL` / `WEBHOOK_SECRET` | Your webhook endpoint and signing secret |
| `KYC_DOCUMENTS_MODE` | `all` or `one`. Which documents customers must upload. |
| `NUMBER_CITIES` | Cities offered (Plivo spelling) |
| `MAX_NUMBERS_PER_CLIENT` | Per-customer limit |
| `PRICE_MARKUP` | Multiplier on Plivo prices |
| `WALLET_BILLING_ENABLED` | Deduct number rental from the seller's Bigship wallet |
| `NUMBER_MONTHLY_PRICE_INR` / `NUMBER_GST_PERCENT` / `BILLING_PERIOD_DAYS` | Rental price (₹, before GST), GST %, period |
| `BILLING_GRACE_DAYS` / `BILLING_RELEASE_AFTER_DAYS` | Days unpaid before off-dialer / before release (0 = never) |
| `BIGSHIP_WALLET_BASE_URL` / `_EMAIL` / `_PASSWORD` | Bigship wallet login (same as AICall) |
