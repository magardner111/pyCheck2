#!/bin/bash
# build_dmg.sh - Build pyCheck2.app + DMG using python-build-standalone
set -e

APP_NAME="pyCheck2"
APP_BUNDLE="${APP_NAME}.app"
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260414/cpython-3.11.15%2B20260414-aarch64-apple-darwin-install_only_stripped.tar.gz"
BUILD_DIR="$(pwd)/build"
APP_DIR="${BUILD_DIR}/${APP_BUNDLE}"

echo "==> Cleaning build dir"
rm -rf "${BUILD_DIR}"
mkdir -p "${APP_DIR}/Contents/MacOS"
mkdir -p "${APP_DIR}/Contents/Resources"

# ---------------------------------------------------------------------------
# 1. Download + unpack standalone Python
# ---------------------------------------------------------------------------
echo "==> Downloading python-build-standalone..."
curl -L "${PYTHON_URL}" -o "${BUILD_DIR}/python.tar.gz"

echo "==> Unpacking Python..."
tar -xf "${BUILD_DIR}/python.tar.gz" -C "${BUILD_DIR}"
mv "${BUILD_DIR}/python" "${APP_DIR}/Contents/MacOS/python-runtime"
rm "${BUILD_DIR}/python.tar.gz"

PYTHON="${APP_DIR}/Contents/MacOS/python-runtime/bin/python3"

# ---------------------------------------------------------------------------
# 2. Install PySide6 into the bundled Python
# ---------------------------------------------------------------------------
echo "==> Installing PySide6..."
"${PYTHON}" -m pip install --quiet PySide6

# Strip unused Qt modules and bloat to save space
echo "==> Stripping unused Qt modules..."
SITE_PACKAGES=$("${PYTHON}" -c "import site; print(site.getsitepackages()[0])")
PYSIDE="${SITE_PACKAGES}/PySide6"

# Keep only what we actually use:
#   QtCore, QtGui, QtWidgets, QtPrintSupport, QtNetwork (PySide6 internals need Network)
KEEP="QtCore QtGui QtWidgets QtPrintSupport QtNetwork"

for item in "${PYSIDE}"/Qt*.so "${PYSIDE}"/Qt*.abi3.so; do
    base=$(basename "${item}" | sed 's/\..*$//')
    keep=false
    for k in $KEEP; do [ "${base}" = "${k}" ] && keep=true && break; done
    $keep || rm -f "${item}" 2>/dev/null
done

# Remove entire unused framework bundles inside Qt/lib
QT_LIB="${PYSIDE}/Qt/lib"
for framework in "${QT_LIB}"/Qt*.framework; do
    name=$(basename "${framework}" .framework)
    keep=false
    for k in $KEEP; do [ "${name}" = "${k}" ] && keep=true && break; done
    $keep || rm -rf "${framework}" 2>/dev/null
done

# Remove Qt plugins we don't need
QT_PLUGINS="${PYSIDE}/Qt/plugins"
for plugin in "${QT_PLUGINS}"/*/; do
    pname=$(basename "${plugin}")
    case "${pname}" in
        platforms|styles|printsupport|imageformats) ;;  # keep
        *) rm -rf "${plugin}" ;;
    esac
done

# Remove translations, examples, qml, resources
rm -rf "${PYSIDE}/Qt/translations" 2>/dev/null || true
rm -rf "${PYSIDE}/Qt/qml" 2>/dev/null || true
rm -rf "${PYSIDE}/Qt/metatypes" 2>/dev/null || true

# Remove .pyi stub files (type hints, not needed at runtime)
find "${PYSIDE}" -name "*.pyi" -delete

# Remove __pycache__ throughout
find "${APP_DIR}" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------------------
# 3. Copy app script
# ---------------------------------------------------------------------------
echo "==> Copying app..."
cp ycheck_printer.py "${APP_DIR}/Contents/MacOS/"

# ---------------------------------------------------------------------------
# 4. Launcher script
# ---------------------------------------------------------------------------
cat > "${APP_DIR}/Contents/MacOS/${APP_NAME}" << 'LAUNCHER'
#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
export MVK_CONFIG_LOG_LEVEL=0
exec "${DIR}/python-runtime/bin/python3" "${DIR}/ycheck_printer.py"
LAUNCHER
chmod +x "${APP_DIR}/Contents/MacOS/${APP_NAME}"

# ---------------------------------------------------------------------------
# 5. Info.plist
# ---------------------------------------------------------------------------
cat > "${APP_DIR}/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>pyCheck2</string>
  <key>CFBundleDisplayName</key>
  <string>pyCheck2 Printer</string>
  <key>CFBundleIdentifier</key>
  <string>com.psg.pycheck2</string>
  <key>CFBundleVersion</key>
  <string>1.0</string>
  <key>CFBundleExecutable</key>
  <string>pyCheck2</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
</dict>
</plist>
PLIST

# ---------------------------------------------------------------------------
# 6. Package into DMG
# ---------------------------------------------------------------------------
echo "==> Creating DMG..."
DMG_PATH="${BUILD_DIR}/${APP_NAME}.dmg"
hdiutil create \
    -volname "${APP_NAME}" \
    -srcfolder "${APP_DIR}" \
    -ov \
    -format UDZO \
    "${DMG_PATH}"

echo ""
echo "==> Done: ${DMG_PATH}"
du -sh "${DMG_PATH}"
