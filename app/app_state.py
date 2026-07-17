"""
Shared application state handed to every plugin's constructor.

The important piece is `gl`: a single moderngl context that WRAPS the GL
context the app window already owns, created lazily on first access from
inside the render loop. This is the key difference from the Tkinter version:
gpu_engine.py / gpu_preview.py / ibl_preview.py each called
moderngl.create_standalone_context() because Tkinter can't host a live GL
surface, so every render had to happen off-screen and get copied back as a
PIL image. Here there's already a live GL context every frame - plugins
render straight into it (or an FBO inside it) and display the result with
imgui.image(), no PIL round-trip, no re-render-on-click.
"""
import os
import time
from collections import deque

import moderngl


class LogEntry:
    __slots__ = ("text", "level", "t")

    def __init__(self, text, level="info"):
        self.text = text
        self.level = level
        self.t = time.time()


class AppState:
    def __init__(self, project_root):
        self.project_root = project_root
        self.input_dir = os.getcwd()
        self.output_dir = os.getcwd()
        self._gl = None
        self.log_entries = deque(maxlen=2000)
        self.active_plugin_index = -1

    @property
    def gl(self) -> moderngl.Context:
        """Only touch this from inside a plugin's gui()/on_activate() - i.e.
        from inside the render loop. No GL context exists before the window
        is created, so accessing this at import time or in __init__ will fail."""
        if self._gl is None:
            self._gl = moderngl.create_context()
        return self._gl

    def log(self, text, level="info"):
        self.log_entries.append(LogEntry(text, level))
        print(f"[{level}] {text}")

    def resource_path(self, rel):
        return os.path.join(self.project_root, rel)
