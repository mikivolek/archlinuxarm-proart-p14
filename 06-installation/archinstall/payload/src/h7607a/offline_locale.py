"""Configure the mounted target's locale and console without booting it.

Archinstall 4.5 only matches commented locale.gen entries and uses a temporary
systemd-nspawn boot for localectl. These narrow replacements accept an already
enabled locale and write the console settings that systemd consumes at boot.
"""
from __future__ import annotations

from pathlib import Path
import re

from h7607a import hardware


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoding_key(value):
    return value.upper().replace("-", "").replace("_", "")


def safe_path(root, relative):
    root = Path(root)
    path = root / relative
    require(root.is_absolute() and root.resolve() == root
            and path.resolve().is_relative_to(root) and not path.is_symlink(),
            "Locale configuration path escapes its target: " + str(path))
    return path


def locale_selection(root, config):
    language, encoding = config.sys_lang, config.sys_enc
    require(isinstance(language, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", language),
            "Invalid locale language")
    require(isinstance(encoding, str) and re.fullmatch(r"[A-Za-z0-9_-]+", encoding),
            "Invalid locale encoding")
    base, separator, modifier = language.partition("@")
    require("@" not in modifier, "Invalid locale modifier")
    base, dotted, embedded_encoding = base.partition(".")
    if dotted and encoding_key(encoding) == "UTF8":
        encoding = embedded_encoding
    require(encoding and "." not in encoding, "Invalid embedded locale encoding")
    if base == "C" and encoding_key(encoding) == "UTF8" and not modifier:
        return {"name": "C.UTF-8", "encoding": "UTF-8", "index": None, "lines": None}
    path = safe_path(root, "etc/locale.gen")
    require(path.is_file(), "Target locale catalog is missing: " + str(path))
    lines = path.read_text().splitlines(keepends=True)
    matches = []
    for index, line in enumerate(lines):
        text = line.strip()
        if text.startswith("#"):
            text = text[1:].strip()
        fields = text.split()
        if len(fields) != 2:
            continue
        candidate, candidate_encoding = fields
        candidate_base, _, candidate_modifier = candidate.partition("@")
        candidate_base = candidate_base.partition(".")[0]
        if (candidate_base == base and candidate_modifier == modifier
                and encoding_key(candidate_encoding) == encoding_key(encoding)):
            matches.append((index, candidate, candidate_encoding))
    require(len(matches) == 1, "Selected locale must match one installed locale.gen entry: "
            + language + " " + encoding)
    index, name, selected_encoding = matches[0]
    if "." not in name.partition("@")[0]:
        name = base + "." + selected_encoding + ("@" + modifier if separator else "")
    return {"name": name, "encoding": selected_encoding, "index": index, "lines": lines}


def catalog_contains(root, relative, name, suffixes):
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_+.-]+", name)
            and name not in {".", ".."}, "Invalid console catalog name")
    directory = safe_path(root, relative)
    require(directory.is_dir(), "Console catalog is missing: " + str(directory))
    matches = [path for suffix in suffixes for path in directory.rglob(name + suffix)
               if path.is_file() and path.resolve().is_relative_to(root)]
    require(matches, "Console catalog has no selected entry: " + name)


def validate_keyboard(root, language):
    catalog_contains(Path(root), "usr/share/kbd/keymaps", language, (".map", ".map.gz"))


def validate_choices(root, config):
    """Read-only validation against the live or installed package catalogs."""
    root = Path(root)
    selection = locale_selection(root, config)
    validate_keyboard(root, config.kb_layout)
    catalog_contains(root, "usr/share/kbd/consolefonts", config.console_font,
                     (".psf", ".psf.gz", ".psfu", ".psfu.gz", ".fnt", ".fnt.gz"))
    return {"locale": selection["name"], "encoding": selection["encoding"],
            "keymap": config.kb_layout, "font": config.console_font}


def set_locale(installation, config):
    target = hardware._root(installation.target)
    selection = locale_selection(target, config)
    lines, index = selection["lines"], selection["index"]
    if index is not None:
        # Remove only this entry's comment marker. An existing enabled entry is
        # valid and remains byte-identical, so retries do not produce duplicates.
        lines[index] = re.sub(r"^(\s*)#\s*", r"\1", lines[index], count=1)
        hardware._write(target, "etc/locale.gen", "".join(lines).encode())
        hardware._run(target, ["locale-gen"], api_filesystems=True, capture=False, timeout=600)
    charmap = hardware._run(target, ["env", "-i", "PATH=/usr/bin:/bin",
                                   "LANG=" + selection["name"], "locale", "charmap"]).strip()
    require(encoding_key(charmap) == encoding_key(selection["encoding"]),
            "Generated locale does not provide the selected encoding: " + selection["name"])
    hardware._write(target, "etc/locale.conf", ("LANG=" + selection["name"] + "\n").encode())
    return True


def set_keyboard_language(installation, language):
    target = hardware._root(installation.target)
    validate_keyboard(target, language)
    path = safe_path(target, "etc/vconsole.conf")
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    matches = [i for i, line in enumerate(lines) if re.match(r"^\s*KEYMAP\s*=", line)]
    require(len(matches) <= 1, "Console configuration contains multiple active KEYMAP entries")
    entry = "KEYMAP=" + language + "\n"
    if matches:
        lines[matches[0]] = entry
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(entry)
    hardware._write(target, "etc/vconsole.conf", "".join(lines).encode())
    return True
