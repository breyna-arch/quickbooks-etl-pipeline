# QuickBooks Financial Sync

A Python-based ETL pipeline that syncs financial data from the QuickBooks 
API into a PostgreSQL database hosted on Supabase.

## Overview

Built to automate financial reporting workflows that were previously 
managed manually. The pipeline runs on a daily cron schedule and keeps 
customer, payment, and invoice data current without manual intervention.

## Features

- OAuth 2.0 authentication with automatic token refresh
- Paginated API requests handling large datasets
- Concurrent sync operations via ThreadPoolExecutor
- Idempotent upsert logic, is safe to run multiple times without duplicates
- Retry logic with exponential backoff for transient API failures
- Structured logging to console and file

## Data Model

- `qb_customers` — tenant/customer records
- `qb_payments` — payment transactions linked to customers
- `qb_invoices` — invoice headers linked to customers
- `qb_invoice_lines` — line items linked to invoices

## Tech Stack

- Python 3
- Supabase (PostgreSQL)
- QuickBooks API (Intuit)
- python-dotenv
- requests + urllib3 Retry

## Setup

1. Clone the repo
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in your credentials
4. Run: `python quickbooks_sync.py`

## Environment Variables

See `.env.example` for required configuration.

## Automating with Cron

To run daily at 2am:
```
0 2 * * * /usr/bin/python3 /path/to/quickbooks_sync.py
```