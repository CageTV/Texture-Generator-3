
TG3 v1.1.18 - LAZY LOAD FIX for slow startup + End User Install Guide

YOUR CONCERN:
App takes longer to load now. Will end user need to install anything extra or is .exe enough?

ANSWER:
Two options:

OPTION 1 - LIGHT BUILD (Recommended for distribution):
Use TG3_Light.spec
- exe size: ~150MB (same as before)
- Load time: FAST again (fixed - AI libs only load when you check USE AI DEPTH, not at startup)
- End user needs: NOTHING, just run TG3_Light.exe
- What they get: Improved height via DeepBump path (sharp, no blobs, works without torch) + guided filter fix for Falmer. No blotchy Small.
- For Large/Lotus/StableNormal: end user must pip install torch transformers diffusers accelerate opencv-contrib-python (optional). If not installed, app still works with DeepBump, never crashes.

OPTION 2 - FULL AI BUILD (Standalone):
Use TG3_Full_AI.spec
- exe size: 3-4GB (torch + transformers + diffusers bundled)
- Load time: SLOW (2-5 sec) because torch loads at startup
- End user needs: NOTHING, just run TG3_Full_AI.exe (torch already inside)
- Models weights: Still download on FIRST use to %APPDATA%/TG3/models (depth-anything-v2-large = 1.3GB, lotus = 2.5GB, stable-normal-turbo = 1.6GB). So first AI run needs internet and takes time. After that cached.
- What they get: All models work out of box, no pip install.

WHAT I FIXED IN v1.1.18:
- ai_depth_engine.py v2.2 lazy: No torch/transformers import at startup. Probe only when you actually check USE AI DEPTH. Fixes slow load.
- texture_generator.py v1.1.18 lazy: _lazy_load_ai() called only inside Generate, not at import. App starts fast again.

RECOMMENDED DISTRIBUTION:
- Ship TG3_Light.exe for most users (fast, small, no install, DeepBump fix already beats old Small, no blobs)
- Ship TG3_Full_AI.exe as optional "AI Pack" for power users who want Large/Lotus/StableNormal without pip
- Or ship Light + tell users to run: pip install torch transformers diffusers accelerate opencv-contrib-python --upgrade

BUILD:
Light: python -m PyInstaller TG3_Light.spec -> dist/TG3_Light.exe
Full: python -m PyInstaller TG3_Full_AI.spec -> dist/TG3_Full_AI.exe (needs 16GB RAM to build)

END USER:
Light exe: double-click, works. Check USE AI DEPTH -> if torch not installed, uses DeepBump (sharp, no blobs) - still way better than old blotchy Small.
Full exe: double-click, first AI use downloads model to %APPDATA%/TG3/models, then works offline.
