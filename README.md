from collections import defaultdict
from decimal import Decimal, InvalidOperation

from dcim.models import Device, Rack
from extras.scripts import Script


DEVICE_POWER_FIELD = "power_consumption_watts"

RACK_CONSUMED_FIELD = "rack_power_consumed_watts"

RACK_CAPACITY_FIELD = "rack_power_capacity_watts"

RACK_USAGE_PERCENT_FIELD = "rack_power_usage_percent"


class RackPowerConsumptionCalculation(Script):

    class Meta:
        name = "Rack Power Consumption Calculation"
        description = "Calculate electrical consumption for each rack."
        commit_default = False

    def get_device_power(self, device):
        custom_fields = device.custom_field_data or {}
        raw_power = custom_fields.get(DEVICE_POWER_FIELD)

        if raw_power in [None, ""]:
            return 0

        try:
            power = Decimal(str(raw_power))
        except (InvalidOperation, ValueError, TypeError):
            self.log_warning(
                f"Invalid power value '{raw_power}' on device {device.name}. Ignored.",
                obj=device
            )
            return 0

        if power < 0:
            self.log_warning(
                f"Negative power value '{raw_power}' on device {device.name}. Ignored.",
                obj=device
            )
            return 0

        return int(power)

    def run(self, data, commit):

        rack_consumption = defaultdict(int)
        rack_device_count = defaultdict(int)

        devices = Device.objects.filter(
            rack__isnull=False
        ).select_related("rack")

        for device in devices.iterator():
            power = self.get_device_power(device)
            rack_consumption[device.rack_id] += power
            rack_device_count[device.rack_id] += 1

        result_lines = []

        for rack in Rack.objects.all().iterator():

            consumed_power = rack_consumption.get(rack.id, 0)
            device_count = rack_device_count.get(rack.id, 0)

            rack.custom_field_data = rack.custom_field_data or {}

            raw_capacity = rack.custom_field_data.get(RACK_CAPACITY_FIELD)
            usage_percent = None

            if raw_capacity not in [None, "", 0, "0"]:
                try:
                    capacity = Decimal(str(raw_capacity))
                    if capacity > 0:
                        usage_percent = round(
                            Decimal(consumed_power) / capacity * 100,
                            2
                        )
                except (InvalidOperation, ValueError, TypeError):
                    self.log_warning(
                        f"Invalid rack capacity value '{raw_capacity}' on rack {rack.name}.",
                        obj=rack
                    )

            rack.custom_field_data[RACK_CONSUMED_FIELD] = consumed_power

            if usage_percent is not None:
                rack.custom_field_data[RACK_USAGE_PERCENT_FIELD] = float(usage_percent)

            message = (
                f"{rack.name}: {consumed_power} W consumed "
                f"across {device_count} device(s)"
            )

            if usage_percent is not None:
                message += f" | Usage: {usage_percent}%"

            if commit:
                rack.full_clean()
                rack.save()
                self.log_success(message, obj=rack)
            else:
                self.log_info("[DRY RUN] " + message, obj=rack)

            result_lines.append(message)

        return "\n".join(result_lines)



---













#!/usr/bin/env bash

set -e

echo "============================================================"
echo "Simple NetBox PostgreSQL Backup using Docker pg_dump"
echo "============================================================"

POSTGRES_CONTAINER="netbox-docker-postgres-1"
DB_NAME="netbox"
DB_USER="netbox"

BACKUP_DIR="/home/netbox_admin/netbox-backups"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/netbox_postgres_$DATE.dump"
CHECKSUM_FILE="$BACKUP_FILE.sha256"

echo "[INFO] PostgreSQL container: $POSTGRES_CONTAINER"
echo "[INFO] Database name       : $DB_NAME"
echo "[INFO] Database user       : $DB_USER"
echo "[INFO] Backup directory    : $BACKUP_DIR"

echo
echo "[STEP 1] Creating backup directory..."
mkdir -p "$BACKUP_DIR"
echo "[OK] Backup directory ready."

echo
echo "[STEP 2] Checking if PostgreSQL container is running..."
docker ps --format '{{.Names}}' | grep -q "^${POSTGRES_CONTAINER}$"
echo "[OK] PostgreSQL container is running."

echo
echo "[STEP 3] Creating PostgreSQL dump..."
echo "[INFO] NetBox can stay online during this backup."
echo "[INFO] Using pg_dump custom format: -Fc"

docker exec "$POSTGRES_CONTAINER" \
  pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc \
  > "$BACKUP_FILE"

echo "[OK] PostgreSQL backup completed."

echo
echo "[STEP 4] Checking backup file..."
if [ ! -s "$BACKUP_FILE" ]; then
  echo "[ERROR] Backup file is empty or was not created."
  exit 1
fi

echo "[OK] Backup file created:"
echo "$BACKUP_FILE"
echo "[INFO] Backup size:"
du -h "$BACKUP_FILE"

echo
echo "[STEP 5] Verifying backup readability..."
docker exec -i "$POSTGRES_CONTAINER" \
  pg_restore -l \
  < "$BACKUP_FILE" > /dev/null

echo "[OK] Backup is readable by pg_restore."

echo
echo "[STEP 6] Creating SHA256 checksum..."
sha256sum "$BACKUP_FILE" > "$CHECKSUM_FILE"

echo "[OK] Checksum created:"
echo "$CHECKSUM_FILE"

echo
echo "============================================================"
echo "Backup completed successfully"
echo "============================================================"
echo "Backup file:"
echo "$BACKUP_FILE"
echo
echo "Checksum file:"
echo "$CHECKSUM_FILE"
echo
echo "To copy it to another server, use:"
echo "scp $BACKUP_FILE user@backup-server:/backup/netbox/"
echo
echo "============================================================"
