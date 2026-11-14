"""See docs/internals/bhvt-editor.md, Pickers."""

from __future__ import annotations

import functools
from pathlib import Path

from ..model import Hfsm
from ..references import asset, own_variables, slot_types, tag_hash, tag_names, user_data
from ..rules import authoring
from ..summaries import short_name


def class_entries(model, slot: str, base: str) -> list[tuple[str, str, str]]:
    rsz = model.blocks[slot]
    used = authoring(rsz.game_version)["usage"]["hfsm" if isinstance(model, Hfsm) else "tree"].get(slot, {})
    return [(short_name(name), name.rsplit(".", 1)[0] + (f"   {used[name]:,}" if name in used else ""), name)
            for name in sorted(slot_types(rsz, base), key=lambda name: (-used.get(name, 0), name))]


def type_entries(rsz, base: str) -> list[tuple[str, str, str]]:
    return [(short_name(name), name.rsplit(".", 1)[0], name) for name in slot_types(rsz, base)]


def user_data_entries(rsz, base: str, path: str, held: int) -> list[tuple[str, str, object]]:
    registry = rsz.type_registry
    return [(asset(user, path)[1].rsplit("/", 1)[-1], short_name(registry.get_type_info(user.type_id)["name"]), user)
            for user in user_data(rsz, base, path) if user.instance_id != held]


def node_entries(structure, nodes: list, home=None) -> list[tuple[str, str, object]]:
    return [(node.name, " / ".join(item.name for item in structure.path(node)[:-1]), node)
            for node in sorted(nodes, key=lambda node: structure.parent.get(id(node)) is not home)]


def variable_entries(document, model, summaries) -> list[tuple[str, str, bytes]]:
    own = {id(uvar): uvar.header.name or "" for uvar in own_variables(model)}
    files = {id(uvar): Path(path).name for path, uvar in document.owner_variables.items()}
    return [(variable.name, f"{variable.type.name}   {own.get(id(source)) or files.get(id(source), '')}", guid)
            for guid, (source, variable) in summaries.found.items()]


@functools.cache
def tag_entries(game: str) -> list[tuple[str, str, int]]:
    first = {name: rank for rank, name in enumerate(authoring(game)["tag_enums"])}
    rows = [(*name.rpartition(".")[::-2], value) for value, name in tag_names(game).items()]
    return sorted(rows, key=lambda row: (first.get(row[1], len(first)), row[1]))


def custom_tag(text: str) -> tuple[str, str, int] | None:
    if text[:2].lower() == "0x":
        try:
            value = int(text, 16)
        except ValueError:
            return None
        return (text, "", value) if 0 <= value <= 0xFFFFFFFF else None
    return (text, f"0x{tag_hash(text):08X}", tag_hash(text)) if "." in text.strip(".") else None
