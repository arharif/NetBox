#!/usr/bin/env bash

set -Eeuo pipefail

echo "============================================================"
echo "NetBox PostgreSQL Dump Cross-Check Verification"
echo "============================================================"

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

POSTGRES_CONTAINER="netbox-docker-postgres-1"
DB_NAME="netbox"
DB_USER="netbox"

DUMP_FILE="${1:-}"

VERIFY_DB="netbox_verify_$(date +%Y%m%d_%H%M%S)"
WORK_DIR="/tmp/netbox_dump_verify_${VERIFY_DB}"

LIVE_RESULT_FILE="${WORK_DIR}/live_database_check.tsv"
RESTORED_RESULT_FILE="${WORK_DIR}/restored_database_check.tsv"
DIFF_FILE="${WORK_DIR}/database_diff.txt"

KEEP_VERIFY_DB="${KEEP_VERIFY_DB:-no}"

# ------------------------------------------------------------
# Print helpers
# ------------------------------------------------------------

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

cleanup() {
  echo
  echo "[CLEANUP] Cleaning verification resources..."

  if [[ "${KEEP_VERIFY_DB}" == "yes" ]]; then
    print_warn "KEEP_VERIFY_DB=yes, temporary database will not be dropped: ${VERIFY_DB}"
  else
    docker exec "${POSTGRES_CONTAINER}" dropdb -U "${DB_USER}" --if-exists "${VERIFY_DB}" >/dev/null 2>&1 || true
    print_ok "Temporary verification database dropped if it existed: ${VERIFY_DB}"
  fi

  print_info "Temporary files are kept here for review:"
  echo "${WORK_DIR}"
}

trap cleanup EXIT
trap 'print_error "Script failed at line $LINENO."' ERR

# ------------------------------------------------------------
# Step 1: Validate input
# ------------------------------------------------------------

print_step "Checking input dump file"

if [[ -z "${DUMP_FILE}" ]]; then
  print_error "No dump file provided."
  echo
  echo "Usage:"
  echo "  ./verify_netbox_dump_crosscheck.sh /home/netbox_admin/netbox-backups/netbox_postgres_YYYYMMDD_HHMMSS.dump"
  echo
  exit 1
fi

if [[ ! -f "${DUMP_FILE}" ]]; then
  print_error "Dump file does not exist:"
  echo "${DUMP_FILE}"
  exit 1
fi

if [[ ! -s "${DUMP_FILE}" ]]; then
  print_error "Dump file is empty:"
  echo "${DUMP_FILE}"
  exit 1
fi

print_ok "Dump file found"
print_info "Dump file: ${DUMP_FILE}"
print_info "Dump size:"
du -h "${DUMP_FILE}"

# ------------------------------------------------------------
# Step 2: Check Docker container
# ------------------------------------------------------------

print_step "Checking PostgreSQL Docker container"

if ! docker ps --format '{{.Names}}' | grep -q "^${POSTGRES_CONTAINER}$"; then
  print_error "PostgreSQL container is not running: ${POSTGRES_CONTAINER}"
  echo "Check with:"
  echo "  docker ps"
  exit 1
fi

print_ok "PostgreSQL container is running: ${POSTGRES_CONTAINER}"

# ------------------------------------------------------------
# Step 3: Check PostgreSQL connectivity
# ------------------------------------------------------------

print_step "Checking live PostgreSQL database connectivity"

docker exec "${POSTGRES_CONTAINER}" \
  pg_isready -U "${DB_USER}" -d "${DB_NAME}"

print_ok "Live database is reachable"

# ------------------------------------------------------------
# Step 4: Show live database information
# ------------------------------------------------------------

print_step "Showing live NetBox database information"

docker exec "${POSTGRES_CONTAINER}" \
  psql -U "${DB_USER}" -d "${DB_NAME}" -c "
SELECT
  current_database() AS database_name,
  pg_size_pretty(pg_database_size(current_database())) AS database_size,
  now() AS check_time,
  version() AS postgresql_version;
"

# ------------------------------------------------------------
# Step 5: Verify dump structure with pg_restore -l
# ------------------------------------------------------------

print_step "Checking if dump is readable with pg_restore -l"

docker exec -i "${POSTGRES_CONTAINER}" \
  pg_restore -l \
  < "${DUMP_FILE}" \
  > /dev/null

print_ok "Dump file is readable by pg_restore"

# ------------------------------------------------------------
# Step 6: Create working directory
# ------------------------------------------------------------

print_step "Creating local working directory"

mkdir -p "${WORK_DIR}"

print_ok "Working directory created:"
echo "${WORK_DIR}"

# ------------------------------------------------------------
# Step 7: Create temporary verification database
# ------------------------------------------------------------

print_step "Creating temporary verification database"

if [[ "${VERIFY_DB}" == "${DB_NAME}" ]]; then
  print_error "Safety check failed: verification DB name equals production DB name."
  exit 1
fi

docker exec "${POSTGRES_CONTAINER}" \
  dropdb -U "${DB_USER}" --if-exists "${VERIFY_DB}"

docker exec "${POSTGRES_CONTAINER}" \
  createdb -U "${DB_USER}" "${VERIFY_DB}"

print_ok "Temporary database created: ${VERIFY_DB}"

# ------------------------------------------------------------
# Step 8: Restore dump into temporary database
# ------------------------------------------------------------

print_step "Restoring dump into temporary verification database"

print_info "This does not touch the real NetBox database."
print_info "Restoring into: ${VERIFY_DB}"

docker exec -i "${POSTGRES_CONTAINER}" \
  pg_restore \
    -U "${DB_USER}" \
    -d "${VERIFY_DB}" \
    --no-owner \
    --no-acl \
    --exit-on-error \
  < "${DUMP_FILE}"

print_ok "Dump restored successfully into temporary database"

# ------------------------------------------------------------
# Function: generate database fingerprint
# ------------------------------------------------------------

generate_fingerprint() {
  local database_name="$1"
  local output_file="$2"

  docker exec "${POSTGRES_CONTAINER}" \
    psql -U "${DB_USER}" -d "${database_name}" -v ON_ERROR_STOP=1 -q -c "
CREATE TEMP TABLE table_fingerprint (
  schema_name text,
  table_name text,
  row_count bigint,
  content_checksum text
);

DO \$\$
DECLARE
  r record;
  v_count bigint;
  v_checksum text;
BEGIN
  FOR r IN
    SELECT
      n.nspname AS schema_name,
      c.relname AS table_name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%'
    ORDER BY n.nspname, c.relname
  LOOP
    EXECUTE format('SELECT count(*) FROM %I.%I', r.schema_name, r.table_name)
      INTO v_count;

    EXECUTE format(
      'SELECT COALESCE(md5(string_agg(row_hash, '''' ORDER BY row_hash)), md5(''''))
       FROM (
         SELECT md5(row_to_json(t)::text) AS row_hash
         FROM %I.%I AS t
       ) s',
      r.schema_name,
      r.table_name
    )
      INTO v_checksum;

    INSERT INTO table_fingerprint
    VALUES (r.schema_name, r.table_name, v_count, v_checksum);
  END LOOP;
END
\$\$;

COPY (
  SELECT
    schema_name || '.' || table_name AS table_name,
    row_count,
    content_checksum
  FROM table_fingerprint
  ORDER BY schema_name, table_name
) TO STDOUT WITH DELIMITER E'\t';
" > "${output_file}"
}

# ------------------------------------------------------------
# Step 9: Generate fingerprint of live database
# ------------------------------------------------------------

print_step "Generating fingerprint of the real live NetBox database"

print_info "This checks table list, row counts, and content checksum per table."
print_info "Database: ${DB_NAME}"

generate_fingerprint "${DB_NAME}" "${LIVE_RESULT_FILE}"

print_ok "Live database fingerprint generated:"
echo "${LIVE_RESULT_FILE}"

# ------------------------------------------------------------
# Step 10: Generate fingerprint of restored database
# ------------------------------------------------------------

print_step "Generating fingerprint of the restored dump database"

print_info "Database: ${VERIFY_DB}"

generate_fingerprint "${VERIFY_DB}" "${RESTORED_RESULT_FILE}"

print_ok "Restored database fingerprint generated:"
echo "${RESTORED_RESULT_FILE}"

# ------------------------------------------------------------
# Step 11: Compare live database with restored database
# ------------------------------------------------------------

print_step "Comparing live database with restored dump database"

if diff -u "${LIVE_RESULT_FILE}" "${RESTORED_RESULT_FILE}" > "${DIFF_FILE}"; then
  print_ok "Cross-check successful."
  print_ok "The restored dump matches the live database based on table list, row counts, and per-table checksums."
else
  print_warn "Cross-check found differences."
  print_warn "This can be normal if the live NetBox database changed after the dump was created."
  print_warn "Review the diff file:"
  echo "${DIFF_FILE}"

  echo
  echo "First differences:"
  head -80 "${DIFF_FILE}"
fi

# ------------------------------------------------------------
# Step 12: Final summary
# ------------------------------------------------------------

echo
echo "============================================================"
echo "Verification Summary"
echo "============================================================"

echo "Live database:"
echo "  ${DB_NAME}"

echo
echo "Temporary restored database:"
echo "  ${VERIFY_DB}"

echo
echo "Dump file:"
echo "  ${DUMP_FILE}"

echo
echo "Live fingerprint:"
echo "  ${LIVE_RESULT_FILE}"

echo
echo "Restored fingerprint:"
echo "  ${RESTORED_RESULT_FILE}"

echo
echo "Diff file:"
echo "  ${DIFF_FILE}"

echo
echo "Important:"
echo "  If the dump was created earlier and NetBox changed since then,"
echo "  differences can appear even if the dump is valid."

echo
print_ok "Verification script completed."
