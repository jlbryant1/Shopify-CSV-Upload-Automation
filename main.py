#!/usr/bin/env python3
"""
ShipStation → GPX → Google Sheets Daily Automation
====================================================
1. Pulls shipped orders from "Shopify Marketing Experiment" on ShipStation
2. Extracts device serial numbers from internal notes
3. Looks up each serial on admin.gpx.co → gets IMEI, ICCID, SIM Provider, Status
4. Creates a Google Sheet named "Shopify Shipment - M.DD.YY" with all data
5. Stores the sheet in a Google Drive folder
6. Runs daily at 3 PM via cron

Usage:
    python main.py                     # Run for today
    python main.py --list-stores       # Find your ShipStation store ID
    python main.py --date 2026-02-25   # Run for a specific date
    python main.py --dry-run           # Test without Google upload
"""

import os
import re
import sys
import csv
import json
import time
import shutil
import logging
import argparse
import tempfile
from datetime import datetime
from base64 import b64encode
from io import StringIO

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuration & Logging
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("automation.log"),
    ],
)
log = logging.getLogger(__name__)

SHIPSTATION_API_KEY = os.getenv("SHIPSTATION_API_KEY")
SHIPSTATION_API_SECRET = os.getenv("SHIPSTATION_API_SECRET")
SHIPSTATION_STORE_ID = os.getenv("SHIPSTATION_STORE_ID")
GPX_ADMIN_URL = "https://admin.gpx.co/"
GPX_USERNAME = os.getenv("GPX_USERNAME")
GPX_PASSWORD = os.getenv("GPX_PASSWORD")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"


def validate_env():
    """Check all required env vars are set."""
    missing = []
    for var in [
        "SHIPSTATION_API_KEY", "SHIPSTATION_API_SECRET", "SHIPSTATION_STORE_ID",
        "GPX_USERNAME", "GPX_PASSWORD", "GOOGLE_DRIVE_FOLDER_ID",
    ]:
        if not os.getenv(var):
            missing.append(var)
    if not os.path.exists("token.json"):
        missing.append("token.json (run 'python auth_setup.py' first)")
    if missing:
        log.error("Missing configuration: %s", ", ".join(missing))
        log.error("Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)


# ===========================================================================
# STEP 1: ShipStation — Pull Shipped Orders
# ===========================================================================

class ShipStationClient:
    """Pulls shipped orders from ShipStation API."""

    BASE_URL = "https://ssapi.shipstation.com"

    def __init__(self, api_key: str, api_secret: str):
        token = b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        })

    def _get(self, endpoint: str, params: dict = None) -> dict:
        url = f"{self.BASE_URL}{endpoint}"
        for attempt in range(5):
            resp = self.session.get(url, params=params)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 30))
                log.warning("ShipStation rate limit. Waiting %ds...", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        raise Exception("ShipStation rate limit exceeded after retries")

    def list_stores(self) -> list:
        """List all stores to find the Shopify Marketing Experiment store ID."""
        return self._get("/stores")

    def get_shipped_orders(self, ship_date: str, store_id: str) -> list:
        """
        Fetch orders shipped on a given date for a specific store.

        Uses the /shipments endpoint (which reliably filters by date) to get
        today's shipment orderIds, then fetches those specific orders to get
        internal notes.
        """
        # Step 1: Get today's shipments (this endpoint filters by date properly)
        log.info("Fetching shipments for %s from store %s...", ship_date, store_id)
        all_shipments = []
        page = 1

        while True:
            data = self._get("/shipments", params={
                "shipDateStart": f"{ship_date}T00:00:00",
                "shipDateEnd": f"{ship_date}T23:59:59",
                "storeId": int(store_id),
                "pageSize": 100,
                "page": page,
            })
            shipments = data.get("shipments", [])
            all_shipments.extend(shipments)
            log.info("Shipments page %d: %d shipments (total: %d)",
                     page, len(shipments), len(all_shipments))
            if page >= data.get("pages", 1):
                break
            page += 1

        if not all_shipments:
            return []

        # Step 2: Get unique order IDs from shipments
        order_ids = list(set(
            s.get("orderId") for s in all_shipments if s.get("orderId")
        ))
        log.info("Found %d shipments → %d unique orders to fetch.", len(all_shipments), len(order_ids))

        # Step 3: Fetch each order to get internal notes
        all_orders = []
        for oid in order_ids:
            try:
                order = self._get(f"/orders/{oid}")
                all_orders.append(order)
            except Exception as e:
                log.warning("Could not fetch order %s: %s", oid, e)

        log.info("Fetched %d orders with full details.", len(all_orders))
        return all_orders


# ===========================================================================
# STEP 2: Extract Serial Numbers from Internal Notes
# ===========================================================================

def extract_serial_numbers(internal_notes: str) -> list[str]:
    """
    Extract serial numbers from internal notes.

    Based on the ShipStation screenshot, serial numbers are plain 6-digit
    numbers in the internal notes field (e.g., "263384").

    If an order has multiple items (e.g., quantity 2), there may be multiple
    serial numbers separated by newlines, commas, or spaces.
    """
    if not internal_notes:
        return []

    # Match 5-7 digit numbers (serial numbers seen in screenshots are 6 digits)
    # Adjust range if your serials differ
    serials = re.findall(r'\b(\d{5,7})\b', internal_notes)
    return serials


def parse_orders(orders: list) -> list[dict]:
    """
    Parse ShipStation orders into flat records, one per serial number.

    Extracts: order number, item SKU, serial number, and raw notes.
    IMEI/ICCID/SIM Provider/Status will be filled in by the GPX lookup.
    """
    records = []

    for order in orders:
        order_number = order.get("orderNumber", "")
        internal_notes = order.get("internalNotes") or ""
        items = order.get("items", [])

        # Get item SKU(s) — if multiple items, join them
        skus = [item.get("sku", "") for item in items]

        serials = extract_serial_numbers(internal_notes)

        if not serials:
            log.warning("Order %s: no serial numbers found in notes: '%s'",
                        order_number, internal_notes[:100])
            # Still record it so nothing is silently lost
            records.append({
                "sku": ", ".join(skus),
                "serial": "NOT FOUND",
                "imei": "",
                "iccid": "",
                "sim_provider": "",
                "retailer": "shopify",
                "status": "unassigned",
                "order_number": order_number,
                "raw_notes": internal_notes[:200],
            })
            continue

        # One record per serial number
        for i, sn in enumerate(serials):
            sku = skus[i] if i < len(skus) else (skus[0] if skus else "")
            records.append({
                "sku": sku,
                "serial": sn,
                "imei": "",
                "iccid": "",
                "sim_provider": "",
                "retailer": "shopify",
                "status": "unassigned",
                "order_number": order_number,
                "raw_notes": "",
            })

    log.info("Parsed %d serial number records from %d orders.", len(records), len(orders))
    return records


# ===========================================================================
# STEP 3: GPX Admin Portal — Look Up IMEI & ICCID
# ===========================================================================

class GPXScraper:
    """
    Automates admin.gpx.co to look up device details by serial number.

    Flow (based on screenshot):
      1. Log in at admin.gpx.co
      2. Use the top search bar to search a serial number
      3. Click the matching device row in the Devices table
      4. Extract IMEI, ICCID, SIM Provider, and Status from the detail page
      5. Navigate back and repeat for the next serial
    """

    def __init__(self, username: str, password: str, headless: bool = True):
        self.username = username
        self.password = password
        self.headless = headless
        self._pw = None
        self._browser = None
        self.page = None

    def start(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        context = self._browser.new_context(viewport={"width": 1440, "height": 900})
        self.page = context.new_page()
        self._login()

    def _login(self):
        """Log in to admin.gpx.co."""
        log.info("Logging in to GPX admin portal...")
        self.page.goto(GPX_ADMIN_URL, wait_until="networkidle", timeout=30000)
        time.sleep(3)

        os.makedirs("/app/screenshots", exist_ok=True)

        try:
            # Fill login form
            email_field = self.page.locator(
                'input[type="email"], '
                'input[name="email"], '
                'input[name="username"], '
                'input[placeholder*="mail"]'
            ).first
            email_field.fill(self.username, timeout=10000)

            password_field = self.page.locator(
                'input[type="password"], '
                'input[name="password"]'
            ).first
            password_field.fill(self.password, timeout=5000)

            submit_btn = self.page.locator(
                'button:has-text("SIGN IN"), '
                'button:has-text("Sign in"), '
                'button:has-text("LOG IN"), '
                'button:has-text("Log in"), '
                'button[type="submit"], '
                'input[type="submit"]'
            ).first
            submit_btn.click(timeout=5000)

            self.page.wait_for_load_state("networkidle", timeout=20000)
            time.sleep(5)

            # --- DISMISS PASSKEY POPUP ---
            # After login, a "Sign in faster next time" passkey modal appears.
            # Click "Not now" or "Don't ask again" to dismiss it.
            for dismiss_text in ["Not now", "Don't ask again", "No thanks", "Skip", "Close"]:
                try:
                    dismiss_btn = self.page.locator(f'text="{dismiss_text}"').first
                    dismiss_btn.click(timeout=3000)
                    log.info("Dismissed passkey popup: clicked '%s'", dismiss_text)
                    time.sleep(2)
                    break
                except Exception:
                    continue

            self.page.screenshot(path="/app/screenshots/02_after_login.png", full_page=True)
            log.info("Login complete. URL: %s", self.page.url)

        except Exception as e:
            self.page.screenshot(path="/app/screenshots/02_login_error.png", full_page=True)
            log.error("Login failed. Screenshot saved. Error: %s", e)
            raise

    def lookup_serial(self, serial: str) -> dict:
        """
        Search for a serial number and extract IMEI + ICCID from search results.

        The GPX search results page shows device cards like:
          274341
          ID: 337927  Serial: 274341  IMEI: 862601768000477  ICCID: 890117...

        So we extract directly from search results — no need to click into each device.
        """
        result = {"imei": "", "iccid": "", "sim_provider": ""}

        try:
            log.info("Looking up serial: %s", serial)

            # --- SEARCH ---
            search_input = self.page.locator(
                'input[placeholder*="IMEI or Serial"], '
                'input[placeholder*="Type Name"], '
                'input[placeholder*="Serial number"], '
                'input[placeholder*="IMEI"]'
            ).first

            search_input.click(timeout=5000)
            search_input.click(click_count=3)
            time.sleep(0.3)
            search_input.fill(serial)
            search_input.press("Enter")

            self.page.wait_for_load_state("networkidle", timeout=15000)
            time.sleep(3)

            # --- EXTRACT from search results text ---
            page_text = self.page.inner_text("body", timeout=5000)

            # Primary: find "Serial: XXXXXX" with IMEI and ICCID nearby
            pattern = (
                rf'Serial:\s*{re.escape(serial)}\s+'
                rf'IMEI:\s*(\d{{14,15}})\s+'
                rf'ICCID:\s*(\d{{19,22}})'
            )
            match = re.search(pattern, page_text)

            if match:
                result["imei"] = match.group(1)
                result["iccid"] = match.group(2)
                log.info("  Serial %s → IMEI: %s | ICCID: %s",
                         serial, result["imei"], result["iccid"])
            else:
                # Fallback: find Serial, then look for IMEI/ICCID nearby
                serial_pattern = rf'Serial:\s*{re.escape(serial)}'
                serial_match = re.search(serial_pattern, page_text)

                if serial_match:
                    nearby = page_text[serial_match.start():serial_match.start() + 300]
                    imei_m = re.search(r'IMEI:\s*(\d{14,15})', nearby)
                    iccid_m = re.search(r'ICCID:\s*(\d{19,22})', nearby)
                    if imei_m:
                        result["imei"] = imei_m.group(1)
                    if iccid_m:
                        result["iccid"] = iccid_m.group(1)

                    if result["imei"] or result["iccid"]:
                        log.info("  Serial %s → IMEI: %s | ICCID: %s (fallback)",
                                 serial, result["imei"] or "—", result["iccid"] or "—")
                    else:
                        self.page.screenshot(path=f"/app/screenshots/gpx_search_{serial}.png",
                                             full_page=True)
                        log.warning("  Serial %s: found but no IMEI/ICCID. Screenshot saved.", serial)
                else:
                    self.page.screenshot(path=f"/app/screenshots/gpx_search_{serial}.png",
                                         full_page=True)
                    log.warning("  Serial %s: not found in results. Screenshot saved.", serial)

            # Clear the search for the next lookup
            try:
                close_btn = self.page.locator('[aria-label="Close"], button:has-text("×")').first
                close_btn.click(timeout=2000)
                time.sleep(1)
            except Exception:
                try:
                    search_input = self.page.locator(
                        'input[placeholder*="IMEI or Serial"], '
                        'input[placeholder*="Type Name"]'
                    ).first
                    search_input.click(click_count=3)
                    search_input.press("Backspace")
                    time.sleep(1)
                except Exception:
                    pass

        except Exception as e:
            self.page.screenshot(path=f"/app/screenshots/gpx_error_{serial}.png")
            log.error("  Error looking up serial %s: %s", serial, e)
            try:
                self.page.goto(GPX_ADMIN_URL, wait_until="networkidle", timeout=15000)
                time.sleep(2)
            except Exception:
                pass

        return result

    def upload_csv(self, csv_path: str):
        """
        Upload a CSV to GPX portal to update retailer info.

        Flow (from screenshots):
          1. Navigate to Devices page
          2. Click "Actions" button (top right)
          3. Click "Update Retailer" from dropdown
          4. Upload CSV to the drag-and-drop area
          5. Click "Verify Data"
          6. Wait for green checkmark
          7. Click "Update Devices"
        """
        log.info("Uploading CSV to GPX portal: %s", csv_path)

        try:
            # Step 1: Go to Devices page
            self.page.locator('text=Devices').first.click(timeout=5000)
            self.page.wait_for_load_state("networkidle", timeout=10000)
            time.sleep(2)

            # Step 2: Click "Actions" button
            self.page.locator(
                'button:has-text("Actions"), '
                'text="Actions"'
            ).first.click(timeout=5000)
            time.sleep(1)

            # Step 3: Click "Update Retailer" from the dropdown
            self.page.locator(
                'text="Update Retailer"'
            ).first.click(timeout=5000)
            self.page.wait_for_load_state("networkidle", timeout=10000)
            time.sleep(2)

            self.page.screenshot(path="/app/screenshots/04_upload_dialog.png")
            log.info("Upload dialog opened.")

            # Step 4: Upload CSV file to the file input
            # The drag-and-drop area has a hidden file input we can set directly
            file_input = self.page.locator('input[type="file"]').first
            file_input.set_input_files(csv_path, timeout=5000)
            time.sleep(3)

            self.page.screenshot(path="/app/screenshots/05_file_uploaded.png")
            log.info("CSV file selected.")

            # Step 5: Click "Verify Data"
            self.page.locator(
                'button:has-text("Verify Data"), '
                'text="Verify Data"'
            ).first.click(timeout=5000)

            # Wait for verification to complete (green checkmark)
            time.sleep(5)
            self.page.wait_for_load_state("networkidle", timeout=30000)
            time.sleep(3)

            self.page.screenshot(path="/app/screenshots/06_verified.png")
            log.info("Data verified.")

            # Step 6: Scroll down and click "Update Devices"
            update_btn = self.page.locator(
                'button:has-text("Update Devices"), '
                'text="Update Devices"'
            ).first
            update_btn.scroll_into_view_if_needed(timeout=5000)
            time.sleep(1)
            update_btn.click(timeout=5000)

            # Wait for the update to complete
            time.sleep(5)
            self.page.wait_for_load_state("networkidle", timeout=30000)
            time.sleep(2)

            self.page.screenshot(path="/app/screenshots/07_updated.png")
            log.info("Devices updated successfully on GPX portal.")

        except Exception as e:
            self.page.screenshot(path="/app/screenshots/gpx_upload_error.png", full_page=True)
            log.error("GPX CSV upload failed. Screenshot saved. Error: %s", e)
            raise

    def close(self):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


def enrich_with_gpx(records: list) -> tuple[list, GPXScraper | None]:
    """
    Look up each serial on GPX portal and fill in IMEI, ICCID, SIM Provider.
    Returns the enriched records AND the scraper (still logged in) for CSV upload.
    """
    to_lookup = [r for r in records if r["serial"] != "NOT FOUND"]

    if not to_lookup:
        log.warning("No serial numbers to look up.")
        return records, None

    log.info("Looking up %d serials on GPX portal...", len(to_lookup))
    scraper = GPXScraper(GPX_USERNAME, GPX_PASSWORD, HEADLESS)

    try:
        scraper.start()
        for record in to_lookup:
            gpx_data = scraper.lookup_serial(record["serial"])
            record["imei"] = gpx_data["imei"]
            record["iccid"] = gpx_data["iccid"]
            record["sim_provider"] = gpx_data["sim_provider"]
            record["retailer"] = "shopify"  # All from Shopify channel
            time.sleep(1)  # Polite delay between lookups
    except Exception as e:
        log.error("GPX lookup failed: %s", e)
        scraper.close()
        return records, None

    # Return scraper still open for the upload step
    return records, scraper


# ===========================================================================
# STEP 4: Google Sheets & Drive
# ===========================================================================

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
]

# Sheet columns matching your screenshot:
# A: SKU | B: (empty) | C: Serial | D: IMEI | E: ICCID | F: (empty) | G: SIM Provider | H: Retailer | I: Status
SHEET_HEADERS = ["SKU", "", "Serial", "IMEI", "ICCID", "", "SIM Provider", "Retailer", "Status"]


def get_google_services():
    """Authenticate with Google using OAuth token and return Drive service."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_file = "token.json"
    if not os.path.exists(token_file):
        log.error("token.json not found. Run 'python auth_setup.py' on your local machine first.")
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    # Refresh the token if expired
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Save refreshed token
        with open(token_file, "w") as f:
            json.dump({
                "token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": list(creds.scopes),
            }, f, indent=2)

    drive = build("drive", "v3", credentials=creds)
    return drive


def format_sheet_date(date_str: str) -> str:
    """
    Convert YYYY-MM-DD to the sheet name format: M.DD.YY
    e.g., 2026-02-26 → "2.26.26"
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{dt.month}.{dt.day:02d}.{dt.strftime('%y')}"


def create_and_populate_sheet(drive_svc, records: list, date_str: str) -> str:
    """
    Create a CSV file and upload it to the Google Drive folder.

    The CSV can be opened as a Google Sheet directly from Drive.
    We upload as CSV because the service account has no Drive storage,
    and Google Sheets conversion counts against the creator's quota.
    CSV files in shared folders count against the folder owner's quota instead.

    Returns the file ID.
    """
    sheet_date = format_sheet_date(date_str)
    title = f"Shopify Shipment - {sheet_date}"
    log.info("Creating spreadsheet: %s", title)

    # Build CSV content
    csv_buffer = StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(SHEET_HEADERS)
    for r in records:
        writer.writerow([
            "", "", r["serial"], r["imei"], r["iccid"],
            "", r["sim_provider"], r["retailer"], r["status"],
        ])
    csv_content = csv_buffer.getvalue().encode("utf-8")

    # Write to temp file
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".csv", delete=False) as f:
        f.write(csv_content)
        temp_path = f.name

    try:
        from googleapiclient.http import MediaFileUpload

        file_metadata = {
            "name": f"{title}.csv",
            "parents": [GOOGLE_DRIVE_FOLDER_ID],
        }

        media = MediaFileUpload(temp_path, mimetype="text/csv")
        uploaded = drive_svc.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, name, webViewLink",
        ).execute()

        file_id = uploaded["id"]
        log.info("CSV uploaded to Drive: %s", uploaded.get("name"))
        log.info("View at: %s", uploaded.get("webViewLink", "N/A"))

    finally:
        os.unlink(temp_path)

    log.info("Sheet populated with %d data rows.", len(records))
    return file_id


# ===========================================================================
# MAIN ORCHESTRATOR
# ===========================================================================

def run(target_date: str = None, dry_run: bool = False):
    """Run the full automation pipeline."""
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")

    log.info("=" * 60)
    log.info("AUTOMATION START — Date: %s", target_date)
    log.info("=" * 60)

    # --- Step 1: Pull shipped orders from ShipStation ---
    log.info("STEP 1: Pulling shipped orders from ShipStation...")
    ss = ShipStationClient(SHIPSTATION_API_KEY, SHIPSTATION_API_SECRET)
    orders = ss.get_shipped_orders(target_date, SHIPSTATION_STORE_ID)

    if not orders:
        log.warning("No shipped orders found for %s. Done.", target_date)
        return
    log.info("Found %d shipped orders for %s.", len(orders), target_date)

    # --- Step 2: Extract serial numbers ---
    log.info("STEP 2: Extracting serial numbers from internal notes...")
    records = parse_orders(orders)
    if not records:
        log.warning("No records to process. Done.")
        return

    serials_found = sum(1 for r in records if r["serial"] != "NOT FOUND")
    log.info("Extracted %d serial numbers (%d orders had no serial).",
             serials_found, len(records) - serials_found)

    # --- Step 3: Look up IMEI/ICCID on GPX ---
    log.info("STEP 3: Looking up serials on GPX admin portal...")
    records, gpx_scraper = enrich_with_gpx(records)

    enriched = sum(1 for r in records if r["imei"])
    log.info("GPX enrichment complete: %d/%d serials got IMEI data.", enriched, serials_found)

    # --- Build the CSV file (used for both Drive upload and GPX upload) ---
    csv_buffer = StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(SHEET_HEADERS)
    for r in records:
        writer.writerow([
            "", "", r["serial"], r["imei"], r["iccid"],
            "", r["sim_provider"], r["retailer"], r["status"],
        ])

    csv_path = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, prefix="shopify_shipment_"
    ).name
    with open(csv_path, "w", newline="") as f:
        f.write(csv_buffer.getvalue())

    # --- Step 4: Create Google Sheet ---
    if dry_run:
        log.info("DRY RUN — skipping Google Drive and GPX uploads.")
        local_csv = f"{target_date}_dry_run.csv"
        shutil.copy(csv_path, local_csv)
        log.info("Saved dry-run CSV: %s", local_csv)
        for r in records:
            log.info("  %s → IMEI: %s, ICCID: %s", r["serial"], r["imei"] or "—", r["iccid"] or "—")
        if gpx_scraper:
            gpx_scraper.close()
        os.unlink(csv_path)
        return

    try:
        log.info("STEP 4: Uploading CSV to Google Drive...")
        drive_svc = get_google_services()
        sheet_id = create_and_populate_sheet(drive_svc, records, target_date)

        # --- Step 5: Upload CSV back to GPX to update retailer ---
        log.info("STEP 5: Uploading CSV to GPX portal to update retailer...")
        if gpx_scraper:
            try:
                gpx_scraper.upload_csv(csv_path)
            except Exception as e:
                log.error("GPX upload failed (non-fatal): %s", e)
                log.error("You can manually upload the CSV from Google Drive.")
        else:
            log.warning("GPX scraper not available — skipping retailer update.")
            log.warning("You can manually upload the CSV at admin.gpx.co → Devices → Actions → Update Retailer")

    finally:
        if gpx_scraper:
            gpx_scraper.close()
        os.unlink(csv_path)

    log.info("=" * 60)
    log.info("DONE! All steps complete.")
    log.info("=" * 60)


def list_stores():
    """List ShipStation stores to find the Shopify Marketing Experiment store ID."""
    ss = ShipStationClient(SHIPSTATION_API_KEY, SHIPSTATION_API_SECRET)
    stores = ss.list_stores()
    print("\nShipStation Stores:")
    print("-" * 70)
    for s in stores:
        marker = " ◄◄◄" if "shopify" in s.get("storeName", "").lower() else ""
        print(f"  ID: {s['storeId']:>6}  |  {s['storeName']:<35}  |  {s.get('marketplaceName', 'N/A')}{marker}")
    print("-" * 70)
    print("Set SHIPSTATION_STORE_ID in .env to the Shopify store ID above.")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ShipStation → GPX → Google Sheets Automation")
    parser.add_argument("--list-stores", action="store_true",
                        help="List ShipStation stores")
    parser.add_argument("--date", type=str, default=None,
                        help="Date to pull orders for (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip Google upload, save local CSV")
    args = parser.parse_args()

    if args.list_stores:
        # Only need API key/secret to list stores
        if not SHIPSTATION_API_KEY or not SHIPSTATION_API_SECRET:
            log.error("Missing SHIPSTATION_API_KEY or SHIPSTATION_API_SECRET in .env")
            sys.exit(1)
        list_stores()
    else:
        validate_env()
        run(target_date=args.date, dry_run=args.dry_run)
