#!/usr/bin/env bash
# Builds Skiff.app — a thin macOS bundle that launches the Python app.
# The bundle is NOT self-contained: it runs Python from PYTHON_BIN below and
# uses the project files in REPO_DIR. Move the project, rebuild.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="Skiff"
APP_DIR="${REPO_DIR}/dist/${APP_NAME}.app"
PYTHON_BIN="${PYTHON_BIN:-/opt/anaconda3/bin/python3}"
ICON_PNG="${REPO_DIR}/assets/icon_1024.png"
ICON_ICNS="${REPO_DIR}/assets/AppIcon.icns"

if [ ! -x "$PYTHON_BIN" ]; then
    echo "error: PYTHON_BIN not found or not executable: $PYTHON_BIN" >&2
    echo "set PYTHON_BIN=/path/to/python3 and re-run." >&2
    exit 1
fi

# ---- 1. ensure icon PNG exists; render if missing
if [ ! -f "$ICON_PNG" ]; then
    echo "icon PNG missing — running tools/make_icon.py"
    "$PYTHON_BIN" "${REPO_DIR}/tools/make_icon.py"
fi

# ---- 2. build .icns from PNG (sips + iconutil are macOS built-ins)
ICONSET_DIR="${REPO_DIR}/assets/AppIcon.iconset"
rm -rf "$ICONSET_DIR"
mkdir -p "$ICONSET_DIR"
sips -z 16   16   "$ICON_PNG" --out "$ICONSET_DIR/icon_16x16.png"     >/dev/null
sips -z 32   32   "$ICON_PNG" --out "$ICONSET_DIR/icon_16x16@2x.png"  >/dev/null
sips -z 32   32   "$ICON_PNG" --out "$ICONSET_DIR/icon_32x32.png"     >/dev/null
sips -z 64   64   "$ICON_PNG" --out "$ICONSET_DIR/icon_32x32@2x.png"  >/dev/null
sips -z 128  128  "$ICON_PNG" --out "$ICONSET_DIR/icon_128x128.png"   >/dev/null
sips -z 256  256  "$ICON_PNG" --out "$ICONSET_DIR/icon_128x128@2x.png">/dev/null
sips -z 256  256  "$ICON_PNG" --out "$ICONSET_DIR/icon_256x256.png"   >/dev/null
sips -z 512  512  "$ICON_PNG" --out "$ICONSET_DIR/icon_256x256@2x.png">/dev/null
sips -z 512  512  "$ICON_PNG" --out "$ICONSET_DIR/icon_512x512.png"   >/dev/null
cp "$ICON_PNG"                     "$ICONSET_DIR/icon_512x512@2x.png"
iconutil -c icns "$ICONSET_DIR" -o "$ICON_ICNS"
rm -rf "$ICONSET_DIR"

# ---- 3. (re)build the .app bundle
echo "Building $APP_DIR"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"
cp "$ICON_ICNS" "$APP_DIR/Contents/Resources/AppIcon.icns"

cat > "$APP_DIR/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleExecutable</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>com.nadavkunievsky.skiff</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>CFBundleVersion</key>
    <string>0.1.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
</dict>
</plist>
EOF

cat > "$APP_DIR/Contents/MacOS/${APP_NAME}" <<EOF
#!/usr/bin/env bash
set -e
exec "${PYTHON_BIN}" "${REPO_DIR}/run.py" "\$@"
EOF
chmod +x "$APP_DIR/Contents/MacOS/${APP_NAME}"

# Force Finder/Launch Services to refresh the bundle metadata.
touch "$APP_DIR"

echo
echo "Built $APP_DIR"
echo "Drag it to /Applications, or run:  open '$APP_DIR'"
echo
echo "It points to:"
echo "  python: $PYTHON_BIN"
echo "  script: $REPO_DIR/run.py"
echo "  icon:   $ICON_ICNS"
