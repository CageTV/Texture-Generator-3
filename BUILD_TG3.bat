@echo off
REM pushd handles UNC/network-share paths (like \\SPARK\Share\...) by mapping
REM a temporary drive letter -- plain "cd" can't set a UNC path as the working
REM directory and silently falls back to C:\Windows instead.
pushd "%~dp0"

echo Building TG3 (GPU-accelerated) ...
echo.
echo Make sure a "shaders\normal.comp" file exists next to this .bat before building.
echo.

if not exist "shaders\normal.comp" (
    echo [!] shaders\normal.comp not found - creating shaders\ and copying it in.
    if not exist "shaders" mkdir shaders
    copy /Y "normal.comp" "shaders\normal.comp" >nul
)

pip install pillow numpy pyinstaller moderngl glcontext opencv-python-headless

python -m PyInstaller TG3.spec --noconfirm

echo.
echo Build complete. Check dist\TG3.exe
echo Ship dist\TG3.exe together with texconv.exe and BSArch.exe in the same folder.
popd
pause
