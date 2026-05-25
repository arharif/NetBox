#!/usr/bin/env bash

set -Eeuo pipefail

# ============================================================
# NetBox PostgreSQL Dump Full Verification Script
# ============================================================
#
# Purpose:
#   Verify a NetBox PostgreSQL .dump backup by restoring it into
#   a temporary database and comparing it with the live NetBox DB.
#
# This script compares:
#   1. Dump readability
#   2. Schema
#   3. Table list
#   4. Row counts
#   5. Per-table checksums
#   6. Normalized data export
#   7. Sequences
#   8. Live database size
#   9. Restored database size
#   10. Compressed .dump file size
#   11. Normal .sql file size, if available
#
# Safety:
#   - The live NetBox database is NOT modified.
#   - The dump is restored into a temporary database.
#   - The temporary database is dropped at the end.
#
# Usage:
#   ./verify_netbox_dump_full.sh
#
# Or:
#   ./verify_netbox_dump_full.sh /path/to/netbox_postgres_xxxxx.dump
#
# Or:
#   ./verify_netbox_dump_full.sh /path/to/netbox_postgres_xxxxx.dump /path/to/netbox.sql
#
# ============================================================

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

DOCKER_CMD="${DOCKER_CMD:-docker}"

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-netbox-docker-postgres-1}"
DB_USER="${DB_USER:-netbox}"
LIVE_DB="${LIVE_DB:-netbox}"

BACKUP_DIR="${BACKUP_DIR:-/home/netbox_admin/netbox-backups}"

DUMP_FILE="${1:-}"
NORMAL_SQL_FILE="${2:-}"

KEEP_VERIFY_DB="${KEEP_VERIFY_DB:-no}"

VERIFY_DB="netbox_verify_$(date +%Y%m%d_%H%M%S)"
WORK_DIR="/tmp/netbox_dump_full_verify_${VERIFY_DB}"

LIVE_SCHEMA_FILE="${WORK_DIR}/live_schema.sql"
RESTORED_SCHEMA_FILE="${WORK_DIR}/restored_schema.sql"
SCHEMA_DIFF_FILE="${WORK_DIR}/schema_diff.txt"

LIVE_FINGERPRINT_FILE="${WORK_DIR}/live_table_fingerprint.tsv"
RESTORED_FINGERPRINT_FILE="${WORK_DIR}/restored_table_fingerprint.tsv"
FINGERPRINT_DIFF_FILE="${WORK_DIR}/fingerprint_diff.txt"

LIVE_TABLES_FILE="${WORK_DIR}/live_tables.txt"
RESTORED_TABLES_FILE="${WORK_DIR}/restored_tables.txt"
ONLY_LIVE_TABLES_FILE="${WORK_DIR}/only_in_live_tables.txt"
ONLY_RESTORED_TABLES_FILE="${WORK_DIR}/only_in_restored_tables.txt"

ROWCOUNT_DIFF_FILE="${WORK_DIR}/rowcount_differences.txt"
CHECKSUM_DIFF_FILE="${WORK_DIR}/checksum_differences.txt"

LIVE_NORMALIZED_EXPORT="${WORK_DIR}/live_normalized_export.txt"
RESTORED_NORMALIZED_EXPORT="${WORK_DIR}/restored_normalized_export.txt"
NORMALIZED_DATA_DIFF_FILE="${WORK_DIR}/normalized_data_diff.txt"

SIZE_COMPARISON_FILE="${WORK_DIR}/size_comparison.txt"
SUMMARY_FILE="${WORK_DIR}/final_summary.txt"

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

colored_status() {
  local value="$1"

  case "${value}" in
    OK|MATCH)
      echo "${GREEN}${BOLD}${value}${RESET}"
      ;;
    INFO)
      echo "${CYAN}${BOLD}${value}${RESET}"
      ;;
    REVIEW|DIFFERENT|"REVIEW NEEDED"|"NOT FOUND")
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
# Utility functions
# ------------------------------------------------------------

human_size() {
  local bytes="$1"

  if [[ -z "${bytes}" || "${bytes}" == "NA" ]]; then
    echo "N/A"
    return
  fi

  if command -v numfmt >/dev/null 2>&1; then
    numfmt --to=iec --suffix=B "${bytes}"
  else
    echo "${bytes} bytes"
  fi
}

file_size_bytes() {
  local file="$1"

  if [[ -n "${file}" && -f "${file}" ]]; then
    stat -c%s "${file}"
  else
    echo "NA"
  fi
}

db_size_bytes() {
  local db_name="$1"

  ${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
    psql -U "${DB_USER}" -d "${db_name}" -At -c "SELECT pg_database_size(current_database());" \
    | tr -d '[:space:]'
}

abs_diff_bytes() {
  local a="$1"
  local b="$2"

  if [[ "${a}" == "NA" || "${b}" == "NA" ]]; then
    echo "NA"
    return
  fi

  local diff=$(( a - b ))

  if [[ "${diff}" -lt 0 ]]; then
    diff=$(( diff * -1 ))
  fi

  echo "${diff}"
}

safe_status_from_diff() {
  local diff_file="$1"

  if [[ -s "${diff_file}" ]]; then
    echo "REVIEW"
  else
    echo "OK"
  fi
}

# ------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------

cleanup() {
  echo
  print_step "Cleanup"

  if [[ "${KEEP_VERIFY_DB}" == "yes" ]]; then
    print_warn "Temporary verification database was kept because KEEP_VERIFY_DB=yes:"
    echo "  ${VERIFY_DB}"
  else
    ${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
      dropdb -U "${DB_USER}" --if-exists "${VERIFY_DB}" >/dev/null 2>&1 || true

    print_ok "Temporary verification database dropped if it existed:"
    echo "  ${VERIFY_DB}"
  fi

  print_info "Evidence files are available here:"
  echo "  ${WORK_DIR}"
}

trap cleanup EXIT
trap 'print_error "Script failed at line $LINENO. Review the error above."' ERR

# ------------------------------------------------------------
# Start
# ------------------------------------------------------------

print_header "NetBox PostgreSQL Dump Full Verification"

print_info "PostgreSQL container : ${POSTGRES_CONTAINER}"
print_info "Live database        : ${LIVE_DB}"
print_info "Database user        : ${DB_USER}"
print_info "Backup directory     : ${BACKUP_DIR}"
print_info "Temporary database   : ${VERIFY_DB}"
print_info "Working directory    : ${WORK_DIR}"

# ------------------------------------------------------------
# Step 1: Detect dump file
# ------------------------------------------------------------

print_step "Detecting PostgreSQL .dump file"

if [[ -z "${DUMP_FILE}" ]]; then
  print_info "No dump file provided. Searching latest .dump file in:"
  echo "  ${BACKUP_DIR}"

  DUMP_FILE="$(find "${BACKUP_DIR}" -type f -name "netbox_postgres_*.dump" -printf "%T@ %p\n" 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2- || true)"
fi

if [[ -z "${DUMP_FILE}" ]]; then
  die "No .dump file found. Provide the dump file path manually."
fi

if [[ ! -f "${DUMP_FILE}" ]]; then
  die "Dump file does not exist: ${DUMP_FILE}"
fi

if [[ ! -s "${DUMP_FILE}" ]]; then
  die "Dump file is empty: ${DUMP_FILE}"
fi

print_ok "Dump file selected:"
echo "  ${DUMP_FILE}"

# ------------------------------------------------------------
# Step 2: Detect normal SQL file
# ------------------------------------------------------------

print_step "Detecting normal .sql backup file"

if [[ -z "${NORMAL_SQL_FILE}" ]]; then
  NORMAL_SQL_FILE="$(find "${BACKUP_DIR}" -type f -name "*.sql" -printf "%T@ %p\n" 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2- || true)"
fi

if [[ -n "${NORMAL_SQL_FILE}" && -f "${NORMAL_SQL_FILE}" ]]; then
  print_ok "Normal SQL file selected:"
  echo "  ${NORMAL_SQL_FILE}"
else
  NORMAL_SQL_FILE=""
  print_warn "No normal .sql backup file found. SQL file size comparison will be marked as N/A."
fi

# ------------------------------------------------------------
# Step 3: Check Docker and container
# ------------------------------------------------------------

print_step "Checking Docker and PostgreSQL container"

if ! command -v ${DOCKER_CMD%% *} >/dev/null 2>&1; then
  die "Docker command not found. Run this script on the Docker host."
fi

if ! ${DOCKER_CMD} ps --format '{{.Names}}' | grep -qx "${POSTGRES_CONTAINER}"; then
  die "PostgreSQL container is not running: ${POSTGRES_CONTAINER}"
fi

print_ok "PostgreSQL container is running."

# ------------------------------------------------------------
# Step 4: Check PostgreSQL tools
# ------------------------------------------------------------

print_step "Checking PostgreSQL tools inside the container"

for tool in psql pg_dump pg_restore createdb dropdb; do
  ${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" sh -c "command -v ${tool} >/dev/null 2>&1" \
    || die "${tool} not found inside the PostgreSQL container."
done

print_ok "Required PostgreSQL tools are available."

# ------------------------------------------------------------
# Step 5: Check live DB connectivity
# ------------------------------------------------------------

print_step "Checking live NetBox database connectivity"

${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
  pg_isready -U "${DB_USER}" -d "${LIVE_DB}"

print_ok "Live NetBox database is reachable."

# ------------------------------------------------------------
# Step 6: Show live DB information
# ------------------------------------------------------------

print_step "Showing live NetBox database information"

${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
  psql -U "${DB_USER}" -d "${LIVE_DB}" -c "
SELECT
  current_database() AS database_name,
  pg_size_pretty(pg_database_size(current_database())) AS database_size,
  now() AS check_time,
  version() AS postgresql_version;
"

# ------------------------------------------------------------
# Step 7: Verify dump readability
# ------------------------------------------------------------

print_step "Checking dump readability with pg_restore -l"

${DOCKER_CMD} exec -i "${POSTGRES_CONTAINER}" \
  pg_restore -l \
  < "${DUMP_FILE}" \
  > /dev/null

print_ok "Dump is readable by pg_restore."

# ------------------------------------------------------------
# Step 8: Create work directory
# ------------------------------------------------------------

print_step "Creating evidence working directory"

mkdir -p "${WORK_DIR}"

print_ok "Working directory created:"
echo "  ${WORK_DIR}"

# ------------------------------------------------------------
# Step 9: Create temporary DB and restore dump
# ------------------------------------------------------------

print_step "Creating temporary verification database"

if [[ "${VERIFY_DB}" == "${LIVE_DB}" ]]; then
  die "Safety check failed: temporary DB name equals live DB name."
fi

${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
  dropdb -U "${DB_USER}" --if-exists "${VERIFY_DB}"

${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
  createdb -U "${DB_USER}" "${VERIFY_DB}"

print_ok "Temporary verification database created:"
echo "  ${VERIFY_DB}"

print_step "Restoring dump into temporary verification database"

print_info "This does not modify the live NetBox database."

${DOCKER_CMD} exec -i "${POSTGRES_CONTAINER}" \
  pg_restore \
    -U "${DB_USER}" \
    -d "${VERIFY_DB}" \
    --no-owner \
    --no-acl \
    --exit-on-error \
  < "${DUMP_FILE}"

print_ok "Dump restored successfully into temporary database."

# ------------------------------------------------------------
# Step 10: Compare schema using pg_dump -s
# ------------------------------------------------------------

print_step "Comparing schema between live DB and restored DB"

${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
  pg_dump -U "${DB_USER}" -d "${LIVE_DB}" -s --no-owner --no-acl \
  > "${LIVE_SCHEMA_FILE}"

${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
  pg_dump -U "${DB_USER}" -d "${VERIFY_DB}" -s --no-owner --no-acl \
  > "${RESTORED_SCHEMA_FILE}"

if diff -u "${LIVE_SCHEMA_FILE}" "${RESTORED_SCHEMA_FILE}" > "${SCHEMA_DIFF_FILE}"; then
  SCHEMA_STATUS="OK"
  print_ok "Schema comparison passed."
else
  SCHEMA_STATUS="REVIEW"
  print_warn "Schema differences detected."
fi

# ------------------------------------------------------------
# Function: Generate table fingerprint
# ------------------------------------------------------------

generate_table_fingerprint() {
  local db_name="$1"
  local output_file="$2"

  ${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
    psql -U "${DB_USER}" -d "${db_name}" -v ON_ERROR_STOP=1 -q -c "
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
# Step 11: Generate and compare table fingerprints
# ------------------------------------------------------------

print_step "Generating table fingerprints"

generate_table_fingerprint "${LIVE_DB}" "${LIVE_FINGERPRINT_FILE}"
generate_table_fingerprint "${VERIFY_DB}" "${RESTORED_FINGERPRINT_FILE}"

print_ok "Fingerprints generated."

print_step "Comparing table fingerprints"

if diff -u "${LIVE_FINGERPRINT_FILE}" "${RESTORED_FINGERPRINT_FILE}" > "${FINGERPRINT_DIFF_FILE}"; then
  FINGERPRINT_STATUS="OK"
  print_ok "Table fingerprint comparison passed."
else
  FINGERPRINT_STATUS="REVIEW"
  print_warn "Table fingerprint differences detected."
fi

# ------------------------------------------------------------
# Step 12: Prepare table and row-level comparison files
# ------------------------------------------------------------

print_step "Preparing table, row count, and checksum comparison files"

cut -f1 "${LIVE_FINGERPRINT_FILE}" > "${LIVE_TABLES_FILE}"
cut -f1 "${RESTORED_FINGERPRINT_FILE}" > "${RESTORED_TABLES_FILE}"

comm -23 "${LIVE_TABLES_FILE}" "${RESTORED_TABLES_FILE}" > "${ONLY_LIVE_TABLES_FILE}" || true
comm -13 "${LIVE_TABLES_FILE}" "${RESTORED_TABLES_FILE}" > "${ONLY_RESTORED_TABLES_FILE}" || true

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
' "${LIVE_FINGERPRINT_FILE}" "${RESTORED_FINGERPRINT_FILE}" > "${ROWCOUNT_DIFF_FILE}"

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
' "${LIVE_FINGERPRINT_FILE}" "${RESTORED_FINGERPRINT_FILE}" > "${CHECKSUM_DIFF_FILE}"

print_ok "Comparison files prepared."

# ------------------------------------------------------------
# Function: Generate normalized export
# ------------------------------------------------------------

generate_normalized_export() {
  local db_name="$1"
  local output_file="$2"
  local table_list_file="${WORK_DIR}/${db_name}_quoted_tables.tsv"
  local sequence_list_file="${WORK_DIR}/${db_name}_quoted_sequences.tsv"

  ${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
    psql -U "${DB_USER}" -d "${db_name}" -At -F $'\t' -c "
SELECT
  n.nspname || '.' || c.relname AS display_name,
  format('%I.%I', n.nspname, c.relname) AS quoted_name
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
ORDER BY n.nspname, c.relname;
" > "${table_list_file}"

  ${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
    psql -U "${DB_USER}" -d "${db_name}" -At -F $'\t' -c "
SELECT
  n.nspname || '.' || c.relname AS display_name,
  format('%I.%I', n.nspname, c.relname) AS quoted_name
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'S'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
ORDER BY n.nspname, c.relname;
" > "${sequence_list_file}"

  {
    echo "__DATABASE_NORMALIZED_EXPORT_VERSION__ 1"
    echo "__DATABASE__ ${db_name}"
    echo "__SECTION__ TABLE_DATA"

    while IFS=$'\t' read -r display_name quoted_name; do
      [[ -z "${display_name}" ]] && continue

      echo "__TABLE__ ${display_name}"

      ${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
        psql -U "${DB_USER}" -d "${db_name}" -qAt -c "
COPY (
  SELECT row_to_json(t)::text
  FROM ${quoted_name} AS t
  ORDER BY row_to_json(t)::text
) TO STDOUT;
"
    done < "${table_list_file}"

    echo "__SECTION__ SEQUENCES"

    while IFS=$'\t' read -r display_name quoted_name; do
      [[ -z "${display_name}" ]] && continue

      echo "__SEQUENCE__ ${display_name}"

      ${DOCKER_CMD} exec "${POSTGRES_CONTAINER}" \
        psql -U "${DB_USER}" -d "${db_name}" -qAt -c "
SELECT last_value::text || '|' || is_called::text
FROM ${quoted_name};
"
    done < "${sequence_list_file}"
  } > "${output_file}"
}

# ------------------------------------------------------------
# Step 13: Generate normalized exports and compare
# ------------------------------------------------------------

print_step "Generating normalized data export for live database"

generate_normalized_export "${LIVE_DB}" "${LIVE_NORMALIZED_EXPORT}"

print_ok "Live normalized export generated:"
echo "  ${LIVE_NORMALIZED_EXPORT}"

print_step "Generating normalized data export for restored database"

generate_normalized_export "${VERIFY_DB}" "${RESTORED_NORMALIZED_EXPORT}"

print_ok "Restored normalized export generated:"
echo "  ${RESTORED_NORMALIZED_EXPORT}"

print_step "Comparing normalized data exports"

# The database name line is intentionally different, so remove that line before comparison.
LIVE_NORMALIZED_FOR_DIFF="${WORK_DIR}/live_normalized_for_diff.txt"
RESTORED_NORMALIZED_FOR_DIFF="${WORK_DIR}/restored_normalized_for_diff.txt"

grep -v '^__DATABASE__ ' "${LIVE_NORMALIZED_EXPORT}" > "${LIVE_NORMALIZED_FOR_DIFF}"
grep -v '^__DATABASE__ ' "${RESTORED_NORMALIZED_EXPORT}" > "${RESTORED_NORMALIZED_FOR_DIFF}"

if diff -u "${LIVE_NORMALIZED_FOR_DIFF}" "${RESTORED_NORMALIZED_FOR_DIFF}" > "${NORMALIZED_DATA_DIFF_FILE}"; then
  NORMALIZED_DATA_STATUS="OK"
  print_ok "Normalized data comparison passed."
else
  NORMALIZED_DATA_STATUS="REVIEW"
  print_warn "Normalized data differences detected."
fi

# ------------------------------------------------------------
# Step 14: Size comparisons
# ------------------------------------------------------------

print_step "Calculating size comparison"

DUMP_FILE_SIZE_BYTES="$(file_size_bytes "${DUMP_FILE}")"
NORMAL_SQL_SIZE_BYTES="$(file_size_bytes "${NORMAL_SQL_FILE}")"
LIVE_DB_SIZE_BYTES="$(db_size_bytes "${LIVE_DB}")"
RESTORED_DB_SIZE_BYTES="$(db_size_bytes "${VERIFY_DB}")"

DUMP_FILE_SIZE_HUMAN="$(human_size "${DUMP_FILE_SIZE_BYTES}")"
NORMAL_SQL_SIZE_HUMAN="$(human_size "${NORMAL_SQL_SIZE_BYTES}")"
LIVE_DB_SIZE_HUMAN="$(human_size "${LIVE_DB_SIZE_BYTES}")"
RESTORED_DB_SIZE_HUMAN="$(human_size "${RESTORED_DB_SIZE_BYTES}")"

SQL_VS_DUMP_DIFF_BYTES="$(abs_diff_bytes "${NORMAL_SQL_SIZE_BYTES}" "${DUMP_FILE_SIZE_BYTES}")"
LIVE_VS_RESTORED_DB_DIFF_BYTES="$(abs_diff_bytes "${LIVE_DB_SIZE_BYTES}" "${RESTORED_DB_SIZE_BYTES}")"

SQL_VS_DUMP_DIFF_HUMAN="$(human_size "${SQL_VS_DUMP_DIFF_BYTES}")"
LIVE_VS_RESTORED_DB_DIFF_HUMAN="$(human_size "${LIVE_VS_RESTORED_DB_DIFF_BYTES}")"

if [[ "${LIVE_DB_SIZE_BYTES}" == "${RESTORED_DB_SIZE_BYTES}" ]]; then
  DB_SIZE_STATUS="OK"
else
  DB_SIZE_STATUS="INFO"
fi

if [[ "${NORMAL_SQL_SIZE_BYTES}" == "NA" ]]; then
  SQL_SIZE_STATUS="NOT FOUND"
else
  SQL_SIZE_STATUS="INFO"
fi

{
  echo "Normal SQL file path              : ${NORMAL_SQL_FILE:-N/A}"
  echo "Normal SQL file size bytes        : ${NORMAL_SQL_SIZE_BYTES}"
  echo "Normal SQL file size readable     : ${NORMAL_SQL_SIZE_HUMAN}"
  echo "Compressed dump file path         : ${DUMP_FILE}"
  echo "Compressed dump file size bytes   : ${DUMP_FILE_SIZE_BYTES}"
  echo "Compressed dump file size readable: ${DUMP_FILE_SIZE_HUMAN}"
  echo "Live DB size bytes                : ${LIVE_DB_SIZE_BYTES}"
  echo "Live DB size readable             : ${LIVE_DB_SIZE_HUMAN}"
  echo "Restored DB size bytes            : ${RESTORED_DB_SIZE_BYTES}"
  echo "Restored DB size readable         : ${RESTORED_DB_SIZE_HUMAN}"
  echo "SQL vs dump size difference       : ${SQL_VS_DUMP_DIFF_HUMAN}"
  echo "Live vs restored DB size diff     : ${LIVE_VS_RESTORED_DB_DIFF_HUMAN}"
  echo "DB size status                    : ${DB_SIZE_STATUS}"
  echo "SQL size status                   : ${SQL_SIZE_STATUS}"
} > "${SIZE_COMPARISON_FILE}"

print_ok "Size comparison completed."

# ------------------------------------------------------------
# Step 15: Metrics
# ------------------------------------------------------------

print_step "Calculating final metrics"

LIVE_TABLE_COUNT="$(wc -l < "${LIVE_TABLES_FILE}" | xargs)"
RESTORED_TABLE_COUNT="$(wc -l < "${RESTORED_TABLES_FILE}" | xargs)"
ONLY_LIVE_TABLE_COUNT="$(wc -l < "${ONLY_LIVE_TABLES_FILE}" | xargs)"
ONLY_RESTORED_TABLE_COUNT="$(wc -l < "${ONLY_RESTORED_TABLES_FILE}" | xargs)"
ROWCOUNT_DIFF_COUNT="$(wc -l < "${ROWCOUNT_DIFF_FILE}" | xargs)"
CHECKSUM_DIFF_COUNT="$(wc -l < "${CHECKSUM_DIFF_FILE}" | xargs)"

if [[ "${LIVE_TABLE_COUNT}" == "${RESTORED_TABLE_COUNT}" ]]; then
  TABLE_COUNT_STATUS="OK"
else
  TABLE_COUNT_STATUS="REVIEW"
fi

if [[ "${ONLY_LIVE_TABLE_COUNT}" == "0" ]]; then
  ONLY_LIVE_STATUS="OK"
else
  ONLY_LIVE_STATUS="REVIEW"
fi

if [[ "${ONLY_RESTORED_TABLE_COUNT}" == "0" ]]; then
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

# Final status is based on logical checks, not physical size.
if [[ "${SCHEMA_STATUS}" == "OK" \
   && "${FINGERPRINT_STATUS}" == "OK" \
   && "${NORMALIZED_DATA_STATUS}" == "OK" \
   && "${TABLE_COUNT_STATUS}" == "OK" \
   && "${ONLY_LIVE_STATUS}" == "OK" \
   && "${ONLY_RESTORED_STATUS}" == "OK" \
   && "${ROWCOUNT_STATUS}" == "OK" \
   && "${CHECKSUM_STATUS}" == "OK" ]]; then
  FINAL_STATUS="MATCH"
  FINAL_NOTE="The restored dump logically matches the live database. Size checks are informational only."
else
  FINAL_STATUS="REVIEW NEEDED"
  FINAL_NOTE="Differences were detected. This may be normal if the live NetBox database changed after the dump was created."
fi

print_ok "Final metrics calculated."

# ------------------------------------------------------------
# Step 16: Print final colored comparison table
# ------------------------------------------------------------

print_header "Final Comparison Summary"

printf "%-38s | %-18s | %-18s | %-18s\n" "Check item" "Live / Normal" "Restored / Dump" "Result"
printf "%-38s-+-%-18s-+-%-18s-+-%-18s\n" "--------------------------------------" "------------------" "------------------" "------------------"

printf "%-38s | %-18s | %-18s | %-18b\n" \
  "Schema comparison" \
  "Live schema" \
  "Restored schema" \
  "$(colored_status "${SCHEMA_STATUS}")"

printf "%-38s | %-18s | %-18s | %-18b\n" \
  "Table count" \
  "${LIVE_TABLE_COUNT}" \
  "${RESTORED_TABLE_COUNT}" \
  "$(colored_status "${TABLE_COUNT_STATUS}")"

printf "%-38s | %-18s | %-18s | %-18b\n" \
  "Tables only in live DB" \
  "${ONLY_LIVE_TABLE_COUNT}" \
  "0" \
  "$(colored_status "${ONLY_LIVE_STATUS}")"

printf "%-38s | %-18s | %-18s | %-18b\n" \
  "Tables only in restored DB" \
  "0" \
  "${ONLY_RESTORED_TABLE_COUNT}" \
  "$(colored_status "${ONLY_RESTORED_STATUS}")"

printf "%-38s | %-18s | %-18s | %-18b\n" \
  "Row count differences" \
  "${ROWCOUNT_DIFF_COUNT}" \
  "${ROWCOUNT_DIFF_COUNT}" \
  "$(colored_status "${ROWCOUNT_STATUS}")"

printf "%-38s | %-18s | %-18s | %-18b\n" \
  "Checksum differences" \
  "${CHECKSUM_DIFF_COUNT}" \
  "${CHECKSUM_DIFF_COUNT}" \
  "$(colored_status "${CHECKSUM_STATUS}")"

printf "%-38s | %-18s | %-18s | %-18b\n" \
  "Table fingerprint comparison" \
  "Live fingerprint" \
  "Restored fingerprint" \
  "$(colored_status "${FINGERPRINT_STATUS}")"

printf "%-38s | %-18s | %-18s | %-18b\n" \
  "Normalized data + sequences" \
  "Live export" \
  "Restored export" \
  "$(colored_status "${NORMALIZED_DATA_STATUS}")"

printf "%-38s | %-18s | %-18s | %-18b\n" \
  "Live DB vs restored DB size" \
  "${LIVE_DB_SIZE_HUMAN}" \
  "${RESTORED_DB_SIZE_HUMAN}" \
  "$(colored_status "${DB_SIZE_STATUS}")"

printf "%-38s | %-18s | %-18s | %-18s\n" \
  "DB size difference" \
  "${LIVE_VS_RESTORED_DB_DIFF_HUMAN}" \
  "-" \
  "Informational"

printf "%-38s | %-18s | %-18s | %-18b\n" \
  "Normal SQL file size" \
  "${NORMAL_SQL_SIZE_HUMAN}" \
  "-" \
  "$(colored_status "${SQL_SIZE_STATUS}")"

printf "%-38s | %-18s | %-18s | %-18s\n" \
  "Compressed dump file size" \
  "${DUMP_FILE_SIZE_HUMAN}" \
  "-" \
  "pg_dump -Fc"

printf "%-38s | %-18s | %-18s | %-18s\n" \
  "SQL vs dump size difference" \
  "${SQL_VS_DUMP_DIFF_HUMAN}" \
  "-" \
  "Compression effect"

echo
echo "------------------------------------------------------------"
echo "Final Result"
echo "------------------------------------------------------------"

if [[ "${FINAL_STATUS}" == "MATCH" ]]; then
  echo -n "Status : "
  print_match "MATCH"
else
  echo -n "Status : "
  print_review "REVIEW NEEDED"
fi

echo "Notes  : ${FINAL_NOTE}"

# ------------------------------------------------------------
# Step 17: Write plain summary file
# ------------------------------------------------------------

{
  echo "============================================================"
  echo "NetBox PostgreSQL Dump Full Verification Summary"
  echo "============================================================"
  echo
  echo "Live database          : ${LIVE_DB}"
  echo "Restored database      : ${VERIFY_DB}"
  echo "Dump file              : ${DUMP_FILE}"
  echo "Normal SQL file        : ${NORMAL_SQL_FILE:-N/A}"
  echo "Check time             : $(date)"
  echo
  echo "------------------------------------------------------------"
  echo "Comparison Table"
  echo "------------------------------------------------------------"
  printf "%-38s | %-18s | %-18s | %-18s\n" "Check item" "Live / Normal" "Restored / Dump" "Result"
  printf "%-38s-+-%-18s-+-%-18s-+-%-18s\n" "--------------------------------------" "------------------" "------------------" "------------------"
  printf "%-38s | %-18s | %-18s | %-18s\n" "Schema comparison" "Live schema" "Restored schema" "${SCHEMA_STATUS}"
  printf "%-38s | %-18s | %-18s | %-18s\n" "Table count" "${LIVE_TABLE_COUNT}" "${RESTORED_TABLE_COUNT}" "${TABLE_COUNT_STATUS}"
  printf "%-38s | %-18s | %-18s | %-18s\n" "Tables only in live DB" "${ONLY_LIVE_TABLE_COUNT}" "0" "${ONLY_LIVE_STATUS}"
  printf "%-38s | %-18s | %-18s | %-18s\n" "Tables only in restored DB" "0" "${ONLY_RESTORED_TABLE_COUNT}" "${ONLY_RESTORED_STATUS}"
  printf "%-38s | %-18s | %-18s | %-18s\n" "Row count differences" "${ROWCOUNT_DIFF_COUNT}" "${ROWCOUNT_DIFF_COUNT}" "${ROWCOUNT_STATUS}"
  printf "%-38s | %-18s | %-18s | %-18s\n" "Checksum differences" "${CHECKSUM_DIFF_COUNT}" "${CHECKSUM_DIFF_COUNT}" "${CHECKSUM_STATUS}"
  printf "%-38s | %-18s | %-18s | %-18s\n" "Table fingerprint comparison" "Live fingerprint" "Restored fingerprint" "${FINGERPRINT_STATUS}"
  printf "%-38s | %-18s | %-18s | %-18s\n" "Normalized data + sequences" "Live export" "Restored export" "${NORMALIZED_DATA_STATUS}"
  printf "%-38s | %-18s | %-18s | %-18s\n" "Live DB vs restored DB size" "${LIVE_DB_SIZE_HUMAN}" "${RESTORED_DB_SIZE_HUMAN}" "${DB_SIZE_STATUS}"
  printf "%-38s | %-18s | %-18s | %-18s\n" "DB size difference" "${LIVE_VS_RESTORED_DB_DIFF_HUMAN}" "-" "Informational"
  printf "%-38s | %-18s | %-18s | %-18s\n" "Normal SQL file size" "${NORMAL_SQL_SIZE_HUMAN}" "-" "${SQL_SIZE_STATUS}"
  printf "%-38s | %-18s | %-18s | %-18s\n" "Compressed dump file size" "${DUMP_FILE_SIZE_HUMAN}" "-" "pg_dump -Fc"
  printf "%-38s | %-18s | %-18s | %-18s\n" "SQL vs dump size difference" "${SQL_VS_DUMP_DIFF_HUMAN}" "-" "Compression effect"
  echo
  echo "------------------------------------------------------------"
  echo "Final Result"
  echo "------------------------------------------------------------"
  echo "Status : ${FINAL_STATUS}"
  echo "Notes  : ${FINAL_NOTE}"
  echo
  echo "------------------------------------------------------------"
  echo "Important Notes"
  echo "------------------------------------------------------------"
  echo "1. The .dump file is compressed, so it is normally smaller than the .sql file."
  echo "2. The restored DB size can differ slightly from the live DB size."
  echo "3. Final MATCH is based on logical checks, not physical size."
  echo "4. If NetBox changed after the dump was created, REVIEW NEEDED can be normal."
  echo
  echo "------------------------------------------------------------"
  echo "Evidence Files"
  echo "------------------------------------------------------------"
  echo "Live schema                  : ${LIVE_SCHEMA_FILE}"
  echo "Restored schema              : ${RESTORED_SCHEMA_FILE}"
  echo "Schema diff                  : ${SCHEMA_DIFF_FILE}"
  echo "Live fingerprint             : ${LIVE_FINGERPRINT_FILE}"
  echo "Restored fingerprint         : ${RESTORED_FINGERPRINT_FILE}"
  echo "Fingerprint diff             : ${FINGERPRINT_DIFF_FILE}"
  echo "Only live tables             : ${ONLY_LIVE_TABLES_FILE}"
  echo "Only restored tables         : ${ONLY_RESTORED_TABLES_FILE}"
  echo "Row count differences        : ${ROWCOUNT_DIFF_FILE}"
  echo "Checksum differences         : ${CHECKSUM_DIFF_FILE}"
  echo "Live normalized export       : ${LIVE_NORMALIZED_EXPORT}"
  echo "Restored normalized export   : ${RESTORED_NORMALIZED_EXPORT}"
  echo "Normalized data diff         : ${NORMALIZED_DATA_DIFF_FILE}"
  echo "Size comparison              : ${SIZE_COMPARISON_FILE}"
  echo
} > "${SUMMARY_FILE}"

print_ok "Summary file generated:"
echo "  ${SUMMARY_FILE}"

# ------------------------------------------------------------
# Step 18: Show detailed review notes if needed
# ------------------------------------------------------------

if [[ "${FINAL_STATUS}" != "MATCH" ]]; then
  print_header "Detailed Review Notes"

  print_warn "Differences were detected."

  if [[ "${SCHEMA_STATUS}" != "OK" ]]; then
    echo
    print_warn "Schema differences detected. First lines:"
    head -80 "${SCHEMA_DIFF_FILE}"
  fi

  if [[ "${ONLY_LIVE_TABLE_COUNT}" != "0" ]]; then
    echo
    print_warn "Tables only in live database:"
    head -50 "${ONLY_LIVE_TABLES_FILE}"
  fi

  if [[ "${ONLY_RESTORED_TABLE_COUNT}" != "0" ]]; then
    echo
    print_warn "Tables only in restored database:"
    head -50 "${ONLY_RESTORED_TABLES_FILE}"
  fi

  if [[ "${ROWCOUNT_DIFF_COUNT}" != "0" ]]; then
    echo
    print_warn "Tables with row count differences:"
    head -50 "${ROWCOUNT_DIFF_FILE}"
  fi

  if [[ "${CHECKSUM_DIFF_COUNT}" != "0" ]]; then
    echo
    print_warn "Tables with checksum differences:"
    head -50 "${CHECKSUM_DIFF_FILE}"
  fi

  if [[ "${NORMALIZED_DATA_STATUS}" != "OK" ]]; then
    echo
    print_warn "Normalized data differences detected. First lines:"
    head -80 "${NORMALIZED_DATA_DIFF_FILE}"
  fi

  echo
  print_warn "Important:"
  echo "  If the dump was created earlier and the live NetBox DB changed after that,"
  echo "  differences may appear even if the dump is valid."
fi

# ------------------------------------------------------------
# Step 19: Final completion
# ------------------------------------------------------------

print_header "Verification Completed"

echo "Dump file:"
echo "  ${DUMP_FILE}"

echo
echo "Normal SQL file:"
echo "  ${NORMAL_SQL_FILE:-N/A}"

echo
echo "Final status:"
if [[ "${FINAL_STATUS}" == "MATCH" ]]; then
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
