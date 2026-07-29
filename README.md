# Holy Grills — Backend API

Python Flask REST API powering the Holy Grills Bookkeeping Platform.

---

## Setup

### 1. Install dependencies

```bash
cd artifacts/holy-grills-api
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Fill in all values — see comments in .env.example
```

### 3. Set up Supabase

1. Create a Supabase project at https://supabase.com
2. Open the SQL Editor and paste the entire contents of `holy_grills_schema.sql` (root of this repo)
3. Run it — all tables, triggers, functions, and RLS policies are created in one shot
4. Copy your Project URL, Anon Key, Service Role Key, and JWT Secret into `.env`

### 4. Set up Google Cloud / Gmail OAuth

1. Go to https://console.cloud.google.com → APIs & Services → Credentials
2. Create an OAuth 2.0 Client ID (Web application)
3. Add `http://localhost:5001/api/sync/gmail/callback` as an authorised redirect URI
4. Copy your Client ID and Client Secret into `.env`
5. Enable the Gmail API in Google Cloud Console

### 5. Run locally

```bash
python app.py
```

The API runs at `http://localhost:5001`.

---

## API Endpoints

### Authentication
All endpoints (except `/api/healthz` and `/api/sync/gmail/callback`) require a Supabase Bearer token in the `Authorization` header:

```
Authorization: Bearer <supabase-access-token>
```

### Overview
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/overview` | Dashboard aggregate (balance, today's totals, recent transactions) |
| GET | `/api/overview/chart?period=30\|90\|year` | Time-series income vs. expense |
| GET | `/api/overview/donut` | Category-wise spend for current month |
| GET | `/api/overview/digests` | List all digests |
| POST | `/api/overview/digests/<id>/read` | Mark digest as read |
| POST | `/api/overview/digests/read-all` | Mark all digests as read |

### Accounts
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/accounts` | List all accounts with computed balances |
| POST | `/api/accounts` | Create account |
| GET | `/api/accounts/<id>` | Single account |
| PATCH | `/api/accounts/<id>` | Update account metadata |
| DELETE | `/api/accounts/<id>` | Deactivate account |
| GET | `/api/accounts/<id>/balance` | Computed balance |
| GET | `/api/accounts/<id>/balance-history?days=30` | Daily balance snapshots |
| POST | `/api/accounts/<id>/balance-adjustment` | Adjust opening balance (with audit log) |
| POST | `/api/accounts/<id>/recompute-reconciliation` | Force-recompute reconciliation |
| GET | `/api/accounts/<id>/transactions` | Filtered transactions for one account |

### Transactions
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/transactions` | Paginated, filterable list |
| GET | `/api/transactions/<id>` | Single transaction |
| POST | `/api/transactions` | Manual transaction entry |
| PATCH | `/api/transactions/<id>` | Update (category, type, etc.) |
| POST | `/api/transactions/<id>/mark-transfer` | Mark as internal transfer |
| POST | `/api/transactions/<id>/flag-mis-parse` | Flag as mis-parsed |
| GET | `/api/transactions/archive` | Archived transactions (>2 years) |

### Categories
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/categories` | List all |
| POST | `/api/categories` | Create |
| GET | `/api/categories/<id>` | Single |
| PATCH | `/api/categories/<id>` | Update |
| DELETE | `/api/categories/<id>` | Delete with reassignment option |
| GET | `/api/categories/<id>/transactions-count` | Count before delete confirmation |

### Review Queue
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/review/queue` | Transactions needing review |
| POST | `/api/review/queue/<id>/categorize` | Assign category + create rule |
| POST | `/api/review/queue/<id>/skip` | Skip for now |
| GET | `/api/review/failed-imports` | Pending failed imports |
| GET | `/api/review/failed-imports/<id>` | Single failed import (full content) |
| POST | `/api/review/failed-imports/<id>/convert` | Convert to manual transaction |
| POST | `/api/review/failed-imports/<id>/ignore` | Dismiss |
| GET | `/api/review/stats` | Queue size counters |

### Reports
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/reports/pl?date_from=&date_to=` | Profit & Loss |
| GET | `/api/reports/cashflow?date_from=&date_to=` | Cash Flow |
| GET | `/api/reports/reconciliation` | Reconciliation Summary |
| GET | `/api/reports/export/pl.pdf` | P&L PDF |
| GET | `/api/reports/export/pl.xlsx` | P&L Excel |
| GET | `/api/reports/export/cashflow.pdf` | Cash Flow PDF |
| GET | `/api/reports/export/cashflow.xlsx` | Cash Flow Excel |
| GET | `/api/reports/export/reconciliation.pdf` | Reconciliation PDF |
| GET | `/api/reports/export/reconciliation.xlsx` | Reconciliation Excel |

### Settings
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/settings` | User settings |
| PATCH | `/api/settings` | Update settings |
| GET | `/api/settings/profile` | User profile |
| PATCH | `/api/settings/profile` | Update profile |
| GET | `/api/settings/parsing-rules` | List parsing rules |
| POST | `/api/settings/parsing-rules` | Create rule |
| PATCH | `/api/settings/parsing-rules/<id>` | Update rule |
| DELETE | `/api/settings/parsing-rules/<id>` | Delete rule |
| GET | `/api/settings/budgets` | List budgets |
| POST | `/api/settings/budgets` | Create/update budget |
| DELETE | `/api/settings/budgets/<id>` | Delete budget |
| GET | `/api/settings/audit-log` | Read-only audit log |

### Sync
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/sync/trigger` | Trigger sync (all accounts or one) |
| POST | `/api/sync/cron` | Cron job endpoint (X-Cron-Key auth) |
| GET | `/api/sync/gmail/initiate?account_id=` | Start Gmail OAuth |
| GET | `/api/sync/gmail/callback` | Gmail OAuth callback (Google redirects here) |
| POST | `/api/sync/gmail/disconnect/<id>` | Revoke Gmail access |
| GET | `/api/sync/jobs` | Recent sync job history |
| GET | `/api/sync/jobs/<id>` | Single sync job |
| POST | `/api/sync/reparse/<account_id>` | Re-parse failed imports with updated rules |
| POST | `/api/sync/housekeeping` | Run purge + archive jobs |

---

## Architecture Notes

### Two-layer recording
Every transaction is recorded with `direction` (debit/credit) first — this is always extracted reliably from the email. Narration-based categorization is a second, best-effort layer. A failed categorization never blocks the transaction from being saved.

### Duplicate detection
Every transaction gets a `fingerprint = MD5(account_id|reference|amount|date)`. This is checked before every insert. Exact matches are silently discarded.

### Parser versioning
Each sender parser has a `VERSION` constant. When you update regex patterns, bump `VERSION`. New transactions store the parser version they were parsed with. Old transactions keep their original version.

### Token encryption
Gmail OAuth tokens are encrypted with AES-256-CBC before storage. The key lives in `TOKEN_ENCRYPTION_KEY` (env var). Never log or expose this key.

### Audit log
The `audit_log` table is append-only. Manual balance adjustments and manual sync triggers are always logged. RLS allows users to read but not write their own audit log — only the service-role key can insert.

---

## Adding a New Sender

1. Create `parsers/<sender_name>.py` extending `BaseParser`
2. Implement `parse_email()` — raise `NonTransactionEmail` for marketing emails, `ParseError` for genuine failures
3. Register the sender email → class mapping in `parsers/__init__.py`

---

## Production Deployment (Render)

1. Create a new Web Service on Render, connected to this repo
2. Set build command: `pip install -r requirements.txt`
3. Set start command: `gunicorn app:app -w 2 -b 0.0.0.0:$PORT`
4. Add all environment variables from `.env.example`
5. Update `GMAIL_REDIRECT_URI` to your production URL
6. Add your production URL to the authorised redirect URIs in Google Cloud Console
