"""See docs/internals/rcol-names.md."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from itertools import chain
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from file_handlers.pak.reader import CachedPakReader  # noqa: E402
from file_handlers.pak.utils import scan_pak_files  # noqa: E402
from file_handlers.rcol.names import names_resource  # noqa: E402
from file_handlers.rcol.rcol_file import RcolFile  # noqa: E402
from file_handlers.rcol.rcol_structures import StrippedName, calc_hash  # noqa: E402
from file_handlers.rsz.game_registry import game_type_registry  # noqa: E402
from ui.project_manager.pak_file_lists import find_default_pak_list_path, read_pak_list_file  # noqa: E402
from utils.resource_file_utils import resource_version_from_path  # noqa: E402


def game_files(game: str, folder: str, extension: str):
    versions = {path: resource_version_from_path(path, extension)
                for path in read_pak_list_file(find_default_pak_list_path(game, REPOSITORY))
                if f".{extension}." in path}
    loaded = max(versions.values(), default=None)
    paths = [path for path, version in versions.items() if version == loaded]
    reader = CachedPakReader.from_paks(scan_pak_files(folder), game=game)
    reader.add_files(*paths)
    reader.cache_entries_for_paths(paths)
    for path in paths:
        if (data := reader.get_file(path)) is not None:
            yield path, data.getvalue()


def rcol_names(game: str, folder: str):
    registry = game_type_registry(game)
    for path, data in game_files(game, folder, "rcol"):
        rcol = RcolFile()
        rcol.type_registry = registry
        with contextlib.redirect_stdout(io.StringIO()):
            rcol.read(data, resource_version_from_path(path, "rcol"), path)
        for group in rcol.groups:
            yield group.info.name
            for shape in (*group.shapes, *group.extra_shapes):
                yield from (shape.info.name, shape.info.primary_joint_name_str, shape.info.secondary_joint_name_str)
        for request_set in rcol.request_sets:
            yield from (request_set.info.name, request_set.info.key_name)
        yield from (tag.tag for tag in rcol.ignore_tags or ())


def rsz_strings(data: bytes):
    text = data[: len(data) // 2 * 2].decode("utf-16le", "replace")
    position = 0
    for chunk in text.split("\0"):
        start, position = position * 2, position + len(chunk) + 1
        if (chunk and chunk.isprintable() and "\ufffd" not in chunk and start >= 4
                and int.from_bytes(data[start - 4:start], "little") == len(chunk) + 1):
            yield chunk


def variants(names):
    for name in names:
        yield from (f"{name}_Copied", f"{name}_symmetry")
        for number in range(1, 11):
            yield from (f"{name}_{number}", f"{name}{number}", f"{name}_{number:02d}")
    for number in range(200):
        yield from (str(number), f"{number}_Copied", f"{number}_symmetry")


def name_hashes(texts, wanted: set[int], found: dict[int, str]) -> None:
    for text in texts:
        if (name_hash := calc_hash(text)) in wanted:
            found.setdefault(name_hash, text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Names for the hashes of stripped RCOL names, per game.")
    parser.add_argument("games", nargs="+", metavar="GAME=FOLDER", help="REasy game name and its install folder")
    games = [tuple(item.split("=", 1)) for item in parser.parse_args().games]
    names, stripped = {}, {}
    for game, folder in games:
        names[game], stripped[game] = set(), set()
        for name in rcol_names(game, folder):
            if isinstance(name, StrippedName):
                stripped[game].add(name.name_hash)
                name = name.text
            if name:
                names[game].add(name)
        print(f"{game}: {len(stripped[game])} stripped hashes", flush=True)
    every_name = set().union(*names.values())
    for game, folder in games:
        if not (wanted := stripped[game]):
            continue
        strings = {text for _path, data in game_files(game, folder, "user") for text in rsz_strings(data)}
        found = {}
        name_hashes(chain(names[game], strings, every_name), wanted, found)
        name_hashes(variants(every_name | set(found.values())), wanted, found)
        output = REPOSITORY / names_resource(game)
        output.parent.mkdir(exist_ok=True)
        table = {f"{name_hash:08X}": found[name_hash] for name_hash in sorted(found)}
        output.write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        output.with_name(f"{output.stem}_comments.json").write_text(json.dumps({"": [
            f"{game}: murmur3 hash (upper-case hex) -> name, for the group, shape and request-set names its RCOL files",
            "keep only as hashes. Generated by tools/rcol_name_catalog.py; candidate names from: "
            + ", ".join(name for name, _ in games), "See docs/internals/rcol-names.md."]}, indent=1) + "\n",
            encoding="utf-8", newline="\n")
        print(f"{game}: {len(table)} of {len(wanted)} hashes named -> {output}")


if __name__ == "__main__":
    main()
