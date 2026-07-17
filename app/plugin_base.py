"""
Plugin contract for the TG3 GL shell.

Every add-on tab is a subclass of TG3Plugin placed in the `plugins/` folder.
The plugin manager auto-discovers and instantiates one instance of each
subclass it finds there, sorted by (order, name). This is the seam every
future integration (Blender bridge, Upscayl bridge, mesh viewer, your own
tools) plugs into - nothing about the app shell itself needs to change to
add a new one.
"""
from __future__ import annotations
from abc import ABC, abstractmethod


class TG3Plugin(ABC):
    # Shown in the tab bar as "{icon}  {name}".
    name: str = "Unnamed Plugin"
    icon: str = "\u2b21"
    # Lower order = further left in the tab bar. Existing tabs occupy
    # 10-90 roughly in their original left-to-right order; leave gaps
    # so new plugins can slot in between without renumbering everything.
    order: int = 100

    def __init__(self, app):
        # `app` is the shared AppState: input/output folders, the live
        # moderngl context (app.gl), and a shared log (app.log(...)).
        self.app = app

    def on_activate(self):
        """Called once, the first time this plugin's tab is selected.
        Good place for expensive one-time setup you don't want paid for
        by plugins the user never opens."""
        pass

    @abstractmethod
    def gui(self):
        """Called every frame while this plugin's tab is the active tab.
        Draw ImGui widgets here (imgui.button(...), imgui.slider_float(...),
        etc.) - this is the plugin's entire UI."""
        raise NotImplementedError

    def on_shutdown(self):
        """Called once when the app is closing. Release GL resources
        (textures, VAOs, framebuffers, programs) here."""
        pass
