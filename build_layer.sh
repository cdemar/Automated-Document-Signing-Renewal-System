#!/bin/bash
# Builds and uploads the church-forms-dependencies Lambda Layer.
# Run from anywhere — paths are relative to this script's location.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/layer_build"
ZIP_PATH="$SCRIPT_DIR/church-forms-dependencies.zip"
REGION="${AWS_REGION:-us-east-2}"
LAYER_NAME="${LAYER_NAME:-church-forms-dependencies}"

echo "=== Cleaning previous build ==="
rm -rf "$BUILD_DIR" "$SCRIPT_DIR/venv"
mkdir -p "$BUILD_DIR/python"

echo "=== Creating virtual environment ==="
python3 -m venv "$SCRIPT_DIR/venv"
source "$SCRIPT_DIR/venv/bin/activate"

echo "=== Installing Python packages (Linux x86_64 binaries for Lambda) ==="
pip install \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version 3.13 \
    --only-binary=:all: \
    --target "$BUILD_DIR/python" \
    google-api-python-client google-auth python-dateutil

deactivate
rm -rf "$SCRIPT_DIR/venv"
echo "Virtual environment cleaned up"

echo "=== Copying shared modules ==="
cp "$SCRIPT_DIR/lambdas/sheets_helper.py"   "$BUILD_DIR/python/"
cp "$SCRIPT_DIR/lambdas/signing_platform.py" "$BUILD_DIR/python/"
cp "$SCRIPT_DIR/lambdas/optout_token.py"     "$BUILD_DIR/python/"

echo "=== Zipping layer ==="
cd "$BUILD_DIR"
zip -r "$ZIP_PATH" python --quiet
echo ""
echo "=== Done ==="
echo "Layer zip ready: $ZIP_PATH ($(du -sh "$ZIP_PATH" | cut -f1))"
echo ""
echo "Upload this zip manually in the AWS console:"
echo "  1. Go to AWS Lambda → Layers → Create layer"
echo "  2. Name: church-forms-dependencies"
echo "  3. Upload the zip file above"
echo "  4. Compatible runtime: Python 3.13"
echo "  5. Click Create"
echo ""
echo "Then update all 5 Lambda functions to use the new layer version."
