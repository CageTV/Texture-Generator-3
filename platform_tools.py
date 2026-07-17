"""
platform_tools.py

Cross-platform layer for the three OS-specific pieces of TG3:
  - DDS <-> other-format conversion  (texconv.exe on Windows;
    nvcompress + ImageMagick on Linux)
  - BSA/BA2 packing/unpacking        (BSArch.exe directly on Windows;
    via Wine on Linux)
  - "reveal in file manager"         (os.startfile / xdg-open / open)

Every other module should go through here instead of calling texconv.exe /
BSArch.exe / os.startfile directly, so platform differences live in one place.

Linux system packages needed (apt):
    python3-tk  imagemagick  nvidia-texture-tools  wine

Note on fidelity: nvcompress's format flags are the closest available match
to texconv's DXGI format strings, not a byte-identical reimplementation --
compression quality/output size may differ slightly. If output looks wrong,
run `nvcompress --help` on the target machine and check _NVCOMPRESS_FLAGS
below against the installed version, since flags have changed across
nvidia-texture-tools releases.
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

IS_WINDOWS = sys.platform.startswith('win')
IS_LINUX = sys.platform.startswith('linux')
IS_MAC = sys.platform == 'darwin'


def resource_path(rel):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


# ═════════════════════════════════════════════════════════════════════════
# texconv (Windows) / nvcompress + ImageMagick (Linux)
# ═════════════════════════════════════════════════════════════════════════

def _texconv_exe():
    return resource_path('texconv.exe')


def texconv_available():
    """Whether DDS conversion is usable at all on this platform/machine."""
    if IS_WINDOWS:
        return os.path.isfile(_texconv_exe())
    return bool(_linux_decoder()) and bool(shutil.which('nvcompress'))


def _linux_decoder():
    return shutil.which('magick') or shutil.which('convert')


# DXGI format string (as used throughout this app) -> nvcompress flag.
# Approximate mapping -- see module docstring.
_NVCOMPRESS_FLAGS = {
    'BC1_UNORM': '-bc1', 'BC1_UNORM_SRGB': '-bc1',
    'BC3_UNORM': '-bc3', 'BC3_UNORM_SRGB': '-bc3',
    'BC4_UNORM': '-bc4', 'BC4_SNORM': '-bc4',
    'BC5_UNORM': '-bc5', 'BC5_SNORM': '-bc5',
    'BC7_UNORM': '-bc7', 'BC7_UNORM_SRGB': '-bc7',
    'R8G8B8A8_UNORM': '-rgb', 'R8G8B8A8_UNORM_SRGB': '-rgb',
}


def dds_decode(src, out_dir):
    """DDS/any -> PNG. Returns the PNG path, or None on failure.
    texconv on Windows; ImageMagick on Linux (nvcompress has no decoder)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / (Path(src).stem + '.png')

    if IS_WINDOWS:
        tc = _texconv_exe()
        if not os.path.isfile(tc):
            return None
        subprocess.run([tc, '-nologo', '-y', '-ft', 'png', '-o', str(out_dir), str(src)],
                        capture_output=True)
        return str(out) if out.exists() else None
    else:
        magick = _linux_decoder()
        if not magick:
            return None
        subprocess.run([magick, str(src), str(out)], capture_output=True)
        return str(out) if out.exists() else None


def dds_encode(src, dst, dxgi_format):
    """any -> DDS at dst, given a DXGI-style format string (e.g. 'BC7_UNORM').
    texconv on Windows; nvcompress on Linux."""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if IS_WINDOWS:
        tc = _texconv_exe()
        if not os.path.isfile(tc):
            return False
        subprocess.run([tc, '-nologo', '-y', '-m', '0', '-bc', 'd',
                        '-f', dxgi_format, '-o', str(dst.parent), str(src)],
                        capture_output=True)
        expected = dst.parent / (Path(src).stem + '.dds')
        if expected.exists() and expected != dst:
            shutil.move(str(expected), str(dst))
        return dst.exists()
    else:
        nvcompress = shutil.which('nvcompress')
        if not nvcompress:
            return False
        flag = _NVCOMPRESS_FLAGS.get(dxgi_format, '-bc7')
        subprocess.run([nvcompress, flag, str(src), str(dst)], capture_output=True)
        return dst.exists()


# ═════════════════════════════════════════════════════════════════════════
# BSArch (Windows binary; run through Wine on Linux)
# ═════════════════════════════════════════════════════════════════════════

def _bsarch_exe():
    return resource_path('BSArch.exe')


def bsarch_available():
    if IS_WINDOWS:
        return os.path.isfile(_bsarch_exe())
    return os.path.isfile(_bsarch_exe()) and bool(shutil.which('wine'))


def bsarch_command(args):
    """Build the full subprocess argv for a BSArch invocation, wrapping with
    Wine on Linux. `args` is the list of BSArch-specific arguments (no exe
    path, no 'wine')."""
    exe = _bsarch_exe()
    if IS_WINDOWS:
        return [exe] + list(args)
    return ['wine', exe] + list(args)


# ═════════════════════════════════════════════════════════════════════════
# "Reveal in file manager"
# ═════════════════════════════════════════════════════════════════════════

def open_in_file_manager(path):
    try:
        if IS_WINDOWS:
            os.startfile(path)  # noqa: only reached on Windows
        elif IS_MAC:
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception:
        pass
