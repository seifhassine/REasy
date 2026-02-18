from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .wel_file import WELEventEntry, WELFile, WELFreeArea, WELPrioritySerialized


class WelViewer(QWidget):
    modified_changed = Signal(bool)

    MODE_ITEMS = [
        ("Newest", 0),
        ("Oldest", 1),
    ]

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self._syncing = False

        root_layout = QVBoxLayout(self)

        header_box = QGroupBox("WEL Header")
        header_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        header_box.setMinimumHeight(56)
        header_box.setMaximumHeight(68)
        header_form = QFormLayout(header_box)
        header_form.setContentsMargins(8, 6, 8, 6)
        header_form.setVerticalSpacing(4)
        self.bank_path_input = QLineEdit()
        self.bank_path_input.setPlaceholderText("UTF-16 bank path")
        self.bank_path_input.textEdited.connect(self._on_bank_path_changed)
        header_form.addRow(QLabel("Bank path"), self.bank_path_input)
        root_layout.addWidget(header_box)

        toolbar_layout = QHBoxLayout()
        self.add_event_btn = QPushButton("Add Event")
        self.remove_event_btn = QPushButton("Remove Event")
        self.add_event_btn.clicked.connect(self._on_add_event)
        self.remove_event_btn.clicked.connect(self._on_remove_event)
        toolbar_layout.addWidget(self.add_event_btn)
        toolbar_layout.addWidget(self.remove_event_btn)
        root_layout.addLayout(toolbar_layout)

        splitter = QSplitter(Qt.Horizontal)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("Events"))
        self.event_list = QListWidget()
        self.event_list.currentRowChanged.connect(self._on_event_selected)
        left_layout.addWidget(self.event_list)

        splitter.addWidget(left_panel)
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([220, 900])

        root_layout.addWidget(splitter, 1)

        self._load_from_handler()

    def _build_detail_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.detail_title = QLabel("Select an event to edit")
        layout.addWidget(self.detail_title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 4, 0)
        content_layout.setSpacing(8)

        core_box = QGroupBox("Core")
        core_form = QFormLayout(core_box)
        core_form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.trigger_id = self._create_number_edit()
        self.event_id = self._create_number_edit()
        self.joint_hash = self._create_number_edit()
        self.game_object_hash = self._create_number_edit()
        core_form.addRow("Trigger Id", self.trigger_id)
        core_form.addRow("Event Id", self.event_id)
        core_form.addRow("Joint Hash", self.joint_hash)
        core_form.addRow("Game Object Hash", self.game_object_hash)
        content_layout.addWidget(core_box)

        flags_box = QGroupBox("Flags")
        flags_form = QFormLayout(flags_box)
        flags_form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.tracking = QCheckBox()
        self.rotation = QCheckBox()
        self.disable_obs_ocl = QCheckBox()
        self.update_obs_ocl = QCheckBox()
        self.disable_max_obs_ocl_distance = QCheckBox()
        self.enable_space_feature = QCheckBox()
        self.wait_until_finished = QCheckBox()
        self.listener_mask = self._create_number_edit()
        flags_form.addRow("Tracking", self.tracking)
        flags_form.addRow("Rotation", self.rotation)
        flags_form.addRow("Disable Obs/Ocl", self.disable_obs_ocl)
        flags_form.addRow("Update Obs/Ocl", self.update_obs_ocl)
        flags_form.addRow("Disable Max Obs/Ocl Distance", self.disable_max_obs_ocl_distance)
        flags_form.addRow("Enable Space Feature", self.enable_space_feature)
        flags_form.addRow("Wait Until Finished", self.wait_until_finished)
        flags_form.addRow("Listener Mask (u32)", self.listener_mask)
        content_layout.addWidget(flags_box)

        priority_box = QGroupBox("Priority")
        priority_form = QFormLayout(priority_box)
        priority_form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.id1 = self._create_number_edit()
        self.id2 = self._create_number_edit()
        self.id3 = self._create_number_edit()
        self.booking_timer = self._create_number_edit()
        self.flanging_timer = self._create_number_edit()
        self.global_id = self._create_uchar_edit()
        self.limit = self._create_uchar_edit()
        self.priority = self._create_uchar_edit()
        self.mode = QComboBox()
        self.mode.setFixedWidth(170)
        for label, value in self.MODE_ITEMS:
            self.mode.addItem(label, value)
        self.release_time = self._create_number_edit()
        priority_form.addRow("Id1 (i16)", self.id1)
        priority_form.addRow("Id2 (i16)", self.id2)
        priority_form.addRow("Id3 (i16)", self.id3)
        priority_form.addRow("Booking Timer (i16)", self.booking_timer)
        priority_form.addRow("Flanging Timer (i16)", self.flanging_timer)
        priority_form.addRow("Global Id (uchar)", self.global_id)
        priority_form.addRow("Limit (uchar)", self.limit)
        priority_form.addRow("Priority (uchar)", self.priority)
        priority_form.addRow("Mode (enum)", self.mode)
        priority_form.addRow("Release Time (i16)", self.release_time)
        content_layout.addWidget(priority_box)

        free_area_box = QGroupBox("Free Area")
        free_area_form = QFormLayout(free_area_box)
        free_area_form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.free_0to7 = self._create_number_edit()
        self.free_8_0 = self._create_number_edit()
        self.free_12_0 = self._create_number_edit()
        free_area_form.addRow("Free 0..7 (u32)", self.free_0to7)
        free_area_form.addRow("Free 8..11 (u32)", self.free_8_0)
        free_area_form.addRow("Free 12..15 (int64)", self.free_12_0)
        content_layout.addWidget(free_area_box)

        content_layout.addStretch(1)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        self._bind_change_handlers()
        return panel

    def _create_number_edit(self) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText("number or hex (e.g. 10, 0xA)")
        edit.setFixedWidth(170)
        edit.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        return edit

    def _create_uchar_edit(self) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText("uchar (0..255)")
        edit.setFixedWidth(96)
        edit.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        return edit

    def _bind_change_handlers(self):
        for checkbox in (
            self.tracking,
            self.rotation,
            self.disable_obs_ocl,
            self.update_obs_ocl,
            self.disable_max_obs_ocl_distance,
            self.enable_space_feature,
            self.wait_until_finished,
        ):
            checkbox.stateChanged.connect(self._on_detail_widget_changed)

        self.mode.currentIndexChanged.connect(self._on_detail_widget_changed)

        for edit in (
            self.trigger_id,
            self.event_id,
            self.joint_hash,
            self.game_object_hash,
            self.listener_mask,
            self.id1,
            self.id2,
            self.id3,
            self.booking_timer,
            self.flanging_timer,
            self.global_id,
            self.limit,
            self.priority,
            self.release_time,
            self.free_0to7,
            self.free_8_0,
            self.free_12_0,
        ):
            edit.textEdited.connect(self._on_detail_widget_changed)

    def _load_from_handler(self):
        wel = self.handler.wel
        if not wel:
            return

        self._syncing = True
        try:
            self.bank_path_input.setText(wel.bank_path)
            self.event_list.clear()
            for i, event in enumerate(wel.events):
                self.event_list.addItem(self._event_title(i, event))
        finally:
            self._syncing = False

        if wel.events:
            self.event_list.setCurrentRow(0)
        else:
            self._set_detail_enabled(False)

    def _on_event_selected(self, row: int):
        wel = self.handler.wel
        if not wel or row < 0 or row >= len(wel.events):
            self._set_detail_enabled(False)
            self.detail_title.setText("Select an event to edit")
            return

        self._set_detail_enabled(True)
        self._syncing = True
        try:
            event = wel.events[row]
            self.detail_title.setText(f"Editing event {row}")
            self.trigger_id.setText(str(event.mTriggerId))
            self.event_id.setText(str(event.mEventId))
            self.joint_hash.setText(str(event.mJointHash))
            self.game_object_hash.setText(str(event.mGameObjectHash))
            self.tracking.setChecked(bool(event.mTracking))
            self.rotation.setChecked(bool(event.mRotation))
            self.disable_obs_ocl.setChecked(bool(event.mDisableObsOcl))
            self.update_obs_ocl.setChecked(bool(event.mUpdateObsOcl))
            self.disable_max_obs_ocl_distance.setChecked(bool(event.mDisableMaxObsOclDistance))
            self.enable_space_feature.setChecked(bool(event.mEnableSpaceFeature))
            self.wait_until_finished.setChecked(bool(event.mWaitUntilFinished))
            self.listener_mask.setText(str(event.mListenerMask))

            self.id1.setText(str(event.mPriority.mId1))
            self.id2.setText(str(event.mPriority.mId2))
            self.id3.setText(str(event.mPriority.mId3))
            self.booking_timer.setText(str(event.mPriority.mBookingTimer))
            self.flanging_timer.setText(str(event.mPriority.mFlangingTimer))
            self.global_id.setText(str(event.mPriority.mGlobalId))
            self.limit.setText(str(event.mPriority.mLimit))
            self.priority.setText(str(event.mPriority.mPriority))
            self._set_mode_value(event.mPriority.mMode)
            self.release_time.setText(str(event.mPriority.mReleaseTime))

            self.free_0to7.setText(str(event.mFreeArea.mFreeArea0to7))
            self.free_8_0.setText(str(event.mFreeArea.mFreeArea8to11))
            self.free_12_0.setText(str(event.mFreeArea.mFreeArea12to15))
        finally:
            self._syncing = False

    def _event_title(self, index: int, event: WELEventEntry) -> str:
        return f"{index}: trigger={event.mTriggerId} event={event.mEventId}"

    def _set_detail_enabled(self, enabled: bool):
        for widget in (
            self.trigger_id,
            self.event_id,
            self.joint_hash,
            self.game_object_hash,
            self.tracking,
            self.rotation,
            self.disable_obs_ocl,
            self.update_obs_ocl,
            self.disable_max_obs_ocl_distance,
            self.enable_space_feature,
            self.wait_until_finished,
            self.listener_mask,
            self.id1,
            self.id2,
            self.id3,
            self.booking_timer,
            self.flanging_timer,
            self.global_id,
            self.limit,
            self.priority,
            self.mode,
            self.release_time,
            self.free_0to7,
            self.free_8_0,
            self.free_12_0,
        ):
            widget.setEnabled(enabled)

    def _parse_int(self, text: str, field_name: str, min_val: int, max_val: int) -> int:
        try:
            value = int(text.strip(), 0)
        except ValueError as error:
            raise ValueError(f"{field_name} must be an integer") from error
        if value < min_val or value > max_val:
            raise ValueError(f"{field_name} must be in range {min_val}..{max_val}")
        return value

    def _mode_value(self) -> int:
        data = self.mode.currentData()
        return int(data) if data is not None else 0

    def _set_mode_value(self, value: int):
        idx = self.mode.findData(value)
        self.mode.setCurrentIndex(idx if idx >= 0 else 0)

    def _selected_event_index(self) -> int:
        return self.event_list.currentRow()

    def _current_event(self) -> tuple[WELFile, WELEventEntry, int] | None:
        wel = self.handler.wel
        row = self._selected_event_index()
        if not wel or row < 0 or row >= len(wel.events):
            return None
        return wel, wel.events[row], row

    def _on_detail_widget_changed(self, *_args):
        if self._syncing:
            return

        payload = self._current_event()
        if not payload:
            return
        wel, event, row = payload

        try:
            event.mTriggerId = self._parse_int(self.trigger_id.text(), "trigger_id", 0, 0xFFFFFFFF)
            event.mEventId = self._parse_int(self.event_id.text(), "event_id", 0, 0xFFFFFFFF)
            event.mJointHash = self._parse_int(self.joint_hash.text(), "joint_hash", 0, 0xFFFFFFFF)
            event.mGameObjectHash = self._parse_int(self.game_object_hash.text(), "game_object_hash", 0, 0xFFFFFFFF)
            event.mTracking = 1 if self.tracking.isChecked() else 0
            event.mRotation = 1 if self.rotation.isChecked() else 0
            event.mDisableObsOcl = 1 if self.disable_obs_ocl.isChecked() else 0
            event.mUpdateObsOcl = 1 if self.update_obs_ocl.isChecked() else 0
            event.mDisableMaxObsOclDistance = 1 if self.disable_max_obs_ocl_distance.isChecked() else 0
            event.mEnableSpaceFeature = 1 if self.enable_space_feature.isChecked() else 0
            event.mWaitUntilFinished = 1 if self.wait_until_finished.isChecked() else 0
            event.mListenerMask = self._parse_int(self.listener_mask.text(), "listener_mask", 0, 0xFFFFFFFF)
            event.mPriority = WELPrioritySerialized(
                mId1=self._parse_int(self.id1.text(), "id1", -32768, 32767),
                mId2=self._parse_int(self.id2.text(), "id2", -32768, 32767),
                mId3=self._parse_int(self.id3.text(), "id3", -32768, 32767),
                mBookingTimer=self._parse_int(self.booking_timer.text(), "booking_timer", -32768, 32767),
                mFlangingTimer=self._parse_int(self.flanging_timer.text(), "flanging_timer", -32768, 32767),
                mGlobalId=self._parse_int(self.global_id.text(), "global_id", 0, 255),
                mLimit=self._parse_int(self.limit.text(), "limit", 0, 255),
                mPriority=self._parse_int(self.priority.text(), "priority", 0, 255),
                mMode=self._mode_value(),
                mReleaseTime=self._parse_int(self.release_time.text(), "release_time", -32768, 32767),
            )
            event.mFreeArea = WELFreeArea(
                mFreeArea0to7=self._parse_int(self.free_0to7.text(), "free_0to7", 0, 0xFFFFFFFF),
                mFreeArea8to11=self._parse_int(self.free_8_0.text(), "free_8_0", 0, 0xFFFFFFFF),
                mFreeArea12to15=self._parse_int(self.free_12_0.text(), "free_12_0", -(1 << 63), (1 << 63) - 1),
            )
            wel.events[row] = event
            self.event_list.item(row).setText(self._event_title(row, event))
            self._set_modified(True)
        except ValueError:
            return

    def _on_bank_path_changed(self, text: str):
        wel = self.handler.wel
        if not wel:
            return
        encoded = [ord(char) for char in text[:256]]
        wel.bank_path_raw = encoded + [0] * (256 - len(encoded))
        self._set_modified(True)

    def _on_add_event(self):
        wel = self.handler.wel
        if not wel:
            return

        event = WELEventEntry()
        wel.events.append(event)
        wel.event_count = len(wel.events)

        index = len(wel.events) - 1
        self.event_list.addItem(self._event_title(index, event))
        self.event_list.setCurrentRow(index)
        self._set_modified(True)

    def _on_remove_event(self):
        payload = self._current_event()
        if not payload:
            return

        wel, _, row = payload
        del wel.events[row]
        wel.event_count = len(wel.events)

        self.event_list.takeItem(row)
        self._rebuild_event_titles()

        if wel.events:
            self.event_list.setCurrentRow(min(row, len(wel.events) - 1))
        else:
            self._set_detail_enabled(False)
            self.detail_title.setText("Select an event to edit")

        self._set_modified(True)

    def _rebuild_event_titles(self):
        wel = self.handler.wel
        if not wel:
            return

        self._syncing = True
        try:
            for i, event in enumerate(wel.events):
                item = self.event_list.item(i)
                if item is None:
                    item = QListWidgetItem()
                    self.event_list.addItem(item)
                item.setText(self._event_title(i, event))
        finally:
            self._syncing = False

    def _set_modified(self, value: bool):
        self.handler.modified = value
        self.modified_changed.emit(value)
