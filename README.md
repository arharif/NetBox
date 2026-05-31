cat <<'EOF' > scripts/test_script.py
from extras.scripts import Script


class TestScript(Script):

    class Meta:
        name = "Test Script"
        description = "Simple test script"

    def run(self, data, commit):
        self.log_success("The test script works.")
        return "OK"
EOF
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
