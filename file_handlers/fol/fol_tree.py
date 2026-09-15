from __future__ import annotations

from .fol_file import FolFile, FolInstanceGroup


def build_fol_tree_model(fol: FolFile):
    from PySide6.QtGui import QStandardItem, QStandardItemModel

    model = QStandardItemModel()
    model.setHorizontalHeaderLabels(["Field", "Value"])
    model.appendRow([_item("version"), _item(str(fol.version))])
    model.appendRow([_item("layout"), _item(_layout_name(fol))])
    if fol.version == 0:
        model.appendRow([_item("padding0C"), _item(f"0x{fol.padding_0c:X}")])
    else:
        _append_aabb(model, "bounds", fol.aabb)
        model.appendRow([_item("padding24"), _item(f"0x{fol.padding_24:X}")])
    model.appendRow([_item("groups"), _item(str(len(fol.groups)))])

    groups_item = _item("instanceGroups")
    model.appendRow([groups_item, _item("")])
    for group in fol.groups:
        item = _item(f"[{group.index:03}] {_basename(group.mesh_path)}", group.mesh_path)
        groups_item.appendRow([item, _item(_summary(fol, group))])
        _append_group(item, fol, group)

    return model


def _layout_name(fol: FolFile) -> str:
    if fol.version == 0:
        return "legacy"
    return "extended" if fol.uses_extended_group_layout else "compact"


def _append_group(parent, fol: FolFile, group: FolInstanceGroup) -> None:
    def add(name: str, value) -> None:
        parent.appendRow([_item(name), _item(str(value))])

    add("mesh", group.mesh_path)
    add("material", group.material_path)
    _append_aabb(parent, "aabb", group.aabb)
    if fol.version == 0:
        add("padding04", f"0x{group.legacy_padding04:X}")
    elif fol.uses_extended_group_layout:
        _append_properties(parent, group)
        add("unknown1E", f"0x{group.unknown1E:X}")
        add("densityCullingNear", f"{group.densityCullingNear:g}")
        add("densityCullingFar", f"{group.densityCullingFar:g}")
    else:
        add("compactPropertyBits", f"0x{group.compactPropertyBits:X}")
    _append_instances(parent, group)


def _summary(fol: FolFile, group: FolInstanceGroup) -> str:
    parts = [f"{group.instance_count} instances"]
    if fol.version == 0:
        return parts[0]
    if fol.uses_extended_group_layout:
        parts.append(f"props 0x{group.properties.raw:X}")
        parts.append(f"density {group.densityCullingNear:g}, {group.densityCullingFar:g}")
    else:
        parts.append(f"compact 0x{group.compactPropertyBits:X}")
    return " | ".join(parts)


def _append_properties(parent, group: FolInstanceGroup) -> None:
    props = group.properties
    item = _item("properties")
    parent.appendRow([item, _item(f"0x{props.raw:X}")])
    for name in (
        "unknownRenderGate0",
        "unknownRenderGate1",
        "unknownRenderGate2",
        "unknownRenderGate3",
        "unknownLodFlag",
        "shadowCastMode",
        "beautyMaskEnabled",
        "beautyMaskChannel",
        "allowDensityCulling",
        "densityCullingRangeOverride",
        "densityCullingRangeMultiply",
        "ignoreGlobalDensity",
        "reserved15",
    ):
        item.appendRow([_item(name), _item(str(getattr(props, name)))])


def _append_aabb(parent, name: str, aabb) -> None:
    item = _item(name)
    parent.appendRow([item, _item(f"min {_vec(aabb.min)} | max {_vec(aabb.max)}")])
    item.appendRow([_item("min"), _item(_vec(aabb.min))])
    item.appendRow([_item("max"), _item(_vec(aabb.max))])


def _append_instances(parent, group: FolInstanceGroup) -> None:
    item = _item("instances")
    parent.appendRow([item, _item(str(group.instance_count))])
    for index, transform in enumerate(group.transforms):
        child = _item(f"[{index:06}]")
        item.appendRow([child, _item(_transform_summary(transform))])
        child.appendRow([_item("position"), _item(_vec(transform.position))])
        child.appendRow([_item("rotation"), _item(_vec(transform.rotation))])
        child.appendRow([_item("scale"), _item(_vec(transform.scale))])


def _transform_summary(transform) -> str:
    return f"pos {_vec(transform.position)} | rot {_vec(transform.rotation)} | scale {_vec(transform.scale)}"


def _vec(values) -> str:
    return ", ".join(f"{value:g}" for value in values)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else "<no mesh>"


def _item(text: str, tooltip: str = ""):
    from PySide6.QtGui import QStandardItem

    item = QStandardItem(text)
    item.setEditable(False)
    item.setToolTip(tooltip or text)
    return item
