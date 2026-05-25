#!/usr/bin/env bash

set -Eeuo pipefail

# ============================================================
# NetBox PostgreSQL Dump Cross-Check Verification Script
# ============================================================
#
# Purpose:
#   1. Verify that a PostgreSQL .dump file is readable.
#   2. Restore the dump into a temporary verification database.
#   3. Compare the restored database with the live NetBox database.
#   4. Generate a clear colored summary:
#        - MATCH
#        - REVIEW NEEDED
#
# Safety:
#   - This script DOES NOT modify the real NetBox database.
#   - It creates a temporary database, restores the dump there,
#     compares it, then drops the temporary database.
#
# Recommended:
#   Run this immediately after creating the dump for best accuracy.
#
# ============================================================

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-netbox-docker-postgres-1}"
DB_NAME="${DB_NAME:-netbox}"
DB_USER="${DB_USER:-netbox}"

BACKUP_DIR="${BACKUP_DIR:-/home/netbox_admin/netbox-backups}"
DUMP_FILE="${1:-}"

KEEP_VERIFY_DB="${KEEP_VERIFY_DB:-no}"

VERIFY_DB="netbox_verify_$(date +%Y%m%d_%H%M%S)"
WORK_DIR="/tmp/netbox_dump_verify_${VERIFY_DB}"

LIVE_RESULT_FILE="${WORK_DIR}/live_database_fingerprint.tsv"
RESTORED_RESULT_FILE="${WORK_DIR}/restored_database_fingerprint.tsv"
DIFF_FILE="${WORK_DIR}/database_full_diff.txt"

LIVE_TABLES_FILE="${WORK_DIR}/live_tables.txt"
RESTORED_TABLES_FILE="${WORK_DIR}/restored_tables.txt"
MATCHING_TABLES_FILE="${WORK_DIR}/matching_tables.txt"
ONLY_LIVE_FILE="${WORK_DIR}/only_in_live.txt"
ONLY_RESTORED_FILE="${WORK_DIR}/only_in_restored.txt"
ROWCOUNT_DIFF_FILE="${WORK_DIR}/rowcount_differences.txt"
CHECKSUM_DIFF_FILE="${WORK_DIR}/checksum_differences.txt"
SUMMARY_FILE="${WORK_DIR}/comparison_summary.txt"

# ------------------------------------------------------------
# Colors
# ------------------------------------------------------------

if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
  RED="$(tput setaf 1 || true)"
  GREEN="$(tput setaf 2 || true)"
  YELLOW="$(tput setaf 3 || true)"
  BLUE="$(tput setaf 4 || true)"
  MAGENTA="$(tput setaf 5 || true)"
  CYAN="$(tput setaf 6 || true)"
  BOLD="$(tput bold || true)"
  RESET="$(tput sgr0 || true)"
else
  RED=""
  GREEN=""
  YELLOW=""
  BLUE=""
  MAGENTA=""
  CYAN=""
  BOLD=""
  RESET=""
fi

# ------------------------------------------------------------
# Print helpers
# ------------------------------------------------------------

print_header() {
  echo
  echo "${BOLD}${CYAN}============================================================${RESET}"
  echo "${BOLD}${CYAN}$1${RESET}"
  echo "${BOLD}${CYAN}============================================================${RESET}"
}

print_step() {
  echo
  echo "${BOLD}${BLUE}[STEP]${RESET} $1"
}

print_info() {
  echo "${CYAN}[INFO]${RESET} $1"
}

print_ok() {
  echo "${GREEN}[OK]${RESET} $1"
}

print_warn() {
  echo "${YELLOW}[WARNING]${RESET} $1"
}

print_error() {
  echo "${RED}[ERROR]${RESET} $1"
}

print_match() {
  echo "${GREEN}${BOLD}$1${RESET}"
}

print_review() {
  echo "${YELLOW}${BOLD}$1${RESET}"
}

die() {
  print_error "$1"
  exit 1
}

# ------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------

cleanup() {
  echo
  print_step "Cleanup"

  if [[ "${KEEP_VERIFY_DB}" == "yes" ]]; then
    print_warn "KEEP_VERIFY_DB=yes, temporary database was kept:"
    echo "  ${VERIFY_DB}"
  else
    docker exec "${POSTGRES_CONTAINER}" \
      dropdb -U "${DB_USER}" --if-exists "${VERIFY_DB}" >/dev/null 2>&1 || true

    print_ok "Temporary verification database dropped if it existed:"
    echo "  ${VERIFY_DB}"
  fi

  print_info "Verification files are available here:"
  echo "  ${WORK_DIR}"
}

trap cleanup EXIT
trap 'print_error "Script failed at line $LINENO. Please review the error above."' ERR

# ------------------------------------------------------------
# Start
# ------------------------------------------------------------

print_header "NetBox PostgreSQL Dump Cross-Check Verification"

print_info "PostgreSQL container : ${POSTGRES_CONTAINER}"
print_info "Live database        : ${DB_NAME}"
print_info "Database user        : ${DB_USER}"
print_info "Backup directory     : ${BACKUP_DIR}"
print_info "Temporary database   : ${VERIFY_DB}"
print_info "Working directory    : ${WORK_DIR}"

# ------------------------------------------------------------
# Step 1: Detect dump file
# ------------------------------------------------------------

print_step "Detecting dump file"

if [[ -z "${DUMP_FILE}" ]]; then
  print_info "No dump file provided as argument."
  print_info "Trying to automatically select the latest dump from:"
  echo "  ${BACKUP_DIR}"

  DUMP_FILE="$(ls -t "${BACKUP_DIR}"/netbox_postgres_*.dump 2>/dev/null | head -1 || true)"

  if [[ -z "${DUMP_FILE}" ]]; then
    die "No dump file found. Provide the dump file manually."
  fi
fi

if [[ ! -f "${DUMP_FILE}" ]]; then
  die "Dump file does not exist: ${DUMP_FILE}"
fi

if [[ ! -s "${DUMP_FILE}" ]]; then
  die "Dump file is empty: ${DUMP_FILE}"
fi

print_ok "Dump file selected:"
echo "  ${DUMP_FILE}"

print_info "Dump file size:"
du -h "${DUMP_FILE}"

# ------------------------------------------------------------
# Step 2: Check Docker and PostgreSQL container
# ------------------------------------------------------------

print_step "Checking Docker and PostgreSQL container"

if ! command -v docker >/dev/null 2>&1; then
  die "Docker command not found. Run this script on the Docker host."
fi

if ! docker ps --format '{{.Names}}' | grep -qx "${POSTGRES_CONTAINER}"; then
  die "PostgreSQL container is not running: ${POSTGRES_CONTAINER}"
fi

print_ok "PostgreSQL container is running."

# ------------------------------------------------------------
# Step 3: Check PostgreSQL tools
# ------------------------------------------------------------

print_step "Checking PostgreSQL tools inside the container"

docker exec "${POSTGRES_CONTAINER}" sh -c "command -v psql >/dev/null 2>&1" \
  || die "psql not found inside the PostgreSQL container."

docker exec "${POSTGRES_CONTAINER}" sh -c "command -v pg_restore >/dev/null 2>&1" \
  || die "pg_restore not found inside the PostgreSQL container."

docker exec "${POSTGRES_CONTAINER}" sh -c "command -v createdb >/dev/null 2>&1" \
  || die "createdb not found inside the PostgreSQL container."

docker exec "${POSTGRES_CONTAINER}" sh -c "command -v dropdb >/dev/null 2>&1" \
  || die "dropdb not found inside the PostgreSQL container."

print_ok "Required PostgreSQL tools are available."

# ------------------------------------------------------------
# Step 4: Check live database connectivity
# ------------------------------------------------------------

print_step "Checking live NetBox database connectivity"

docker exec "${POSTGRES_CONTAINER}" \
  pg_isready -U "${DB_USER}" -d "${DB_NAME}"

print_ok "Live NetBox database is reachable."

# ------------------------------------------------------------
# Step 5: Show live database information
# ------------------------------------------------------------

print_step "Showing live database information"

docker exec "${POSTGRES_CONTAINER}" \
  psql -U "${DB_USER}" -d "${DB_NAME}" -c "
SELECT
  current_database() AS database_name,
  pg_size_pretty(pg_database_size(current_database())) AS database_size,
  now() AS check_time,
  version() AS postgresql_version;
"

# ------------------------------------------------------------
# Step 6: Verify dump readability
# ------------------------------------------------------------

print_step "Checking dump readability with pg_restore -l"

docker exec -i "${POSTGRES_CONTAINER}" \
  pg_restore -l \
  < "${DUMP_FILE}" \
  > /dev/null

print_ok "Dump is readable by pg_restore."

# ------------------------------------------------------------
# Step 7: Create working directory
# ------------------------------------------------------------

print_step "Creating working directory"

mkdir -p "${WORK_DIR}"

print_ok "Working directory created:"
echo "  ${WORK_DIR}"

# ------------------------------------------------------------
# Step 8: Create temporary verification database
# ------------------------------------------------------------

print_step "Creating temporary verification database"

if [[ "${VERIFY_DB}" == "${DB_NAME}" ]]; then
  die "Safety check failed: temporary database name equals live database name."
fi

docker exec "${POSTGRES_CONTAINER}" \
  dropdb -U "${DB_USER}" --if-exists "${VERIFY_DB}"

docker exec "${POSTGRES_CONTAINER}" \
  createdb -U "${DB_USER}" "${VERIFY_DB}"

print_ok "Temporary verification database created:"
echo "  ${VERIFY_DB}"

# ------------------------------------------------------------
# Step 9: Restore dump into temporary database
# ------------------------------------------------------------

print_step "Restoring dump into temporary verification database"

print_info "This does not touch the live NetBox database."
print_info "Restoring dump into:"
echo "  ${VERIFY_DB}"

docker exec -i "${POSTGRES_CONTAINER}" \
  pg_restore \
    -U "${DB_USER}" \
    -d "${VERIFY_DB}" \
    --no-owner \
    --no-acl \
    --exit-on-error \
  < "${DUMP_FILE}"

print_ok "Dump restored successfully into temporary database."

# ------------------------------------------------------------
# Function: Generate database fingerprint
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
      'SELECT COALESCE(md5(string_agg(row_hash, '','' ORDER BY row_hash)), md5(''''))
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
# Step 10: Generate live database fingerprint
# ------------------------------------------------------------

print_step "Generating fingerprint of the live NetBox database"

print_info "The fingerprint includes:"
echo "  - table name"
echo "  - row count"
echo "  - content checksum per table"

generate_fingerprint "${DB_NAME}" "${LIVE_RESULT_FILE}"

print_ok "Live database fingerprint generated:"
echo "  ${LIVE_RESULT_FILE}"

# ------------------------------------------------------------
# Step 11: Generate restored database fingerprint
# ------------------------------------------------------------

print_step "Generating fingerprint of the restored dump database"

generate_fingerprint "${VERIFY_DB}" "${RESTORED_RESULT_FILE}"

print_ok "Restored database fingerprint generated:"
echo "  ${RESTORED_RESULT_FILE}"

# ------------------------------------------------------------
# Step 12: Prepare comparison files
# ------------------------------------------------------------

print_step "Preparing comparison files"

cut -f1 "${LIVE_RESULT_FILE}" > "${LIVE_TABLES_FILE}"
cut -f1 "${RESTORED_RESULT_FILE}" > "${RESTORED_TABLES_FILE}"

comm -12 "${LIVE_RESULT_FILE}" "${RESTORED_RESULT_FILE}" > "${MATCHING_TABLES_FILE}" || true
comm -23 "${LIVE_TABLES_FILE}" "${RESTORED_TABLES_FILE}" > "${ONLY_LIVE_FILE}" || true
comm -13 "${LIVE_TABLES_FILE}" "${RESTORED_TABLES_FILE}" > "${ONLY_RESTORED_FILE}" || true

awk -F '\t' '
  NR==FNR {
    live_count[$1]=$2
    next
  }
  ($1 in live_count) {
    if (live_count[$1] != $2) {
      print $1 "\tLIVE_ROWS=" live_count[$1] "\tRESTORED_ROWS=" $2
    }
  }
' "${LIVE_RESULT_FILE}" "${RESTORED_RESULT_FILE}" > "${ROWCOUNT_DIFF_FILE}"

awk -F '\t' '
  NR==FNR {
    live_count[$1]=$2
    live_checksum[$1]=$3
    next
  }
  ($1 in live_checksum) {
    if (live_count[$1] == $2 && live_checksum[$1] != $3) {
      print $1 "\tLIVE_CHECKSUM=" live_checksum[$1] "\tRESTORED_CHECKSUM=" $3
    }
  }
' "${LIVE_RESULT_FILE}" "${RESTORED_RESULT_FILE}" > "${CHECKSUM_DIFF_FILE}"

if diff -u "${LIVE_RESULT_FILE}" "${RESTORED_RESULT_FILE}" > "${DIFF_FILE}"; then
  OVERALL_STATUS="MATCH"
  OVERALL_NOTE="The restored dump matches the live database based on table list, row counts, and per-table checksums."
else
  OVERALL_STATUS="REVIEW NEEDED"
  OVERALL_NOTE="Differences were found. This can be normal if NetBox changed after the dump was created."
fi

print_ok "Comparison files prepared."

# ------------------------------------------------------------
# Step 13: Calculate metrics
# ------------------------------------------------------------

print_step "Calculating comparison metrics"

LIVE_TABLE_COUNT="$(wc -l < "${LIVE_TABLES_FILE}" | xargs)"
RESTORED_TABLE_COUNT="$(wc -l < "${RESTORED_TABLES_FILE}" | xargs)"
MATCHING_TABLE_COUNT="$(wc -l < "${MATCHING_TABLES_FILE}" | xargs)"
ONLY_LIVE_COUNT="$(wc -l < "${ONLY_LIVE_FILE}" | xargs)"
ONLY_RESTORED_COUNT="$(wc -l < "${ONLY_RESTORED_FILE}" | xargs)"
ROWCOUNT_DIFF_COUNT="$(wc -l < "${ROWCOUNT_DIFF_FILE}" | xargs)"
CHECKSUM_DIFF_COUNT="$(wc -l < "${CHECKSUM_DIFF_FILE}" | xargs)"

if [[ "${LIVE_TABLE_COUNT}" == "${RESTORED_TABLE_COUNT}" ]]; then
  TABLE_COUNT_STATUS="OK"
else
  TABLE_COUNT_STATUS="DIFFERENT"
fi

if [[ "${ONLY_LIVE_COUNT}" == "0" ]]; then
  ONLY_LIVE_STATUS="OK"
else
  ONLY_LIVE_STATUS="REVIEW"
fi

if [[ "${ONLY_RESTORED_COUNT}" == "0" ]]; then
  ONLY_RESTORED_STATUS="OK"
else
  ONLY_RESTORED_STATUS="REVIEW"
fi

if [[ "${ROWCOUNT_DIFF_COUNT}" == "0" ]]; then
  ROWCOUNT_STATUS="OK"
else
  ROWCOUNT_STATUS="REVIEW"
fi

if [[ "${CHECKSUM_DIFF_COUNT}" == "0" ]]; then
  CHECKSUM_STATUS="OK"
else
  CHECKSUM_STATUS="REVIEW"
fi

print_ok "Metrics calculated."

# ------------------------------------------------------------
# Step 14: Helper for colored status
# ------------------------------------------------------------

colored_status() {
  local value="$1"

  case "${value}" in
    OK|MATCH)
      echo "${GREEN}${BOLD}${value}${RESET}"
      ;;
    REVIEW|DIFFERENT|"REVIEW NEEDED")
      echo "${YELLOW}${BOLD}${value}${RESET}"
      ;;
    FAILED|ERROR)
      echo "${RED}${BOLD}${value}${RESET}"
      ;;
    *)
      echo "${value}"
      ;;
  esac
}

# ------------------------------------------------------------
# Step 15: Print final colored comparison table
# ------------------------------------------------------------

print_header "Final Comparison Summary"

printf "%-36s | %-14s | %-14s | %-20s\n" "Check item" "Live DB" "Restored DB" "Result"
printf "%-36s-+-%-14s-+-%-14s-+-%-20s\n" "------------------------------------" "--------------" "--------------" "--------------------"

printf "%-36s | %-14s | %-14s | %-20b\n" \
  "Total tables" \
  "${LIVE_TABLE_COUNT}" \
  "${RESTORED_TABLE_COUNT}" \
  "$(colored_status "${TABLE_COUNT_STATUS}")"

printf "%-36s | %-14s | %-14s | %-20s\n" \
  "Fully matching tables" \
  "${MATCHING_TABLE_COUNT}" \
  "${MATCHING_TABLE_COUNT}" \
  "Rows + checksum"

printf "%-36s | %-14s | %-14s | %-20b\n" \
  "Tables only in live DB" \
  "${ONLY_LIVE_COUNT}" \
  "0" \
  "$(colored_status "${ONLY_LIVE_STATUS}")"

printf "%-36s | %-14s | %-14s | %-20b\n" \
  "Tables only in restored DB" \
  "0" \
  "${ONLY_RESTORED_COUNT}" \
  "$(colored_status "${ONLY_RESTORED_STATUS}")"

printf "%-36s | %-14s | %-14s | %-20b\n" \
  "Row count differences" \
  "${ROWCOUNT_DIFF_COUNT}" \
  "${ROWCOUNT_DIFF_COUNT}" \
  "$(colored_status "${ROWCOUNT_STATUS}")"

printf "%-36s | %-14s | %-14s | %-20b\n" \
  "Checksum differences" \
  "${CHECKSUM_DIFF_COUNT}" \
  "${CHECKSUM_DIFF_COUNT}" \
  "$(colored_status "${CHECKSUM_STATUS}")"

echo
echo "------------------------------------------------------------"
echo "Final Result"
echo "------------------------------------------------------------"

if [[ "${OVERALL_STATUS}" == "MATCH" ]]; then
  echo -n "Status : "
  print_match "MATCH"
else
  echo -n "Status : "
  print_review "REVIEW NEEDED"
fi

echo "Notes  : ${OVERALL_NOTE}"

# ------------------------------------------------------------
# Step 16: Write plain summary file
# ------------------------------------------------------------

{
  echo "============================================================"
  echo "NetBox Dump Verification - Final Comparison Summary"
  echo "============================================================"
  echo
  echo "Live database     : ${DB_NAME}"
  echo "Restored database : ${VERIFY_DB}"
  echo "Dump file         : ${DUMP_FILE}"
  echo "Check time        : $(date)"
  echo
  echo "------------------------------------------------------------"
  echo "Comparison Table"
  echo "------------------------------------------------------------"
  printf "%-36s | %-14s | %-14s | %-20s\n" "Check item" "Live DB" "Restored DB" "Result"
  printf "%-36s-+-%-14s-+-%-14s-+-%-20s\n" "------------------------------------" "--------------" "--------------" "--------------------"
  printf "%-36s | %-14s | %-14s | %-20s\n" "Total tables" "${LIVE_TABLE_COUNT}" "${RESTORED_TABLE_COUNT}" "${TABLE_COUNT_STATUS}"
  printf "%-36s | %-14s | %-14s | %-20s\n" "Fully matching tables" "${MATCHING_TABLE_COUNT}" "${MATCHING_TABLE_COUNT}" "Rows + checksum"
  printf "%-36s | %-14s | %-14s | %-20s\n" "Tables only in live DB" "${ONLY_LIVE_COUNT}" "0" "${ONLY_LIVE_STATUS}"
  printf "%-36s | %-14s | %-14s | %-20s\n" "Tables only in restored DB" "0" "${ONLY_RESTORED_COUNT}" "${ONLY_RESTORED_STATUS}"
  printf "%-36s | %-14s | %-14s | %-20s\n" "Row count differences" "${ROWCOUNT_DIFF_COUNT}" "${ROWCOUNT_DIFF_COUNT}" "${ROWCOUNT_STATUS}"
  printf "%-36s | %-14s | %-14s | %-20s\n" "Checksum differences" "${CHECKSUM_DIFF_COUNT}" "${CHECKSUM_DIFF_COUNT}" "${CHECKSUM_STATUS}"
  echo
  echo "------------------------------------------------------------"
  echo "Final Result"
  echo "------------------------------------------------------------"
  echo "Status : ${OVERALL_STATUS}"
  echo "Notes  : ${OVERALL_NOTE}"
  echo
  echo "------------------------------------------------------------"
  echo "Generated Evidence Files"
  echo "------------------------------------------------------------"
  echo "Live fingerprint              : ${LIVE_RESULT_FILE}"
  echo "Restored fingerprint          : ${RESTORED_RESULT_FILE}"
  echo "Full diff file                : ${DIFF_FILE}"
  echo "Tables only in live DB        : ${ONLY_LIVE_FILE}"
  echo "Tables only in restored DB    : ${ONLY_RESTORED_FILE}"
  echo "Row count differences         : ${ROWCOUNT_DIFF_FILE}"
  echo "Checksum differences          : ${CHECKSUM_DIFF_FILE}"
  echo
} > "${SUMMARY_FILE}"

print_ok "Plain summary file generated:"
echo "  ${SUMMARY_FILE}"

# ------------------------------------------------------------
# Step 17: Show detailed notes if differences exist
# ------------------------------------------------------------

if [[ "${OVERALL_STATUS}" != "MATCH" ]]; then
  print_header "Detailed Review Notes"

  print_warn "Differences were detected."

  if [[ "${ONLY_LIVE_COUNT}" != "0" ]]; then
    echo
    print_warn "Tables present only in live database:"
    head -50 "${ONLY_LIVE_FILE}"
  fi

  if [[ "${ONLY_RESTORED_COUNT}" != "0" ]]; then
    echo
    print_warn "Tables present only in restored database:"
    head -50 "${ONLY_RESTORED_FILE}"
  fi

  if [[ "${ROWCOUNT_DIFF_COUNT}" != "0" ]]; then
    echo
    print_warn "Tables with row count differences:"
    head -50 "${ROWCOUNT_DIFF_FILE}"
  fi

  if [[ "${CHECKSUM_DIFF_COUNT}" != "0" ]]; then
    echo
    print_warn "Tables with checksum differences but same row count:"
    head -50 "${CHECKSUM_DIFF_FILE}"
  fi

  echo
  print_warn "Important note:"
  echo "  If the dump was created earlier and the live NetBox database changed after that,"
  echo "  differences can appear even if the dump is valid."
  echo
  echo "  For the most accurate result, run verification immediately after creating the dump,"
  echo "  or temporarily stop NetBox application containers during backup and verification."
fi

# ------------------------------------------------------------
# Step 18: Final script completion message
# ------------------------------------------------------------

print_header "Verification Completed"

echo "Dump file:"
echo "  ${DUMP_FILE}"

echo
echo "Final status:"
if [[ "${OVERALL_STATUS}" == "MATCH" ]]; then
  echo "  ${GREEN}${BOLD}MATCH${RESET}"
else
  echo "  ${YELLOW}${BOLD}REVIEW NEEDED${RESET}"
fi

echo
echo "Summary file:"
echo "  ${SUMMARY_FILE}"

echo
echo "Evidence directory:"
echo "  ${WORK_DIR}"

echo
print_ok "Script completed successfully."
