# Plivo Numbers for Clients

Lets your dashboard clients complete their **own KYC** and **buy Indian phone numbers** on Bigship's
master Plivo account, so the AI marketing calls you run for them go out from their own verified numbers.

It uses Plivo's **PhoneNumber Compliance API** in the *reseller* model:

```
client KYC (end user + documents) ──► Plivo compliance application (one per client & number type)
                                         │ submitted → accepted / rejected (callback + poller)
                                         ▼
client searches numbers ──► buys ──► POST /PhoneNumber/{n} with compliance_application_id + PLIVO_APP_ID
                                         ▼
your AI dialer asks /internal/clients/{id}/caller-ids for the "from" number to use
```

## India rules this follows ([Plivo: India Number KYC](https://www.plivo.com/docs/numbers/rent-india-numbers))

- **Reseller model with subaccounts.** Each client gets its own Plivo subaccount (`PLIVO_USE_SUBACCOUNTS=true`).
  Its compliance application is filed with `data.subaccount`, and its numbers are bought with `subaccount` +
  `compliance_application_id`. A client's KYC can only back that client's numbers. A number can't be moved to
  another client, only released and bought again.
- **Businesses only.** Only India-registered businesses can rent Indian numbers.
- **Documents (`KYC_DOCUMENTS_MODE`).** The form is built from Plivo's Requirements API.
  - Plivo's API has two slots, **Registration Certificate** and **GST certificate**, and **both must be sent**
    (confirmed by Plivo support, Nov 2026). Keep `KYC_DOCUMENTS_MODE=all`; `one` is rejected by Plivo today.
  - Accepted documents: GST Certificate, Certificate of Incorporation (COI) or Udyam Certificate. A business
    with only one of them uploads **the same file in both slots**, via `"same_file_for_all": true` in the API
    or the "I have only one document" checkbox in the form.
  - The business name must match exactly across `end_user.name` and every `business_name` field (filled in
    automatically, and a mismatch is rejected). Files must be PDF/JPEG/PNG, at most 5 MB, sealed and signed,
    and current copies from the government portal.
  - A business PAN alone is rejected, and the registration number must be a valid GSTIN, CIN or Udyam number.
- **Search & buy** ([Phone Numbers API](https://www.plivo.com/docs/numbers/phone-numbers)).
  - Search uses `type=local` and `services=voice`, with `city` for local numbers. `pattern` is a **prefix** after
    the country code (`80` → +91 80…), and results come 20 per page. Numbers with a `restriction` (address or
    terms verification) aren't offered.
  - Buy calls `POST /PhoneNumber/{number}/` (no `+`) with main-account credentials, `subaccount`,
    `compliance_application_id` and `app_id`.
    - `status: fulfilled` + `Success` → **active**.
    - Anything Plivo hasn't fulfilled yet → **pending_activation**: shown as "activating", excluded from the
      dialer, and the raw response is kept in `failure_reason`.
    - A failure → **failed**.
  - Prices are in USD.
- **Rate limits.** Plivo allows 60 compliance requests/minute. Status checks from page views are throttled
  (20 s per application), and the poller refreshes at most 30 pending applications per run. Plivo callbacks always sync.
- **Statuses:** `submitted` (automated review, ~5 min for 080/022), `accepted`, `rejected` (fix & resubmit),
  `expired` (submit a new one), `suspended` (unresolved UCC/spam complaints). A suspended client can't open a
  new KYC, and its numbers are dropped from the dialer's caller-ID list.
- **Number types.** Only `local` (landline 080/022…) is self-serve. Plivo allows it for **service and
  transactional calls only, no promotional content**. Promotional calls need **140-series** numbers, and
  BFSI transactional calls need **160-series** numbers. Both go through a manual Plivo + Tata DLT process
  (declaration form, NOC, Voice Header and Template, ~5–14 business days) and are not handled by this service.
- **Calling.** The caller ID must be a number rented on the account, both call legs must stay in India,
  and commercial calls need the recipient's explicit consent. Otherwise they count as UCC: complaints need
  opt-in proof within 5 business days, and repeated violations lead to suspension, then TRAI blacklisting.

## Run locally

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # PLIVO_MOCK=true by default, so nothing is spent
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000. With `DEV_MODE=true` you can sign in as any client id.
In mock mode KYC is auto-approved after about 5 seconds. An applicant name containing "reject" is
rejected, so you can try the resubmit flow.

API docs: http://localhost:8000/docs. Tests: `.venv\Scripts\python -m pytest`.

## Integrating with your dashboard

**Recommended: the server-to-server Integration API (`/v1`).** Your backend calls it with
`X-API-Key: <INTEGRATION_API_KEY>` and your own customer ids, and receives signed webhooks
(`kyc.accepted`, `number.purchased`, …). See **[docs/INTEGRATION.md](docs/INTEGRATION.md)** and `/docs` → *Integration v1*.

The options below are for embedding the bundled UI instead.

1. **Auth.** Your dashboard backend signs a short-lived HS256 JWT with `JWT_SECRET`:
   ```python
   jwt.encode({"sub": client.id, "name": client.company_name, "exp": now + 3600}, JWT_SECRET, "HS256")
   ```
   Then either open or iframe `https://<this-service>/?token=<jwt>` (the token is moved out of the URL
   into sessionStorage on load), or call the `/api/*` endpoints yourself with `Authorization: Bearer <jwt>`.
   `sub` is the client id that owns the KYC and the numbers.
2. **AI dialer.** Before placing a call for a client, pick the caller ID from
   `GET /internal/clients/{client_id}/caller-ids`, or check one with
   `GET /internal/caller-ids/validate?client_id=..&number=..`. Both need the header `X-Admin-Key`.
   Every purchased number is attached to `PLIVO_APP_ID`, so inbound calls and callbacks hit your existing app.
3. **Billing.** Each purchase writes `setup` and `monthly_rental` lines to `ledger_entries`, at Plivo cost
   × `PRICE_MARKUP`. Run `POST /internal/billing/accrue` daily (cron) to add the next month's rental lines.
   Read them from `GET /internal/ledger` into your wallet or invoicing. No money is collected here.

## Endpoints

| Who | Method & path | Purpose |
|---|---|---|
| client | `GET /api/me` | config for the UI |
| client | `GET /api/compliance/requirements?number_type&user_type=business` | the 3 accepted registration documents & their fields |
| client | `GET/POST /api/compliance/applications` | list / submit KYC (multipart: `payload` JSON + `file_<document_type_id>`) |
| client | `GET /api/compliance/applications/{id}` | status (refreshed from Plivo) |
| client | `POST /api/compliance/applications/{id}/resubmit` | correct a rejected KYC |
| client | `GET /api/numbers/search?number_type&city&pattern&offset` | requires accepted KYC |
| client | `POST /api/numbers` `{number}` | buy (only numbers from the client's own recent search) |
| client | `GET /api/numbers`, `PATCH/DELETE /api/numbers/{id}` | list, label, release |
| Plivo | `GET/POST /webhooks/plivo/compliance/{id}?token=` | status-change callback (body ignored, re-fetched from Plivo) |
| internal | `/internal/clients/{id}/caller-ids`, `/internal/caller-ids/validate`, `/internal/applications`, `/internal/numbers`, `/internal/ledger`, `POST /internal/compliance/sync`, `POST /internal/billing/accrue` | dialer & admin |

## Going live checklist

- [ ] The Plivo account must be on the **India data region** (Plivo requirement; it cannot be changed later).
- [ ] `PLIVO_MOCK=false`, real `PLIVO_AUTH_ID` / `PLIVO_AUTH_TOKEN` (**main** account; subaccount credentials get 404 on buy).
- [ ] `PLIVO_APP_ID` = the Plivo application your AI calls use.
- [ ] `PUBLIC_BASE_URL=https://...` so Plivo can send compliance callbacks (HTTPS only). Without it the poller (`COMPLIANCE_POLL_SECONDS`) still picks up status changes.
- [ ] `DEV_MODE=false`, long random `JWT_SECRET` and `ADMIN_API_KEY`.
- [ ] `DATABASE_URL` → Postgres (e.g. `postgresql+psycopg://...`, plus `pip install psycopg[binary]`). SQLite is fine for a single instance.
- [ ] Check the live document requirements once with a test client. The form is built from Plivo's
      Requirements API, so the document list and fields adapt automatically.
- [ ] Your organization's *first* compliance application must be sealed and signed (Plivo takes billing and GST details from it).
- [ ] Confirm with Plivo how your AI dialer should place calls from numbers held in client subaccounts
      (main-account credentials vs. the subaccount's own). `/internal/clients/{id}/caller-ids` returns the subaccount auth_id.
- [ ] Decide on call purpose. Landline numbers are for service and transactional calls only; promotional campaigns need 140-series numbers.

## Layout

```
app/
  main.py               FastAPI app, background compliance poller, static UI
  routers.py            client API, Plivo webhook, internal API
  services/compliance.py  KYC validation + Plivo compliance workflow
  services/numbers.py     search / buy / release / billing ledger
  plivo_client.py       Plivo REST wrapper + MockPlivoClient
  models.py             compliance_applications, phone_numbers, ledger_entries
  static/               client UI (vanilla JS, no build step)
tests/test_flow.py      end-to-end tests against the mock
```
