#!/usr/bin/env bash
# Installs, updates or removes the OpenCreator Send plugin for OrcaSlicer (Linux and macOS).
#   ./install.sh                      install or update from this checkout
#   curl -fsSL https://raw.githubusercontent.com/wuyscc/opencreator-send/main/install.sh | bash
set -euo pipefail

REPO="wuyscc/opencreator-send"
FILE="opencreator_send_any.py"
REF="main"
DIR="${ORCA_DIR:-}"
MODE="install"
PURGE=0

usage() {
    cat <<USAGE
Usage: install.sh [options]
  --dir PATH    OrcaSlicer data folder (Help > Show Configuration Folder). Also ORCA_DIR.
  --ref REF     branch or tag to download when not run from a checkout (default: main)
  --check       report what would change, change nothing
  --uninstall   remove the plugin (keeps your saved options and log)
  --purge       with --uninstall, also remove saved options and log
  -h, --help    this help
USAGE
}

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) [ $# -ge 2 ] || die "--dir needs a path"; DIR="$2"; shift ;;
        --ref) [ $# -ge 2 ] || die "--ref needs a value"; REF="$2"; shift ;;
        --check) MODE="check" ;;
        --uninstall) MODE="uninstall" ;;
        --purge) PURGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

find_orca_dir() {
    local candidate
    case "$(uname -s)" in
        Darwin) set -- "$HOME/Library/Application Support/OrcaSlicer" ;;
        *) set -- "${XDG_CONFIG_HOME:-$HOME/.config}/OrcaSlicer" \
                  "$HOME/.var/app/io.github.softfever.OrcaSlicer/config/OrcaSlicer" ;;
    esac
    for candidate in "$@"; do
        if [ -d "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

plugin_version() {
    sed -n 's/^# version = "\(.*\)"$/\1/p' "$1" | head -n 1
}

fetch() {
    local url="https://raw.githubusercontent.com/$REPO/$REF/$FILE"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$1" || die "couldn't download $url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q "$url" -O "$1" || die "couldn't download $url"
    else
        die "need curl or wget to download the plugin"
    fi
}

if [ -z "$DIR" ]; then
    DIR="$(find_orca_dir)" || die "couldn't find OrcaSlicer's data folder. Start Orca once, or pass --dir PATH (Help > Show Configuration Folder)."
fi
[ -d "$DIR" ] || die "not a folder: $DIR"
TARGET_DIR="$DIR/orca_plugins/opencreator_send"
TARGET="$TARGET_DIR/$FILE"

if [ "$MODE" = "uninstall" ]; then
    if [ ! -f "$TARGET" ]; then
        echo "Not installed in $TARGET_DIR"
        exit 0
    fi
    rm -f "$TARGET" "$TARGET.bak"
    if [ "$PURGE" = 1 ]; then
        rm -f "$TARGET_DIR/opencreator_send_prefs.json" "$TARGET_DIR/opencreator_send.log"
    fi
    rmdir "$TARGET_DIR" 2>/dev/null || true
    echo "Removed OpenCreator Send from $TARGET_DIR"
    echo "Restart OrcaSlicer, and clear the slicing pipeline plugin setting in your Creator 5 print preset."
    exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -f "$HERE/$FILE" ]; then
    SOURCE="$HERE/$FILE"
else
    fetch "$WORK/$FILE"
    SOURCE="$WORK/$FILE"
fi
grep -q 'tool.orcaslicer.plugin' "$SOURCE" || die "$SOURCE doesn't look like the plugin"

NEW="$(plugin_version "$SOURCE")"
if [ ! -f "$TARGET" ]; then
    ACTION="install"
    SUMMARY="installed ${NEW:-unknown version}"
elif cmp -s "$SOURCE" "$TARGET"; then
    ACTION="none"
    SUMMARY="already up to date (${NEW:-unknown version})"
else
    ACTION="update"
    OLD="$(plugin_version "$TARGET")"
    SUMMARY="updated ${OLD:-unknown} -> ${NEW:-unknown}"
fi

if [ "$MODE" = "check" ]; then
    case "$ACTION" in
        install) echo "Would install ${NEW:-unknown version} into $TARGET_DIR" ;;
        update) echo "Would update ${OLD:-unknown} -> ${NEW:-unknown} in $TARGET_DIR" ;;
        none) echo "Already up to date (${NEW:-unknown version}) in $TARGET_DIR" ;;
    esac
    exit 0
fi

if [ "$ACTION" != "none" ]; then
    mkdir -p "$TARGET_DIR"
    [ "$ACTION" = "update" ] && cp "$TARGET" "$TARGET.bak"
    cp "$SOURCE" "$TARGET.new"
    mv "$TARGET.new" "$TARGET"
fi
echo "OpenCreator Send $SUMMARY ($TARGET)"

if pgrep -ix 'orca-?slicer' >/dev/null 2>&1; then
    echo "OrcaSlicer is running: restart it to load the new plugin."
fi
if [ "$ACTION" = "install" ]; then
    cat <<NEXT

Next:
  1. Restart OrcaSlicer and enable OpenCreator Send under File > Plugins.
  2. In your Creator 5 print preset, add it to the slicing pipeline plugin setting (Others).
  3. Set the printer connection to your Moonraker address.
  4. Turn off "Use 3MF instead of G-code" under Printer settings > Basic information.
Run this script again any time to update.
NEXT
fi
