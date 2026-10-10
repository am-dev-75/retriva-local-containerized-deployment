#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# ingest_crm.sh — ingest spreadsheets into a Retriva KB with a CRM tag.
#
# Uploads one or more files to the Gateway's document-upload endpoint with
# user_metadata carrying the KB membership list and a user-defined tag.
# Equivalent to the WebUI upload flow (which sends kb_ids + tag inside
# user_metadata) — usable for scripted/bulk ingestion without the UI.
#
# Usage:
#   ./ingest_crm.sh -k KB_ID -t TAG [-g GATEWAY_URL] [-f] FILE [FILE...]
#
# Examples:
#   ./ingest_crm.sh -k dept-sales -t dept_sales_potential_customer \
#       2026-05-15-dave-eu-final-v2-companies-only.xlsx \
#       2026-08-04-sps-italia-dave-companies-review-v8.xlsx
#
#   ./ingest_crm.sh -k dept-sales -t dept_sales_offering -f offering.xlsx
#     (-f = force re-ingest even if the file is unchanged)
#
# Note: uploads go DIRECTLY to the Core ingestion API (default
# http://localhost:8200) because the Gateway has no multipart upload
# route.  Override with UPLOAD_URL env var if your port differs.
# -----------------------------------------------------------------------------
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8202}"
# Upload endpoint. The Gateway proxies documents list/search/delete but has
# NO multipart upload route (405), so uploads go directly to Core's
# ingestion API (host port INGESTION_PORT, default 8200).
UPLOAD_URL="${UPLOAD_URL:-${GATEWAY_URL/8202/8200}}"
KB_ID=""
TAG=""
FORCE="false"
FILES=()

usage() {
  sed -n '2,20p' "$0"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -k) KB_ID="${2:-}"; shift 2 ;;
    -t) TAG="${2:-}"; shift 2 ;;
    -g) GATEWAY_URL="${2:-}"; shift 2 ;;
    -f) FORCE="true"; shift ;;
    -h|--help) usage ;;
    -*) echo "ERROR: unknown option $1" >&2; usage ;;
    *)  FILES+=("$1"); shift ;;
  esac
done

[[ -n "$KB_ID" ]] || { echo "ERROR: -k KB_ID is required" >&2; usage; }
[[ -n "$TAG"    ]] || { echo "ERROR: -t TAG is required" >&2; usage; }
[[ ${#FILES[@]} -gt 0 ]] || { echo "ERROR: at least one file is required" >&2; usage; }

# Label only when force is actually enabled (FORCE is "true"/"false").
FORCE_LABEL=""
[[ "$FORCE" == "true" ]] && FORCE_LABEL=" [force]"

for f in "${FILES[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "ERROR: file not found: $f" >&2
    exit 1
  fi
  fname="$(basename "$f")"
  echo "==> Uploading ${fname} → KB '${KB_ID}' (tag: ${TAG})${FORCE_LABEL}"
  curl -sS -X POST "${UPLOAD_URL}/api/v2/documents/upload" \
    -F "file=@${f}" \
    -F "source_path=${fname}" \
    -F "user_metadata={\"kb_ids\": [\"${KB_ID}\"], \"type\": \"${TAG}\"}" \
    -F "force=${FORCE}"
  echo
done

echo "==> Accepted. Ingestion runs asynchronously."
echo "    Monitor:  docker logs -f \$(docker ps -qf name=retriva-ingestion) \\"
echo "              | grep -E 'file_hash_computed|crm_companies_synced|Table paragraph|completed'"
