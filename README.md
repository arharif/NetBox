#!/usr/bin/env bash

set -Eeuo pipefail

# ============================================================
# NetBox Docker PostgreSQL Snapshot Script
# Compatible with netbox-community/netbox-docker 3.4.1
#
# Tested logic for services:
#   - netbox
#   - netbox-worker
#   - postgres
#   - redis
#   - redis-cache
#
# This script:
#   1. Checks that you are inside the NetBox Docker repo
#   2. Checks Docker Compose services
#   3. Creates a PostgreSQL dump using pg_dump
#   4. Verifies the dump using pg_restore -l
#   5. Generates SHA256 checksum
#   6. Saves metadata
#   7. Backs up config/env files
#   8. Backs up media/reports/scripts volumes from the NetBox container
# ============================================================

# -----------------------------
# User variables
# -----------------------------

BACKUP_ROOT="${BACKUP_ROOT:-$HOME/netbox-backups}"
PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"

COMPOSE_CMD="${COMPOSE_CMD:-docker compose}"

POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
NETBOX_SERVICE="${NETBOX_SERVICE:-netbox}"

BACKUP_CONFIG="${BACKUP_CONFIG:-yes}"
BACKUP_MEDIA="${BACKUP_MEDIA:-yes}"
BACKUP_REPORTS="${BACKUP_REPORTS:-yes}"
BACKUP_SCRIPTS="${BACKUP_SCRIPTS:-yes}"

CLEANUP_OLD_BACKUPS="${CLEANUP_OLD_BACKUPS:-no}"
KEEP_DAYS="${KEEP_DAYS:-30}"

TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${BACKUP_ROOT}/${TS}"

DB_BACKUP_FILE="${BACKUP_DIR}/netbox_postgres_${TS}.dump"
DB_OBJECT_LIST="${BACKUP_DIR}/netbox_postgres_${TS}_objects.list"
DB_METADATA_FILE="${BACKUP_DIR}/netbox_postgres_${TS}_metadata.txt"
CHECKSUM_FILE="${DB_BACKUP_FILE}.sha256"

CONFIG_BACKUP_FILE="${BACKUP_DIR}/netbox_docker_config_${TS}.tar.gz"
MEDIA_BACKUP_FILE="${BACKUP_DIR}/netbox_media_${TS}.tar.gz"
REPORTS_BACKUP_FILE="${BACKUP_DIR}/netbox_reports_${TS}.tar.gz"
SCRIPTS_BACKUP_FILE="${BACKUP_DIR}/netbox_scripts_${TS}.tar.gz"

# -----------------------------
# Print functions
# -----------------------------

print_header() {
  echo
  echo "============================================================"
  echo "$1"
  echo "============================================================"
}

print_step() {
  echo
  echo "[STEP] $1"
}

print_info() {
  echo "[INFO] $1"
}

print_ok() {
  echo "[OK] $1"
}

print_warn() {
  echo "[WARNING] $1"
}

print_error() {
  echo "[ERROR] $1"
}

die() {
  print_error "$1"
  exit 1
}

trap 'print_error "Script failed at line $LINENO. Please review the error above."' ERR

# -----------------------------
# Start
# -----------------------------

print_header "NetBox Docker Snapshot Started"

print_info "Project directory : ${PROJECT_DIR}"
print_info "Backup root       : ${BACKUP_ROOT}"
print_info "Backup directory  : ${BACKUP_DIR}"
print_info "Compose command   : ${COMPOSE_CMD}"
print_info "Postgres service  : ${POSTGRES_SERVICE}"
print_info "NetBox service    : ${NETBOX_SERVICE}"

# -----------------------------
# Step 1: Validate current directory
# -----------------------------

print_step "Checking NetBox Docker repository structure"

cd "${PROJECT_DIR}"

if [[ ! -f "docker-compose.yml" ]]; then
  die "docker-compose.yml not found. Please run this script from the netbox-docker directory."
fi

if [[ ! -d "env" ]]; then
  die "env directory not found. This does not look like the official netbox-docker repo."
fi

if [[ ! -f "env/postgres.env" ]]; then
  die "env/postgres.env not found. Cannot safely detect PostgreSQL settings."
fi

if [[ ! -f "env/netbox.env" ]]; then
  die "env/netbox.env not found. Cannot safely detect NetBox settings."
fi

print_ok "NetBox Docker repository structure found"

# -----------------------------
# Step 2: Check Docker and Docker Compose
# -----------------------------

print_step "Checking Docker availability"

if ! command -v docker >/dev/null 2>&1; then
  die "Docker is not installed or not available in PATH."
fi

print_ok "Docker command is available"

print_step "Checking Docker Compose services"

SERVICES="$(${COMPOSE_CMD} config --services)"

echo "${SERVICES}" | grep -qx "${POSTGRES_SERVICE}" \
  || die "Service '${POSTGRES_SERVICE}' not found in docker compose config."

echo "${SERVICES}" | grep -qx "${NETBOX_SERVICE}" \
  || die "Service '${NETBOX_SERVICE}' not found in docker compose config."

print_ok "Required services found: ${POSTGRES_SERVICE}, ${NETBOX_SERVICE}"

# -----------------------------
# Step 3: Check running containers
# -----------------------------

print_step "Checking service status"

${COMPOSE_CMD} ps

print_info "Checking if PostgreSQL service is running"

if ! ${COMPOSE_CMD} ps --status running --services | grep -qx "${POSTGRES_SERVICE}"; then
  die "PostgreSQL service '${POSTGRES_SERVICE}' is not running."
fi

print_ok "PostgreSQL service is running"

print_info "Checking if NetBox service is running"

if ${COMPOSE_CMD} ps --status running --services | grep -qx "${NETBOX_SERVICE}"; then
  print_ok "NetBox service is running"
else
  print_warn "NetBox service is not running. Database backup can continue, but file-volume backups may fail."
fi

# -----------------------------
# Step 4: Create backup directory
# -----------------------------

print_step "Creating backup directory"

mkdir -p "${BACKUP_DIR}"

print_ok "Backup directory created: ${BACKUP_DIR}"

# -----------------------------
# Step 5: Show detected PostgreSQL environment
# -----------------------------

print_step "Reading PostgreSQL environment from the running postgres service"

${COMPOSE_CMD} exec -T "${POSTGRES_SERVICE}" sh -c '
  echo "POSTGRES_DB=${POSTGRES_DB}"
  echo "POSTGRES_USER=${POSTGRES_USER}"
  echo "PostgreSQL version:"
  postgres --version
'

print_ok "PostgreSQL environment displayed"

# -----------------------------
# Step 6: Test database readiness
# -----------------------------

print_step "Testing PostgreSQL readiness with pg_isready"

${COMPOSE_CMD} exec -T "${POSTGRES_SERVICE}" sh -c '
  pg_isready -q -d "$POSTGRES_DB" -U "$POSTGRES_USER"
'

print_ok "PostgreSQL is ready"

# -----------------------------
# Step 7: Save database metadata
# -----------------------------

print_step "Saving PostgreSQL metadata"

{
  echo "NetBox PostgreSQL Backup Metadata"
  echo "Generated at: $(date)"
  echo
  echo "Docker Compose project directory:"
  echo "${PROJECT_DIR}"
  echo
  echo "Docker Compose services:"
  echo "${SERVICES}"
  echo
  echo "PostgreSQL metadata:"
  ${COMPOSE_CMD} exec -T "${POSTGRES_SERVICE}" sh -c '
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
      SELECT
        current_database() AS database_name,
        pg_size_pretty(pg_database_size(current_database())) AS database_size,
        now() AS backup_time,
        version() AS postgresql_version;
    "
  '
} > "${DB_METADATA_FILE}"

print_ok "Metadata saved to: ${DB_METADATA_FILE}"

# -----------------------------
# Step 8: Create PostgreSQL dump
# -----------------------------

print_step "Creating PostgreSQL dump using pg_dump"

print_info "This will create a consistent PostgreSQL snapshot."
print_info "NetBox can remain online during this dump."
print_info "Using custom format: -Fc"
print_info "Important: using docker compose exec -T to avoid corrupting binary output."

${COMPOSE_CMD} exec -T "${POSTGRES_SERVICE}" sh -c '
  pg_dump \
    -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" \
    -Fc \
    --no-owner \
    --no-acl
' > "${DB_BACKUP_FILE}"

print_ok "PostgreSQL dump completed"

# -----------------------------
# Step 9: Validate dump file
# -----------------------------

print_step "Checking dump file size"

if [[ ! -s "${DB_BACKUP_FILE}" ]]; then
  die "Dump file is missing or empty: ${DB_BACKUP_FILE}"
fi

print_ok "Dump file exists"
print_info "Dump file: ${DB_BACKUP_FILE}"
print_info "Dump size: $(du -h "${DB_BACKUP_FILE}" | awk '{print $1}')"

# -----------------------------
# Step 10: Verify dump readability
# -----------------------------

print_step "Verifying dump with pg_restore -l"

${COMPOSE_CMD} exec -T "${POSTGRES_SERVICE}" sh -c '
  pg_restore -l
' < "${DB_BACKUP_FILE}" > "${DB_OBJECT_LIST}"

if [[ ! -s "${DB_OBJECT_LIST}" ]]; then
  die "pg_restore could not read the dump file."
fi

print_ok "Dump is readable by pg_restore"
print_info "Object list saved to: ${DB_OBJECT_LIST}"

print_info "Showing first 20 objects from the dump:"
head -20 "${DB_OBJECT_LIST}"

# -----------------------------
# Step 11: Generate checksum
# -----------------------------

print_step "Generating SHA256 checksum"

sha256sum "${DB_BACKUP_FILE}" > "${CHECKSUM_FILE}"

print_ok "Checksum generated"
print_info "Checksum file: ${CHECKSUM_FILE}"

cat "${CHECKSUM_FILE}"

# -----------------------------
# Step 12: Backup NetBox Docker configuration
# -----------------------------

if [[ "${BACKUP_CONFIG}" == "yes" ]]; then
  print_step "Backing up NetBox Docker configuration files"

  CONFIG_ITEMS=()

  for item in \
    docker-compose.yml \
    docker-compose.override.yml \
    docker-compose.override.yml.example \
    env \
    configuration
  do
    if [[ -e "${item}" ]]; then
      CONFIG_ITEMS+=("${item}")
    fi
  done

  if [[ "${#CONFIG_ITEMS[@]}" -eq 0 ]]; then
    print_warn "No configuration files found to back up."
  else
    tar -czf "${CONFIG_BACKUP_FILE}" "${CONFIG_ITEMS[@]}"
    print_ok "Configuration backup completed"
    print_info "Configuration backup file: ${CONFIG_BACKUP_FILE}"
    print_info "Configuration backup size: $(du -h "${CONFIG_BACKUP_FILE}" | awk '{print $1}')"
  fi
else
  print_info "Configuration backup disabled."
fi

# -----------------------------
# Step 13: Backup NetBox media files
# -----------------------------

if [[ "${BACKUP_MEDIA}" == "yes" ]]; then
  print_step "Backing up NetBox media files"

  if ${COMPOSE_CMD} ps --status running --services | grep -qx "${NETBOX_SERVICE}"; then
    if ${COMPOSE_CMD} exec -T "${NETBOX_SERVICE}" sh -c 'test -d /opt/netbox/netbox/media && command -v tar >/dev/null 2>&1'; then
      ${COMPOSE_CMD} exec -T "${NETBOX_SERVICE}" sh -c '
        tar -czf - -C /opt/netbox/netbox/media .
      ' > "${MEDIA_BACKUP_FILE}"

      if [[ -s "${MEDIA_BACKUP_FILE}" ]]; then
        print_ok "Media backup completed"
        print_info "Media backup file: ${MEDIA_BACKUP_FILE}"
        print_info "Media backup size: $(du -h "${MEDIA_BACKUP_FILE}" | awk '{print $1}')"
      else
        print_warn "Media backup file is empty. This may be normal if no media files exist."
      fi
    else
      print_warn "Media directory or tar command not available inside NetBox container."
    fi
  else
    print_warn "NetBox service is not running. Skipping media backup."
  fi
else
  print_info "Media backup disabled."
fi

# -----------------------------
# Step 14: Backup NetBox reports files
# -----------------------------

if [[ "${BACKUP_REPORTS}" == "yes" ]]; then
  print_step "Backing up NetBox reports files"

  if ${COMPOSE_CMD} ps --status running --services | grep -qx "${NETBOX_SERVICE}"; then
    if ${COMPOSE_CMD} exec -T "${NETBOX_SERVICE}" sh -c 'test -d /opt/netbox/netbox/reports && command -v tar >/dev/null 2>&1'; then
      ${COMPOSE_CMD} exec -T "${NETBOX_SERVICE}" sh -c '
        tar -czf - -C /opt/netbox/netbox/reports .
      ' > "${REPORTS_BACKUP_FILE}"

      if [[ -s "${REPORTS_BACKUP_FILE}" ]]; then
        print_ok "Reports backup completed"
        print_info "Reports backup file: ${REPORTS_BACKUP_FILE}"
        print_info "Reports backup size: $(du -h "${REPORTS_BACKUP_FILE}" | awk '{print $1}')"
      else
        print_warn "Reports backup file is empty. This may be normal if no reports exist."
      fi
    else
      print_warn "Reports directory or tar command not available inside NetBox container."
    fi
  else
    print_warn "NetBox service is not running. Skipping reports backup."
  fi
else
  print_info "Reports backup disabled."
fi

# -----------------------------
# Step 15: Backup NetBox scripts files
# -----------------------------

if [[ "${BACKUP_SCRIPTS}" == "yes" ]]; then
  print_step "Backing up NetBox scripts files"

  if ${COMPOSE_CMD} ps --status running --services | grep -qx "${NETBOX_SERVICE}"; then
    if ${COMPOSE_CMD} exec -T "${NETBOX_SERVICE}" sh -c 'test -d /opt/netbox/netbox/scripts && command -v tar >/dev/null 2>&1'; then
      ${COMPOSE_CMD} exec -T "${NETBOX_SERVICE}" sh -c '
        tar -czf - -C /opt/netbox/netbox/scripts .
      ' > "${SCRIPTS_BACKUP_FILE}"

      if [[ -s "${SCRIPTS_BACKUP_FILE}" ]]; then
        print_ok "Scripts backup completed"
        print_info "Scripts backup file: ${SCRIPTS_BACKUP_FILE}"
        print_info "Scripts backup size: $(du -h "${SCRIPTS_BACKUP_FILE}" | awk '{print $1}')"
      else
        print_warn "Scripts backup file is empty. This may be normal if no custom scripts exist."
      fi
    else
      print_warn "Scripts directory or tar command not available inside NetBox container."
    fi
  else
    print_warn "NetBox service is not running. Skipping scripts backup."
  fi
else
  print_info "Scripts backup disabled."
fi

# -----------------------------
# Step 16: Optional cleanup
# -----------------------------

if [[ "${CLEANUP_OLD_BACKUPS}" == "yes" ]]; then
  print_step "Cleaning backups older than ${KEEP_DAYS} days"

  find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -mtime "+${KEEP_DAYS}" -print -exec rm -rf {} \;

  print_ok "Old backup cleanup completed"
else
  print_info "Old backup cleanup disabled."
fi

# -----------------------------
# Step 17: Final summary
# -----------------------------

print_header "Backup Completed Successfully"

print_info "Backup folder:"
echo "${BACKUP_DIR}"

print_info "PostgreSQL dump:"
echo "${DB_BACKUP_FILE}"

print_info "PostgreSQL metadata:"
echo "${DB_METADATA_FILE}"

print_info "PostgreSQL object list:"
echo "${DB_OBJECT_LIST}"

print_info "SHA256 checksum:"
echo "${CHECKSUM_FILE}"

if [[ -f "${CONFIG_BACKUP_FILE}" ]]; then
  print_info "Configuration backup:"
  echo "${CONFIG_BACKUP_FILE}"
fi

if [[ -f "${MEDIA_BACKUP_FILE}" ]]; then
  print_info "Media backup:"
  echo "${MEDIA_BACKUP_FILE}"
fi

if [[ -f "${REPORTS_BACKUP_FILE}" ]]; then
  print_info "Reports backup:"
  echo "${REPORTS_BACKUP_FILE}"
fi

if [[ -f "${SCRIPTS_BACKUP_FILE}" ]]; then
  print_info "Scripts backup:"
  echo "${SCRIPTS_BACKUP_FILE}"
fi

print_header "Recommended Off-Server Copy"

echo "Copy this full backup directory to another server, NAS, SFTP, or object storage:"
echo
echo "scp -r ${BACKUP_DIR} user@backup-server:/backup/netbox/"
echo

print_header "Restore Example - Documentation Only"

echo "Use this only during a restore operation."
echo "This will replace the current NetBox database."
echo
echo "1. Stop NetBox application containers:"
echo "   ${COMPOSE_CMD} stop netbox netbox-worker"
echo
echo "2. Drop and recreate the database:"
echo "   ${COMPOSE_CMD} exec -T postgres sh -c 'dropdb -U \"\$POSTGRES_USER\" --if-exists \"\$POSTGRES_DB\"'"
echo "   ${COMPOSE_CMD} exec -T postgres sh -c 'createdb -U \"\$POSTGRES_USER\" \"\$POSTGRES_DB\"'"
echo
echo "3. Restore the dump:"
echo "   ${COMPOSE_CMD} exec -T postgres sh -c 'pg_restore -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\"' < ${DB_BACKUP_FILE}"
echo
echo "4. Restart NetBox:"
echo "   ${COMPOSE_CMD} up -d"
echo

print_ok "Script finished successfully."
