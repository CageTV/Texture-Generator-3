"""
Auto-discovers plugins: imports every .py file in plugins_dir and
instantiates every TG3Plugin subclass it defines. A broken plugin is logged
and skipped rather than crashing the whole app - one bad add-on should
never take the others down with it.
"""
import importlib.util
import inspect
from pathlib import Path

from app.plugin_base import TG3Plugin


def discover_plugins(app, plugins_dir: Path):
    plugins = []
    for path in sorted(plugins_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue

        mod_name = f"tg3_plugins.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            app.log(f"[plugin] FAILED to load {path.name}: {type(e).__name__}: {e}", "error")
            continue

        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if obj is TG3Plugin or not issubclass(obj, TG3Plugin):
                continue
            if obj.__module__ != mod_name:
                continue  # skip base classes imported into this module from elsewhere
            try:
                plugins.append(obj(app))
                app.log(f"[plugin] loaded {obj.__name__}")
            except Exception as e:
                app.log(f"[plugin] FAILED to init {obj.__name__}: {type(e).__name__}: {e}", "error")

    plugins.sort(key=lambda p: (p.order, p.name))
    return plugins
