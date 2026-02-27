FROM python:3.12-slim

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium + all its system dependencies
RUN playwright install chromium && playwright install-deps chromium

# Copy project files
COPY main.py .
COPY .env .
COPY token.json .

# Set up the cron job — runs at 3 PM daily
# The cron runs inside the container using the container's timezone
RUN echo "45 15 * * * cd /app && /usr/local/bin/python main.py >> /app/logs/automation.log 2>&1" > /etc/cron.d/shipstation-cron \
    && chmod 0644 /etc/cron.d/shipstation-cron \
    && crontab /etc/cron.d/shipstation-cron

# Create logs directory
RUN mkdir -p /app/logs

# Script that loads env vars into cron and starts it
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
