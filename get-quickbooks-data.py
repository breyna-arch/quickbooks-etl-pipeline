#!/usr/bin/env python3

import os
import json
import time
import logging
import requests
from datetime import datetime, timedelta
from supabase import create_client, Client
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter, Retry
from dateutil import parser as dateutil_parser
from concurrent.futures import ThreadPoolExecutor, as_completed

# Load environment variables
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path)

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("quickbooks_sync.log")
    ]
)

# Configuration
CONFIG = {
    "supabase": {
        "url": os.getenv("SUPABASE_URL"),
        "key": os.getenv("SUPABASE_KEY"),
    },
    "quickbooks": {
        "client_id": os.getenv("client_id"),
        "client_secret": os.getenv("client_secret"),
        "redirect_uri": os.getenv("redirect_uri"),
        "auth_code": os.getenv("auth_code"),
        "realm_id": os.getenv("realm_id"),
        "token_file": os.path.join(os.path.dirname(__file__), os.getenv("token_file", "token.json")),
        "api_base": "https://quickbooks.api.intuit.com/v3/company",
        "token_url": "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    }
}

class QuickBooksSync:
    def __init__(self):
        self.session = self._create_session()
        self.supabase: Client = self._connect_supabase()
        self.access_token = self._get_valid_access_token()

    # ------------------------------------------------------------------
    # Infrastructure
    # ------------------------------------------------------------------

    def _create_session(self):
        """Create a requests.Session with retry logic."""
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
        session.mount("https://", HTTPAdapter(max_retries=retries))
        return session

    def _connect_supabase(self) -> Client:
        """Connect to Supabase and return the client."""
        url = CONFIG["supabase"]["url"]
        key = CONFIG["supabase"]["key"]
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
        client = create_client(url, key)
        logging.info("Connected to Supabase")
        return client

        # ------------------------------------------------------------------
        # Token management
        # ------------------------------------------------------------------

    def _load_tokens(self):
        token_file = CONFIG["quickbooks"]["token_file"]
        if os.path.exists(token_file):
            with open(token_file, "r") as f:
                return json.load(f)
        return None

    def _save_tokens(self, tokens):
        tokens["timestamp"] = int(time.time())
        with open(CONFIG["quickbooks"]["token_file"], "w") as f:
            json.dump(tokens, f)

    def _refresh_access_token(self, refresh_token):
        logging.info("Refreshing access token...")
        response = self.session.post(
            CONFIG["quickbooks"]["token_url"],
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(CONFIG["quickbooks"]["client_id"], CONFIG["quickbooks"]["client_secret"])
        )
        if response.status_code == 200:
            tokens = response.json()
            self._save_tokens(tokens)
            return tokens["access_token"]
        logging.error(f"Error refreshing token: {response.text}")
        return None

    def _get_new_tokens(self):
        logging.info("Getting new access token using auth code...")
        response = self.session.post(
            CONFIG["quickbooks"]["token_url"],
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": CONFIG["quickbooks"]["auth_code"],
                "redirect_uri": CONFIG["quickbooks"]["redirect_uri"],
            },
            auth=(CONFIG["quickbooks"]["client_id"], CONFIG["quickbooks"]["client_secret"])
        )
        if response.status_code == 200:
            tokens = response.json()
            self._save_tokens(tokens)
            return tokens["access_token"]
        logging.error(f"Error getting acces token: {response.text}")
        return None

    def _get_valid_access_token(self):
        tokens = self._load_tokens()
        if tokens:
            expires_in = tokens.get("expires_in", 3600)
            token_age = int(time.time()) - tokens.get("timestamp", 0)
            if token_age < expires_in - 60:
                logging.info("Using existing access token")
                return tokens["access_token"]
            elif "refresh_token" in tokens:
                refresh_expiry = tokens.get("x_refresh_token_expires_in", 0)
                if refresh_expiry and token_age > refresh_expiry:
                    logging.error("Refresh token expired, manual re-auth required")
                    return None
                return self._refresh_access_token(tokens["refresh_token"])
        return self._get_new_tokens()
    
    # ------------------------------------------------------------------
    # Quickbooks API
    # ------------------------------------------------------------------

    def _quickbooks_request(self, query):
        """Make a paginated request to the Quickbooks API."""
        url = f"{CONFIG['quickbooks']['api_base']}/{CONFIG['quickbooks']['realm_id']}/query"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": "application/text"
        }

        all_data = []
        start_position = 1
        while True:
            paginated_query = f"{query} STARTPOSITION {start_position} MAXRESULTS 1000"
            try:
                response = self.session.post(url, headers=headers, data=paginated_query)
                response.raise_for_status()
                data = response.json()
                all_data.append(data)
                count = len(data.get("QueryResponse", {}).get(
                    next(iter(data.get("QueryResponse", {})), []), []
                ))
                if count < 1000:
                    break
                start_position += 1000
            except requests.exceptions.RequestException as e:
                logging.error(f"QuickBooks API request failed {e}")
                break
        return all_data

    # ------------------------------------------------------------------
    # Sync methods
    # ------------------------------------------------------------------

    def sync_customers(self):
        """Upsert customer data from QuickBooks into qb_customers."""
        logging.info("Starting customer sync...")
        five_years_ago = (datetime.now() - timedelta(days=365 * 5)).strftime("%Y-%m-%d")
        query = f"SELECT * FROM Customer WHERE MetaData.LastUpdatedTime >= '{five_years_ago}'"

        responses = self._quickbooks_request(query)
        if not responses:
            return False
        
        rows = []
        for response in responses:
            for customer in response.get("QueryResponse", {}).get("Customer", []):
                customer_id = customer.get("Id")
                if not customer_id:
                    continue

                last_updated = customer.get("MetaData", {}).get("LastUpdatedTime") or datetime.utcnow().isoformat()

                rows.append({
                    "id": customer_id,
                    "display_name": customer.get("DisplayName", "N/A"),
                    "email": customer.get("PrimaryEmailAddr", {}).get("Address", ""),
                    "phone": customer.get("PrimaryPhone", {}).get("FreeFormNumber", ""),
                    "balance": float(customer.get("Balance", 0.0)),
                    "last_updated_time": last_updated,
                    "synced_at": datetime.utcnow().isoformat(),
                })

        if not rows:
            logging.info("No customers to sync")
            return True

        try:
            # upsert: insert or update on primary key conflict
            self.supabase.table("qb_customers").upsert(rows, on_conflict="id").execute()
            logging.info(f"Upserted {len(rows)} customers")
            return True
        except Exception as e:
            logging.error(f"Error upserting customers: {e}")
            return False

    def sync_payments(self):
        """Upsert payment data from QuickBooks into qb_paymets."""
        logging.info("Starting payments sync...")
        five_years_ago = (datetime.now() - timedelta(days=365 * 5)).strftime("%Y-%m-%d")
        query = f"SELECT * FROM Payment WHERE TxnDate >= '{five_years_ago}' ORDER BY TxnDate DESC"

        responses = self._quickbooks_request(query)
        if not responses:
            logging.info("No payments fetched from QuickBooks")
            return False

        rows = []
        for response in responses:
            for payment in response.get("QueryResponse", {}).get("Payment", []):
                payment_id = payment.get("Id")
                customer_id = payment.get("CustomerRef", {}).get("value")
                txn_date_raw = payment.get("TxnDate", "")

                if not all([payment_id, customer_id, txn_date_raw]):
                    continue

                try:
                    txn_date = dateutil_parser.parse(txn_date_raw).date().isoformat()
                except Exception:
                    logging.warning(f"Invalid date '{txn_date_raw}', using today")
                    txn_date = datetime.utcnow().date().isoformat()

                rows.append({
                    "id": payment_id,
                    "customer_id": customer_id,
                    "total_amt": float(payment.get("TotalAmt", 0)),
                    "txn_date": txn_date,
                    "synced_at": datetime.utcnow().isoformat(),
                })

        if not rows:
            logging.info("No payments to sync")
            return True

        try:
            # Deduplication is handled by the primary key upsert
            self.supabase.table("qb_payments").upsert(rows, on_conflict="id").execute()
            logging.info(f"Upserted {len(rows)} payments")
            return True
        except Exception as e:
            logging.error(f"Error upserting payments: {e}")
            return False

    def sync_rentals(self):
        """Upsert invoice + line item data from QuickBooks into qb_invoices / qb_invoice_lines."""
        logging.info("Starting invoice/rental sync...")
        five_years_ago = (datetime.now() - timedelta(days=365 * 5)).strftime("%Y-%m-%d")
        query = f"SELECT * FROM Invoice WHERE TxnDate >= '{five_years_ago}' ORDER BY TxnDate DESC"

        responses = self._quickbooks_request(query)
        if not responses:
            return False

        invoice_rows = []
        line_rows = []

        for response in responses:
            for invoice in response.get("QueryResponse", {}).get("Invoice", []):
                invoice_id = invoice.get("Id")
                customer_id = invoice.get("CustomerRef", {}).get("value")
                txn_date_raw = invoice.get("TxnDate", "")

                if not all([invoice_id, customer_id, txn_date_raw]):
                    continue

                try:
                    txn_date = dateutil_parser.parse(txn_date_raw).date().isoformat()
                except Exception:
                    logging.warning(f"Invalid date '{txn_date_raw}', using today")
                    txn_date = datetime.utcnow().date().isoformat()

                invoice_rows.append({
                    "id": invoice_id,
                    "customer_id": customer_id,
                    "txn_date": txn_date,
                    "synced_at": datetime.utcnow().isoformat(),
                })

                for line in invoice.get("Line", []):
                    line_id = line.get("Id")
                    if not line_id:
                        continue

                    item_detail = line.get("SalesItemLineDetail", {})
                    item_name = item_detail.get("ItemRef", {}).get("name", "Unknown")

                    try:
                        qty = int(float(item_detail.get("Qty", 1)))
                    except (ValueError, TypeError):
                        qty = 1

                    line_rows.append({
                        "invoice_id": invoice_id,
                        "customer_id": customer_id,
                        "line_id": line_id,
                        "item_name": item_name,
                        "amount": float(line.get("Amount", 0)),
                        "unit_price": float(item_detail.get("UnitPrice", 0)),
                        "qty": qty,
                        "synced_at": datetime.utcnow().isoformat(),
                    })

        try:
            if invoice_rows:
                self.supabase.table("qb_invoices").upsert(invoice_rows, on_conflict="id").execute()
                logging.info(f"Upserted {len(invoice_rows)} invoices")

            if line_rows:
                # Dedup by the unique (invoice_id, line_id) constraint defined in schema
                self.supabase.table("qb_invoice_lines").upsert(
                    line_rows, on_conflict="invoice_id,line_id"
                ).execute()
                logging.info(f"Upserted {len(line_rows)} invoice line items")

            return True
        except Exception as e:
            logging.error(f"Error upserting invoices/lines: {e}")
            return False

    # ------------------------------------------------------------------
    # Entry Point
    # ------------------------------------------------------------------

    def run_sync(self):
        """Run all sync operations."""
        if not self.access_token:
            logging.error("No valid access token")
            return False
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(self.sync_customers): "customers",
                executor.submit(self.sync_payments): "payments",
                executor.submit(self.sync_rentals): "rentals",
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    new_thread = {"name": name, "error": f"thread raised an exception: {e}"}
                    threads.append(new_thread)
                else:
                    new_thread = {"name": name, "error": None if result else "sync returned False"}
                    threads.append(new_thread)

        success_count = sum(1 for t in threads if t.get("error") is None)
        if success_count == 3:
            for thread in threads:
                logging.info(f"The {thread['name']} sync completed successfully")
            return True
        else:
            for thread in threads:
                logging.warning(f"Error during the {thread['name']} sync: {thread['error']}")
            return False

if __name__ == "__main__":
    sync = QuickBooksSync()
    sync.run_sync()
