from typing import Any, Dict

from file_handlers.base_handler import BaseFileHandler
from .wel_file import WELFile, WELEventEntry
from PySide6.QtGui import QStandardItem


class WelHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.filepath: str = ""
        self.wel: WELFile | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        # WEL has no magic value; routed by extension in file_handlers.factory.
        return False

    def supports_editing(self) -> bool:
        return True

    def read(self, data: bytes):
        if not self.filepath.lower().endswith(".wel.11"):
            raise ValueError("WEL files are only supported for .wel.11 extension")

        parsed = WELFile()
        if not parsed.read(data):
            raise ValueError("Failed to parse WEL file")

        self.wel = parsed
        self.modified = False

    def rebuild(self) -> bytes:
        if not self.wel:
            return b""
        self.modified = False
        return self.wel.write()

    def populate_treeview(self, tree, parent_item, metadata_map: dict):
        if not self.wel:
            return

        from PySide6.QtGui import QStandardItem, QStandardItemModel

        model = QStandardItemModel()
        model.setHorizontalHeaderLabels(["Field", "Value"])

        header_item = QStandardItem("header")
        model.appendRow([header_item, QStandardItem("")])
        header_item.appendRow([QStandardItem("bankPathRaw (decoded UTF-16)"), QStandardItem(self.wel.bank_path)])

        model.appendRow([QStandardItem("eventCount"), QStandardItem(str(len(self.wel.events)))])

        events_root = QStandardItem("events")
        model.appendRow([events_root, QStandardItem(str(len(self.wel.events)))])

        for idx, event in enumerate(self.wel.events):
            event_item = QStandardItem(f"[{idx}]")
            events_root.appendRow([event_item, QStandardItem("")])
            self._append_event(event_item, event)

        if self.wel.trailing_data:
            model.appendRow([QStandardItem("trailing_data_bytes"), QStandardItem(str(len(self.wel.trailing_data)))])

        tree.setModel(model)
        tree.expandToDepth(2)

    def _append_event(self, parent, event: WELEventEntry):

        def add(name: str, value: Any):
            parent.appendRow([QStandardItem(name), QStandardItem(str(value))])

        for field in (
            "mTriggerId",
            "mEventId",
            "mJointHash",
            "mGameObjectHash",
            "mTracking",
            "mRotation",
        ):
            add(field, getattr(event, field))

        priority_item = QStandardItem("mPriority")
        parent.appendRow([priority_item, QStandardItem("")])
        priority = event.mPriority
        for field in (
            "mId1",
            "mId2",
            "mId3",
            "mBookingTimer",
            "mFlangingTimer",
            "mGlobalId",
            "mLimit",
            "mPriority",
            "mMode",
            "mReleaseTime",
        ):
            priority_item.appendRow([QStandardItem(field), QStandardItem(str(getattr(priority, field)))])

        for field in (
            "mDisableObsOcl",
            "mUpdateObsOcl",
            "mDisableMaxObsOclDistance",
            "mEnableSpaceFeature",
            "mWaitUntilFinished",
            "mListenerMask",
        ):
            add(field, getattr(event, field))

        free_area_item = QStandardItem("mFreeArea")
        parent.appendRow([free_area_item, QStandardItem("")])
        for field in ("mFreeArea0to7", "mFreeArea8to11", "mFreeArea12to15"):
            free_area_item.appendRow([QStandardItem(field), QStandardItem(str(getattr(event.mFreeArea, field)))])

    def get_context_menu(self, tree, item, meta: dict):
        return None

    def handle_edit(self, meta: Dict[str, Any], new_val, old_val, item):
        return None

    def add_variables(self, target, prefix: str, count: int):
        return None

    def update_strings(self):
        return None

    def create_viewer(self):
        from .wel_viewer import WelViewer

        viewer = WelViewer(self)
        viewer.modified_changed.connect(self.modified_changed.emit)
        return viewer
