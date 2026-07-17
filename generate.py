import copy
import json
import sys
from pathlib import Path

SUPPORTED_EXTS = {".dds", ".png", ".tga", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}

# Texture engine requirement: Skyrim always needs a diffuse + normal map.
# Everything else (height/parallax, rmaos, glow/emissive, fuzz, subsurface,
# coat/multilayer) is OPTIONAL and auto-detected per texture set below —
# matching Step2.ps1's behavior, not the old all-four-or-skip requirement.
MIN_REQUIRED_CHANNELS = {"diffuse", "normal"}

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

DEFAULT_CONFIG = {
    "defaults": {
        "emissive": False,
        "parallax": True,
        "subsurface": False,
        "subsurface_foliage": False,
        "smooth_angle": 75,
        "specular_level": 0.04,
        "subsurface_color": [1, 1, 1],
        "roughness_scale": 1,
        "subsurface_opacity": 1,
        "displacement_scale": 1,
    },
    "keywords": {},
    "file_overrides": {},
}


def fail(message):
    print()
    print("ERROR:")
    print(message)
    print()
    sys.exit(1)


def load_config():
    if not CONFIG_PATH.exists():
        print(f"WARNING: Missing config.json at {CONFIG_PATH}; using built-in defaults.", flush=True)
        return copy.deepcopy(DEFAULT_CONFIG)

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        user_config = json.load(f)

    config = copy.deepcopy(DEFAULT_CONFIG)
    deep_update(config, user_config)
    return config


def deep_update(target, source):
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_update(target[key], value)
        else:
            target[key] = value


def normalize_slashes(value):
    return str(value).replace("/", "\\")


# Suffix -> channel name. Order matters: longer/more-specific suffixes must
# be checked before shorter ones that could accidentally match as a substring
# of a filename stem (handled below by checking longest-suffix-first).
SUFFIX_CHANNELS = [
    ("_rmaos", "rmaos"),
    ("_cnr",   "coat"),        # coat diffuse/normal/roughness -> multilayer
    ("_n",     "normal"),
    ("_p",     "height"),      # parallax/height map
    ("_g",     "glow"),        # emissive
    ("_f",     "fuzz"),
    ("_s",     "subsurface"),
    ("_d",     "diffuse"),     # explicit diffuse suffix (optional convention)
    ("_ao",    "ao"),          # not consumed by PBRNIFPatcher directly, but
    ("_r",     "roughness"),   # recognized so it doesn't get misread as diffuse
    ("_m",     "metalness"),
]


def strip_known_suffix(stem):
    lower = stem.lower()
    for suffix, channel in sorted(SUFFIX_CHANNELS, key=lambda x: -len(x[0])):
        if lower.endswith(suffix):
            return stem[: -len(suffix)], channel
    # No recognized suffix -> treat as the diffuse/source texture
    # (matches TG3's own convention, e.g. "background.png" with no suffix).
    return stem, "diffuse"


def texture_key(path, mod_root):
    rel = path.relative_to(mod_root)
    parts = list(rel.parts)

    if parts and parts[0].lower() == "textures":
        parts = parts[1:]

    if parts and parts[0].lower() == "pbr":
        parts = parts[1:]

    stem, channel = strip_known_suffix(Path(parts[-1]).stem)
    parts[-1] = stem
    key = normalize_slashes(Path(*parts))
    return key, channel


def scan_texture_sets(mod_root):
    sets = {}

    for path in mod_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_EXTS:
            continue

        key, channel = texture_key(path, mod_root)
        sets.setdefault(key, {})[channel] = path

    complete = {}
    skipped = {}
    for key, channels in sets.items():
        missing = MIN_REQUIRED_CHANNELS - set(channels.keys())
        if missing:
            skipped[key] = sorted(missing)
        else:
            complete[key] = channels

    return complete, skipped


def setting_from_value(value):
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return {"displacement_scale": value}


def auto_detected_settings(channels):
    """Capability flags/fields derived from which map files actually exist,
    mirroring Step2.ps1's logic (the proven-working reference tool) instead
    of using fixed config values for things that should be per-texture."""
    has_glow = "glow" in channels
    has_height = "height" in channels
    has_fuzz = "fuzz" in channels
    has_subsurface_map = "subsurface" in channels
    has_coat = "coat" in channels

    settings = {
        "emissive": has_glow,
        "parallax": has_height,
        "subsurface": bool(has_subsurface_map and not has_coat),
        "displacement_scale": 2.0 if has_coat else 1.0,
    }

    if has_coat:
        settings["multilayer"] = True
        settings["coat_diffuse"] = True
        settings["coat_normal"] = True
        settings["coat_parallax"] = True
        settings["coat_strength"] = 1.0
        settings["coat_roughness"] = 1.0
        settings["coat_specular_level"] = 0.018

    if has_fuzz:
        settings["fuzz"] = {"texture": True}

    return settings


def settings_for_texture(texture_key_value, config, channels):
    # 1) start from the config's static defaults
    settings = copy.deepcopy(config.get("defaults", {}))

    # 2) overlay auto-detected, per-texture capability flags (these reflect
    #    which map files actually exist, so they should win over static
    #    config defaults for the fields they control)
    deep_update(settings, auto_detected_settings(channels))

    # 3) keyword overrides (substring match against the texture path)
    lower_texture = texture_key_value.lower()
    file_name = lower_texture.split("\\")[-1]
    for keyword, value in config.get("keywords", {}).items():
        if str(keyword).lower() in lower_texture:
            deep_update(settings, setting_from_value(value))

    # 4) explicit per-file overrides — highest precedence, always wins
    overrides = config.get("file_overrides", {})
    for override_key, value in overrides.items():
        normalized = normalize_slashes(str(override_key)).lower()
        if normalized == lower_texture or normalized == file_name:
            deep_update(settings, setting_from_value(value))

    return settings


def build_entry(texture_key_value, channels, config):
    entry = {"texture": texture_key_value}
    deep_update(entry, settings_for_texture(texture_key_value, config, channels))
    return entry


def output_path_for(mod_root, json_name):
    name = json_name.strip()
    if not name:
        name = mod_root.name

    if not name.lower().endswith(".json"):
        name += ".json"

    output_folder = mod_root / "PBRNIFPatcher"
    output_folder.mkdir(parents=True, exist_ok=True)

    return output_folder / name


def print_usage():
    print()
    print("Skyking PBR JSON Builder")
    print("Usage:")
    print("  python generate.py <mod_folder> <json_name>")
    print()


def main():
    if len(sys.argv) < 3:
        print_usage()
        fail("Missing arguments.")

    mod_root = Path(sys.argv[1]).resolve()
    json_name = sys.argv[2].strip()

    if not mod_root.exists():
        fail(f"Mod folder does not exist: {mod_root}")

    config = load_config()
    out_path = output_path_for(mod_root, json_name)

    print()
    print("=== Skyking PBR JSON Builder ===")
    print(f"Mod folder: {mod_root}")
    print(f"Output JSON: {out_path}")
    print(f"Config: {CONFIG_PATH}")
    print()

    texture_sets, skipped = scan_texture_sets(mod_root)
    total = len(texture_sets)
    print(f"__SKYKING_TOTAL__={total}", flush=True)

    entries = []
    for done, texture_name in enumerate(sorted(texture_sets.keys()), start=1):
        channels = texture_sets[texture_name]
        tags = []
        if "height" in channels: tags.append("parallax")
        if "glow" in channels: tags.append("emissive")
        if "coat" in channels: tags.append("multilayer")
        if "fuzz" in channels: tags.append("fuzz")
        if "subsurface" in channels and "coat" not in channels: tags.append("subsurface")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        print(f"Adding {texture_name}{tag_str}", flush=True)
        entries.append(build_entry(texture_name, channels, config))
        print(f"__SKYKING_PROGRESS__={done}/{total}", flush=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(entries, f, indent=4)
        f.write("\n")

    if skipped:
        print()
        print(f"Skipped {len(skipped)} incomplete texture sets (need at least diffuse + normal):", flush=True)
        for key, missing in sorted(skipped.items()):
            print(f"  skipped {key} - missing: {', '.join(missing)}", flush=True)

    print()
    print(f"done - wrote {len(entries)} texture sets", flush=True)


if __name__ == "__main__":
    main()
