#!/usr/bin/env bash

set -Eeuo pipefail

# ============================================================
# NetBox PostgreSQL Dump Full Verification Script - v2
# ============================================================
#
# Purpose:
#   Verify a NetBox PostgreSQL .dump backup by restoring it into
#   a temporary database and comparing it with the live NetBox DB.
#
# Main checks:
#   1. Dump readability
#   2. Restore test into temporary DB
#   3. Schema comparison
#   4. Table list comparison
#   5. Row count comparison
#   6. Per-table checksum comparison
#   7. Sequence comparison
#   8. Live DB size vs restored DB size
#   9. Normal .sql file size vs compressed .dump file size
#
# Accuracy improvements:
#   - Excludes pg_temp_* schemas
#   - Excludes pg_toast_temp_* schemas
#   - Normalizes pg_dump schema output before comparison
#   - Avoids false positives from pg_dump timestamp/header lines
#
# Safety:
#   - Does NOT modify the live NetBox database.
#   - Creates a temporary verification database.
#   - Drops the temporary verification database at the end.
#
# Usage:
#   ./verify_netbox_dump_full_v2.sh
#
# Or:
#   ./verify_netbox_dump_full_v2.sh /path/to/netbox_postgres_xxxxx.dump
#
# Or:
#   ./verify_netbox_dump_full_v2.sh /path/to/netbox_postgres_xxxxx.dump /path/to/netbox.sql
#
# Optional:
#   KEEP_VERIFY_DB=yes ./verify_netbox_dump_full_v2.sh
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
WORK_DIR="/tmp/netbox_dump_verify_${VERIFY_DB}"

LIVE_SCHEMA_RAW="${WORK_DIR}/live_schema_raw.sql"
RESTORED_SCHEMA_RAW="${WORK_DIR}/restored_schema_raw.sql"
LIVE_SCHEMA_FILE="${WORK_DIR}/live_schema_normalized.sql"
RESTORED_SCHEMA_FILE="${WORK_DIR}/restored_schema_normalized.sql"
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

LIVE_SEQUENCES_FILE="${WORK_DIR}/live_sequences.tsv"
RESTORED_SEQUENCES_FILE="${WORK_DIR}/restored_sequences.tsv"
SEQUENCE_DIFF_FILE="${WORK_DIR}/sequence_diff.txt"

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
  CYAN="$(tput setaf 6 || true)"
  BOLD="$(tput bold || true)"
  RESET="$(tput sgr0 || true)"
else
  RED=""
  GREEN=""
  YELLOW=""
  BLUE=""
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
    REVIEW|"REVIEW NEEDED"|"NOT FOUND")
      echo "${YELLOW}${BOLD}${value}${RESET}"
      ;;
    ERROR|FAILED)
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

run_docker() {
  ${DOCKER_CMD} "$@"
}

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

  run_docker exec "${POSTGRES_CONTAINER}" \
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

status_from_file_empty() {
  local file="$1"

  if [[ -s "${file}" ]]; then
    echo "REVIEW"
  else
    echo "OK"
  fi
}

normalize_schema_dump() {
  local input_file="$1"
  local output_file="$2"

  sed \
    -e '/^--/d' \
    -e '/^$/d' \
    -e '/^\\restrict/d' \
    -e '/^\\unrestrict/d' \
    -e '/^SET transaction_timeout/d' \
    -e '/^SET statement_timeout/d' \
    -e '/^SET lock_timeout/d' \
    -e '/^SET idle_in_transaction_session_timeout/d' \
    -e '/^SET client_encoding/d' \
    -e '/^SET standard_conforming_strings/d' \
    -e '/^SELECT pg_catalog.set_config/d' \
    "${input_file}" > "${output_file}"
}

# ------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------

cleanup() {
  echo
  print_step "Cleanup"

  if [[ "${KEEP_VERIFY_DB}" == "yes" ]]; then
    print_warn "Temporary verification database was kept:"
    echo "  ${VERIFY_DB}"
  else
    run_docker exec "${POSTGRES_CONTAINER}" \
      dropdb -U "${DB_USER}" --if-exists "${VERIFY_DB}" >/dev/null 2>&1 || true

    print_ok "Temporary verification database dropped:"
    echo "  ${VERIFY_DB}"
  fi

  print_info "Evidence files:"
  echo "  ${WORK_DIR}"
}

trap cleanup EXIT
trap 'print_error "Script failed at line $LINENO. Review the error above."' ERR

# ------------------------------------------------------------
# Start
# ------------------------------------------------------------

print_header "NetBox PostgreSQL Dump Full Verification - v2"

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
  print_warn "No normal .sql file found. SQL size comparison will be N/A."
fi

# ------------------------------------------------------------
# Step 3: Check Docker/container/tools
# ------------------------------------------------------------

print_step "Checking Docker access and PostgreSQL container"

run_docker ps >/dev/null 2>&1 || die "Docker is not accessible. Try running the script with sudo."

if ! run_docker ps --format '{{.Names}}' | grep -qx "${POSTGRES_CONTAINER}"; then
  die "PostgreSQL container is not running: ${POSTGRES_CONTAINER}"
fi

print_ok "PostgreSQL container is running."

print_step "Checking PostgreSQL tools inside container"

for tool in psql pg_dump pg_restore createdb dropdb; do
  run_docker exec "${POSTGRES_CONTAINER}" sh -c "command -v ${tool} >/dev/null 2>&1" \
    || die "${tool} not found inside PostgreSQL container."
done

print_ok "Required PostgreSQL tools are available."

# ------------------------------------------------------------
# Step 4: Check live DB
# ------------------------------------------------------------

print_step "Checking live NetBox database connectivity"

run_docker exec "${POSTGRES_CONTAINER}" \
  pg_isready -U "${DB_USER}" -d "${LIVE_DB}"

print_ok "Live NetBox database is reachable."

print_step "Showing live database information"

run_docker exec "${POSTGRES_CONTAINER}" \
  psql -U "${DB_USER}" -d "${LIVE_DB}" -c "
SELECT
  current_database() AS database_name,
  pg_size_pretty(pg_database_size(current_database())) AS database_size,
  now() AS check_time,
  version() AS postgresql_version;
"

# ------------------------------------------------------------
# Step 5: Verify dump readability
# ------------------------------------------------------------

print_step "Checking dump readability"

run_docker exec -i "${POSTGRES_CONTAINER}" \
  pg_restore -l \
  < "${DUMP_FILE}" \
  > /dev/null

print_ok "Dump is readable by pg_restore."

# ------------------------------------------------------------
# Step 6: Prepare work dir and restore temp DB
# ------------------------------------------------------------

print_step "Creating evidence directory"

mkdir -p "${WORK_DIR}"

print_ok "Evidence directory created:"
echo "  ${WORK_DIR}"

print_step "Creating temporary verification database"

if [[ "${VERIFY_DB}" == "${LIVE_DB}" ]]; then
  die "Safety check failed: temporary DB name equals live DB name."
fi

run_docker exec "${POSTGRES_CONTAINER}" \
  dropdb -U "${DB_USER}" --if-exists "${VERIFY_DB}"

run_docker exec "${POSTGRES_CONTAINER}" \
  createdb -U "${DB_USER}" "${VERIFY_DB}"

print_ok "Temporary database created:"
echo "  ${VERIFY_DB}"

print_step "Restoring dump into temporary database"

run_docker exec -i "${POSTGRES_CONTAINER}" \
  pg_restore \
    -U "${DB_USER}" \
    -d "${VERIFY_DB}" \
    --no-owner \
    --no-acl \
    --exit-on-error \
  < "${DUMP_FILE}"

print_ok "Dump restored successfully into temporary database."

# ------------------------------------------------------------
# Step 7: Schema comparison
# ------------------------------------------------------------

print_step "Comparing normalized schema"

run_docker exec "${POSTGRES_CONTAINER}" \
  pg_dump -U "${DB_USER}" -d "${LIVE_DB}" -s --no-owner --no-acl \
  > "${LIVE_SCHEMA_RAW}"

run_docker exec "${POSTGRES_CONTAINER}" \
  pg_dump -U "${DB_USER}" -d "${VERIFY_DB}" -s --no-owner --no-acl \
  > "${RESTORED_SCHEMA_RAW}"

normalize_schema_dump "${LIVE_SCHEMA_RAW}" "${LIVE_SCHEMA_FILE}"
normalize_schema_dump "${RESTORED_SCHEMA_RAW}" "${RESTORED_SCHEMA_FILE}"

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

  run_docker exec "${POSTGRES_CONTAINER}" \
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
      AND n.nspname NOT LIKE 'pg_temp%'
      AND n.nspname NOT LIKE 'pg_toast_temp%'
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
# Step 8: Fingerprint comparison
# ------------------------------------------------------------

print_step "Generating table fingerprints"

generate_table_fingerprint "${LIVE_DB}" "${LIVE_FINGERPRINT_FILE}"
generate_table_fingerprint "${VERIFY_DB}" "${RESTORED_FINGERPRINT_FILE}"

print_ok "Fingerprints generated."

print_step "Comparing table fingerprints"

if diff -u "${LIVE_FINGERPRINT_FILE}" "${RESTORED_FINGERPRINT_FILE}" > "${FINGERPRINT_DIFF_FILE}"; then
  FINGERPRINT_STATUS="OK"
  print_ok "Fingerprint comparison passed."
else
  FINGERPRINT_STATUS="REVIEW"
  print_warn "Fingerprint differences detected."
fi

# ------------------------------------------------------------
# Step 9: Table, row count, checksum details
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

print_ok "Detailed comparison files prepared."

# ------------------------------------------------------------
# Function: Generate sequence state
# ------------------------------------------------------------

generate_sequences() {
  local db_name="$1"
  local output_file="$2"

  run_docker exec "${POSTGRES_CONTAINER}" \
    psql -U "${DB_USER}" -d "${db_name}" -v ON_ERROR_STOP=1 -q -c "
CREATE TEMP TABLE sequence_state (
  sequence_name text,
  last_value text,
  is_called text
);

DO \$\$
DECLARE
  r record;
  v_last text;
  v_called text;
BEGIN
  FOR r IN
    SELECT
      n.nspname AS schema_name,
      c.relname AS sequence_name
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'S'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%'
      AND n.nspname NOT LIKE 'pg_temp%'
      AND n.nspname NOT LIKE 'pg_toast_temp%'
    ORDER BY n.nspname, c.relname
  LOOP
    EXECUTE format('SELECT last_value::text, is_called::text FROM %I.%I', r.schema_name, r.sequence_name)
      INTO v_last, v_called;

    INSERT INTO sequence_state
    VALUES (r.schema_name || '.' || r.sequence_name, v_last, v_called);
  END LOOP;
END
\$\$;

COPY (
  SELECT sequence_name, last_value, is_called
  FROM sequence_state
  ORDER BY sequence_name
) TO STDOUT WITH DELIMITER E'\t';
" > "${output_file}"
}

# ------------------------------------------------------------
# Step 10: Sequence comparison
# ------------------------------------------------------------

print_step "Comparing sequence states"

generate_sequences "${LIVE_DB}" "${LIVE_SEQUENCES_FILE}"
generate_sequences "${VERIFY_DB}" "${RESTORED_SEQUENCES_FILE}"

if diff -u "${LIVE_SEQUENCES_FILE}" "${RESTORED_SEQUENCES_FILE}" > "${SEQUENCE_DIFF_FILE}"; then
  SEQUENCE_STATUS="OK"
  print_ok "Sequence comparison passed."
else
  SEQUENCE_STATUS="REVIEW"
  print_warn "Sequence differences detected."
fi

# ------------------------------------------------------------
# Step 11: Size comparison
# ------------------------------------------------------------

print_step "Calculating file and database size comparison"

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
# Step 12: Metrics
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

if [[ "${SCHEMA_STATUS}" == "OK" \
   && "${FINGERPRINT_STATUS}" == "OK" \
   && "${SEQUENCE_STATUS}" == "OK" \
   && "${TABLE_COUNT_STATUS}" == "OK" \
   && "${ONLY_LIVE_STATUS}" == "OK" \
   && "${ONLY_RESTORED_STATUS}" == "OK" \
   && "${ROWCOUNT_STATUS}" == "OK" \
   && "${CHECKSUM_STATUS}" == "OK" ]]; then
  FINAL_STATUS="MATCH"
  FINAL_NOTE="The restored dump logically matches the live NetBox database. Size checks are informational only."
else
  FINAL_STATUS="REVIEW NEEDED"
  FINAL_NOTE="Differences were detected. This may be normal if the live DB changed after the dump was created."
fi

print_ok "Final metrics calculated."

# ------------------------------------------------------------
# Step 13: Final table
# ------------------------------------------------------------

print_header "Final Comparison Summary"

printf "%-38s | %-18s | %-18s | %-18s\n" "Check item" "Live / Normal" "Restored / Dump" "Result"
printf "%-38s-+-%-18s-+-%-18s-+-%-18s\n" "--------------------------------------" "------------------" "------------------" "------------------"

printf "%-38s | %-18s | %-18s | %-18b\n" "Schema comparison" "Live schema" "Restored schema" "$(colored_status "${SCHEMA_STATUS}")"
printf "%-38s | %-18s | %-18s | %-18b\n" "Table count" "${LIVE_TABLE_COUNT}" "${RESTORED_TABLE_COUNT}" "$(colored_status "${TABLE_COUNT_STATUS}")"
printf "%-38s | %-18s | %-18s | %-18b\n" "Tables only in live DB" "${ONLY_LIVE_TABLE_COUNT}" "0" "$(colored_status "${ONLY_LIVE_STATUS}")"
printf "%-38s | %-18s | %-18s | %-18b\n" "Tables only in restored DB" "0" "${ONLY_RESTORED_TABLE_COUNT}" "$(colored_status "${ONLY_RESTORED_STATUS}")"
printf "%-38s | %-18s | %-18s | %-18b\n" "Row count differences" "${ROWCOUNT_DIFF_COUNT}" "${ROWCOUNT_DIFF_COUNT}" "$(colored_status "${ROWCOUNT_STATUS}")"
printf "%-38s | %-18s | %-18s | %-18b\n" "Checksum differences" "${CHECKSUM_DIFF_COUNT}" "${CHECKSUM_DIFF_COUNT}" "$(colored_status "${CHECKSUM_STATUS}")"
printf "%-38s | %-18s | %-18s | %-18b\n" "Table fingerprint" "Live fingerprint" "Restored fingerprint" "$(colored_status "${FINGERPRINT_STATUS}")"
printf "%-38s | %-18s | %-18s | %-18b\n" "Sequences" "Live sequences" "Restored sequences" "$(colored_status "${SEQUENCE_STATUS}")"
printf "%-38s | %-18s | %-18s | %-18b\n" "Live DB vs restored DB size" "${LIVE_DB_SIZE_HUMAN}" "${RESTORED_DB_SIZE_HUMAN}" "$(colored_status "${DB_SIZE_STATUS}")"
printf "%-38s | %-18s | %-18s | %-18s\n" "DB size difference" "${LIVE_VS_RESTORED_DB_DIFF_HUMAN}" "-" "Informational"
printf "%-38s | %-18s | %-18s | %-18b\n" "Normal SQL file size" "${NORMAL_SQL_SIZE_HUMAN}" "-" "$(colored_status "${SQL_SIZE_STATUS}")"
printf "%-38s | %-18s | %-18s | %-18s\n" "Compressed dump file size" "${DUMP_FILE_SIZE_HUMAN}" "-" "pg_dump -Fc"
printf "%-38s | %-18s | %-18s | %-18s\n" "SQL vs dump size difference" "${SQL_VS_DUMP_DIFF_HUMAN}" "-" "Compression effect"

echo
echo "------------------------------------------------------------"
echo "Final Result"
echo "------------------------------------------------------------"

if [[ "${FINAL_STATUS}" == "MATCH" ]]; then
  echo "Status : ${GREEN}${BOLD}MATCH${RESET}"
else
  echo "Status : ${YELLOW}${BOLD}REVIEW NEEDED${RESET}"
fi

echo "Notes  : ${FINAL_NOTE}"

# ------------------------------------------------------------
# Step 14: Summary file
# ------------------------------------------------------------

{
  echo "============================================================"
  echo "NetBox PostgreSQL Dump Verification Summary - v2"
  echo "============================================================"
  echo
  echo "Live database          : ${LIVE_DB}"
  echo "Restored database      : ${VERIFY_DB}"
  echo "Dump file              : ${DUMP_FILE}"
  echo "Normal SQL file        : ${NORMAL_SQL_FILE:-N/A}"
  echo "Check time             : $(date)"
  echo
  echo "------------------------------------------------------------"
  echo "Final Result"
  echo "------------------------------------------------------------"
  echo "Status : ${FINAL_STATUS}"
  echo "Notes  : ${FINAL_NOTE}"
  echo
  echo "------------------------------------------------------------"
  echo "Metrics"
  echo "------------------------------------------------------------"
  echo "Schema status                 : ${SCHEMA_STATUS}"
  echo "Table count live              : ${LIVE_TABLE_COUNT}"
  echo "Table count restored          : ${RESTORED_TABLE_COUNT}"
  echo "Tables only in live           : ${ONLY_LIVE_TABLE_COUNT}"
  echo "Tables only in restored       : ${ONLY_RESTORED_TABLE_COUNT}"
  echo "Row count differences         : ${ROWCOUNT_DIFF_COUNT}"
  echo "Checksum differences          : ${CHECKSUM_DIFF_COUNT}"
  echo "Fingerprint status            : ${FINGERPRINT_STATUS}"
  echo "Sequence status               : ${SEQUENCE_STATUS}"
  echo "Live DB size                  : ${LIVE_DB_SIZE_HUMAN}"
  echo "Restored DB size              : ${RESTORED_DB_SIZE_HUMAN}"
  echo "DB size difference            : ${LIVE_VS_RESTORED_DB_DIFF_HUMAN}"
  echo "Normal SQL file size          : ${NORMAL_SQL_SIZE_HUMAN}"
  echo "Compressed dump file size     : ${DUMP_FILE_SIZE_HUMAN}"
  echo "SQL vs dump size difference   : ${SQL_VS_DUMP_DIFF_HUMAN}"
  echo
  echo "------------------------------------------------------------"
  echo "Important Notes"
  echo "------------------------------------------------------------"
  echo "1. Final MATCH is based on logical checks, not physical database size."
  echo "2. Physical DB size may differ after restore due to PostgreSQL storage layout."
  echo "3. The .dump file is compressed, so it is normally smaller than the .sql file."
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
  echo "Live sequences               : ${LIVE_SEQUENCES_FILE}"
  echo "Restored sequences           : ${RESTORED_SEQUENCES_FILE}"
  echo "Sequence diff                : ${SEQUENCE_DIFF_FILE}"
  echo "Size comparison              : ${SIZE_COMPARISON_FILE}"
} > "${SUMMARY_FILE}"

print_ok "Summary file generated:"
echo "  ${SUMMARY_FILE}"

# ------------------------------------------------------------
# Step 15: Review details
# ------------------------------------------------------------

if [[ "${FINAL_STATUS}" != "MATCH" ]]; then
  print_header "Detailed Review Notes"

  if [[ "${SCHEMA_STATUS}" != "OK" ]]; then
    print_warn "Schema differences detected. First lines:"
    head -60 "${SCHEMA_DIFF_FILE}"
  fi

  if [[ "${ONLY_LIVE_TABLE_COUNT}" != "0" ]]; then
    echo
    print_warn "Tables only in live database:"
    cat "${ONLY_LIVE_TABLES_FILE}"
  fi

  if [[ "${ONLY_RESTORED_TABLE_COUNT}" != "0" ]]; then
    echo
    print_warn "Tables only in restored database:"
    cat "${ONLY_RESTORED_TABLES_FILE}"
  fi

  if [[ "${ROWCOUNT_DIFF_COUNT}" != "0" ]]; then
    echo
    print_warn "Row count differences:"
    head -50 "${ROWCOUNT_DIFF_FILE}"
  fi

  if [[ "${CHECKSUM_DIFF_COUNT}" != "0" ]]; then
    echo
    print_warn "Checksum differences:"
    head -50 "${CHECKSUM_DIFF_FILE}"
  fi

  if [[ "${SEQUENCE_STATUS}" != "OK" ]]; then
    echo
    print_warn "Sequence differences:"
    head -50 "${SEQUENCE_DIFF_FILE}"
  fi
fi

# ------------------------------------------------------------
# Step 16: End
# ------------------------------------------------------------

print_header "Verification Completed"

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
