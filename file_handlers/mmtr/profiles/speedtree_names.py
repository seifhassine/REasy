"""See docs/internals/speedtree.md, Program names."""
from __future__ import annotations

import re
from typing import NamedTuple

_PROGRAM = re.compile(
    r"^(?:SpeedTreeV8)?(?P<marker>A|TS|ATS)?"
    r"(?P<kind>TreeTwoSide|Tree|LeafTwoSide|Leaf|FacingLeafTwoSide|FacingLeaf|Grass|Billboard)"
    r"(?P<foliage>Foliage)?(?:FaceCrossFading)?(?P<input>O0_1|O[0-2](?:TreeO1|TreeO2|Tree|Grass)?)"
    r"(?P<pass>DepthWrite|Shadow|Forward|Pick|Outline|PointCloud)?$")
_FAMILIES = {None: "deferred-gbuffer", "DepthWrite": "z-prepass", "Shadow": "shadow", "Forward": "forward",
             "Pick": "pick", "Outline": "named-engine-pass", "PointCloud": "named-engine-pass"}


class SpeedTreeName(NamedTuple):
    family: str
    alpha_test: bool
    two_sided: bool
    foliage: bool


def speedtree_name(name: str) -> SpeedTreeName | None:
    found = _PROGRAM.match(name)
    if found is None:
        return None
    marker = found["marker"] or ""
    return SpeedTreeName(_FAMILIES[found["pass"]], marker.startswith("A"), marker.endswith("TS"),
                         found["foliage"] is not None)


__all__ = ["SpeedTreeName", "speedtree_name"]
