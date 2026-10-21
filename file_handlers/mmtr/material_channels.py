from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

lazy from .pass_purpose import purpose_title
lazy from ui.icons import line_icon
lazy from ui.styles import set_style_role


translate = QCoreApplication.translate


def channel_nodes(graph):
    master = next((node for node in graph.nodes if node.properties.get("semantic_reconstruction_boundary")
                   and "output_pins" in node.properties), None)
    surface = next((node for node in graph.nodes if node.kind == "material_surface"
                    and node.properties.get("accepts_recovered_material")), None)
    return (master, surface) if master is not None and surface is not None else (None, None)


def edited_channels(graph, master, surface) -> set[str]:
    result = set()
    for link in graph.links:
        if (link.target_node == surface.node_id and link.target_pin.startswith("channel_")
                and not (link.source_node == master.node_id and link.source_pin == link.target_pin)):
            result.add(link.target_pin)
        elif (link.source_node == master.node_id and link.source_pin.startswith("channel_")
              and not (link.target_node == surface.node_id and link.target_pin == link.source_pin)):
            result.add(link.source_pin)
    return result


def visible_channel_pins(graph, node, pins):
    master, surface = channel_nodes(graph)
    if master is None or node.node_id not in {master.node_id, surface.node_id}:
        return pins
    shown = set(master.properties.get("shown_channels", ())) | edited_channels(graph, master, surface)
    return tuple(pin for pin in pins if not pin.name.startswith("channel_") or pin.name in shown)


_GROUPS = ("pixel", "discard", "read", "vertex", "geometry", "hull", "domain", "compute")


def _group(record) -> str:
    stage, scope = str(record["channel"]).split(":", 2)[:2]
    return scope if scope in {"discard", "read"} else stage


_GROUP_TITLES = {
    "pixel": QT_TRANSLATE_NOOP("MaterialChannels", "Surface (pixel)"),
    "discard": QT_TRANSLATE_NOOP("MaterialChannels", "Cut-out"),
    "read": QT_TRANSLATE_NOOP("MaterialChannels", "Textures as read"),
    "vertex": QT_TRANSLATE_NOOP("MaterialChannels", "Vertex"),
    "geometry": QT_TRANSLATE_NOOP("MaterialChannels", "Geometry"),
    "hull": QT_TRANSLATE_NOOP("MaterialChannels", "Tessellation (hull)"),
    "domain": QT_TRANSLATE_NOOP("MaterialChannels", "Tessellation (domain)"),
    "compute": QT_TRANSLATE_NOOP("MaterialChannels", "Compute"),
}


def _group_title(group: str) -> str:
    return translate("MaterialChannels", _GROUP_TITLES[group]) if group in _GROUP_TITLES else group.title()


def reached_by(master, pin: str) -> list[tuple[str, str, int]]:
    return [(record["source"], row["how"], row["passes"]) for record in master.properties.get("reach", ())
            for row in record["channels"] if row["pin"] == pin]


def reach_summary(master, kind: str, name: str) -> str:
    labels = {record["pin"]: record["label"] for record in master.properties.get("output_pins", ())}
    record = next((item for item in master.properties.get("reach", ())
                   if item["kind"] == kind and item["source"] == name), None)
    if record is None:
        return translate("MaterialChannels", "Reaches no output in the shipped shaders.")
    lines = []
    for row in record["channels"]:
        text = (translate("MaterialChannels", "{channel}: {count} passes") if row["how"] == "value"
                else translate("MaterialChannels", "{channel}: decides a branch in {count} passes"))
        lines.append(text.format(channel=labels.get(row["pin"], row["pin"]), count=row["passes"]))
    return "\n".join(lines)


class ChannelPanel(QWidget):
    changed = Signal()

    def __init__(self, graph, master, surface, set_shown, parent=None):
        super().__init__(parent)
        self.graph, self.master, self.surface, self.set_shown = graph, master, surface, set_shown
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel(self.tr("Each channel is a value the shader writes. Show one on the node, then put nodes "
                              "between the master's output and the surface's input to change it."))
        note.setWordWrap(True)
        set_style_role(note, tone="muted")
        layout.addWidget(note)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels((self.tr("Channel"), self.tr("Type"), self.tr("Passes"), self.tr("Drawn for")))
        self.tree.setMinimumHeight(260)
        layout.addWidget(self.tree)
        buttons = QHBoxLayout()
        self.restore = QPushButton(self.tr("Restore Original"))
        self.restore.setIcon(line_icon("reset"))
        self.restore.setToolTip(self.tr("Wire the selected channel straight from the master again."))
        self.restore.clicked.connect(self._restore)
        buttons.addWidget(self.restore)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self._fill()
        self.tree.itemChanged.connect(self._toggled)
        self.tree.currentItemChanged.connect(lambda *_: self._selection())
        self._selection()

    def _fill(self) -> None:
        shown = set(self.master.properties.get("shown_channels", ()))
        edited = edited_channels(self.graph, self.master, self.surface)
        groups: dict[str, QTreeWidgetItem] = {}
        records = sorted(self.master.properties.get("output_pins", ()), key=lambda record: (
            _GROUPS.index(_group(record)) if _group(record) in _GROUPS else len(_GROUPS), _group(record)))
        for record in records:
            name = _group(record)
            if name not in groups:
                groups[name] = QTreeWidgetItem(self.tree, (_group_title(name),))
                groups[name].setFlags(Qt.ItemFlag.ItemIsEnabled)
                groups[name].setExpanded(name == "pixel")
            pin = str(record["pin"])
            item = QTreeWidgetItem(groups[name], (str(record["label"]), str(record["value_type"]),
                                                  str(len(record.get("passes", ())))))
            item.setText(3, " · ".join(
                self.tr("{purpose}: {count}").format(purpose=purpose_title(purpose), count=count)
                for purpose, count in record.get("purposes", {}).items()))
            item.setData(0, Qt.ItemDataRole.UserRole, pin)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                          | (Qt.ItemFlag.ItemIsUserCheckable if pin not in edited else Qt.ItemFlag.NoItemFlags))
            item.setCheckState(0, Qt.CheckState.Checked if pin in shown | edited else Qt.CheckState.Unchecked)
            reach = reached_by(self.master, pin)
            tips = [self.tr("Written by: {passes}").format(passes=", ".join(record.get("passes", ())[:12])
                                                           + (" …" if len(record.get("passes", ())) > 12 else ""))]
            if pin in edited:
                tips.insert(0, self.tr("Changed by this graph."))
            for purpose, meaning in record.get("meanings", {}).items():
                if meaning["slot"]:
                    text = (self.tr("In {purpose} passes this is the engine's G-buffer slot {slot}; other passes in "
                                    "the captured frame read it as {names}.") if meaning["read_as"] else
                            self.tr("In {purpose} passes this is the engine's G-buffer slot {slot}; no other pass "
                                    "read it in the captured frame."))
                else:
                    text = (self.tr("In {purpose} passes other passes in the captured frame read this target as "
                                    "{names}.") if meaning["read_as"] else
                            self.tr("In {purpose} passes no other pass read this target in the captured frame."))
                tips.append(text.format(purpose=purpose_title(purpose), slot=meaning["slot"],
                                        names=", ".join(meaning["read_as"])))
            if reach:
                tips.append(self.tr("Reached by: {sources}").format(sources=", ".join(
                    source if how == "value" else self.tr("{source} (decides a branch)").format(source=source)
                    for source, how, _passes in reach)))
            item.setToolTip(0, "\n".join(tips))
        for column in range(4):
            self.tree.resizeColumnToContents(column)

    def _toggled(self, item, column) -> None:
        pin = item.data(0, Qt.ItemDataRole.UserRole)
        if column != 0 or pin is None:
            return
        shown = [name for name in self.master.properties.get("shown_channels", ()) if name != pin]
        if item.checkState(0) == Qt.CheckState.Checked:
            shown.append(pin)
        self.set_shown(shown)

    def _selection(self) -> None:
        item = self.tree.currentItem()
        pin = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        self.restore.setEnabled(pin is not None and pin in edited_channels(self.graph, self.master, self.surface))

    def _restore(self) -> None:
        item = self.tree.currentItem()
        pin = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        if pin is None:
            return
        self.graph.disconnect_target(self.surface.node_id, pin)
        if any(record["pin"] == pin and record["form"] == "value" for record in self.master.properties["output_pins"]):
            self.graph.connect(self.master.node_id, pin, self.surface.node_id, pin)
        self.changed.emit()


__all__ = ["ChannelPanel", "channel_nodes", "edited_channels", "reach_summary", "reached_by", "visible_channel_pins"]
