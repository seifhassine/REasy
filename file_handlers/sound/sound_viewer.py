"""User-facing BNK/PCK audio and Wwise routing editor."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import wave
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSlider,
    QSplitter,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tools.wem_converter import (
    SAMPLE_RATE_KEEP_SOURCE,
    SAMPLE_RATE_MATCH_ORIGINAL,
    convert_file_to_wwise_wem,
    plan_wwise_sample_rate,
    wem_codec_tag,
)
from tools.wwise_ir_converter import (
    convert_file_to_convolution_reverb_ir,
    convert_file_to_hybrid_reverb_ir,
)
from tools.riff_metadata import (
    RiffMetadataInheritanceError,
    read_riff_metadata,
    write_riff_metadata,
)
from tools.wwise_toolchain import (
    WwiseToolchainError,
    configured_wwise_path,
    set_configured_wwise_path,
    suggested_wwise_browse_path,
    validate_wwise_installation,
    wwise_profile_for_game,
)

from .bnk_parser import (
    WWISE_ANY_OBJECT_ID,
    can_edit_hirc_children,
    clone_hirc_payload,
    compatible_hirc_reference_targets,
    compatible_hirc_reference_types,
    create_play_action_payload,
    create_stop_action_payload,
    export_non_streaming_pck,
    extract_embedded_wem,
    format_bnk_property_value,
    parse_soundbank,
    parse_wem_metadata,
    patch_hirc_reference,
    set_action_fields,
    set_hirc_children,
    wwise_id_from_name,
)
from .sound_graph import EventFlowGraph
from .sound_hirc_editor import ActionPickerDialog, HircPropertiesDialog, WwiseFieldsEditor
from .sound_metadata import SoundMetadata
from .sound_profile import sound_profile_for_handler
from .sound_riff_editor import RiffMetadataDialog
from .sound_resources import (
    local_sound_path,
    open_sound_resource,
    resource_key,
)
from .sound_waveform import (
    WaveformWidget,
    analyze_wave_activity,
    write_wave_channel,
)
from .wwise_schema import STRUCTURED_BANK_VERSIONS, is_hirc_bus, is_hirc_plugin
from .wwise_v132 import set_v132_fields
from .wwise_media import (
    WwiseMediaKind,
    convolution_reverb_to_wav,
    detect_media_kind,
    hybrid_reverb_to_wav,
    midi_to_wwise,
    parse_convolution_reverb_media,
    parse_hybrid_reverb_media,
    parse_wwise_midi,
    validate_crankcase_rev_model,
    wwise_to_midi,
)


_COLUMNS = [
    QT_TRANSLATE_NOOP("SoundViewer", "#"),
    QT_TRANSLATE_NOOP("SoundViewer", "Source ID"),
    QT_TRANSLATE_NOOP("SoundViewer", "Where to edit"),
    QT_TRANSLATE_NOOP("SoundViewer", "Duration"),
    QT_TRANSLATE_NOOP("SoundViewer", "Format"),
    QT_TRANSLATE_NOOP("SoundViewer", "Used by"),
]
_MUTED_TEXT_STYLE = "color: #b8bdc7;"
_AUDIO_FILTER = "Sound Replacement (*.wem *.wav);;WEM Files (*.wem);;WAV Files (*.wav)"
_MIDI_FILTER = "MIDI Replacement (*.mid *.wmid);;MIDI Files (*.mid);;Wwise MIDI (*.wmid)"
_IR_FILTER = "Impulse Response (*.wav *.wir);;WAV Files (*.wav);;Compiled IR (*.wir)"
_REV_FILTER = "Crankcase REV Model (*.model *.adm);;REV Model Files (*.model *.adm)"
_vgmstream_downloader = None


def _asset_preferences(system: str, machine: str) -> list[str]:
    if system == "windows" or os.name == "nt":
        return ["win64" if "64" in machine else "win32"]
    return {"darwin": ["macos", "mac"], "linux": ["linux"]}.get(system, ["win64"])


def _vgmstream_asset_url(tag, assets):
    import platform

    for preference in _asset_preferences(platform.system().lower(), platform.machine().lower()):
        for asset in assets:
            name = asset.get("name", "").lower()
            if preference in name and name.endswith((".zip", ".tar.gz")):
                return asset.get("browser_download_url", "")
    archive = "vgmstream-win64.zip" if os.name == "nt" else "vgmstream-linux-cli.tar.gz"
    base = "https://github.com/vgmstream/vgmstream/releases"
    return f"{base}/download/{tag}/{archive}" if tag else f"{base}/latest/download/{archive}"


def _get_vgmstream_downloader():
    global _vgmstream_downloader
    if _vgmstream_downloader is None:
        from tools.github_downloader import GitHubToolDownloader

        _vgmstream_downloader = GitHubToolDownloader(
            owner_repo="vgmstream/vgmstream",
            cache_subdir="vgmstream_cli",
            exe_name="vgmstream-cli.exe" if os.name == "nt" else "vgmstream-cli",
            asset_url_fn=_vgmstream_asset_url,
            display_name="vgmstream-cli",
        )
    return _vgmstream_downloader


class SoundViewer(QWidget):
    modified_changed = Signal(bool)
    waveform_ready = Signal(object)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self._sound_profile = sound_profile_for_handler(handler)
        self._sound_metadata = (
            self._sound_profile.metadata_for_handler(handler)
            if self._sound_profile else SoundMetadata()
        )
        self._modified = False
        self._parse_result = None
        self._parsed_tracks = []
        self._visible_tracks = []
        self._resolved_media = {}
        self._active_event_id = None
        self._selected_flow_node = (None, None)
        self._event_filter_initialized = False
        self._temp_dir = tempfile.mkdtemp(prefix="reasy_sound_")
        self._current_audio: tuple[str | None, str | None] = (None, None)
        self._preview_audio_path = None
        self._channel_audio_path = None
        self._preview_source_id = None
        self._duration_ms = 0
        self._is_seeking = False
        self._active_ms = []
        self._waveform_job = 0
        self._cleanup_done = False
        self._setup_ui()
        self._update_profile_text()
        self.waveform_ready.connect(self._on_waveform_ready)
        self.destroyed.connect(lambda *_: self._finalize())
        QTimer.singleShot(0, self._on_analyze)

    @property
    def modified(self):
        return self._modified

    @modified.setter
    def modified(self, value):
        value = bool(value)
        if value != self._modified:
            self._modified = value
            self.modified_changed.emit(value)

    def _refresh_sound_profile(self, result=None):
        profile = sound_profile_for_handler(
            self.handler,
            getattr(result, "bank_version", None),
        )
        if profile is self._sound_profile:
            return
        self._sound_profile = profile
        self._sound_metadata = (
            profile.metadata_for_handler(self.handler) if profile else SoundMetadata()
        )
        self._update_profile_text()

    def _update_profile_text(self):
        if not hasattr(self, "profile_note"):
            return
        if self._sound_profile:
            self.profile_note.clear()
            self.profile_note.hide()
        else:
            self.profile_note.setText(self.tr(
                "No sound profile matches this game. IDs are shown numerically, and "
                "linked-file replacement is unavailable."
            ))
            self.profile_note.show()

    def closeEvent(self, event):
        self._finalize()
        super().closeEvent(event)

    def cleanup(self):
        self._finalize()

    def _finalize(self):
        if self._cleanup_done:
            return
        self._cleanup_done = True
        self._cleanup_audio()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _make_btn(self, text, icon, callback, *, enabled=True):
        button = QPushButton(text)
        button.setIcon(self.style().standardIcon(icon))
        button.setEnabled(enabled)
        button.clicked.connect(callback)
        return button

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel(self.tr("Sound Modding"))
        title.setStyleSheet("font-weight: 600; font-size: 18px;")
        self.summary_label = QLabel(self.tr("Reading the sound container…"))
        self.summary_label.setStyleSheet(_MUTED_TEXT_STYLE)
        heading.addWidget(title)
        heading.addWidget(self.summary_label)
        header.addLayout(heading, 1)
        self.analyze_btn = self._make_btn(self.tr("Refresh"), QStyle.SP_BrowserReload, self._on_analyze)
        header.addWidget(self.analyze_btn)
        layout.addLayout(header)
        layout.addWidget(self._build_role_banner())

        self.tabs = QTabWidget()
        self.sound_graph_page = self._build_mod_tab()
        self.all_objects_page = self._build_bank_graph_tab()
        self.bank_settings_page = self._build_bank_settings_tab()
        for page, title in (
            (self.sound_graph_page, self.tr("Audio")),
            (self.all_objects_page, self.tr("All Objects")),
            (self.bank_settings_page, self.tr("Bank Settings")),
        ):
            index = self.tabs.addTab(page, title)
            self.tabs.setTabVisible(index, False)
        layout.addWidget(self.tabs, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(_MUTED_TEXT_STYLE)
        layout.addWidget(self.status)
        self._setup_player()

    def _build_role_banner(self):
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(frame)
        text = QVBoxLayout()
        self.role_title = QLabel(self.tr("Sound container"))
        self.role_title.setStyleSheet("font-weight: 600;")
        self.role_help = QLabel("")
        self.role_help.setWordWrap(True)
        text.addWidget(self.role_title)
        text.addWidget(self.role_help)
        layout.addLayout(text, 1)
        actions = QVBoxLayout()
        self.streaming_pck_btn = self._make_btn(
            self.tr("Open Full Streaming PCK…"),
            QStyle.SP_DirOpenIcon,
            self._on_open_streaming_pck,
        )
        self.streaming_pck_btn.setToolTip(self.tr(
            "Open the full package containing this indexed audio."
        ))
        self.streaming_pck_btn.hide()
        actions.addWidget(self.streaming_pck_btn)
        self.companion_btn = self._make_btn(
            self.tr("Open Companion…"),
            QStyle.SP_DirOpenIcon,
            self._on_open_companion,
        )
        self.companion_btn.hide()
        actions.addWidget(self.companion_btn)
        layout.addLayout(actions)
        return frame

    def _build_mod_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)

        self.event_panel = QGroupBox(self.tr("1. Choose an event"))
        self.event_panel.setMinimumWidth(230)
        self.event_panel.setMaximumWidth(340)
        event_layout = QVBoxLayout(self.event_panel)
        self.event_search = QLineEdit()
        self.event_search.setPlaceholderText(self.tr("Find event name, ShortID, or source ID"))
        self.event_search.textChanged.connect(self._on_event_search_changed)
        event_layout.addWidget(self.event_search)
        self.event_list = QListWidget()
        self.event_list.setMinimumWidth(220)
        self.event_list.currentItemChanged.connect(self._on_event_filter_changed)
        event_layout.addWidget(self.event_list)
        row = QHBoxLayout()
        self.quick_add_event_btn = self._make_btn(self.tr("New Event…"), QStyle.SP_FileDialogNewFolder, self._on_add_event)
        self.quick_edit_event_btn = self._make_btn(self.tr("Edit Actions…"), QStyle.SP_FileDialogDetailedView, self._on_edit_filtered_event, enabled=False)
        row.addWidget(self.quick_add_event_btn)
        row.addWidget(self.quick_edit_event_btn)
        event_layout.addLayout(row)
        self.profile_note = QLabel()
        self.profile_note.setWordWrap(True)
        self.profile_note.setStyleSheet(_MUTED_TEXT_STYLE)
        event_layout.addWidget(self.profile_note)
        self.event_context_label = QLabel("")
        self.event_context_label.setWordWrap(True)
        self.event_context_label.setStyleSheet(_MUTED_TEXT_STYLE)
        event_layout.addWidget(self.event_context_label)
        splitter.addWidget(self.event_panel)

        source_panel = QGroupBox(self.tr("2. Edit playback flow and audio"))
        source_layout = QVBoxLayout(source_panel)
        search = QHBoxLayout()
        self.source_context_label = QLabel(self.tr("All media sources"))
        self.source_context_label.setStyleSheet("font-weight: 600;")
        self.source_search = QLineEdit()
        self.source_search.setPlaceholderText(self.tr("Filter source ID"))
        self.source_search.setMaximumWidth(220)
        self.source_search.textChanged.connect(lambda _text: self._populate(self._parsed_tracks))
        search.addWidget(self.source_context_label)
        search.addStretch()
        search.addWidget(self.source_search)
        source_layout.addLayout(search)

        self.mod_content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.mod_content_splitter.setChildrenCollapsible(False)
        self.mod_content_splitter.setHandleWidth(6)
        source_layout.addWidget(self.mod_content_splitter, 1)

        self.flow_group = QGroupBox(self.tr("Event playback graph"))
        flow = QVBoxLayout(self.flow_group)
        graph_help = QLabel(self.tr(
            "Double-click a node to edit it. Scroll to zoom; drag to move the graph."
        ))
        graph_help.setWordWrap(True)
        graph_help.setStyleSheet(_MUTED_TEXT_STYLE)
        flow.addWidget(graph_help)
        flow_header = QHBoxLayout()
        self.flow_context_label = QLabel("")
        self.flow_context_label.setStyleSheet("font-weight: 600;")
        flow_header.addWidget(self.flow_context_label)
        flow_header.addStretch()
        self.flow_all_objects_btn = self._make_btn(
            self.tr("All Objects"), QStyle.SP_FileDialogDetailedView,
            self._show_flow_in_all_objects,
        )
        flow_header.addWidget(self.flow_all_objects_btn)
        flow_header.addWidget(self._make_btn(self.tr("Fit"), QStyle.SP_DesktopIcon, lambda: self.event_graph.fit_graph()))
        flow.addLayout(flow_header)
        self.event_graph = EventFlowGraph()
        self.event_graph.node_selected.connect(self._on_flow_node_selected)
        self.event_graph.node_activated.connect(self._on_flow_node_activated)
        flow.addWidget(self.event_graph)
        self.flow_selection_label = QLabel(self.tr("Select a node for details; double-click to edit or preview."))
        self.flow_selection_label.setWordWrap(True)
        self.flow_selection_label.setStyleSheet(_MUTED_TEXT_STYLE)
        flow.addWidget(self.flow_selection_label)
        context_actions = QHBoxLayout()
        self.flow_edit_btn = self._make_btn(self.tr("Edit…"), QStyle.SP_FileDialogContentsView, self._on_edit_flow_node, enabled=False)
        self.flow_add_action_btn = self._make_btn(self.tr("Add Action…"), QStyle.SP_FileDialogNewFolder, self._on_add_action, enabled=False)
        self.flow_detach_btn = self._make_btn(self.tr("Detach Action"), QStyle.SP_ArrowLeft, self._on_detach_flow_action, enabled=False)
        self.flow_connect_btn = self._make_btn(self.tr("Connect Child…"), QStyle.SP_ArrowRight, self._on_connect_flow_child, enabled=False)
        self.flow_disconnect_btn = self._make_btn(self.tr("Disconnect Child…"), QStyle.SP_ArrowLeft, self._on_disconnect_flow_child, enabled=False)
        self.flow_delete_btn = self._make_btn(self.tr("Delete Event"), QStyle.SP_TrashIcon, self._on_delete_filtered_event, enabled=False)
        for button in (
            self.flow_edit_btn, self.flow_add_action_btn, self.flow_detach_btn,
            self.flow_connect_btn, self.flow_disconnect_btn, self.flow_delete_btn,
        ):
            context_actions.addWidget(button)
        flow.addLayout(context_actions)
        self.mod_content_splitter.addWidget(self.flow_group)

        media_panel = QWidget()
        media_layout = QVBoxLayout(media_panel)
        media_layout.setContentsMargins(0, 0, 0, 0)
        media_layout.setSpacing(6)

        media_layout.addWidget(self._build_table(), 1)
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card_layout = QVBoxLayout(card)
        self.source_title = QLabel(self.tr("Select a media source"))
        self.source_title.setStyleSheet("font-weight: 600;")
        self.source_help = QLabel("")
        self.source_help.setWordWrap(True)
        self.source_usage = QLabel("")
        self.source_usage.setWordWrap(True)
        self.source_usage.setStyleSheet(_MUTED_TEXT_STYLE)
        card_layout.addWidget(self.source_title)
        card_layout.addWidget(self.source_help)
        card_layout.addWidget(self.source_usage)
        media_layout.addWidget(card)

        actions = QGridLayout()
        self.play_btn = self._make_btn(self.tr("Preview"), QStyle.SP_MediaPlay, self._on_play, enabled=False)
        self.stop_btn = self._make_btn(self.tr("Stop"), QStyle.SP_MediaStop, self._on_stop, enabled=False)
        self.rep_wem = self._make_btn(self.tr("Replace Audio…"), QStyle.SP_BrowserReload, self._on_replace, enabled=False)
        self.rep_wem.setToolTip(self.tr(
            "WAV import matches the original WEM codec and, by default, its sample "
            "rate. WAV metadata wins when provided; otherwise REasy inherits the "
            "original loops, cue points, and marker labels and verifies the authored WEM."
        ))
        self.meta_wem = self._make_btn(self.tr("Loop / Markers…"), QStyle.SP_FileDialogDetailedView, self._on_edit_wem_metadata, enabled=False)
        self.exp_wem = self._make_btn(self.tr("Export WEM…"), QStyle.SP_DialogSaveButton, self._on_export_wem, enabled=False)
        self.exp_wav = self._make_btn(self.tr("Export WAV…"), QStyle.SP_DialogSaveButton, self._on_export_wav, enabled=False)
        for index, button in enumerate((self.play_btn, self.stop_btn, self.rep_wem, self.meta_wem, self.exp_wem, self.exp_wav)):
            actions.addWidget(button, index // 3, index % 3)
        media_layout.addLayout(actions)

        rate = QHBoxLayout()
        rate.addWidget(QLabel(self.tr("WAV import rate")))
        self.sample_rate_combo = QComboBox()
        self.sample_rate_combo.addItem(
            self.tr("Match original WEM (Recommended)"),
            SAMPLE_RATE_MATCH_ORIGINAL,
        )
        self.sample_rate_combo.addItem(
            self.tr("Keep imported WAV rate"), SAMPLE_RATE_KEEP_SOURCE
        )
        self.sample_rate_combo.setToolTip(self.tr(
            "Matching the original keeps the game's storage and runtime profile. "
            "A codec-required rate, such as RE4 WEM Opus at 48 kHz, always wins."
        ))
        rate.addWidget(self.sample_rate_combo, 1)
        media_layout.addLayout(rate)

        batch = QHBoxLayout()
        self.rep_bulk = self._make_btn(self.tr("Bulk Replace…"), QStyle.SP_DirOpenIcon, self._on_bulk_replace)
        self.add_pck_source = self._make_btn(self.tr("Add PCK Source…"), QStyle.SP_FileDialogNewFolder, self._on_add_pck_source)
        self.exp_all = self._make_btn(self.tr("Export All…"), QStyle.SP_DialogSaveButton, self._on_export_all)
        self.exp_pck = self._make_btn(self.tr("Export Non-Streaming PCK…"), QStyle.SP_DialogSaveButton, self._on_export_pck)
        for button in (self.rep_bulk, self.exp_all, self.add_pck_source, self.exp_pck):
            batch.addWidget(button)
        media_layout.addLayout(batch)
        media_layout.addWidget(self._build_preview_controls())
        self.mod_content_splitter.addWidget(media_panel)
        self.mod_content_splitter.setStretchFactor(0, 3)
        self.mod_content_splitter.setStretchFactor(1, 3)
        self.mod_content_splitter.setSizes([700, 700])
        self.flow_group.hide()
        splitter.addWidget(source_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 1000])
        layout.addWidget(splitter)
        return page

    def _build_bank_graph_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.bank_capability_label = QLabel("")
        self.bank_capability_label.setWordWrap(True)
        self.bank_capability_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.bank_capability_label)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)
        browser = QWidget()
        browser.setMinimumWidth(230)
        browser.setMaximumWidth(340)
        browser_layout = QVBoxLayout(browser)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        self.bank_type_filter = QComboBox()
        self.bank_type_filter.currentIndexChanged.connect(self._populate_bank_objects)
        browser_layout.addWidget(self.bank_type_filter)
        self.bank_search = QLineEdit()
        self.bank_search.setPlaceholderText(self.tr("Find type, ShortID, source ID, or capability"))
        self.bank_search.textChanged.connect(self._populate_bank_objects)
        browser_layout.addWidget(self.bank_search)
        self.bank_object_list = QListWidget()
        self.bank_object_list.currentItemChanged.connect(self._on_bank_object_changed)
        browser_layout.addWidget(self.bank_object_list)
        splitter.addWidget(browser)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        self.hirc_duplicate_btn = self._make_btn(self.tr("Duplicate…"), QStyle.SP_FileDialogNewFolder, self._on_duplicate_hirc, enabled=False)
        self.hirc_rename_btn = self._make_btn(self.tr("Rename ID…"), QStyle.SP_FileDialogDetailedView, self._on_rename_hirc, enabled=False)
        self.hirc_delete_btn = self._make_btn(self.tr("Delete"), QStyle.SP_TrashIcon, self._on_delete_hirc, enabled=False)
        self.hirc_properties_btn = self._make_btn(self.tr("Properties…"), QStyle.SP_FileDialogContentsView, self._on_edit_hirc, enabled=False)
        self.hirc_connect_btn = self._make_btn(self.tr("Connect Child…"), QStyle.SP_ArrowRight, self._on_connect_hirc, enabled=False)
        self.hirc_disconnect_btn = self._make_btn(self.tr("Disconnect Child…"), QStyle.SP_ArrowLeft, self._on_disconnect_hirc, enabled=False)
        self.hirc_reference_btn = self._make_btn(self.tr("Retarget Link…"), QStyle.SP_BrowserReload, self._on_retarget_hirc_reference, enabled=False)
        common = QHBoxLayout()
        for button in (
            self.hirc_properties_btn, self.hirc_duplicate_btn,
            self.hirc_connect_btn, self.hirc_disconnect_btn,
        ):
            common.addWidget(button)
        common.addStretch()
        editor_layout.addLayout(common)
        advanced = QGroupBox(self.tr("Advanced / destructive"))
        advanced_layout = QHBoxLayout(advanced)
        for button in (
            self.hirc_rename_btn, self.hirc_delete_btn,
            self.hirc_reference_btn,
        ):
            advanced_layout.addWidget(button)
        advanced_layout.addStretch()
        editor_layout.addWidget(advanced)
        self.bank_graph = EventFlowGraph()
        self.bank_graph.node_selected.connect(self._on_bank_graph_node_selected)
        self.bank_graph.node_activated.connect(self._on_bank_graph_node_activated)
        editor_layout.addWidget(self.bank_graph, 1)
        self.bank_detail = QLabel(self.tr("Select a HIRC object."))
        self.bank_detail.setWordWrap(True)
        self.bank_detail.setStyleSheet(_MUTED_TEXT_STYLE)
        editor_layout.addWidget(self.bank_detail)
        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([285, 1000])
        layout.addWidget(splitter, 1)
        return page

    def _build_bank_settings_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel(self.tr(
            "Double-click an editable value to change a global SoundBank setting."
        ))
        hint.setWordWrap(True)
        hint.setStyleSheet(_MUTED_TEXT_STYLE)
        layout.addWidget(hint)
        selector = QHBoxLayout()
        selector.addWidget(QLabel(self.tr("Chunk")))
        self.bank_chunk_combo = QComboBox()
        self.bank_chunk_combo.currentIndexChanged.connect(self._show_bank_chunk)
        selector.addWidget(self.bank_chunk_combo, 1)
        self.bank_chunk_apply = self._make_btn(
            self.tr("Apply Settings"), QStyle.SP_DialogApplyButton,
            self._apply_bank_chunk, enabled=False,
        )
        selector.addWidget(self.bank_chunk_apply)
        layout.addLayout(selector)
        self.bank_chunk_note = QLabel("")
        self.bank_chunk_note.setWordWrap(True)
        self.bank_chunk_note.setStyleSheet(_MUTED_TEXT_STYLE)
        layout.addWidget(self.bank_chunk_note)
        self.bank_chunk_host = QWidget()
        self.bank_chunk_layout = QVBoxLayout(self.bank_chunk_host)
        self.bank_chunk_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.bank_chunk_host, 1)
        self._bank_chunk_editor = None
        return page

    def _build_preview_controls(self):
        group = QGroupBox(self.tr("Preview controls"))
        layout = QVBoxLayout(group)
        position = QHBoxLayout()
        self.pos_cur = QLabel("0:00")
        self.pos_slider = QSlider(Qt.Horizontal)
        self.pos_slider.setEnabled(False)
        self.pos_tot = QLabel("0:00")
        position.addWidget(self.pos_cur)
        position.addWidget(self.pos_slider, 1)
        position.addWidget(self.pos_tot)
        layout.addLayout(position)
        options = QHBoxLayout()
        options.addWidget(QLabel(self.tr("Volume")))
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(70)
        self.vol_slider.setMaximumWidth(180)
        options.addWidget(self.vol_slider)
        options.addWidget(QLabel(self.tr("Speed")))
        self.speed_combo = QComboBox()
        for speed in (0.50, 0.75, 1.00, 1.25, 1.50, 2.00):
            self.speed_combo.addItem(f"{speed:.2f}×", speed)
        self.speed_combo.setCurrentIndex(2)
        options.addWidget(self.speed_combo)
        options.addWidget(QLabel(self.tr("Listen")))
        self.channel_combo = QComboBox()
        self.channel_combo.addItem(self.tr("Mix"), -1)
        self.channel_combo.setEnabled(False)
        self.channel_combo.setToolTip(
            self.tr("Preview the complete mix or solo one channel; audio data is not modified.")
        )
        options.addWidget(self.channel_combo)
        self.skip_btn = self._make_btn(
            self.tr("Skip Silence"),
            QStyle.SP_MediaSkipForward,
            self._on_skip,
            enabled=False,
        )
        options.addWidget(self.skip_btn)
        options.addStretch()
        layout.addLayout(options)
        self.waveform = WaveformWidget()
        layout.addWidget(self.waveform)
        return group

    def _build_table(self):
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([self.tr(value) for value in _COLUMNS])
        header = self.table.horizontalHeader()
        for column in (0, 1, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        for column in (2, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._on_sel)
        self.table.itemDoubleClicked.connect(self._on_dbl)
        return self.table

    def _setup_player(self):
        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.audio_out.setVolume(0.7)
        self.player.setAudioOutput(self.audio_out)
        self.vol_slider.valueChanged.connect(lambda value: self.audio_out.setVolume(value / 100))
        self.speed_combo.currentIndexChanged.connect(lambda index: self.player.setPlaybackRate(float(self.speed_combo.itemData(index))))
        self.channel_combo.currentIndexChanged.connect(self._on_channel_changed)
        self.pos_slider.sliderPressed.connect(lambda: setattr(self, "_is_seeking", True))
        self.pos_slider.sliderReleased.connect(self._on_seek_done)
        self.pos_slider.sliderMoved.connect(lambda value: self.pos_cur.setText(self._fmt_ms(value)))
        self.waveform.seek_requested.connect(self._on_waveform_seek)
        self.player.durationChanged.connect(self._on_duration)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_state)

    def _on_duration(self, milliseconds):
        self._duration_ms = max(0, milliseconds)
        self.pos_slider.setRange(0, self._duration_ms)
        self.pos_slider.setEnabled(bool(self._duration_ms))
        self.pos_tot.setText(self._fmt_ms(self._duration_ms))

    def _on_position(self, milliseconds):
        if not self._is_seeking:
            self.pos_slider.setValue(max(0, milliseconds))
            self.pos_cur.setText(self._fmt_ms(milliseconds))
            self.waveform.set_position(
                milliseconds / self._duration_ms if self._duration_ms else 0.0
            )

    def _on_seek_done(self):
        self._is_seeking = False
        self.player.setPosition(self.pos_slider.value())

    def _on_waveform_seek(self, permille):
        if self._duration_ms:
            self.player.setPosition(permille * self._duration_ms // 1000)

    def _on_skip(self):
        next_start = next(
            (start for start, _end in self._active_ms if start > self.player.position() + 250),
            None,
        )
        if next_start is None:
            self.status.setText(self.tr("No later activity segment was found."))
        else:
            self.player.setPosition(next_start)
            self.status.setText(
                self.tr("Skipped silence to {time}.").format(time=self._fmt_ms(next_start))
            )

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.stop_btn.setEnabled(playing)
        if not playing and state == QMediaPlayer.PlaybackState.StoppedState:
            self.pos_slider.setValue(0)
            self.pos_cur.setText("0:00")
            self.waveform.set_position(0.0)

    def _selected(self):
        row = self.table.currentRow()
        item = self.table.item(row, 1) if row >= 0 else None
        source_id = item.data(Qt.UserRole) if item else None
        track = next((value for value in self._parsed_tracks if value.source_id == source_id), None)
        return (track.index, track) if track else (None, None)

    def _require_track(self, action, *, require_available=True):
        index, track = self._selected()
        if track is None:
            QMessageBox.warning(self, self.tr("{action} Error").format(action=action), self.tr("Select a media source first."))
            return None
        if require_available and not (track.available and track.payload_complete):
            QMessageBox.warning(self, self.tr("{action} Error").format(action=action), self._track_status(track)[1])
            return None
        return index, track

    def _on_sel(self):
        self._update_source_card()

    def _on_dbl(self, _item):
        if self.play_btn.isEnabled():
            self._on_play()

    def _vgmstream(self):
        settings = getattr(self.handler.app, "settings", {}) if self.handler.app else {}
        configured = str(settings.get("vgmstream_cli_path", "")).strip()
        return shutil.which(configured) or shutil.which("vgmstream-cli") or shutil.which("vgmstream-cli.exe")

    def _prompt_vgmstream_download(self):
        downloader = _get_vgmstream_downloader()
        needs_download, latest = downloader.status()
        if not needs_download:
            return str(downloader.exe_path) if downloader.exe_path.exists() else None
        if QMessageBox.question(
            self,
            self.tr("Download VGMStream CLI?"),
            self.tr("VGMStream is required for preview and WAV export. Download {version} now?").format(version=latest or self.tr("the latest release")),
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return None
        try:
            executable = downloader.ensure(auto_download=True, parent_window=self)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Download Failed"), str(exc))
            return None
        if self.handler.app and isinstance(getattr(self.handler.app, "settings", None), dict):
            self.handler.app.settings["vgmstream_cli_path"] = str(executable)
            try:
                from settings import save_settings

                save_settings(self.handler.app.settings)
            except Exception:
                pass
        return str(executable)

    @staticmethod
    def _remove_file(path):
        try:
            if path:
                os.remove(path)
        except OSError:
            pass

    def _cleanup_audio(self):
        self._waveform_job += 1
        self._active_ms = []
        self.skip_btn.setEnabled(False)
        self.waveform.clear()
        try:
            self.player.stop()
            self.player.setSource(QUrl())
        except RuntimeError:
            pass
        for path in {*self._current_audio, self._channel_audio_path}:
            self._remove_file(path)
        self._current_audio = (None, None)
        self._preview_audio_path = None
        self._channel_audio_path = None
        self._preview_source_id = None
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()
        self.channel_combo.addItem(self.tr("Mix"), -1)
        self.channel_combo.setEnabled(False)
        self.channel_combo.blockSignals(False)

    @staticmethod
    def _run_vgmstream(arguments):
        kwargs = {"check": False, "capture_output": True, "text": True}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            return subprocess.run(arguments, **kwargs)
        except OSError as exc:
            return subprocess.CompletedProcess(arguments, 1, stderr=str(exc))

    def _media_kind(self, track, payload=b""):
        kind = getattr(track, "media_kind", WwiseMediaKind.UNKNOWN)
        plugin_id = getattr(track, "plugin_id", None)
        if plugin_id is None:
            plugins = self._sound_metadata.media_plugin_ids(track.source_id)
            plugin_id = plugins[0] if len(plugins) == 1 else None
        return (
            detect_media_kind(payload, plugin_id)
            if kind == WwiseMediaKind.UNKNOWN and (payload or plugin_id is not None)
            else kind
        )

    def _is_split_prefetch(self, track) -> bool:
        if not (track and track.stream_type is None and track.available):
            return False
        path = self.handler.filepath or getattr(self.handler, "filename", "")
        return bool(self._sound_metadata.prefetch_event_banks(track.source_id, path))

    def _complete_media(self, track) -> bytes:
        key = (resource_key(self.handler.filepath or ""), track.source_id)
        if key not in self._resolved_media:
            self._resolved_media[key] = (
                self._sound_profile.resolve_replacement(
                    self.handler, self._parse_result, track
                ).original_wem
                if self._sound_profile and self._parse_result
                and self._is_split_prefetch(track)
                else extract_embedded_wem(self.handler.raw_data, track)
            )
        return self._resolved_media[key]

    def _media_sample_rate(self):
        return getattr(self._sound_profile, "sound_engine_sample_rate", 48_000)

    def _decode_track(self, track):
        media = self._complete_media(track)
        kind = self._media_kind(track, media)
        ir_exporters = {
            WwiseMediaKind.HYBRID_REVERB_IR: lambda: hybrid_reverb_to_wav(
                media, self._media_sample_rate()
            ),
            WwiseMediaKind.CONVOLUTION_REVERB_IR: lambda: convolution_reverb_to_wav(media),
        }
        if kind in ir_exporters:
            descriptor, wav_path = tempfile.mkstemp(dir=self._temp_dir, suffix=".wav")
            os.close(descriptor)
            try:
                Path(wav_path).write_bytes(ir_exporters[kind]())
            except (OSError, ValueError):
                self._remove_file(wav_path)
                raise
            return None, wav_path
        if kind != WwiseMediaKind.AUDIO:
            return None, None
        return self._decode_wem(media)

    def _decode_wem(self, wem_data):
        executable = self._vgmstream() or self._prompt_vgmstream_download()
        if not wem_data or not executable:
            return None, None
        wem_fd, wem_path = tempfile.mkstemp(dir=self._temp_dir, suffix=".wem")
        wav_fd, wav_path = tempfile.mkstemp(dir=self._temp_dir, suffix=".wav")
        os.close(wem_fd)
        os.close(wav_fd)
        Path(wem_path).write_bytes(wem_data)
        # vgmstream otherwise renders looped WEMs multiple times and appends a
        # synthetic fade. Preview and activity analysis need one native pass.
        result = self._run_vgmstream([executable, "-i", "-o", wav_path, wem_path])
        if result.returncode or not Path(wav_path).stat().st_size:
            self._remove_file(wem_path)
            self._remove_file(wav_path)
            QMessageBox.warning(self, self.tr("Decode Error"), (result.stderr or result.stdout or self.tr("VGMStream could not decode this WEM.")).strip())
            return None, None
        return wem_path, wav_path

    def _configure_channels(self, wav_path):
        try:
            with wave.open(str(wav_path), "rb") as source:
                channels = source.getnchannels()
        except (OSError, wave.Error):
            channels = 1
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()
        self.channel_combo.addItem(
            self.tr("Mono") if channels == 1 else self.tr("Mix ({count} channels)").format(count=channels),
            -1,
        )
        if channels > 1:
            for channel in range(channels):
                self.channel_combo.addItem(self.tr("Channel {number}").format(number=channel + 1), channel)
        self.channel_combo.setCurrentIndex(0)
        self.channel_combo.setEnabled(channels > 1)
        self.channel_combo.blockSignals(False)

    def _start_preview(self, wav_path, *, keep_position=False):
        position = self.player.position() if keep_position else 0
        self._preview_audio_path = wav_path
        self.player.setSource(QUrl.fromLocalFile(wav_path))
        if position:
            self.player.setPosition(position)
        self.player.play()
        self._queue_waveform(wav_path)
        self.status.setText(
            self.tr("Playing source {id} — {channels}; analyzing activity…").format(
                id=self._preview_source_id,
                channels=self.channel_combo.currentText(),
            )
        )

    def _on_channel_changed(self, index):
        mix_path = self._current_audio[1]
        if not mix_path or index < 0:
            return
        channel = int(self.channel_combo.itemData(index))
        new_path = mix_path
        try:
            if channel >= 0:
                descriptor, new_path = tempfile.mkstemp(dir=self._temp_dir, suffix=".wav")
                os.close(descriptor)
                write_wave_channel(mix_path, new_path, channel)
        except (OSError, ValueError, wave.Error) as exc:
            self._remove_file(new_path if new_path != mix_path else None)
            QMessageBox.warning(self, self.tr("Channel Preview"), str(exc))
            return
        self._remove_file(self._channel_audio_path)
        self._channel_audio_path = new_path if new_path != mix_path else None
        self._start_preview(new_path, keep_position=True)

    def _on_play(self):
        selected = self._require_track(self.tr("Playback"))
        if not selected:
            return
        self._cleanup_audio()
        _, track = selected
        self.status.setText(self.tr("Decoding source {id}…").format(id=track.source_id))
        try:
            self._current_audio = self._decode_track(track)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Preview Error"), str(exc))
            self.status.setText(self.tr("Decode failed."))
            return
        if not self._current_audio[1]:
            self.status.setText(self.tr("Decode failed."))
            return
        self._preview_source_id = track.source_id
        self._configure_channels(self._current_audio[1])
        self._start_preview(self._current_audio[1])

    def _queue_waveform(self, wav_path):
        self._waveform_job += 1
        job = self._waveform_job
        width = max(300, self.waveform.width())

        def analyze():
            payload = analyze_wave_activity(wav_path, width)
            with suppress(RuntimeError):
                self.waveform_ready.emit((job, wav_path, payload))

        threading.Thread(target=analyze, daemon=True).start()

    def _on_waveform_ready(self, result):
        job, wav_path, payload = result
        if job != self._waveform_job or wav_path != self._preview_audio_path:
            return
        if payload is None:
            self.status.setText(self.tr("Playing audio; waveform analysis is unavailable."))
            return
        self._active_ms = payload["active_ms"]
        skippable = len(self._active_ms) > 1 or any(start > 250 for start, _end in self._active_ms)
        self.skip_btn.setEnabled(skippable)
        self.waveform.set_data(payload["peaks"], payload["ranges"])
        coverage = sum(end - start for start, end in payload["ranges"])
        if coverage >= 0.995:
            summary = self.tr("no silence detected")
        elif not self._active_ms:
            summary = self.tr("no activity detected")
        else:
            summary = self.tr("{count} activity segment(s) detected").format(
                count=len(self._active_ms),
            )
        self.status.setText(
            self.tr("Source {id} — {channels}: {result}.").format(
                id=self._preview_source_id,
                channels=self.channel_combo.currentText(),
                result=summary,
            )
        )

    def _on_stop(self):
        self._cleanup_audio()
        self.status.clear()

    @staticmethod
    def _save_path(parent, title, default, extension, file_filter):
        path, _ = QFileDialog.getSaveFileName(parent, title, default, file_filter)
        if path and not path.lower().endswith(extension):
            path += extension
        return path or None

    def _write_export(self, path, data, label):
        try:
            Path(path).write_bytes(data)
        except OSError as exc:
            QMessageBox.warning(self, self.tr("Export Error"), self.tr("Failed to export {label}:\n{error}").format(label=label, error=exc))
            return False
        self.status.setText(self.tr("{label} exported to {path}").format(label=label, path=path))
        return True

    def _on_export_wem(self):
        selected = self._require_track(self.tr("Export"))
        if not selected:
            return
        _, track = selected
        kind = self._media_kind(track)
        title, extension, file_filter, label = {
            WwiseMediaKind.AUDIO: (self.tr("Export WEM"), ".wem", "WEM Files (*.wem)", "WEM"),
            WwiseMediaKind.MIDI: (self.tr("Export Wwise MIDI"), ".wmid", "Wwise MIDI (*.wmid)", self.tr("Wwise MIDI")),
            WwiseMediaKind.HYBRID_REVERB_IR: (self.tr("Export Compiled IR"), ".wir", "Compiled IR (*.wir)", self.tr("compiled IR")),
            WwiseMediaKind.CONVOLUTION_REVERB_IR: (self.tr("Export Compiled IR"), ".wir", "Compiled IR (*.wir)", self.tr("compiled IR")),
            WwiseMediaKind.CRANKCASE_REV_MODEL: (self.tr("Export REV Model"), ".model", _REV_FILTER, self.tr("REV model")),
        }.get(kind, (self.tr("Export Raw Media"), ".bin", "Binary Files (*.bin)", self.tr("raw media")))
        path = self._save_path(
            self, title, f"{track.source_id}{extension}", extension, file_filter
        )
        if path:
            try:
                self._write_export(path, self._complete_media(track), label)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, self.tr("Export Error"), str(exc))

    def _on_export_wav(self):
        selected = self._require_track(self.tr("Export"))
        if not selected:
            return
        _, track = selected
        try:
            media = self._complete_media(track)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Export Error"), str(exc))
            return
        kind = self._media_kind(track, media)
        if kind == WwiseMediaKind.MIDI:
            path = self._save_path(
                self, self.tr("Export MIDI"), f"{track.source_id}.mid", ".mid",
                "MIDI Files (*.mid)",
            )
            if path:
                try:
                    self._write_export(path, wwise_to_midi(media), self.tr("MIDI"))
                except ValueError as exc:
                    QMessageBox.warning(self, self.tr("Export Error"), str(exc))
            return
        if kind == WwiseMediaKind.HYBRID_REVERB_IR:
            path = self._save_path(
                self, self.tr("Export Processed IR WAV"), f"{track.source_id}.wav",
                ".wav", "WAV Files (*.wav)",
            )
            if path:
                try:
                    self._write_export(
                        path,
                        hybrid_reverb_to_wav(media, self._media_sample_rate()),
                        self.tr("processed early-reflection WAV"),
                    )
                except ValueError as exc:
                    QMessageBox.warning(self, self.tr("Export Error"), str(exc))
            return
        if kind == WwiseMediaKind.CONVOLUTION_REVERB_IR:
            path = self._save_path(
                self, self.tr("Export Processed IR WAV"), f"{track.source_id}.wav",
                ".wav", "WAV Files (*.wav)",
            )
            if path:
                try:
                    self._write_export(
                        path, convolution_reverb_to_wav(media),
                        self.tr("processed convolution IR WAV"),
                    )
                except ValueError as exc:
                    QMessageBox.warning(self, self.tr("Export Error"), str(exc))
            return
        if kind != WwiseMediaKind.AUDIO:
            QMessageBox.warning(
                self, self.tr("Export Error"),
                self.tr("This unknown payload has no safe editable export format."),
            )
            return
        path = self._save_path(self, self.tr("Export WAV"), f"{track.source_id}.wav", ".wav", "WAV Files (*.wav)")
        if not path:
            return
        temporary = self._decode_track(track)
        try:
            if not temporary[1]:
                return
            shutil.copyfile(temporary[1], path)
            self.status.setText(self.tr("WAV exported to {path}").format(path=path))
        except OSError as exc:
            QMessageBox.warning(self, self.tr("Export Error"), str(exc))
        finally:
            for value in temporary:
                self._remove_file(value)

    def _on_edit_wem_metadata(self):
        selected = self._require_track(self.tr("Loop / Markers"), require_available=False)
        if not selected:
            return
        _, track = selected
        try:
            plan = self._replacement_plan(track)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Loop / Markers"), str(exc))
            return
        if self._media_kind(track, plan.original_wem) != WwiseMediaKind.AUDIO:
            QMessageBox.information(
                self, self.tr("Loop / Markers"),
                self.tr("Loop, cue, and marker chunks apply only to RIFF WEM audio."),
            )
            return
        try:
            metadata = read_riff_metadata(plan.original_wem)
        except ValueError as exc:
            QMessageBox.warning(self, self.tr("Loop / Markers"), str(exc))
            return
        dialog = RiffMetadataDialog(metadata, self, profile=self._sound_profile)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        edited = dialog.edited_metadata()
        if edited == metadata:
            self.status.setText(self.tr("Loop and marker metadata was unchanged."))
            return
        installation = self._wwise_installation()
        if installation is None:
            return
        if not self._confirm_replacement_plans((plan,)):
            return
        temporary = self._decode_wem(plan.original_wem)
        try:
            if not temporary[1]:
                return
            wav_path = Path(temporary[1])
            edited_wav = write_riff_metadata(wav_path.read_bytes(), edited)
            wav_path.write_bytes(edited_wav)
            authored = convert_file_to_wwise_wem(
                wav_path,
                game=installation.profile.game,
                installation=installation,
                preserve_metadata_from=edited_wav,
                match_codec_from=plan.original_wem,
            )
            verified = read_riff_metadata(authored)
            if verified.loops != edited.loops or verified.markers != edited.markers:
                raise ValueError(self.tr("Wwise did not preserve the requested loop/cue metadata."))
            count, changes = self._apply_replacement_plan(plan, authored)
            self.status.setText(
                self.tr("Updated {loops} loop(s) and {markers} marker(s) in source {id} across {count} verified file(s).").format(
                    loops=len(edited.loops), markers=len(edited.markers),
                    id=track.source_id, count=count,
                )
            )
            self.status.setText("\n".join((self.status.text(), *changes)))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Loop / Markers"), str(exc))
        finally:
            for value in temporary:
                self._remove_file(value)

    def _on_export_pck(self):
        path = self._save_path(self, self.tr("Export Non-Streaming PCK"), "sound_non_streaming.pck", ".pck", "PCK Files (*.pck)")
        if path:
            self._write_export(path, export_non_streaming_pck(self.handler.raw_data), self.tr("Non-streaming PCK"))

    def _on_export_all(self):
        tracks = [track for track in self._parsed_tracks if track.available and track.payload_complete]
        if not tracks:
            QMessageBox.information(self, self.tr("Export All"), self.tr("No complete media sources are available."))
            return
        modes = [self.tr("Compiled media"), self.tr("Editable media"), self.tr("Both")]
        mode, accepted = QInputDialog.getItem(
            self, self.tr("Export All Sources"), self.tr("Export format:"), modes, 2, False
        )
        directory = QFileDialog.getExistingDirectory(self, self.tr("Export All Tracks"), "") if accepted else ""
        if not directory:
            return
        compiled = mode in (modes[0], modes[2])
        editable = mode in (modes[1], modes[2])
        progress = QProgressDialog(self.tr("Exporting media…"), self.tr("Cancel"), 0, len(tracks), self)
        failures = []
        for index, track in enumerate(tracks):
            progress.setValue(index)
            progress.setLabelText(self.tr("Exporting source {id}").format(id=track.source_id))
            QApplication.processEvents()
            if progress.wasCanceled():
                break
            try:
                media = self._complete_media(track)
            except (OSError, ValueError):
                failures.append(str(track.source_id))
                continue
            kind = self._media_kind(track, media)
            raw_extension = {
                WwiseMediaKind.AUDIO: ".wem",
                WwiseMediaKind.MIDI: ".wmid",
                WwiseMediaKind.HYBRID_REVERB_IR: ".wir",
                WwiseMediaKind.CONVOLUTION_REVERB_IR: ".wir",
                WwiseMediaKind.CRANKCASE_REV_MODEL: ".model",
            }.get(kind, ".bin")
            if compiled:
                try:
                    Path(directory, f"{track.source_id}{raw_extension}").write_bytes(media)
                except OSError:
                    failures.append(f"{track.source_id}{raw_extension}")
            if editable:
                extension = {
                    WwiseMediaKind.MIDI: ".mid",
                    WwiseMediaKind.CRANKCASE_REV_MODEL: ".model",
                }.get(kind, ".wav")
                target = Path(directory, f"{track.source_id}{extension}")
                temporary = (None, None)
                try:
                    if kind == WwiseMediaKind.MIDI:
                        target.write_bytes(wwise_to_midi(media))
                    elif kind == WwiseMediaKind.HYBRID_REVERB_IR:
                        target.write_bytes(
                            hybrid_reverb_to_wav(media, self._media_sample_rate())
                        )
                    elif kind == WwiseMediaKind.CONVOLUTION_REVERB_IR:
                        target.write_bytes(convolution_reverb_to_wav(media))
                    elif kind == WwiseMediaKind.CRANKCASE_REV_MODEL:
                        validate_crankcase_rev_model(media)
                        target.write_bytes(media)
                    elif kind == WwiseMediaKind.AUDIO:
                        temporary = self._decode_track(track)
                        if not temporary[1]:
                            raise OSError("decode failed")
                        shutil.copyfile(temporary[1], target)
                    else:
                        raise ValueError("unknown media")
                except (OSError, ValueError):
                    failures.append(target.name)
                finally:
                    for value in temporary:
                        self._remove_file(value)
        progress.setValue(len(tracks))
        self.status.setText(self.tr("Export finished: {count} source(s), {failed} failure(s).").format(count=len(tracks), failed=len(failures)))
        if failures:
            QMessageBox.warning(self, self.tr("Export All"), self.tr("Failed: {files}").format(files=", ".join(failures[:12])))

    def _wwise_game(self):
        return self._sound_profile.game if self._sound_profile else ""

    def _sample_rate_policy(self):
        return self.sample_rate_combo.currentData() or SAMPLE_RATE_MATCH_ORIGINAL

    @staticmethod
    def _rate_text(sample_rate):
        return (
            f"{sample_rate / 1000:g} kHz" if sample_rate else "unknown rate"
        )

    def _wav_rate_plan(self, path, original_wem=b""):
        profile = wwise_profile_for_game(self._wwise_game())
        if profile is None:
            return None
        return plan_wwise_sample_rate(
            path,
            profile,
            original_wem or None,
            self._sample_rate_policy(),
        )

    def _rate_plan_text(self, plan):
        text = self.tr("{source} → {target}").format(
            source=self._rate_text(plan.source_rate),
            target=self._rate_text(plan.target_rate),
        )
        if plan.forced_by_codec:
            text += self.tr(" (required by codec)")
        elif plan.policy == SAMPLE_RATE_MATCH_ORIGINAL and plan.original_rate:
            text += self.tr(" (matched original WEM)")
        else:
            text += self.tr(" (kept imported WAV)")
        return text

    def _wwise_installation(self, *, always_prompt=False):
        profile = wwise_profile_for_game(self._wwise_game())
        if profile is None:
            QMessageBox.warning(
                self,
                self.tr("Wwise Not Configured"),
                self.tr(
                    "Wwise authoring is unavailable because this game has no "
                    "registered sound profile."
                ),
            )
            return None
        settings = getattr(self.handler.app, "settings", None)
        settings = settings if isinstance(settings, dict) else {}
        configured = configured_wwise_path(settings, profile.game)
        if configured and not always_prompt:
            try:
                return validate_wwise_installation(configured, profile.game)
            except WwiseToolchainError:
                pass

        QMessageBox.information(
            self,
            self.tr("Wwise Installation Required — {game}").format(game=profile.display_name),
            profile.requirement_message() + "\n\n" + self.tr("Select the version folder containing Authoring."),
        )
        start = suggested_wwise_browse_path(configured)
        while True:
            selected = QFileDialog.getExistingDirectory(
                self,
                self.tr("Select Wwise {version} for {game}").format(version=profile.required_version_text, game=profile.display_name),
                start,
                QFileDialog.Option.ShowDirsOnly,
            )
            if not selected:
                return None
            try:
                installation = validate_wwise_installation(selected, profile.game)
            except WwiseToolchainError as exc:
                retry = QMessageBox.warning(self, self.tr("Incompatible Wwise Installation"), str(exc), QMessageBox.Retry | QMessageBox.Cancel, QMessageBox.Retry)
                if retry != QMessageBox.Retry:
                    return None
                start = selected
                continue
            if settings is not None and self.handler.app:
                set_configured_wwise_path(settings, profile.game, installation.root)
                try:
                    from settings import save_settings

                    save_settings(settings)
                except Exception:
                    pass
            return installation

    def _read_replacement_audio(
        self,
        path,
        *,
        installation=None,
        original_wem=b"",
        preserve_original_metadata=True,
        sample_rate_policy=None,
    ):
        suffix = Path(path).suffix.casefold()
        if suffix == ".wem":
            data = Path(path).read_bytes()
            if detect_media_kind(data) != WwiseMediaKind.AUDIO:
                raise ValueError(self.tr("The selected WEM is not RIFF/WAVE audio."))
            return data, wem_codec_tag(data)
        if suffix != ".wav":
            raise ValueError(self.tr("Only WEM and WAV replacement files are supported."))
        profile = wwise_profile_for_game(self._wwise_game())
        if profile is None:
            raise ValueError(
                self.tr("Wwise WAV authoring is not configured for this game.")
            )
        installation = installation or self._wwise_installation()
        if installation is None:
            return b"", None
        authored = convert_file_to_wwise_wem(
            path,
            game=profile.game,
            installation=installation,
            preserve_metadata_from=(
                original_wem if preserve_original_metadata and original_wem else None
            ),
            match_codec_from=original_wem or None,
            sample_rate_policy=(
                sample_rate_policy or self._sample_rate_policy()
            ),
        )
        return authored, wem_codec_tag(authored)

    def _read_replacement_media(
        self,
        path,
        track,
        *,
        installation=None,
        original=b"",
        preserve_original_metadata=True,
        sample_rate_policy=None,
    ):
        kind = self._media_kind(track, original)
        suffix = Path(path).suffix.casefold()
        if kind == WwiseMediaKind.AUDIO:
            return self._read_replacement_audio(
                path,
                installation=installation,
                original_wem=original,
                preserve_original_metadata=preserve_original_metadata,
                sample_rate_policy=sample_rate_policy,
            )
        if kind == WwiseMediaKind.MIDI:
            if suffix == ".mid":
                data = midi_to_wwise(Path(path).read_bytes())
            elif suffix == ".wmid":
                data = Path(path).read_bytes()
                parse_wwise_midi(data)
            else:
                raise ValueError(self.tr("Wwise MIDI accepts MID or WMID replacement files."))
            return data, None
        if kind == WwiseMediaKind.HYBRID_REVERB_IR:
            if suffix == ".wir":
                data = Path(path).read_bytes()
                replacement = parse_hybrid_reverb_media(data)
                current = parse_hybrid_reverb_media(original)
                if replacement.tuning != current.tuning:
                    raise ValueError(self.tr(
                        "The compiled IR uses different decay tuning. Import its WAV "
                        "instead so Wwise can author it with this effect's existing tuning."
                    ))
                return data, None
            if suffix != ".wav":
                raise ValueError(self.tr("Hybrid Reverb accepts WAV or compiled WIR files."))
            installation = installation or self._wwise_installation()
            if installation is None:
                return b"", None
            return convert_file_to_hybrid_reverb_ir(
                path,
                game=installation.profile.game,
                installation=installation,
                preserve_tuning_from=original or None,
            ), None
        if kind == WwiseMediaKind.CONVOLUTION_REVERB_IR:
            if suffix == ".wir":
                data = Path(path).read_bytes()
                parse_convolution_reverb_media(data)
                return data, None
            if suffix != ".wav":
                raise ValueError(self.tr(
                    "Wwise Convolution Reverb accepts WAV or compiled WIR files."
                ))
            installation = installation or self._wwise_installation()
            if installation is None:
                return b"", None
            return convert_file_to_convolution_reverb_ir(
                path,
                game=installation.profile.game,
                installation=installation,
            ), None
        if kind == WwiseMediaKind.CRANKCASE_REV_MODEL:
            if suffix not in {".model", ".adm"}:
                raise ValueError(self.tr(
                    "Crankcase REV accepts a compiled MODEL or ADM file. "
                    "Creating a model from recordings requires the licensed REV authoring tool."
                ))
            data = Path(path).read_bytes()
            validate_crankcase_rev_model(data)
            return data, None
        raise ValueError(
            self.tr(
                "REasy cannot identify this payload. It can be exported raw, but "
                "replacement is disabled to avoid corrupting non-audio media."
            )
        )

    def _can_replace_track(self, track):
        if not (self._sound_profile and self._parse_result and track):
            return False
        kind = self._media_kind(track)
        if kind == WwiseMediaKind.HYBRID_REVERB_IR:
            return self._sound_profile.supports_hybrid_reverb_ir
        if kind == WwiseMediaKind.CONVOLUTION_REVERB_IR:
            return self._sound_profile.supports_convolution_reverb_ir
        if kind in (
            WwiseMediaKind.AUDIO,
            WwiseMediaKind.MIDI,
            WwiseMediaKind.CRANKCASE_REV_MODEL,
        ):
            return True
        # An index-only PCK has no bytes to sniff. Its replacement plan resolves
        # and verifies the full package before the import dialog is shown.
        return not track.available

    def _replacement_plan(self, track):
        if not self._parse_result:
            raise ValueError(self.tr("Analyze the sound file before replacing audio."))
        if self._sound_profile is None:
            raise ValueError(
                self.tr("No sound profile is registered for this game.")
            )
        plan = self._sound_profile.resolve_replacement(
            self.handler, self._parse_result, track
        )
        related = [path for path, _role in plan.output_roles() if path != plan.current_path]
        context = getattr(self.handler, "resource_context", None)
        current = str(self.handler.filepath or self.handler.filename).replace("\\", "/")
        if related and not getattr(context, "project_dir", "") and not (
            Path(current).is_absolute() and "natives/" in current.casefold()
        ):
            raise ValueError(
                self.tr("Automatic multi-file replacement requires an active REasy project.")
            )
        return plan

    def _confirm_replacement_plans(self, plans):
        files = {}
        for plan in plans:
            files.update(dict(plan.output_roles()))
        if len(files) <= 1:
            return True
        details = "\n".join(f"• {role}: {path}" for path, role in files.items())
        return QMessageBox.question(
            self,
            self.tr("Confirm Related Sound Files"),
            self.tr(
                "REasy verified the Source ID relationships and will save these files together:\n\n"
            ) + details,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        ) == QMessageBox.Yes

    def _apply_replacement_plan(self, plan, wem_data):
        outputs, changes = plan.build_outputs(wem_data, report_changes=True)
        self.handler.apply_replacement_outputs(outputs)
        self._apply_result(parse_soundbank(self.handler.raw_data))
        self.modified = True
        return len(outputs), changes

    def _confirm_replacement_scope(self, source_ids):
        selected = set(source_ids)
        prefetch = any(track.source_id in selected and track.available and not track.payload_complete for track in self._parsed_tracks)
        partial_pck = bool(
            self._parse_result
            and self._parse_result.container_type.lower() == "pck"
            and any(not track.available and track.source_id not in selected for track in self._parsed_tracks)
        )
        notes = []
        if prefetch:
            notes.append(self.tr("Some selected BNK entries are prefetch fragments. Replace the same source IDs in the matching PCK."))
        if partial_pck:
            notes.append(self.tr("This is a partial PCK; unrelated missing media cannot be preserved by this file."))
        return not notes or QMessageBox.question(
            self,
            self.tr("Confirm Sound Replacement"),
            "\n\n".join(notes) + "\n\n" + self.tr("Continue?"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) == QMessageBox.Yes

    def _refresh_tracks(self):
        try:
            self.handler.raw_data = self.handler.rebuild()
            result = parse_soundbank(self.handler.raw_data)
        except Exception as exc:
            QMessageBox.warning(self, self.tr("Sound Edit Error"), str(exc))
            return False
        self._apply_result(result)
        self.modified = True
        return True

    def _replacement_dialog_spec(self, kind, source_id):
        return {
            WwiseMediaKind.AUDIO: (
                self.tr("Select Replacement Audio"), f"{source_id}.wem", _AUDIO_FILTER
            ),
            WwiseMediaKind.MIDI: (
                self.tr("Select Replacement MIDI"), f"{source_id}.mid", _MIDI_FILTER
            ),
            WwiseMediaKind.HYBRID_REVERB_IR: (
                self.tr("Select Replacement Impulse Response"),
                f"{source_id}.wav", _IR_FILTER,
            ),
            WwiseMediaKind.CONVOLUTION_REVERB_IR: (
                self.tr("Select Replacement Impulse Response"),
                f"{source_id}.wav", _IR_FILTER,
            ),
            WwiseMediaKind.CRANKCASE_REV_MODEL: (
                self.tr("Select Replacement REV Model"),
                f"{source_id}.model", _REV_FILTER,
            ),
        }.get(kind)

    def _on_replace(self):
        selected = self._require_track(self.tr("Replace"), require_available=False)
        if not selected:
            return
        _, track = selected
        try:
            plan = self._replacement_plan(track)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Replace Error"), str(exc))
            return
        kind = self._media_kind(track, plan.original_wem)
        dialog_spec = self._replacement_dialog_spec(kind, track.source_id)
        if dialog_spec is None:
            QMessageBox.warning(
                self, self.tr("Replace Error"),
                self.tr("This payload is unknown and cannot be replaced safely."),
            )
            return
        if not self._confirm_replacement_plans((plan,)):
            return
        path, _ = QFileDialog.getOpenFileName(self, *dialog_spec)
        if not path:
            return
        rate_policy = self._sample_rate_policy()
        rate_plan = None
        try:
            if Path(path).suffix.casefold() == ".wav" and kind == WwiseMediaKind.AUDIO:
                rate_plan = self._wav_rate_plan(path, plan.original_wem)
                if rate_plan:
                    self.status.setText(
                        self.tr("Authoring {file}: {rates}…").format(
                            file=Path(path).name,
                            rates=self._rate_plan_text(rate_plan),
                        )
                    )
                    QApplication.processEvents()
            try:
                data, codec = self._read_replacement_media(
                    path, track, original=plan.original_wem,
                    sample_rate_policy=rate_policy,
                )
            except RiffMetadataInheritanceError as exc:
                discard = QMessageBox.question(
                    self,
                    self.tr("Loop / Marker Metadata Does Not Fit"),
                    str(exc) + "\n\n" + self.tr(
                        "Replace the audio without inheriting the original loops and markers?"
                    ),
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if discard != QMessageBox.Yes:
                    return
                data, codec = self._read_replacement_media(
                    path,
                    track,
                    original=plan.original_wem,
                    preserve_original_metadata=False,
                    sample_rate_policy=rate_policy,
                )
            if not data:
                return
            count, changes = self._apply_replacement_plan(plan, data)
            authoring = wwise_profile_for_game(self._wwise_game())
            authored_codec = authoring.wem_codec(codec) if authoring else None
            encoding = authored_codec.name if authored_codec else {
                WwiseMediaKind.MIDI: self.tr("Wwise MIDI"),
                WwiseMediaKind.HYBRID_REVERB_IR: self.tr("Hybrid Reverb IR"),
                WwiseMediaKind.CONVOLUTION_REVERB_IR: self.tr("Convolution Reverb IR"),
                WwiseMediaKind.CRANKCASE_REV_MODEL: self.tr("Crankcase Audio REV model"),
            }.get(kind, self.tr("WEM"))
            if authored_codec:
                original_codec = authoring.wem_codec(wem_codec_tag(plan.original_wem))
                encoding += self.tr(" (matched original)") if (
                    original_codec == authored_codec
                ) else self.tr(" (profile default)")
            self.status.setText(
                self.tr("Replaced source {id} in {count} verified file(s) with {file} ({encoding}){rates}.").format(
                    id=track.source_id, count=count, file=Path(path).name,
                    encoding=encoding,
                    rates=(
                        " · " + self._rate_plan_text(rate_plan)
                        if rate_plan else ""
                    ),
                )
            )
            self.status.setText("\n".join((self.status.text(), *changes)))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Replace Error"), str(exc))

    def _on_add_pck_source(self):
        if not self._parse_result or self._parse_result.container_type.lower() != "pck":
            return
        if export_non_streaming_pck(self.handler.raw_data) == self.handler.raw_data:
            QMessageBox.warning(
                self,
                self.tr("Add PCK Source"),
                self.tr(
                    "This is the index-only PCK. Open the full streaming PCK "
                    "before adding a source; REasy will keep its index in sync."
                ),
            )
            return
        source_id = self._prompt_new_hirc_id(self.tr("Add PCK Source"))
        if source_id is None:
            return
        if any(track.source_id == source_id for track in self._parsed_tracks):
            QMessageBox.warning(self, self.tr("Add PCK Source"), self.tr("Source ID {id} already exists.").format(id=source_id))
            return
        if not self._confirm_replacement_scope((source_id,)):
            return
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select Audio for Source {id}").format(id=source_id),
            f"{source_id}.wem", _AUDIO_FILTER,
        )
        if not path:
            return
        try:
            rate_plan = (
                self._wav_rate_plan(path)
                if Path(path).suffix.casefold() == ".wav" else None
            )
            if rate_plan:
                self.status.setText(
                    self.tr("Authoring {file}: {rates}…").format(
                        file=Path(path).name,
                        rates=self._rate_plan_text(rate_plan),
                    )
                )
                QApplication.processEvents()
            data, _codec = self._read_replacement_audio(
                path, sample_rate_policy=self._sample_rate_policy()
            )
            if not data:
                return
            self.handler.replace_track_data(source_id, data)
            if self._refresh_tracks():
                self._stage_pck_index()
                self._select_source_id(source_id)
                self.status.setText(
                    self.tr("Added streamed PCK source {id} from {file}.").format(
                        id=source_id, file=Path(path).name
                    )
                )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Add PCK Source"), str(exc))

    def _stage_pck_index(self):
        """Regenerate the header-only index PCK for the rewritten streaming PCK."""

        path = str(self.handler.filepath or self.handler.filename)
        if self._sound_profile and (paths := self._sound_profile.related_paths(path)):
            self.handler.apply_replacement_outputs({
                paths.index_pck: export_non_streaming_pck(self.handler.raw_data)
            })

    def _on_bulk_replace(self):
        directory = QFileDialog.getExistingDirectory(self, self.tr("Select Replacement Folder"), "")
        if not directory:
            return
        replaceable = {str(track.source_id): track for track in self._parsed_tracks if self._can_replace_track(track)}
        files = {
            path.stem: path
            for path in Path(directory).iterdir()
            if path.is_file()
            and path.suffix.casefold() in {
                ".wem", ".wav", ".mid", ".wmid", ".wir", ".model", ".adm",
            }
            and path.stem in replaceable
        }
        if not files:
            QMessageBox.information(self, self.tr("Bulk Replace"), self.tr("No file names match a replaceable source ID."))
            return
        plans = {}
        try:
            plans = {
                source: self._replacement_plan(replaceable[source])
                for source in files
            }
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Bulk Replace"), str(exc))
            return
        if not self._confirm_replacement_plans(plans.values()):
            return
        needs_wwise = any(
            files[source].suffix.casefold() == ".wav"
            and self._media_kind(
                replaceable[source], plans[source].original_wem
            ) in (
                WwiseMediaKind.AUDIO,
                WwiseMediaKind.HYBRID_REVERB_IR,
                WwiseMediaKind.CONVOLUTION_REVERB_IR,
            )
            for source in files
        )
        installation = self._wwise_installation() if needs_wwise else None
        if needs_wwise and installation is None:
            return

        progress = QProgressDialog(self.tr("Replacing media…"), self.tr("Cancel"), 0, len(files), self)
        failures = []
        authored = {}
        rate_policy = self._sample_rate_policy()
        for index, source in enumerate(sorted(files, key=int)):
            progress.setValue(index)
            progress.setLabelText(self.tr("Replacing source {id}").format(id=source))
            QApplication.processEvents()
            if progress.wasCanceled():
                break
            try:
                kind = self._media_kind(
                    replaceable[source], plans[source].original_wem
                )
                if files[source].suffix.casefold() == ".wav" and kind == WwiseMediaKind.AUDIO:
                    rate_plan = self._wav_rate_plan(
                        files[source], plans[source].original_wem
                    )
                    if rate_plan:
                        progress.setLabelText(
                            self.tr("Replacing source {id} · {rates}").format(
                                id=source, rates=self._rate_plan_text(rate_plan)
                            )
                        )
                        QApplication.processEvents()
                data, _ = self._read_replacement_media(
                    files[source],
                    replaceable[source],
                    installation=installation,
                    original=plans[source].original_wem,
                    sample_rate_policy=rate_policy,
                )
                if not data:
                    raise ValueError(self.tr("No authored media was produced."))
                authored[int(source)] = data
            except (OSError, ValueError):
                failures.append(files[source].name)
        progress.setValue(len(files))
        replaced = len(authored)
        if replaced:
            outputs = self._sound_profile.build_replacement_outputs(
                (plans[str(source_id)] for source_id in authored), authored
            )
            self.handler.apply_replacement_outputs(outputs)
            self._apply_result(parse_soundbank(self.handler.raw_data))
            self.modified = True
        self.status.setText(self.tr("Bulk replace: {done}/{total} succeeded.").format(done=replaced, total=len(files)))
        if failures:
            QMessageBox.warning(self, self.tr("Bulk Replace"), self.tr("Failed: {files}").format(files=", ".join(failures[:12])))

    def _on_analyze(self):
        try:
            result = parse_soundbank(self.handler.raw_data)
        except Exception as exc:
            QMessageBox.warning(self, self.tr("Analyze Error"), str(exc))
            return
        self._apply_result(result)
        self.status.setText(
            self.tr("{type}: {sources} source(s), {events} event(s).").format(
                type=result.container_type.upper(),
                sources=len(result.tracks),
                events=len(result.events),
            )
        )

    def _apply_result(self, result):
        self._refresh_sound_profile(result)
        self._resolved_media.clear()
        self._parse_result = result
        self._parsed_tracks = result.tracks
        self._update_role_banner(result)
        self._populate_event_filters(result)
        self._populate_bank_filters(result)
        self._populate_bank_objects()
        self._populate_bank_chunks(result)
        self._populate(result.tracks)
        is_pck = result.container_type.lower() == "pck"
        available = (
            (self.sound_graph_page, bool(result.tracks or result.events)),
            (self.all_objects_page, not is_pck and bool(result.objects)),
            (self.bank_settings_page, not is_pck and bool(result.bank_chunks)),
        )
        for page, shown in available:
            index = self.tabs.indexOf(page)
            self.tabs.setTabEnabled(index, shown)
            self.tabs.setTabVisible(index, shown)
        self.tabs.setTabText(
            self.tabs.indexOf(self.sound_graph_page),
            self.tr("Events & Audio") if result.events and result.tracks else
            self.tr("Events") if result.events else self.tr("Audio"),
        )
        self.flow_all_objects_btn.setVisible(available[1][1])
        current = self.tabs.currentWidget()
        if current is None or not any(page is current and shown for page, shown in available):
            first = next((page for page, shown in available if shown), None)
            if first is not None:
                self.tabs.setCurrentWidget(first)
        self.exp_pck.setVisible(is_pck)
        self.add_pck_source.setVisible(bool(is_pck and self._sound_profile))
        self.quick_add_event_btn.setEnabled(self._bank_edits_supported())
        self.rep_bulk.setEnabled(any(self._can_replace_track(track) for track in result.tracks))
        self.exp_all.setEnabled(any(track.available and track.payload_complete for track in result.tracks))

    def _populate_bank_chunks(self, result):
        current = self.bank_chunk_combo.currentData()
        self.bank_chunk_combo.blockSignals(True)
        self.bank_chunk_combo.clear()
        for index, chunk in enumerate(result.bank_chunks):
            self.bank_chunk_combo.addItem(
                f"{chunk.title} ({chunk.chunk_id})", index
            )
        selected = self.bank_chunk_combo.findData(current)
        self.bank_chunk_combo.setCurrentIndex(selected if selected >= 0 else 0)
        self.bank_chunk_combo.blockSignals(False)
        self._show_bank_chunk()

    def _show_bank_chunk(self, _index=None):
        while self.bank_chunk_layout.count():
            item = self.bank_chunk_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._bank_chunk_editor = None
        index = self.bank_chunk_combo.currentData()
        chunks = self._parse_result.bank_chunks if self._parse_result else ()
        if index is None or not 0 <= int(index) < len(chunks):
            self.bank_chunk_note.clear()
            self.bank_chunk_apply.setEnabled(False)
            return
        chunk = chunks[int(index)]
        structure = chunk.structure
        if not structure.complete:
            self.bank_chunk_note.setText(
                self.tr("Layout error: {error}").format(error=structure.error)
            )
            self.bank_chunk_apply.setEnabled(False)
            return
        editable = sum(field.visible and field.editable for field in structure.fields)
        self.bank_chunk_note.setText(
            self.tr("{count} editable setting(s)").format(
                count=editable
            )
        )
        self._bank_chunk_editor = WwiseFieldsEditor(
            structure, self._sound_metadata, parent=self.bank_chunk_host
        )
        self.bank_chunk_layout.addWidget(self._bank_chunk_editor)
        self.bank_chunk_apply.setEnabled(bool(editable))

    def _apply_bank_chunk(self):
        editor = self._bank_chunk_editor
        index = self.bank_chunk_combo.currentData()
        chunks = self._parse_result.bank_chunks if self._parse_result else ()
        if editor is None or index is None or not 0 <= int(index) < len(chunks):
            return
        chunk = chunks[int(index)]
        try:
            changes = editor.changes()
            if not changes:
                self.status.setText(self.tr("No bank-setting values changed."))
                return
            payload = set_v132_fields(chunk.payload, chunk.structure, changes)
            self.handler.set_bank_chunk_payload(chunk.chunk_id, payload)
            if not self._refresh_tracks():
                return
            self.status.setText(
                self.tr("Updated {title} ({chunk}).").format(
                    title=chunk.title, chunk=chunk.chunk_id
                )
            )
        except (OverflowError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Bank Settings Error"), str(exc))

    def _selected_bank_object(self):
        item = self.bank_object_list.currentItem()
        object_id = item.data(Qt.UserRole) if item else None
        object_index = item.data(Qt.UserRole + 1) if item else None
        return next(
            (
                obj for obj in (self._parse_result.objects if self._parse_result else ())
                if obj.object_id == object_id and (object_index is None or obj.index == object_index)
            ),
            None,
        )

    def _select_bank_object(self, object_id=None, *, type_id=None, object_index=None):
        objects_by_index = {
            obj.index: obj for obj in (self._parse_result.objects if self._parse_result else ())
        }
        for row in range(self.bank_object_list.count()):
            item = self.bank_object_list.item(row)
            item_id = item.data(Qt.UserRole)
            item_index = item.data(Qt.UserRole + 1)
            obj = objects_by_index.get(item_index)
            if (
                (object_id is None or item_id == object_id)
                and (object_index is None or item_index == object_index)
                and (type_id is None or (obj and obj.type_id == type_id))
            ):
                self.bank_object_list.setCurrentRow(row)
                self.bank_object_list.scrollToItem(item)
                return True
        if self.bank_search.text():
            self.bank_search.clear()
            return self._select_bank_object(
                object_id, type_id=type_id, object_index=object_index
            )
        if self.bank_type_filter.currentIndex() > 0:
            self.bank_type_filter.setCurrentIndex(0)
            return self._select_bank_object(
                object_id, type_id=type_id, object_index=object_index
            )
        return False

    def _populate_bank_objects(self, _text=None):
        if not hasattr(self, "bank_object_list"):
            return
        selected = self._selected_bank_object()
        selected_index = selected.index if selected else None
        needle = self.bank_search.text().strip().casefold()
        scope = self.bank_type_filter.currentData() or ""
        objects = self._parse_result.objects if self._parse_result else ()
        id_counts = {obj.object_id: 0 for obj in objects}
        typed_counts = {(obj.object_id, obj.type_id): 0 for obj in objects}
        for obj in objects:
            id_counts[obj.object_id] += 1
            typed_counts[(obj.object_id, obj.type_id)] += 1
        self.bank_object_list.blockSignals(True)
        self.bank_object_list.clear()
        selected_row = -1
        for obj in objects:
            base_level = self._hirc_edit_level(obj)
            collision = typed_counts[(obj.object_id, obj.type_id)] > 1
            if collision:
                edit_level = self.tr("Type + ID collision · read-only")
            elif id_counts[obj.object_id] > 1:
                edit_level = self.tr("Shared ShortID · {level}").format(level=base_level)
            else:
                edit_level = base_level
            if scope == "structured" and (base_level == "Read-only" or collision):
                continue
            if scope == "read_only" and base_level != "Read-only":
                continue
            if str(scope).startswith("type:") and obj.type_id != int(str(scope)[5:], 16):
                continue
            recovered = (
                self._sound_metadata.search_text("event", obj.object_id, event=True)
                if obj.type_id == 0x04 else
                self._action_summary(obj) if obj.type_id == 0x03 else
                self._playback_summary(obj)
            )
            source_line = ", ".join(
                f"{source.source_id} (0x{source.source_id:08X})"
                for source in obj.sources
            )
            if source_line:
                recovered = f"{recovered}\nSources: {source_line}"
            haystack = f"{obj.type_name} {obj.object_id} 0x{obj.object_id:08X} {edit_level} {recovered}".casefold()
            if needle and needle not in haystack:
                continue
            title = obj.type_name
            if obj.type_id == 0x04:
                names = self._sound_metadata.event_names(obj.object_id)
                if names:
                    title += f" · {' / '.join(names)}"
            item = QListWidgetItem(
                self.tr("{type}\n{id} · {editing}").format(
                    type=title, id=obj.object_id, editing=edit_level
                )
            )
            item.setData(Qt.UserRole, obj.object_id)
            item.setData(Qt.UserRole + 1, obj.index)
            item.setToolTip(self.tr(
                "HIRC type 0x{type:02X} · {bytes} payload bytes\n{detail}"
            ).format(type=obj.type_id, bytes=len(obj.payload), detail=recovered))
            self.bank_object_list.addItem(item)
            if obj.index == selected_index:
                selected_row = self.bank_object_list.count() - 1
        if selected_row < 0 and self.bank_object_list.count():
            selected_row = 0
        if selected_row >= 0:
            self.bank_object_list.setCurrentRow(selected_row)
        self.bank_object_list.blockSignals(False)
        self._on_bank_object_changed(self.bank_object_list.currentItem(), None)

    def _populate_bank_filters(self, result):
        current = self.bank_type_filter.currentData()
        self.bank_type_filter.blockSignals(True)
        self.bank_type_filter.clear()
        self.bank_type_filter.addItem(self.tr("All object types"), "")
        self.bank_type_filter.addItem(self.tr("Structured editing"), "structured")
        self.bank_type_filter.addItem(self.tr("Read-only"), "read_only")
        seen = set()
        for obj in result.objects:
            if obj.type_id in seen:
                continue
            seen.add(obj.type_id)
            self.bank_type_filter.addItem(obj.type_name, f"type:{obj.type_id:02x}")
        index = self.bank_type_filter.findData(current)
        self.bank_type_filter.setCurrentIndex(max(0, index))
        self.bank_type_filter.blockSignals(False)
        id_counts = {}
        typed_counts = {}
        for obj in result.objects:
            id_counts[obj.object_id] = id_counts.get(obj.object_id, 0) + 1
            key = (obj.object_id, obj.type_id)
            typed_counts[key] = typed_counts.get(key, 0) + 1
        collisions = sum(count for count in typed_counts.values() if count > 1)
        colliding_ids = {
            object_id
            for (object_id, _type_id), count in typed_counts.items()
            if count > 1
        }
        structured = sum(
            typed_counts[(obj.object_id, obj.type_id)] == 1
            and self._hirc_edit_level(obj) != "Read-only"
            for obj in result.objects
        )
        read_only = len(result.objects) - structured - collisions
        read_only_types = {}
        for obj in result.objects:
            if (
                typed_counts[(obj.object_id, obj.type_id)] == 1
                and self._hirc_edit_level(obj) == "Read-only"
            ):
                read_only_types[obj.type_name] = read_only_types.get(obj.type_name, 0) + 1
        shared = sum(
            count > 1 and object_id not in colliding_ids
            for object_id, count in id_counts.items()
        )
        read_only_detail = ", ".join(
            f"{name}: {count}" for name, count in sorted(read_only_types.items())
        )
        self.bank_capability_label.setText(
            self.tr(
                "Editable objects: {structured}/{total} · Read-only: {read_only}"
                " · Conflicting IDs: {collisions} · Shared IDs: {shared}{detail}"
            ).format(
                structured=structured,
                total=len(result.objects),
                read_only=max(0, read_only),
                collisions=collisions,
                shared=shared,
                detail=(
                    f"\nPreserved read-only types — {read_only_detail}"
                    if read_only_detail else ""
                ),
            )
        )

    @staticmethod
    def _hirc_tone(obj):
        return "event" if obj.type_id == 0x04 else "action" if obj.type_id == 0x03 else "object"

    def _bank_edits_supported(self):
        result = self._parse_result
        version = result.bank_version if result else None
        return bool(
            result
            and result.container_type.lower() == "bnk"
            and version in STRUCTURED_BANK_VERSIONS
        )

    def _hirc_edit_level(self, obj):
        if not self._bank_edits_supported():
            return "Read-only"
        features = []
        if obj.type_id == 0x04 and obj.event_action_ids is not None:
            features.append("Actions")
        if obj.type_id == 0x03 and obj.action_type is not None:
            features.append("Action")
        if obj.sources:
            features.append("Audio")
        if obj.property_bundle is not None:
            features.append("Playback")
        if obj.random_sequence:
            features.append("Playlist")
        if obj.switch_container:
            features.append("Switches")
        if obj.music_segment:
            features.append("Cues")
        if obj.music_track:
            features.append("Clips")
        if obj.attenuation:
            features.append("Attenuation curves")
        if obj.silence_source:
            features.append("Silence timing")
        if obj.fx_plugin:
            features.append(
                "Generator parameters"
                if obj.fx_plugin.plugin_id & 0x0F == 0x02
                else "Effect parameters"
            )
        if can_edit_hirc_children(obj) and not (obj.random_sequence or obj.switch_container or obj.music_segment):
            features.append("Children")
        if obj.structure and obj.structure.complete:
            features.append("All Wwise settings")
        return " + ".join(features) or "Read-only"

    def _hirc_read_only_reason(self, obj):
        if obj.structure and obj.structure.error:
            return self.tr("The exact Wwise layout could not be completed: {error}").format(
                error=obj.structure.error
            )
        if is_hirc_plugin(obj.type_id, obj.bank_version):
            return (
                "This Wwise plug-in's parameter layout is not decoded, so its payload is "
                "preserved unchanged."
            )
        return "This object layout is not decoded; its bytes are preserved unchanged."

    def _action_summary(self, action):
        settings = action.settings if hasattr(action, "settings") else action.action_settings
        kind = action.target_kind if hasattr(action, "target_kind") else action.action_target_kind
        raw_id = action.raw_id if hasattr(action, "raw_id") else (action.action_raw_id or 0)
        target_id = action.target_id if hasattr(action, "target_id") else (action.action_target_id or 0)
        if kind in {"state", "switch"} and settings:
            return (
                f"{self._sound_metadata.label(f'{kind}_group', settings.group_id or 0)} → "
                f"{self._sound_metadata.label(f'{kind}_value', settings.value_id or 0)}"
            )
        if kind == "game_parameter":
            name = self._sound_metadata.label("game_parameter", raw_id)
            value = f" = {settings.value:g}" if settings and settings.value is not None else ""
            return f"{name}{value}"
        if kind == "trigger":
            return self._sound_metadata.id_label("trigger", raw_id)
        if kind == "event":
            return self._sound_metadata.id_label("event", target_id, event=True)
        return (
            self._sound_metadata.external_object_label(target_id)
            if self._sound_metadata.external_object(target_id) else
            self.tr("Target object {id}").format(id=target_id)
        )

    def _playback_summary(self, obj):
        if obj.type_id == 0x04:
            return self._sound_metadata.id_label("event", obj.object_id, event=True)
        if obj.type_id == 0x03:
            return f"ID {obj.object_id}\n{self._action_summary(obj)}"
        if obj.switch_container:
            data = obj.switch_container
            prefix = "switch" if data.group_type == 0 else "state"
            return (
                f"ID {obj.object_id}\n"
                f"{self._sound_metadata.label(f'{prefix}_group', data.group_id)} · "
                f"default {self._sound_metadata.label(f'{prefix}_value', data.default_value_id)}"
            )
        if obj.fx_plugin:
            count = len(obj.fx_plugin.parameters)
            detail = f"{count} settings" if count else "structured settings and curves"
            return f"ID {obj.object_id}\n{obj.fx_plugin.name} · {detail}"
        if obj.silence_source:
            data = obj.silence_source
            random = max(
                abs(data.random_minus_seconds), abs(data.random_plus_seconds)
            )
            suffix = f" ± up to {random:g}s" if random else ""
            return f"ID {obj.object_id}\n{data.duration_seconds:g}s silence{suffix}"
        if obj.attenuation:
            active = sum(index >= 0 for index in obj.attenuation.assignments)
            return f"ID {obj.object_id}\n{len(obj.attenuation.curves)} curves · {active} assignments"
        bundle = obj.property_bundle
        if not bundle:
            return f"ShortID {obj.object_id}"
        labels = (
            {
                0x00: "Vol", 0x02: "Pitch", 0x06: "Initial delay",
                (0x11 if bundle.bank_version == 132 else 0x10): "Priority",
            }
            if bundle.kind == "state" else
            {
                0x00: "Vol", 0x02: "Pitch", 0x07: "Priority",
                0x0F: "Delay", 0x10: "Fade", 0x11: "Chance",
                0x3A: "Loop", 0x3B: "Initial delay",
            }
        )
        values = []
        for item in bundle.values:
            if item.property_id not in labels:
                continue
            value = format_bnk_property_value(
                item.property_id, item.value_bits, bundle.kind, bundle.bank_version
            )
            if item.property_id == 0x3A and value == "0":
                value = "∞"
            values.append(f"{labels[item.property_id]} {value}")
        suffix = " · ".join(values[:3])
        return f"ID {obj.object_id}" + (f"\n{suffix}" if suffix else "")

    def _bank_graph_model(self, selected):
        objects_by_id = {}
        for obj in self._parse_result.objects:
            objects_by_id.setdefault(obj.object_id, []).append(obj)
        tracks = {track.source_id: track for track in self._parsed_tracks}
        nodes, edges = {}, []

        def add(obj, depth, detail=None):
            shared = len(objects_by_id[obj.object_id]) > 1
            key = ("hirc_index", obj.index) if shared else ("hirc", obj.object_id)
            nodes.setdefault(key, {
                "key": key,
                "kind": "hirc_index" if shared else "hirc",
                "object_id": obj.index if shared else obj.object_id,
                "title": obj.type_name,
                "detail": detail or self._playback_summary(obj),
                "tone": self._hirc_tone(obj), "depth": depth,
            })
            return key

        root = add(selected, 1, self.tr("Selected · ShortID {id}").format(id=selected.object_id))
        for source in self._parse_result.objects:
            fields = [
                field for field in source.reference_fields
                if field.target_id == selected.object_id and field.role != "parent"
            ]
            if not fields or source.index == selected.index:
                continue
            key = add(source, 0, ", ".join(dict.fromkeys(field.role for field in fields)))
            edges.append((key, root))
        outgoing: dict[int, list[str]] = {}
        for field in selected.reference_fields:
            if field.role != "parent":
                outgoing.setdefault(field.target_id, []).append(field.role)
        for target_id, roles in outgoing.items():
            targets = objects_by_id.get(target_id, ())
            for target in targets:
                key = add(target, 2, ", ".join(dict.fromkeys(roles)))
                edges.append((root, key))
            if targets:
                continue
            key = ("external_object", target_id)
            if target_id == WWISE_ANY_OBJECT_ID:
                title = self.tr("Any")
                detail = ", ".join(dict.fromkeys(roles))
                tone = "any"
            else:
                external = self._sound_metadata.external_object(target_id)
                banks = external.get("banks", ()) if external else ()
                title = (
                    self.tr("Cross-bank Wwise object")
                    if banks else self.tr("Unavailable compiled Wwise object")
                )
                detail = self._sound_metadata.external_object_label(target_id)
                tone = "external" if banks else "missing"
            nodes[key] = {
                "key": key,
                "kind": "external",
                "object_id": target_id,
                "title": title,
                "detail": detail,
                "tone": tone,
                "depth": 2,
            }
            edges.append((root, key))
        for source in selected.sources:
            if not source.source_id:
                continue
            track = tracks.get(source.source_id)
            tone = (
                "ready" if track and track.available and track.payload_complete
                else "partial" if track else "missing"
            )
            key = ("source", source.source_id)
            nodes[key] = {
                "key": key, "kind": "source", "object_id": source.source_id,
                "title": self.tr("Audio source"),
                "detail": self.tr("ID {id}").format(id=source.source_id),
                "tone": tone, "depth": 2,
            }
            edges.append((root, key))
        return list(nodes.values()), list(dict.fromkeys(edges))

    def _on_bank_object_changed(self, current, _previous):
        obj = self._selected_bank_object()
        id_count = sum(
            item.object_id == obj.object_id for item in self._parse_result.objects
        ) if obj and self._parse_result else 0
        typed_count = sum(
            item.object_id == obj.object_id and item.type_id == obj.type_id
            for item in self._parse_result.objects
        ) if obj and self._parse_result else 0
        is_bnk = bool(
            obj and self._parse_result.container_type.lower() == "bnk"
        )
        structured = bool(obj and self._hirc_edit_level(obj) != "Read-only")
        exact_editable = (
            is_bnk and self._bank_edits_supported()
            and typed_count == 1 and structured
        )
        global_editable = exact_editable and id_count == 1
        safe_children = bool(obj and can_edit_hirc_children(obj))
        self.hirc_duplicate_btn.setEnabled(exact_editable)
        self.hirc_rename_btn.setEnabled(global_editable)
        self.hirc_delete_btn.setEnabled(global_editable)
        event_editable = bool(obj and (obj.type_id != 0x04 or global_editable))
        self.hirc_properties_btn.setEnabled(exact_editable and structured and event_editable)
        self.hirc_connect_btn.setEnabled(global_editable and safe_children)
        self.hirc_disconnect_btn.setEnabled(global_editable and safe_children and bool(obj.child_ids))
        scalar_links = bool(
            obj
            and any(
                field.role not in {
                    "child", "parent", "event action", "exception",
                    "playlist item", "switch assignment", "switch child",
                    "layer child",
                }
                and compatible_hirc_reference_targets(
                    obj, field, self._parse_result.objects
                )
                for field in obj.reference_fields
            )
        )
        self.hirc_reference_btn.setEnabled(global_editable and scalar_links)
        if obj is None:
            self.bank_graph.clear_message(self.tr("Select a HIRC object."))
            self.bank_detail.setText(self.tr("No object selected."))
            return
        self.bank_graph.set_graph(*self._bank_graph_model(obj))
        incoming = sum(
            field.target_id == obj.object_id and field.role != "parent"
            for source in self._parse_result.objects
            for field in source.reference_fields
            if source.index != obj.index
        )
        decoded = self._hirc_edit_level(obj)
        if typed_count > 1:
            decoded = self.tr(
                "read-only because type 0x{type_id:02X} + ShortID identifies {count} objects"
            ).format(type_id=obj.type_id, count=typed_count)
        elif id_count > 1:
            decoded = self.tr("{level} (type-qualified; ShortID is shared)").format(level=decoded)
        detail = self.tr(
            "{type} {id} · HIRC 0x{type_id:02X} · {size} bytes · "
            "{incoming} incoming / {outgoing} outgoing · Editing: {decoded}"
        ).format(
            type=obj.type_name, id=obj.object_id, type_id=obj.type_id,
            size=len(obj.payload), incoming=incoming,
            outgoing=len(obj.reference_fields), decoded=decoded,
        )
        if obj.type_id == 0x04:
            detail += "\n" + self._sound_metadata.id_label(
                "event", obj.object_id, event=True
            )
            contexts = self._sound_metadata.event_context_lines(obj.object_id)
            if contexts:
                detail += "\n" + "\n".join(contexts[:4])
        elif obj.type_id == 0x03:
            detail += "\n" + self._action_summary(obj)
        if self._hirc_edit_level(obj) == "Read-only":
            detail += "\n" + self.tr(self._hirc_read_only_reason(obj))
        self.bank_detail.setText(detail)

    def _on_bank_graph_node_selected(self, kind, object_id):
        if kind == "hirc":
            self._select_bank_object(object_id)
        elif kind == "hirc_index":
            self._select_bank_object(object_index=object_id)
        elif kind == "source":
            self._select_source_id(object_id)
            self.bank_detail.setText(self.tr("Audio source {id}; edit its media on Sound Graph.").format(id=object_id))
        elif kind == "external":
            self.bank_detail.setText(
                self.tr("Any — matches any music object in a transition rule.")
                if object_id == WWISE_ANY_OBJECT_ID
                else self._sound_metadata.external_object_label(object_id)
            )

    def _on_bank_graph_node_activated(self, kind, object_id):
        if kind != "source" and not self._bank_edits_supported():
            return
        if (
            kind == "hirc" and self._select_bank_object(object_id)
        ) or (
            kind == "hirc_index"
            and self._select_bank_object(object_index=object_id)
        ):
            self._on_edit_hirc()
        elif kind == "source" and self._select_source_id(object_id):
            self.tabs.setCurrentWidget(self.sound_graph_page)

    def _finish_hirc_edit(self, message, object_id=None, type_id=None):
        if not self._refresh_tracks():
            return False
        if object_id is not None:
            self._select_bank_object(object_id, type_id=type_id)
        self.status.setText(message)
        return True

    def _prompt_new_hirc_id(self, title, current=""):
        value, accepted = QInputDialog.getText(
            self, title, self.tr("Name or numeric ShortID:"), text=str(current)
        )
        if not accepted:
            return None
        try:
            object_id, _named = self._parse_object_id(value, allow_name=True)
        except ValueError:
            QMessageBox.warning(self, title, self.tr("Invalid name or ShortID."))
            return None
        return object_id

    def _on_duplicate_hirc(self):
        obj = self._selected_bank_object()
        if obj is None:
            return
        new_id = self._prompt_new_hirc_id(self.tr("Duplicate HIRC Object"))
        if new_id is None:
            return
        if any(item.object_id == new_id for item in self._parse_result.objects):
            QMessageBox.warning(self, self.tr("Duplicate HIRC Object"), self.tr("ShortID {id} already exists.").format(id=new_id))
            return
        payload = obj.payload
        if can_edit_hirc_children(obj):
            payload = set_hirc_children(obj, ())
        template = replace(obj, payload=payload)
        if obj.parent_reference_offset is not None:
            template.payload = patch_hirc_reference(template, obj.parent_reference_offset, 0)
        self.handler.upsert_hirc_object(obj.type_id, new_id, clone_hirc_payload(template, new_id))
        self._finish_hirc_edit(
            self.tr("Duplicated {type} {old} as {new}; connect it where needed.").format(type=obj.type_name, old=obj.object_id, new=new_id),
            new_id,
            obj.type_id,
        )

    def _on_rename_hirc(self):
        obj = self._selected_bank_object()
        if obj is None:
            return
        new_id = self._prompt_new_hirc_id(self.tr("Rename HIRC ShortID"), obj.object_id)
        if new_id is None or new_id == obj.object_id:
            return
        if any(item.object_id == new_id for item in self._parse_result.objects):
            QMessageBox.warning(self, self.tr("Rename HIRC ShortID"), self.tr("ShortID {id} already exists.").format(id=new_id))
            return
        old_id = obj.object_id
        self.handler.rename_hirc_object(old_id, new_id)
        self._finish_hirc_edit(
            self.tr("Renamed HIRC ShortID {old} to {new} and updated resolved links.").format(old=old_id, new=new_id),
            new_id,
            obj.type_id,
        )

    def _on_delete_hirc(self):
        obj = self._selected_bank_object()
        if obj is None:
            return
        incoming = [
            source for source in self._parse_result.objects
            if source.object_id != obj.object_id
            and any(field.target_id == obj.object_id for field in source.reference_fields)
        ]
        warning = self.tr("Delete {type} {id}?").format(type=obj.type_name, id=obj.object_id)
        if incoming:
            warning += "\n\n" + self.tr(
                "{count} object(s) still link to it. Delete is intentionally low-level and those links will become unresolved; retarget or disconnect them first for a valid graph."
            ).format(count=len(incoming))
        if QMessageBox.warning(
            self, self.tr("Delete HIRC Object"), warning,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self.handler.delete_hirc_objects((obj.object_id,))
        self._finish_hirc_edit(self.tr("Deleted {type} {id}.").format(type=obj.type_name, id=obj.object_id))

    def _on_edit_hirc(self):
        obj = self._selected_bank_object()
        if obj is None:
            return
        if obj.type_id == 0x04:
            event = next((item for item in self._parse_result.events if item.object_id == obj.object_id), None)
            actions = self._prompt_action_ids(self.tr("Edit Event Actions"), event.action_ids if event else ())
            if actions is None:
                return
            self.handler.set_event_actions(obj.object_id, actions)
        else:
            dialog = HircPropertiesDialog(obj, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self.handler.upsert_hirc_object(obj.type_id, obj.object_id, dialog.edited_payload())
        self._finish_hirc_edit(
            self.tr("Updated {type} {id}.").format(type=obj.type_name, id=obj.object_id),
            obj.object_id,
            obj.type_id,
        )

    def _child_candidates(self, parent):
        allowed = (
            {0x0B} if parent.type_id == 0x0A else
            {0x02, 0x05, 0x06, 0x07, 0x09}
        )
        return [
            obj for obj in self._parse_result.objects
            if obj.object_id != parent.object_id
            and obj.object_id not in parent.child_ids
            and obj.type_id in allowed
        ]

    def _on_connect_hirc(self):
        parent = self._selected_bank_object()
        candidates = self._child_candidates(parent) if parent else []
        if not candidates:
            QMessageBox.information(self, self.tr("Connect Child"), self.tr("No compatible unconnected child is available."))
            return
        labels = [f"{obj.type_name} {obj.object_id}" for obj in candidates]
        choice, accepted = QInputDialog.getItem(self, self.tr("Connect Child"), self.tr("Child object:"), labels, 0, False)
        if not accepted:
            return
        child = candidates[labels.index(choice)]
        by_id = {obj.object_id: obj for obj in self._parse_result.objects}
        edits = []
        old_parent = by_id.get(child.parent_id)
        if old_parent and old_parent.object_id != parent.object_id and child.object_id in old_parent.child_ids:
            if QMessageBox.question(
                self, self.tr("Reparent Child"),
                self.tr("{child} currently belongs to {parent}. Move it to {new_parent}?").format(
                    child=child.object_id, parent=old_parent.object_id, new_parent=parent.object_id
                ),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
            if not can_edit_hirc_children(old_parent):
                QMessageBox.warning(
                    self,
                    self.tr("Reparent Child"),
                    self.tr("The old parent's companion layout is not safely editable."),
                )
                return
            edits.append((old_parent.type_id, old_parent.object_id, set_hirc_children(old_parent, tuple(value for value in old_parent.child_ids if value != child.object_id))))
        edits.append((parent.type_id, parent.object_id, set_hirc_children(parent, parent.child_ids + (child.object_id,))))
        if child.parent_reference_offset is not None:
            edits.append((child.type_id, child.object_id, patch_hirc_reference(child, child.parent_reference_offset, parent.object_id)))
        self.handler.upsert_hirc_objects(edits)
        self._finish_hirc_edit(
            self.tr("Connected child {child} to {parent}.").format(child=child.object_id, parent=parent.object_id),
            parent.object_id,
            parent.type_id,
        )

    def _on_disconnect_hirc(self):
        parent = self._selected_bank_object()
        if parent is None or not parent.child_ids:
            return
        by_id = {obj.object_id: obj for obj in self._parse_result.objects}
        labels = [
            f"{by_id[value].type_name if value in by_id else 'Missing'} {value}"
            for value in parent.child_ids
        ]
        choice, accepted = QInputDialog.getItem(self, self.tr("Disconnect Child"), self.tr("Child object:"), labels, 0, False)
        if not accepted:
            return
        child_id = parent.child_ids[labels.index(choice)]
        edits = [
            (parent.type_id, parent.object_id, set_hirc_children(parent, tuple(value for value in parent.child_ids if value != child_id)))
        ]
        child = by_id.get(child_id)
        if child and child.parent_reference_offset is not None:
            edits.append((child.type_id, child.object_id, patch_hirc_reference(child, child.parent_reference_offset, 0)))
        self.handler.upsert_hirc_objects(edits)
        self._finish_hirc_edit(
            self.tr("Disconnected child {child} from {parent}.").format(child=child_id, parent=parent.object_id),
            parent.object_id,
            parent.type_id,
        )

    def _on_retarget_hirc_reference(self):
        obj = self._selected_bank_object()
        excluded = {
            "child", "parent", "event action", "exception", "playlist item",
            "switch assignment", "switch child", "layer child",
        }
        options = [
            (
                field,
                compatible_hirc_reference_targets(
                    obj, field, self._parse_result.objects
                ),
            )
            for field in (obj.reference_fields if obj else ())
            if field.role not in excluded
        ]
        options = [(field, targets) for field, targets in options if targets]
        if not options:
            return
        fields = [field for field, _targets in options]
        by_id = {}
        for item in self._parse_result.objects:
            by_id.setdefault(item.object_id, []).append(item)
        labels = [
            self.tr("offset {offset} · {role} → {type} {id}").format(
                offset=field.offset, role=field.role,
                type=(
                    by_id[field.target_id][0].type_name
                    if len(by_id.get(field.target_id, ())) == 1 else
                    self._sound_metadata.external_object_label(field.target_id)
                ),
                id=field.target_id,
            )
            for field in fields
        ]
        choice, accepted = QInputDialog.getItem(self, self.tr("Retarget Exact Link"), self.tr("Link field:"), labels, 0, False)
        if not accepted:
            return
        field = fields[labels.index(choice)]
        targets = list(options[fields.index(field)][1])
        target_labels = [f"{item.type_name} {item.object_id}" for item in targets]
        current = next((
            index for index, item in enumerate(targets)
            if item.object_id == field.target_id
        ), 0)
        target_choice, accepted = QInputDialog.getItem(
            self, self.tr("Retarget Exact Link"), self.tr("New target:"), target_labels, current, False
        )
        if not accepted:
            return
        target = targets[target_labels.index(target_choice)]
        self.handler.set_hirc_reference(obj.object_id, field.offset, target.object_id)
        self._finish_hirc_edit(
            self.tr("Retargeted {role} in {id} to {target}.").format(
                role=field.role, id=obj.object_id, target=target.object_id
            ),
            obj.object_id,
            obj.type_id,
        )

    def _populate_event_filters(self, result):
        wanted = self._active_event_id
        needle = self.event_search.text().strip().casefold()
        events = [
            event for event in result.events
            if not needle or needle in (
                f"{event.object_id} 0x{event.object_id:08x} "
                f"{self._sound_metadata.search_text('event', event.object_id, event=True)} "
                f"{' '.join(f'{sid} 0x{sid:08x}' for sid in event.source_ids)}"
            ).casefold()
        ]
        self.event_list.blockSignals(True)
        self.event_list.clear()
        all_sources = QListWidgetItem(self.tr("All Sources\n{count} media source(s)").format(count=len(result.tracks)))
        all_sources.setData(Qt.UserRole, None)
        self.event_list.addItem(all_sources)
        selected_row = 1 if events and (needle or not self._event_filter_initialized) else 0
        for event in events:
            names = self._sound_metadata.event_names(event.object_id)
            title = " / ".join(names) or self.tr("Event {id}").format(id=event.object_id)
            item = QListWidgetItem(
                self.tr(
                    "{title}\nID {id} · {sources} source(s) · {actions} action(s)"
                ).format(
                    title=title,
                    id=event.object_id,
                    sources=len(event.source_ids),
                    actions=len(event.action_ids),
                )
            )
            item.setData(Qt.UserRole, event.object_id)
            context = self._sound_metadata.event_context_lines(event.object_id)
            bindings = self._sound_metadata.bindings("event", event.object_id)
            tooltip = [self._sound_metadata.id_label("event", event.object_id, event=True)]
            if self._sound_metadata.live_wel_path:
                tooltip.append(self.tr("WEL: {path}").format(path=self._sound_metadata.live_wel_path))
            tooltip.extend(bindings)
            tooltip.extend(context)
            if event.source_ids:
                tooltip.append(self.tr("Sources: {ids}").format(
                    ids=", ".join(
                        f"{sid} (0x{sid:08X})" for sid in event.source_ids
                    )
                ))
            item.setToolTip("\n".join(tooltip))
            self.event_list.addItem(item)
            if event.object_id == wanted:
                selected_row = self.event_list.count() - 1
        self.event_list.setCurrentRow(selected_row)
        current = self.event_list.currentItem()
        self._active_event_id = current.data(Qt.UserRole) if current else None
        self.event_list.blockSignals(False)
        self._event_filter_initialized = True
        self._update_event_context()
        self.quick_edit_event_btn.setEnabled(
            self._bank_edits_supported() and self._active_event_id is not None
        )

    def _on_event_search_changed(self, _text):
        if self._parse_result is None:
            return
        self._populate_event_filters(self._parse_result)
        self._populate(self._parsed_tracks)

    def _on_event_filter_changed(self, current, _previous):
        self._active_event_id = current.data(Qt.UserRole) if current else None
        self._update_event_context()
        editable = bool(
            self._active_event_id is not None and self._bank_edits_supported()
        )
        self.quick_edit_event_btn.setEnabled(editable)
        self._populate(self._parsed_tracks)

    def _update_event_context(self):
        if self._active_event_id is None:
            self.event_context_label.setText("")
            return
        contexts = self._sound_metadata.event_contexts(self._active_event_id)
        summaries = []
        for context in contexts[:3]:
            resource = (
                context.get("scene") or context.get("prefab")
                or context.get("user") or self.tr("unknown resource")
            )
            detail = str(resource).replace("\\", "/").rsplit("/", 1)[-1]
            if context.get("game_object"):
                detail += self.tr(" · GameObject {name}").format(name=context["game_object"])
            if context.get("joint"):
                detail += self.tr(" · Joint {name}").format(name=context["joint"])
            summaries.append(detail)
        self.event_context_label.setText(
            self.tr("Used by: {contexts}").format(contexts="\n".join(summaries))
            if summaries else self.tr("No shipped scene, prefab, or USER binding was recovered for this Event.")
        )

    def _event_graph_model(self, event):
        objects = {obj.object_id: obj for obj in self._parse_result.objects}
        actions = {action.object_id: action for action in self._parse_result.actions}
        tracks = {track.source_id: track for track in self._parsed_tracks}
        nodes, edges, expanded = {}, [], set()

        def add(key, kind, object_id, title, detail, tone, depth):
            nodes.setdefault(key, {
                "key": key,
                "kind": kind,
                "object_id": int(object_id),
                "title": title,
                "detail": detail,
                "tone": tone,
                "depth": depth,
            })
            return key

        event_names = self._sound_metadata.event_names(event.object_id)
        root = add(
            ("event", event.object_id),
            "event",
            event.object_id,
            " / ".join(event_names) or self.tr("Event"),
            self.tr("ShortID {id} · {count} Actions").format(
                id=event.object_id, count=len(event.action_ids)
            ),
            "event",
            0,
        )

        def visit_object(object_id, parent, depth, path):
            obj = objects.get(object_id)
            if obj is None:
                external = self._sound_metadata.external_object(object_id)
                if external:
                    types = " / ".join(external.get("types", ())) or self.tr("External Wwise object")
                    banks = ", ".join(external.get("banks", ()))
                    key = add(
                        ("external_object", object_id), "external", object_id,
                        (
                            self.tr("Cross-bank {type}").format(type=types)
                            if banks else self.tr("Unavailable compiled target")
                        ),
                        self._sound_metadata.external_object_label(object_id),
                        "external" if banks else "missing", depth,
                    )
                    edges.append((parent, key))
                    return
                key = add(
                    ("missing", object_id), "missing", object_id,
                    self.tr("External Wwise target"),
                    self.tr("Object {id}").format(id=object_id), "missing", depth,
                )
                edges.append((parent, key))
                return
            is_event = obj.type_id == 0x04
            kind = "event" if is_event else "object"
            title = (
                " / ".join(self._sound_metadata.event_names(object_id)) or self.tr("Event")
                if is_event else obj.type_name
            )
            key = add((kind, object_id), kind, object_id, title, self._playback_summary(obj), "event" if is_event else "object", depth)
            edges.append((parent, key))
            if object_id in expanded or object_id in path:
                return
            expanded.add(object_id)
            if is_event:
                for action_id in obj.event_action_ids:
                    visit_action(action_id, key, depth + 1, path | {object_id})
                return
            for source in obj.sources:
                if not source.source_id:
                    continue
                track = tracks.get(source.source_id)
                location = self._track_status(track)[0] if track else self.tr("Not resolved")
                tone = "ready" if track and track.available and track.payload_complete else "partial" if track else "missing"
                source_key = add(
                    ("source", source.source_id),
                    "source",
                    source.source_id,
                    self.tr("Audio source"),
                    self.tr("ID {id} · {location}").format(
                        id=source.source_id, location=location
                    ),
                    tone,
                    depth + 1,
                )
                edges.append((key, source_key))
            for child_id in obj.child_ids:
                visit_object(child_id, key, depth + 1, path | {object_id})

        def visit_external(action, parent, depth):
            settings, kind = action.settings, action.target_kind
            if kind in {"state", "switch"} and settings:
                prefix = self.tr("State") if kind == "state" else self.tr("Switch")
                object_id = settings.value_id or 0
                detail = self._action_summary(action)
            elif kind == "game_parameter":
                prefix, object_id = self.tr("Game Parameter"), action.raw_id
                detail = self._action_summary(action)
            else:
                prefix, object_id = self.tr("Trigger"), action.raw_id
                detail = self._action_summary(action)
            key = (f"external_{kind}", action.object_id)
            add(key, "external", object_id, prefix, detail, "external", depth)
            edges.append((parent, key))

        def visit_action(action_id, parent, depth, path):
            action = actions.get(action_id)
            if action is None:
                key = add(("missing_action", action_id), "missing", action_id, self.tr("Missing Action"), str(action_id), "missing", depth)
                edges.append((parent, key))
                return
            action_key = add(
                ("action", action_id), "action", action_id,
                self.tr("{name} Action").format(name=action.action_name),
                self.tr("Action {id} · {target}").format(
                    id=action_id, target=self._action_summary(action)
                ),
                "action", depth,
            )
            edges.append((parent, action_key))
            if action_id in path:
                return
            if action.target_kind in {"object", "event"}:
                visit_object(action.target_id, action_key, depth + 1, path | {action_id})
            else:
                visit_external(action, action_key, depth + 1)

        for action_id in event.action_ids:
            visit_action(action_id, root, 1, {event.object_id})
        return list(nodes.values()), list(dict.fromkeys(edges))

    def _populate_event_graph(self, event):
        self.flow_group.setVisible(event is not None)
        if event is None:
            self._selected_flow_node = (None, None)
            self._update_flow_actions()
            return
        event_name = " / ".join(self._sound_metadata.event_names(event.object_id))
        self.flow_context_label.setText(
            self.tr(
                "{name}Event {id} · {actions} Actions · {sources} media sources"
            ).format(
                name=f"{event_name} · " if event_name else "",
                id=event.object_id,
                actions=len(event.action_ids),
                sources=len(event.source_ids),
            )
        )
        self.event_graph.set_graph(*self._event_graph_model(event))
        self._selected_flow_node = ("event", event.object_id)
        self.event_graph.select_node("event", event.object_id)
        self._update_flow_selection()

    def _select_source_id(self, source_id):
        for row in range(self.table.rowCount()):
            if self.table.item(row, 1).text() == str(source_id):
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 1))
                self._on_sel()
                return True
        if self.source_search.text():
            self.source_search.clear()
            return self._select_source_id(source_id)
        if self._active_event_id is not None and self.event_list.count():
            self.event_list.setCurrentRow(0)
            return self._select_source_id(source_id)
        return False

    def _on_flow_node_selected(self, kind, object_id):
        self._selected_flow_node = (kind, object_id)
        if kind == "source":
            self._select_source_id(object_id)
        self._update_flow_selection()

    def _on_flow_node_activated(self, kind, object_id):
        self._selected_flow_node = (kind, object_id)
        if kind == "source":
            if self._select_source_id(object_id) and self.play_btn.isEnabled():
                self._on_play()
            return
        self._on_edit_flow_node()

    def _flow_object(self):
        kind, object_id = self._selected_flow_node
        if kind not in {"object", "action", "event"} or self._parse_result is None:
            return None
        return next((obj for obj in self._parse_result.objects if obj.object_id == object_id), None)

    def _update_flow_selection(self):
        kind, object_id = self._selected_flow_node
        obj = self._flow_object()
        if kind == "source":
            text = self.tr("Audio source {id} · use the media controls below.").format(id=object_id)
        elif kind == "external":
            external = self._sound_metadata.external_object(object_id)
            text = (
                self._sound_metadata.external_object_label(object_id)
                if external else
                self.tr(
                    "Wwise control value {id}; edit it through its parent Action."
                ).format(id=object_id)
            )
        elif obj:
            properties = len(obj.property_bundle.values) + len(obj.property_bundle.ranges) if obj.property_bundle else 0
            text = self.tr("{type} {id} · {links} outgoing link(s) · {properties} playback property entries").format(
                type=obj.type_name, id=obj.object_id, links=len(obj.reference_fields), properties=properties
            )
        else:
            text = self.tr("Unresolved object {id}").format(id=object_id) if object_id else self.tr("Select a node to edit it.")
        self.flow_selection_label.setText(text)
        self._update_flow_actions()

    def _update_flow_actions(self):
        kind, _object_id = self._selected_flow_node
        editable = self._bank_edits_supported()
        obj = self._flow_object()
        is_event, is_action, is_object = (
            kind == "event", kind == "action", kind == "object"
        )
        self.flow_edit_btn.setVisible(is_event or is_action or is_object)
        self.flow_add_action_btn.setVisible(is_event)
        self.flow_detach_btn.setVisible(is_action)
        self.flow_connect_btn.setVisible(is_object)
        self.flow_disconnect_btn.setVisible(is_object)
        self.flow_delete_btn.setVisible(is_event)
        self.flow_edit_btn.setEnabled(editable and kind in {"event", "action", "object"})
        self.flow_edit_btn.setText(
            self.tr("Edit Actions…") if kind == "event" else
            self.tr("Edit Action…") if kind == "action" else
            self.tr("Open in All Objects") if obj and self._hirc_edit_level(obj) == "Read-only" else
            self.tr("Properties…")
        )
        self.flow_add_action_btn.setEnabled(editable and kind == "event")
        self.flow_detach_btn.setEnabled(editable and kind == "action" and self._active_event_id is not None)
        safe_children = bool(obj and can_edit_hirc_children(obj))
        self.flow_connect_btn.setEnabled(editable and kind == "object" and safe_children)
        self.flow_disconnect_btn.setEnabled(editable and kind == "object" and safe_children and bool(obj.child_ids))
        self.flow_delete_btn.setEnabled(editable and kind == "event")

    def _on_edit_flow_node(self):
        kind, object_id = self._selected_flow_node
        if kind == "event":
            self._edit_event_actions(object_id)
        elif kind == "action":
            self._edit_action(object_id)
        elif kind == "object":
            obj = self._flow_object()
            if obj and self._hirc_edit_level(obj) == "Read-only":
                self._show_flow_in_all_objects()
            else:
                self._edit_hirc_object(object_id)

    def _on_edit_filtered_event(self):
        self._edit_event_actions(self._active_event_id)

    def _show_flow_in_all_objects(self):
        kind, object_id = self._selected_flow_node
        if kind == "source":
            return
        if kind == "event" and not object_id:
            object_id = self._active_event_id
        if object_id and self._select_bank_object(object_id):
            self.tabs.setCurrentWidget(self.all_objects_page)

    def _on_detach_flow_action(self):
        kind, action_id = self._selected_flow_node
        event = next((item for item in self._parse_result.events if item.object_id == self._active_event_id), None)
        if kind != "action" or event is None:
            return
        self.handler.set_event_actions(event.object_id, tuple(value for value in event.action_ids if value != action_id))
        if self._refresh_tracks():
            self.status.setText(self.tr("Detached Action {action} from Event {event}; the Action remains in the bank.").format(action=action_id, event=event.object_id))

    def _on_connect_flow_child(self):
        obj = self._flow_object()
        if obj and self._select_bank_object(obj.object_id):
            self._on_connect_hirc()

    def _on_disconnect_flow_child(self):
        obj = self._flow_object()
        if obj and self._select_bank_object(obj.object_id):
            self._on_disconnect_hirc()

    @staticmethod
    def _parse_object_id(value, *, allow_name):
        value = value.strip()
        try:
            object_id = int(value, 0)
        except ValueError:
            try:
                object_id = int(value, 10)
            except ValueError:
                if not allow_name or not value:
                    raise
                return wwise_id_from_name(value), True
        if not 0 <= object_id <= 0xFFFFFFFF:
            raise ValueError(value)
        return object_id, False

    def _prompt_action_ids(self, title, current=()):
        dialog = ActionPickerDialog(self._parse_result.actions, self._parse_result.objects, current, self)
        dialog.setWindowTitle(title)
        return dialog.selected_action_ids() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def _on_add_action(self, _checked=False, event_id=None):
        if event_id is None and self._selected_flow_node[0] == "event":
            event_id = self._selected_flow_node[1]
        actions = [obj for obj in self._parse_result.objects if obj.type_id == 0x03]
        play_label, stop_label = self.tr("New Play Action"), self.tr("New Stop Action")
        labels = [play_label, stop_label] + [
            self.tr("Clone {name} Action {id} — {target}").format(
                name=obj.action_name or "Unknown", id=obj.object_id,
                target=self._action_summary(obj),
            )
            for obj in actions
        ]
        choice, accepted = QInputDialog.getItem(
            self, self.tr("Add Action"), self.tr("Action template:"), labels, 0, False
        )
        if not accepted:
            return
        new_id = self._prompt_new_hirc_id(self.tr("Add Action"))
        if new_id is None:
            return
        if any(obj.object_id == new_id for obj in self._parse_result.objects):
            QMessageBox.warning(self, self.tr("Add Action"), self.tr("ShortID {id} already exists.").format(id=new_id))
            return
        template = actions[labels.index(choice) - 2] if choice not in {play_label, stop_label} else None
        target_kind = template.action_target_kind if template else "object"
        objects = self._parse_result.objects
        id_counts = {obj.object_id: 0 for obj in objects}
        for obj in objects:
            id_counts[obj.object_id] += 1
        unique_objects = [obj for obj in objects if id_counts[obj.object_id] == 1]
        playback_types = {0x02, 0x05, 0x06, 0x07, 0x09, 0x0A, 0x0C, 0x0D}
        if choice == play_label:
            targets = [obj for obj in unique_objects if obj.type_id in playback_types]
        elif choice == stop_label:
            targets = [
                obj for obj in unique_objects
                if obj.type_id in playback_types
                or is_hirc_bus(obj.type_id, obj.bank_version)
            ]
        elif target_kind in {"object", "event"}:
            field = next((
                field for field in template.reference_fields
                if field.role in {"action target", "event target"}
            ), None)
            allowed = compatible_hirc_reference_types(template, field) if field else ()
            targets = [obj for obj in unique_objects if obj.type_id in allowed]
        else:
            targets = []
        target = None
        if targets:
            target_labels = [f"{obj.type_name} {obj.object_id}" for obj in targets]
            target_choice, accepted = QInputDialog.getItem(
                self, self.tr("Add Action"), self.tr("Initial target:"), target_labels, 0, False
            )
            if not accepted:
                return
            target = targets[target_labels.index(target_choice)]
        elif template is None or target_kind in {"object", "event"}:
            QMessageBox.warning(self, self.tr("Add Action"), self.tr("This bank has no compatible target."))
            return
        if choice == play_label:
            payload = create_play_action_payload(new_id, target.object_id)
        elif choice == stop_label:
            payload = create_stop_action_payload(
                new_id,
                target.object_id,
                target_is_bus=is_hirc_bus(target.type_id, target.bank_version),
            )
        else:
            payload = template.payload
            if target is not None:
                payload = set_action_fields(
                    template,
                    template.action_type,
                    target.object_id,
                    is_hirc_bus(target.type_id, target.bank_version),
                )
            payload = clone_hirc_payload(replace(template, payload=payload), new_id)
        self.handler.upsert_hirc_object(0x03, new_id, payload)
        event = next((item for item in self._parse_result.events if item.object_id == event_id), None)
        if event:
            self.handler.set_event_actions(event.object_id, event.action_ids + (new_id,))
        message = (
            self.tr("Added Action {action} to Event {event}.").format(action=new_id, event=event.object_id)
            if event else
            self.tr("Added unattached Action {id}; connect it from an Event when needed.").format(id=new_id)
        )
        if self._finish_hirc_edit(
            message,
            new_id,
            0x03,
        ):
            if event:
                self.tabs.setCurrentWidget(self.sound_graph_page)
                self._selected_flow_node = ("action", new_id)
                self.event_graph.select_node("action", new_id)
                self._update_flow_selection()
            else:
                self.tabs.setCurrentWidget(self.all_objects_page)

    def _on_add_event(self):
        value, accepted = QInputDialog.getText(self, self.tr("Add Event"), self.tr("Event name or numeric ShortID:"))
        if not accepted:
            return
        try:
            event_id, was_name = self._parse_object_id(value, allow_name=True)
        except ValueError:
            QMessageBox.warning(self, self.tr("Add Event"), self.tr("Invalid Event name or ID."))
            return
        if any(obj.object_id == event_id for obj in self._parse_result.objects):
            QMessageBox.warning(self, self.tr("Add Event"), self.tr("Object ID {id} already exists.").format(id=event_id))
            return
        actions = self._prompt_action_ids(self.tr("Add Event"))
        if actions is None:
            return
        self.handler.set_event_actions(event_id, actions)
        if self._refresh_tracks():
            note = self.tr(" from '{name}'").format(name=value) if was_name else ""
            self.status.setText(self.tr("Added Event {id}{note}.").format(id=event_id, note=note))

    def _edit_event_actions(self, event_id):
        event = next((value for value in self._parse_result.events if value.object_id == event_id), None)
        if event is None:
            return
        actions = self._prompt_action_ids(self.tr("Edit Event Actions"), event.action_ids)
        if actions is not None:
            self.handler.set_event_actions(event_id, actions)
            if self._refresh_tracks():
                self.status.setText(self.tr("Updated Event {id}.").format(id=event_id))

    def _on_delete_filtered_event(self):
        self._delete_event(self._selected_flow_node[1] if self._selected_flow_node[0] == "event" else self._active_event_id)

    def _delete_event(self, event_id):
        if event_id is None or QMessageBox.question(
            self,
            self.tr("Delete Event"),
            self.tr("Delete Event {id}? Actions remain in the bank.").format(
                id=event_id
            ),
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self.handler.delete_event(event_id)
        if self._refresh_tracks():
            self.status.setText(self.tr("Deleted Event {id}.").format(id=event_id))

    def _edit_action(self, action_id):
        self._edit_hirc_object(action_id)

    def _edit_hirc_object(self, object_id):
        if not self._select_bank_object(object_id):
            return
        self._on_edit_hirc()

    @staticmethod
    def _is_media_bank(result):
        return bool(
            result and result.container_type.lower() != "pck"
            and result.tracks and not result.objects
            and any(track.available for track in result.tracks)
        )

    def _update_role_banner(self, result):
        is_pck = result.container_type.lower() == "pck"
        is_media_bank = self._is_media_bank(result)
        is_prefetch_bank = is_media_bank and any(
            self._is_split_prefetch(track) for track in result.tracks
        )
        incomplete = sum(not track.available or not track.payload_complete for track in result.tracks)
        if is_pck:
            self.role_title.setText(self.tr("PCK: media (partial package)") if incomplete else self.tr("PCK: complete media"))
            help_text = (
                self.tr("Replace audio here. Event routing is stored in the matching BNK.")
                if not incomplete
                else self.tr(
                    "This file only indexes the audio. Open the full streaming PCK to "
                    "preview or replace it; Event routing is stored in the matching BNK."
                )
            )
            self.role_help.setText(help_text)
            self.companion_btn.setText(self.tr("Open Matching BNK…"))
        elif is_media_bank:
            self.role_title.setText(
                self.tr("SBNK: split audio")
                if is_prefetch_bank else self.tr("SBNK: complete media")
            )
            self.role_help.setText(self.tr(
                "This bank stores the beginning of each sound. REasy finds the full "
                "audio and its Event bank for preview and replacement."
            ) if is_prefetch_bank else self.tr(
                "Replace audio here. REasy finds the Event bank that uses it."
            ))
            self.companion_btn.setText(self.tr("Open Matching Event SBNK…"))
        else:
            self.role_title.setText(self.tr("BNK: events and playback routing"))
            self.role_help.setText(self.tr(
                "Choose an Event, edit its playback flow, or replace its audio. "
                "Matching media files are updated automatically."
            ))
            self.companion_btn.setText(self.tr("Open Matching Media…"))
        sources = self._sound_metadata.source_lines()
        self.role_help.setToolTip("\n".join(sources))
        path = self.handler.filepath or getattr(self.handler, "filename", "")
        if is_pck:
            has_companion = any(
                self._sound_metadata.banks_for_package(path, track.source_id)
                for track in result.tracks
            )
        elif is_media_bank:
            has_companion = any(
                self._sound_metadata.source_event_banks(track.source_id, path)
                for track in result.tracks
            )
        else:
            has_embedded_media = any(
                self._sound_metadata.embedded_media_banks(track.source_id, path)
                for track in result.tracks
            )
            has_packaged_media = any(
                self._sound_metadata.media_packages(track.source_id, path)
                or track.stream_type in {1, 2}
                for track in result.tracks
            )
            has_companion = has_embedded_media or has_packaged_media
            self.companion_btn.setText(
                self.tr("Open Matching Media SBNK…")
                if has_embedded_media else self.tr("Open Matching PCK…")
            )
        has_companion = bool(self._sound_profile and has_companion)
        is_media_index = bool(
            is_pck
            and incomplete
            and export_non_streaming_pck(self.handler.raw_data) == self.handler.raw_data
        )
        self.streaming_pck_btn.setVisible(bool(self._sound_profile and is_media_index))
        self.companion_btn.setVisible(has_companion)
        self.event_panel.setVisible(not is_pck and not is_media_bank)
        parts = [result.container_type.upper(), self.tr("{count} sources").format(count=len(result.tracks))]
        if result.bank_version is not None:
            parts.insert(1, self.tr("Wwise v{version}").format(version=result.bank_version))
        if not is_pck:
            parts.append(self.tr("{count} Events").format(count=len(result.events)))
        self.summary_label.setText(" · ".join(parts))

    def _on_open_streaming_pck(self):
        source = self.handler.filepath or getattr(self.handler, "filename", "")
        profile = self._sound_profile
        if profile is None:
            QMessageBox.warning(
                self,
                self.tr("Streaming PCK"),
                self.tr("No sound profile is registered for this game."),
            )
            return
        try:
            match = profile.resolve_indexed_package(self.handler)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Streaming PCK Mismatch"), str(exc))
            match = None
        if match is not None and open_sound_resource(self.handler, match.path):
            self.status.setText(self.tr(
                "Opened the full streaming PCK after an exact AKPK index match."
            ))
            return

        hint = profile.streaming_package_hint(source)
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Locate Full Streaming PCK"),
            str(local_sound_path(self.handler, hint) or Path(source).parent),
            "Wwise Packages (*.pck* *.spck*);;All Files (*)",
        )
        if not path:
            return
        try:
            profile.validate_indexed_package(
                self.handler.raw_data, Path(path).read_bytes()
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Streaming PCK Mismatch"), str(exc))
            return
        if open_sound_resource(self.handler, path):
            self.status.setText(self.tr(
                "Opened the selected full PCK after an exact AKPK index match."
            ))

    def _on_open_companion(self):
        source = self.handler.filepath or getattr(self.handler, "filename", "")
        selected = self._selected()[1]
        tracks = (selected,) if selected else tuple(self._parsed_tracks)
        candidates = []
        is_pck = bool(self._parse_result and self._parse_result.container_type.lower() == "pck")
        is_media_bank = self._is_media_bank(self._parse_result)
        for track in tracks:
            if is_pck:
                candidates.extend(self._sound_metadata.banks_for_package(source, track.source_id))
            elif is_media_bank:
                candidates.extend(
                    self._sound_metadata.source_event_banks(track.source_id, source)
                )
            else:
                candidates.extend(
                    self._sound_metadata.embedded_media_banks(track.source_id, source)
                )
                candidates.extend(
                    package.get("streaming") or package.get("index")
                    for package in self._sound_metadata.media_packages(
                        track.source_id, source
                    )
                )
        current = resource_key(source)
        candidates = list(dict.fromkeys(
            value for value in candidates if value and resource_key(value) != current
        ))
        if not candidates:
            fallback = (
                self._sound_profile.matching_companion_path(source)
                if self._sound_profile and not is_media_bank else None
            )
            candidates = [fallback] if fallback else []
        if len(candidates) > 1:
            candidate, accepted = QInputDialog.getItem(
                self, self.tr("Open Verified Companion"),
                self.tr("Select the Source-ID-matched file:"), candidates, 0, False,
            )
            if not accepted:
                return
        else:
            candidate = candidates[0] if candidates else None
        if open_sound_resource(self.handler, candidate):
            return
        title = (
            self.tr("Open Matching BNK") if is_pck else
            self.tr("Open Matching Event SBNK") if is_media_bank else
            self.tr("Open Matching Media")
        )
        path, _ = QFileDialog.getOpenFileName(
            self,
            title,
            str(local_sound_path(self.handler, candidate) or Path(source).parent),
            "Wwise Sound Files (*.bnk* *.pck* *.sbnk* *.spck*);;All Files (*)",
        )
        if path:
            open_sound_resource(self.handler, path)

    def _track_status(self, track):
        if track is None:
            return self.tr("Unavailable"), self.tr("No matching media entry was parsed.")
        kind = self._media_kind(track)
        media_name = {
            WwiseMediaKind.AUDIO: self.tr("audio"),
            WwiseMediaKind.MIDI: self.tr("Wwise MIDI sequence"),
            WwiseMediaKind.HYBRID_REVERB_IR: self.tr("Hybrid Reverb impulse media"),
            WwiseMediaKind.CONVOLUTION_REVERB_IR: self.tr("Convolution Reverb impulse media"),
            WwiseMediaKind.CRANKCASE_REV_MODEL: self.tr("Crankcase Audio REV vehicle model"),
        }.get(kind, self.tr("unknown media"))
        is_pck = bool(self._parse_result and self._parse_result.container_type.lower() == "pck")
        if self._sound_profile is None:
            if track.available and track.payload_complete:
                return self.tr("Export only"), self.tr(
                    "The {kind} is complete, but replacement requires a profile for this game."
                ).format(kind=media_name)
            return self.tr("Game profile required"), self.tr(
                "Add a sound profile to resolve this media's external containers safely."
            )
        if self._is_split_prefetch(track):
            return self.tr("Split SBNK prefetch"), self.tr(
                "This SBNK contains only the prefetch prefix. Preview, export, and "
                "replacement resolve the full Source-ID-matched PCK automatically."
            )
        if track.available and track.payload_complete:
            where = self.tr("Ready in this PCK") if is_pck else self.tr("Ready in this BNK")
            guidance = {
                WwiseMediaKind.AUDIO: self.tr("It can be previewed, replaced, or exported here."),
                WwiseMediaKind.MIDI: self.tr("It can be inspected, replaced from MID, or exported here."),
                WwiseMediaKind.HYBRID_REVERB_IR: self.tr("Its processed early reflections can be previewed/exported; WAV replacement is authored by Wwise."),
                WwiseMediaKind.CONVOLUTION_REVERB_IR: self.tr("Its processed impulse can be previewed/exported; WAV replacement is authored by Wwise."),
                WwiseMediaKind.CRANKCASE_REV_MODEL: self.tr("It can be replaced or exported as a validated compiled REV model; creating one from recordings requires the licensed REV authoring tool."),
            }.get(kind, self.tr("It can only be exported raw because its format is unknown."))
            return where, self.tr("This file contains the complete {kind}. ").format(kind=media_name) + guidance
        if is_pck:
            return self.tr("Indexed streaming media"), self.tr(
                "Use Open Full Streaming PCK to inspect the actual media data. "
                "REasy verifies the complete AKPK index before opening it."
            )
        if track.available and track.stream_type == 1:
            return self.tr("Automatic BNK + PCK update"), self.tr("This is a BNK prefetch fragment. REasy will update it and all verified PCK counterparts together.")
        if track.stream_type == 0:
            return self.tr("Automatic SBNK media update"), self.tr("REasy resolves a complete sibling media bank by exact Source ID and updates it; if none exists, Replace can embed the expected media here.")
        return self.tr("Automatic PCK update"), self.tr("REasy will resolve the complete PCK by Source ID and update its index automatically.")

    def _configure_media_buttons(self, track):
        kind = self._media_kind(track) if track else WwiseMediaKind.UNKNOWN
        labels = {
            WwiseMediaKind.AUDIO: (
                self.tr("Replace Audio…"), self.tr("Export WEM…"), self.tr("Export WAV…")
            ),
            WwiseMediaKind.MIDI: (
                self.tr("Replace MIDI…"), self.tr("Export Wwise MIDI…"), self.tr("Export MIDI…")
            ),
            WwiseMediaKind.HYBRID_REVERB_IR: (
                self.tr("Replace Impulse…"), self.tr("Export Compiled IR…"),
                self.tr("Export Processed IR WAV…"),
            ),
            WwiseMediaKind.CONVOLUTION_REVERB_IR: (
                self.tr("Replace Impulse…"), self.tr("Export Compiled IR…"),
                self.tr("Export Processed IR WAV…"),
            ),
            WwiseMediaKind.CRANKCASE_REV_MODEL: (
                self.tr("Replace REV Model…"), self.tr("Export REV Model…"),
                self.tr("No Waveform Export"),
            ),
        }.get(kind, (
            self.tr("Replace Media…"), self.tr("Export Raw Media…"),
            self.tr("No Editable Export"),
        ))
        self.rep_wem.setText(labels[0])
        self.exp_wem.setText(labels[1])
        self.exp_wav.setText(labels[2])
        if kind == WwiseMediaKind.MIDI:
            self.rep_wem.setToolTip(self.tr(
                "Import an editable Standard MIDI File or an already compiled WMID. "
                "Wwise MIDI stores one global tempo, so tempo-changing MID files are rejected."
            ))
        elif kind == WwiseMediaKind.HYBRID_REVERB_IR:
            self.rep_wem.setToolTip(self.tr(
                "Import a mono/stereo PCM16 or PCM24 WAV through the matching, licensed "
                "iZotope Hybrid Reverb plug-in. Existing decay tuning is retained."
            ))
        elif kind == WwiseMediaKind.CONVOLUTION_REVERB_IR:
            self.rep_wem.setToolTip(self.tr(
                "Import a PCM16 or PCM24 WAV through the game's matching Wwise "
                "Convolution Reverb plug-in, or import an already compiled WIR."
            ))
        elif kind == WwiseMediaKind.AUDIO:
            self.rep_wem.setToolTip(self.tr(
                "WAV import matches the original WEM codec and the sample-rate policy "
                "shown below. WAV metadata wins when provided; otherwise REasy inherits "
                "the original loops, cue points, and marker labels and verifies the authored WEM."
            ))
        elif kind == WwiseMediaKind.CRANKCASE_REV_MODEL:
            self.rep_wem.setToolTip(self.tr(
                "Import an already compiled Crankcase Audio REV ADM3 model. "
                "REV models are interactive vehicle-engine data, not WAV audio."
            ))
        elif kind == WwiseMediaKind.UNKNOWN:
            self.rep_wem.setToolTip(self.tr(
                "Unknown payloads cannot be replaced until their format is proven."
            ))

    def _update_source_card(self):
        _, track = self._selected()
        if track is None or track not in self._visible_tracks:
            self.source_title.setText(self.tr("Select a media source"))
            self.source_help.setText(self.tr("Select a row to see where its media is stored."))
            self.source_usage.clear()
            self.source_usage.setToolTip("")
            for button in (self.play_btn, self.rep_wem, self.meta_wem, self.exp_wem, self.exp_wav):
                button.setEnabled(False)
            self._configure_media_buttons(None)
            return
        self._configure_media_buttons(track)
        location, help_text = self._track_status(track)
        self.source_title.setText(self.tr("Source {id} — {location}").format(id=track.source_id, location=location))
        events = ", ".join(map(str, track.event_ids))
        self.source_help.setText(help_text + (self.tr(" Used by Event(s): {events}.").format(events=events) if events else ""))
        media_path = (
            self.handler.filepath or getattr(self.handler, "filename", "")
            if self._is_media_bank(self._parse_result) else None
        )
        contexts = self._sound_metadata.source_context_lines(
            track.source_id, media_path
        )
        self.source_usage.setText("\n".join(contexts[:4]))
        self.source_usage.setToolTip("\n".join(contexts))
        ready = bool(track.available and track.payload_complete)
        kind = self._media_kind(track)
        self.play_btn.setEnabled(
            ready and kind in (
                WwiseMediaKind.AUDIO,
                WwiseMediaKind.HYBRID_REVERB_IR,
                WwiseMediaKind.CONVOLUTION_REVERB_IR,
            )
        )
        self.rep_wem.setEnabled(self._can_replace_track(track))
        self.meta_wem.setEnabled(
            self._can_replace_track(track)
            and (kind == WwiseMediaKind.AUDIO or (kind == WwiseMediaKind.UNKNOWN and not ready))
        )
        self.exp_wem.setEnabled(ready)
        self.exp_wav.setEnabled(
            ready and kind in (
                WwiseMediaKind.AUDIO,
                WwiseMediaKind.MIDI,
                WwiseMediaKind.HYBRID_REVERB_IR,
                WwiseMediaKind.CONVOLUTION_REVERB_IR,
            )
        )

    def _populate(self, tracks):
        selected = self._selected()[1]
        selected_id = selected.source_id if selected else None
        event = next((value for value in (self._parse_result.events if self._parse_result else ()) if value.object_id == self._active_event_id), None)
        self._populate_event_graph(event)
        visible = list(tracks)
        if event:
            source_ids = set(event.source_ids)
            visible = [track for track in visible if track.source_id in source_ids]
        needle = self.source_search.text().strip()
        if needle:
            visible = [track for track in visible if needle in str(track.source_id)]
        self._visible_tracks = visible
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        selected_row = -1
        media_path = (
            self.handler.filepath or getattr(self.handler, "filename", "")
            if self._is_media_bank(self._parse_result) else None
        )
        for track in visible:
            split_prefetch = self._is_split_prefetch(track)
            wem = (
                extract_embedded_wem(self.handler.raw_data, track)
                if track.available and not split_prefetch else b""
            )
            metadata = parse_wem_metadata(wem, track.plugin_id) if wem else None
            location, guidance = self._track_status(track)
            duration = (
                self.tr("Full media in PCK") if split_prefetch else
                self._fmt_duration(metadata.duration_seconds)
                if metadata and track.payload_complete else
                self.tr("Partial") if track.available else "—"
            )
            format_text = self.tr("Prefetch fragment") if split_prefetch else " · ".join(
                value for value in (
                    metadata.codec if metadata else "",
                    self.tr("{count} ch").format(count=metadata.channels) if metadata and metadata.channels else "",
                    f"{metadata.sample_rate} Hz" if metadata and metadata.sample_rate else "",
                    metadata.details if metadata else "",
                ) if value
            ) or "—"
            used_by_values = tuple(
                self._sound_metadata.label("event", event_id, event=True)
                for event_id in track.event_ids
            ) or self._sound_metadata.source_event_labels(
                track.source_id, media_path
            )
            used_by = ", ".join(used_by_values[:3])
            if len(used_by_values) > 3:
                used_by += self.tr(" · +{count} more").format(
                    count=len(used_by_values) - 3
                )
            if not used_by:
                used_by = self.tr("Not resolved")
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate((str(track.index), str(track.source_id), location, duration, format_text, used_by)):
                item = QTableWidgetItem(value)
                tooltip = (
                    guidance if column == 2 else
                    "\n".join(used_by_values) if column == 5 and used_by_values else
                    value
                )
                item.setToolTip(tooltip)
                if column == 1:
                    item.setData(Qt.UserRole, track.source_id)
                self.table.setItem(row, column, item)
            if track.source_id == selected_id:
                selected_row = row
        if selected_row < 0 and visible:
            selected_row = 0
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        self.table.blockSignals(False)
        self.source_context_label.setText(
            self.tr("{name}Event {id} — {count} media source(s)").format(
                name=(
                    " / ".join(self._sound_metadata.event_names(event.object_id)) + " · "
                    if self._sound_metadata.event_names(event.object_id) else ""
                ),
                id=event.object_id,
                count=len(visible),
            )
            if event else self.tr("All media sources — {count} shown").format(count=len(visible))
        )
        self._update_source_card()

    @staticmethod
    def _fmt_duration(seconds):
        if seconds is None:
            return "Unknown"
        minutes = int(seconds // 60)
        return f"{minutes}:{seconds - minutes * 60:05.2f}"

    @staticmethod
    def _fmt_ms(milliseconds):
        minutes, seconds = divmod(max(0, int(milliseconds // 1000)), 60)
        return f"{minutes}:{seconds:02d}"
