#!/bin/bash
# Zips each Lambda function file ready for manual upload to AWS console.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAMBDAS_DIR="$SCRIPT_DIR/lambdas"
OUT_DIR="$SCRIPT_DIR/function_zips"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

for fn in webhook_handler reminder_checker sync_to_sheets sheets_to_dynamo optout_handler; do
    zip -j "$OUT_DIR/${fn}.zip" "$LAMBDAS_DIR/${fn}.py" --quiet
    echo "Built: $OUT_DIR/${fn}.zip"
done

echo ""
echo "=== Done ==="
echo "Upload each zip manually in the AWS console:"
echo "  Lambda → Functions → [function name] → Code → Upload from → .zip file"
