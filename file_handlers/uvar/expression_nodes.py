"""See docs/internals/uvar-format.md."""

import functools

lazy from file_handlers.layout_codec import load_table
lazy from utils.hash_util import murmur3_hash


@functools.cache
def expression_nodes() -> dict:
    return load_table("resources/data/uvar", "expression_nodes", ValueError)


@functools.cache
def expression_parameter_names() -> dict[int, str]:
    return {murmur3_hash(name.encode("ascii")): name
            for node in expression_nodes()["nodes"].values() for name in node["parameters"]}
