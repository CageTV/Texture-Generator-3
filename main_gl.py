"""
main_gl.py - TG3's new OpenGL / Dear ImGui application shell.

This is a NEW, separate entry point that lives next to texture_generator.py -
it does not replace or modify that file, which still runs standalone exactly
as before. This is Step 1 of the plan: a GL-native shell + plugin system that
texture_generator.py's tabs get migrated onto one at a time, and that future
integrations (Blender bridge, Upscayl bridge, mesh viewer, PyTorch-backed
tools) plug into the same way.

Run:      python main_gl.py
Requires: see requirements_gl_shell.txt (imgui-bundle, moderngl, pillow, numpy)

Adding a plugin: drop a .py file in plugins/ defining a TG3Plugin subclass
(see app/plugin_base.py and PLUGIN_GUIDE.md). It's auto-discovered on
startup - no changes to this file needed.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from imgui_bundle import imgui, immapp, hello_imgui

from app.app_state import AppState
from app.plugin_manager import discover_plugins
from app.theme import apply_dark_theme
from app.file_dialogs import pick_folder

try:
    from version import VERSION
except Exception:
    VERSION = "1.1.6"

PLUGINS_DIR = PROJECT_ROOT / "plugins"

app_state = AppState(str(PROJECT_ROOT))
plugins = []
_initialized = False


def _lazy_init():
    """Runs once, on the first frame - deferred because it needs a live GL
    context (for the theme's font/style setup and for plugins that touch
    app_state.gl in on_activate) which doesn't exist until the window does."""
    global _initialized
    if _initialized:
        return
    apply_dark_theme()
    PLUGINS_DIR.mkdir(exist_ok=True)
    plugins[:] = discover_plugins(app_state, PLUGINS_DIR)
    if not plugins:
        app_state.log("No plugins found in plugins/ - see PLUGIN_GUIDE.md", "warn")
    _initialized = True


def _folder_row(label, get_val, set_val, dialog_key):
    imgui.push_item_width(-90)
    changed, val = imgui.input_text(f"##{label}", get_val(), 1024)
    if changed:
        set_val(val)
    imgui.pop_item_width()
    imgui.same_line()
    if imgui.button(f"Browse##{dialog_key}"):
        d = pick_folder(get_val())
        if d:
            set_val(d)
    imgui.same_line()
    imgui.text(label)


def _top_bar():
    imgui.text("TEXTURE GENERATOR 3")
    imgui.same_line()
    imgui.text_disabled(f" v{VERSION}")
    imgui.same_line()
    imgui.text_disabled(" |  GL shell (preview)")
    imgui.separator()

    _folder_row("Input Folder", lambda: app_state.input_dir,
                lambda v: setattr(app_state, "input_dir", v), "in")
    _folder_row("Output Folder", lambda: app_state.output_dir,
                lambda v: setattr(app_state, "output_dir", v), "out")
    imgui.separator()

    # Plugin load status - visible in-app, not just printed to a console
    # window you may not have open. This is the first place to look if a
    # tab you expect to see (Upscayl, Pinta, anything in plugins/) isn't
    # showing up in the tab bar below.
    errors = [e for e in app_state.log_entries if e.level == "error"]
    warns = [e for e in app_state.log_entries if e.level == "warn"]
    names = ", ".join(p.name for p in plugins) if plugins else "(none)"
    imgui.text_disabled(f"{len(plugins)} plugin(s) loaded: {names}")

    if errors or warns:
        color = imgui.ImVec4(0.96, 0.28, 0.28, 1.0) if errors else imgui.ImVec4(0.81, 0.57, 0.47, 1.0)
        label = f"\u26a0 {len(errors)} plugin error(s), {len(warns)} warning(s) - click to expand"
        imgui.push_style_color(imgui.Col_.header.value, color)
        opened = imgui.collapsing_header(label)
        imgui.pop_style_color()
        if opened:
            for entry in list(app_state.log_entries):
                if entry.level in ("error", "warn"):
                    c = imgui.ImVec4(0.96, 0.28, 0.28, 1.0) if entry.level == "error" else imgui.ImVec4(0.81, 0.57, 0.47, 1.0)
                    imgui.text_colored(c, entry.text)
        imgui.separator()


def _gui():
    _lazy_init()

    viewport = imgui.get_main_viewport()
    imgui.set_next_window_pos(viewport.pos)
    imgui.set_next_window_size(viewport.size)
    flags = (imgui.WindowFlags_.no_title_bar | imgui.WindowFlags_.no_resize |
              imgui.WindowFlags_.no_move | imgui.WindowFlags_.no_collapse |
              imgui.WindowFlags_.no_bring_to_front_on_focus)
    imgui.begin("##root", None, flags)

    _top_bar()

    if imgui.begin_tab_bar("##tabs"):
        for i, plugin in enumerate(plugins):
            opened, _ = imgui.begin_tab_item(f"{plugin.icon}  {plugin.name}")
            if opened:
                if app_state.active_plugin_index != i:
                    app_state.active_plugin_index = i
                    try:
                        plugin.on_activate()
                    except Exception as e:
                        app_state.log(f"[{plugin.name}] on_activate error: {e}", "error")
                try:
                    plugin.gui()
                except Exception as e:
                    imgui.text_colored(imgui.ImVec4(0.96, 0.28, 0.28, 1.0),
                                        f"Plugin error: {type(e).__name__}: {e}")
                imgui.end_tab_item()
        imgui.end_tab_bar()

    imgui.end()


def _shutdown():
    for plugin in plugins:
        try:
            plugin.on_shutdown()
        except Exception as e:
            print(f"[shutdown] {plugin.name}: {e}")


def main():
    runner_params = hello_imgui.RunnerParams()
    runner_params.app_window_params.window_title = f"TG3 - Texture Generator v{VERSION}"
    runner_params.app_window_params.window_geometry.size = (1300, 860)
    runner_params.imgui_window_params.default_imgui_window_type = \
        hello_imgui.DefaultImGuiWindowType.no_default_window
    runner_params.callbacks.show_gui = _gui
    runner_params.callbacks.before_exit = _shutdown

    icon_path = PROJECT_ROOT / "TG_ICO.ico"
    if icon_path.exists():
        runner_params.app_window_params.window_icon = str(icon_path)

    immapp.run(runner_params)


if __name__ == "__main__":
    main()
