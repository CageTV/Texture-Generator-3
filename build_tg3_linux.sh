#!/usr/bin/env bash
# build_tg3_linux.sh -- build the Linux TG3 executable.
set -e
cd "$(dirname "$0")"

echo "Checking system packages (apt)..."
MISSING=()
command -v python3 >/dev/null || MISSING+=("python3")
python3 -c "import tkinter" 2>/dev/null || MISSING+=("python3-tk")
command -v magick >/dev/null || command -v convert >/dev/null || MISSING+=("imagemagick")
command -v nvcompress >/dev/null || MISSING+=("nvidia-texture-tools")
command -v wine >/dev/null || MISSING+=("wine")

if [ ${#MISSING[@]} -ne 0 ]; then
    echo "Missing system packages: ${MISSING[*]}"
    echo "Install with:  sudo apt install ${MISSING[*]}"
    echo "(nvidia-texture-tools/imagemagick/wine are only needed for DDS"
    echo " conversion / BSA packing respectively -- everything else works"
    echo " without them.)"
    read -p "Continue anyway? [y/N] " ans
    [ "$ans" = "y" ] || [ "$ans" = "Y" ] || exit 1
fi

echo "Installing/verifying Python dependencies..."
python3 -m pip install --upgrade pillow numpy opencv-python moderngl glcontext pyinstaller

echo
echo "Building TG3 from TG3_linux.spec..."
python3 -m PyInstaller TG3_linux.spec

echo
echo "Done. Output is in dist/TG3"
echo "Run it with: ./dist/TG3"
