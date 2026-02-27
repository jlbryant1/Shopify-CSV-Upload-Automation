# ShipStation → GPX → Google Drive Automation

Automated daily pipeline that pulls shipped Shopify orders from ShipStation, looks up device data (IMEI/ICCID) on the GPX admin portal, and generates a CSV report in Google Drive.

## What It Does

1. **ShipStation** — Pulls all orders shipped today from a specific Shopify store using the ShipStation API
2. **Serial Extraction** — Parses internal notes on each order to extract 6-digit device serial numbers
3. **GPX Lookup** — Searches each serial on admin.gpx.co via browser automation (Playwright) and extracts the IMEI and ICCID directly from search results
4. **Google Drive** — Generates a CSV named `Shopify Shipment - M.DD.YY` and uploads it to a shared Google Drive folder

## Tech Stack

- **Python 3.12** — Core automation logic
- **Playwright** — Headless Chromium browser automation for GPX portal interaction
- **ShipStation REST API** — Order and shipment data retrieval
- **Google Drive API (OAuth2)** — CSV file upload to shared Drive folder
- **Docker + Cron** — Containerized daily scheduling

## Setup

### Prerequisites
- Docker and Docker Compose installed
- ShipStation API credentials (Settings → Account → API Settings)
- Google Cloud project with Drive API enabled and OAuth2 Desktop credentials
- GPX admin portal login credentials

### Configuration

1. Copy `.env.example` to `.env` and fill in your credentials:
   ```
   SHIPSTATION_API_KEY=your_key
   SHIPSTATION_API_SECRET=your_secret
   SHIPSTATION_STORE_ID=your_store_id
   GPX_USERNAME=your_email
   GPX_PASSWORD=your_password
   GOOGLE_DRIVE_FOLDER_ID=your_folder_id
   ```

2. Place your Google OAuth credentials file as `oauth_credentials.json` in the project root

3. Run the one-time OAuth setup to generate `token.json`:
   ```bash
   docker compose build
   docker compose run --rm -it --entrypoint python -v ${PWD}:/app shipstation-auto auth_setup.py
   ```

4. Start the container:
   ```bash
   docker compose up -d
   ```

### Usage

```bash
# Run manually for today
docker compose run --rm --entrypoint python shipstation-auto main.py

# Run for a specific date
docker compose run --rm --entrypoint python shipstation-auto main.py --date 2026-02-25

# Dry run (skips Google Drive and GPX uploads)
docker compose run --rm --entrypoint python shipstation-auto main.py --dry-run

# View logs
docker logs shipstation-auto

# Find your ShipStation store ID
docker compose run --rm --entrypoint python shipstation-auto main.py --list-stores
```

## Schedule

Runs daily at 3:45 PM (configurable in the Dockerfile cron entry). Timezone is set via the `TZ` environment variable in `docker-compose.yml`.
