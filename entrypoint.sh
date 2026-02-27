#!/bin/bash

# Export all environment variables so cron can access them
# (cron doesn't inherit env vars by default)
printenv | grep -v "no_proxy" >> /etc/environment

echo "============================================"
echo " ShipStation Automation Container Started"
echo "============================================"
echo " Timezone: $(cat /etc/timezone 2>/dev/null || echo 'UTC')"
echo " Current time: $(date)"
echo " Cron schedule: Daily at 3:00 PM"
echo " Logs: docker logs shipstation-auto"
echo "============================================"

# Start cron in the foreground (keeps container running)
cron -f
