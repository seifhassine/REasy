import os
import shutil
import struct
import subprocess
import tempfile
import wave

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QFileDialog, QSlider, QSpinBox, QStyle,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .bnk_parser import (
    export_non_streaming_pck, extract_embedded_wem,
    extract_embedded_wem_from_data_chunk, get_data_chunk,
    parse_soundbank, parse_wem_metadata,
)

_MISSING_TOOL = "VGMStream CLI was not found. Set its path in Settings or add it to PATH."
_COLUMNS = ["Subsong", "ID", "Duration", "Codec", "Channels", "Sample Rate"]
_COL_WEIGHTS = [10, 20, 16, 16, 12, 26]

_vgmstream_dl = None

def _vgmstream_asset_url(tag, assets):
    import platform as _plat
    sys_name = _plat.system().lower()
    machine = _plat.machine().lower()
    if sys_name == "windows" or os.name == "nt":
        prefs = ["win64"] if ("64" in machine or machine in ("amd64", "x86_64")) else ["win32"]
    elif sys_name == "linux":
        prefs = ["linux"]
    elif sys_name == "darwin":
        prefs = ["macos", "mac"]
    else:
        prefs = ["win64"]
    for pref in prefs:
        for a in assets:
            name, url = a.get("name", "").lower(), a.get("browser_download_url", "")
            if pref in name and (name.endswith(".zip") or name.endswith(".tar.gz")):
                return url
    archive = "vgmstream-win64.zip" if os.name == "nt" else "vgmstream-linux-cli.tar.gz"
    repo = "vgmstream/vgmstream"
    return f"https://github.com/{repo}/releases/download/{tag}/{archive}" if tag \
        else f"https://github.com/{repo}/releases/latest/download/{archive}"

def _get_vgmstream_downloader():
    global _vgmstream_dl
    if _vgmstream_dl is None:
        from tools.github_downloader import GitHubToolDownloader
        _vgmstream_dl = GitHubToolDownloader(
            owner_repo="vgmstream/vgmstream",
            cache_subdir="vgmstream_cli",
            exe_name="vgmstream-cli.exe" if os.name == "nt" else "vgmstream-cli",
            asset_url_fn=_vgmstream_asset_url,
            display_name="vgmstream-cli",
        )
    return _vgmstream_dl


class WaveformWidget(QWidget):
    seek_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._peaks: list[float] = []
        self._pos_ratio = 0.0
        self._ranges: list[tuple[float, float]] = []
        self.setMinimumHeight(54)
        self.setMaximumHeight(72)

    def set_waveform(self, peaks: list[float], ranges: list[tuple[float, float]]):
        self._peaks, self._ranges = peaks, ranges
        self.update()

    def set_position_ratio(self, r: float):
        self._pos_ratio = max(0.0, min(1.0, r))
        self.update()

    def clear(self):
        self._peaks, self._ranges, self._pos_ratio = [], [], 0.0
        self.update()

    def mousePressEvent(self, ev):
        if not self._peaks or ev.button() != Qt.LeftButton:
            return super().mousePressEvent(ev)
        self.seek_requested.emit(int(max(0.0, min(1.0, ev.position().x() / max(1, self.width()))) * 1000))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        r = self.rect().adjusted(0, 0, -1, -1)
        p.fillRect(r, QColor("#1e1e1e"))
        p.setPen(QPen(QColor("#3d3d3d")))
        p.drawRect(r)
        if not self._peaks:
            p.setPen(QPen(QColor("#8a8a8a")))
            p.drawText(r, Qt.AlignCenter, "Waveform preview will appear after decode")
            return
        w, h, mid = max(1, r.width()), r.height(), r.center().y()
        p.setPen(QPen(QColor("#4a6f95"), 1))
        for s, e in self._ranges:
            x1, x2 = r.left() + int(s * w), r.left() + int(e * w)
            p.fillRect(x1, r.top() + 1, max(1, x2 - x1), h - 1, QColor(39, 76, 49, 120))
        p.setPen(QPen(QColor("#7ab6ff"), 1))
        for x, pk in enumerate(self._peaks):
            a = int((h // 2 - 2) * pk)
            p.drawLine(QPoint(r.left() + x, mid - a), QPoint(r.left() + x, mid + a))
        cx = r.left() + int(self._pos_ratio * w)
        p.setPen(QPen(QColor("#f9d66b"), 1))
        p.drawLine(cx, r.top() + 1, cx, r.bottom() - 1)


class SoundViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self._modified = False
        self._parsed_tracks = []
        self._temp_dir = tempfile.mkdtemp(prefix="reasy_sound_")
        self._current_wem: str | None = None
        self._current_wav: str | None = None
        self._is_seeking = False
        self._duration_ms = 0
        self._active_ms: list[tuple[int, int]] = []
        self._sensitivity = 0.52
        self._cleanup_done = False
        self._setup_ui()
        self.destroyed.connect(lambda *_: self._finalize())
        QTimer.singleShot(0, self._on_analyze)

    @property
    def modified(self):
        return self._modified

    @modified.setter
    def modified(self, v: bool):
        if self._modified != v:
            self._modified = v
            self.modified_changed.emit(v)

    def closeEvent(self, ev):
        self._finalize()
        super().closeEvent(ev)

    def _finalize(self):
        if self._cleanup_done:
            return
        self._cleanup_done = True
        self._cleanup_temp_dir()

    def cleanup(self):
        self._finalize()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._apply_col_widths()

    def _setup_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(12)
        hdr = QLabel("Sound Preview")
        hdr.setStyleSheet("font-weight: 600; font-size: 16px;")
        lay.addWidget(hdr)
        lay.addWidget(self._build_controls())

        hint = QLabel("Double-click a subsong to play")
        hint.setStyleSheet("color: #6a6a6a;")
        lay.addWidget(hint)
        lay.addWidget(self._build_table())

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #6a6a6a;")
        lay.addWidget(self.status)
        self._setup_player()

    def _build_controls(self) -> QGroupBox:
        g = QGroupBox("Playback")
        vl = QVBoxLayout(g)
        vl.setContentsMargins(12, 12, 12, 12)
        vl.setSpacing(10)
        row = QHBoxLayout()
        mk = self._make_btn
        self.play_btn = mk("Decode && Play", QStyle.SP_MediaPlay, self._on_play)
        self.analyze_btn = mk("Refresh Tracks", QStyle.SP_BrowserReload, self._on_analyze)
        self.stop_btn = mk("Stop", QStyle.SP_MediaStop, self._on_stop, enabled=False)
        self.skip_btn = mk("Skip Silence", QStyle.SP_MediaSkipForward, self._on_skip, enabled=False)
        self.exp_wav = mk("Export WAV", QStyle.SP_DialogSaveButton, self._on_export_wav)
        self.exp_wem = mk("Export WEM", QStyle.SP_DialogSaveButton, self._on_export_wem)
        self.exp_pck = mk("Export Non-Streaming PCK", QStyle.SP_DialogSaveButton, self._on_export_pck)
        self.rep_wem = mk("Replace WEM", QStyle.SP_BrowserReload, self._on_replace)
        for b in (self.play_btn, self.analyze_btn, self.stop_btn, self.skip_btn,
                  self.exp_wav, self.exp_wem, self.exp_pck, self.rep_wem):
            row.addWidget(b)
        row.addStretch()
        vl.addLayout(row)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(70)
        form.addRow("Volume", self.vol_slider)
        self.stream_spin = QSpinBox()
        self.stream_spin.setRange(1, 9999)
        self.stream_spin.setValue(1)
        form.addRow("Subsong", self.stream_spin)

        self.pos_slider = QSlider(Qt.Horizontal)
        self.pos_slider.setRange(0, 0)
        self.pos_slider.setEnabled(False)
        self.pos_cur = QLabel("0:00")
        self.pos_tot = QLabel("0:00")
        pr = QHBoxLayout()
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(8)
        pr.addWidget(self.pos_cur)
        pr.addWidget(self.pos_slider, 1)
        pr.addWidget(self.pos_tot)
        form.addRow("Position", pr)

        self.speed_combo = QComboBox()
        for s in (0.50, 0.75, 1.00, 1.25, 1.50, 2.00):
            self.speed_combo.addItem(f"{s:.2f}\u00d7", s)
        self.speed_combo.setCurrentIndex(2)
        form.addRow("Speed", self.speed_combo)
        vl.addLayout(form)

        self.waveform = WaveformWidget()
        vl.addWidget(self.waveform)
        return g

    def _build_table(self) -> QTableWidget:
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        h = self.table.horizontalHeader()
        h.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for i in range(len(_COLUMNS)):
            h.setSectionResizeMode(i, QHeaderView.Interactive)
        self.table.setStyleSheet("QTableWidget::item { padding: 4px 8px; }")
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(False)
        self.table.setMinimumHeight(220)
        self.table.itemSelectionChanged.connect(self._on_sel)
        self.table.itemDoubleClicked.connect(self._on_dbl)
        QTimer.singleShot(0, self._apply_col_widths)
        return self.table

    def _apply_col_widths(self):
        if not hasattr(self, "table"):
            return
        w, tot = max(1, self.table.viewport().width()), sum(_COL_WEIGHTS)
        for c, wt in enumerate(_COL_WEIGHTS):
            self.table.setColumnWidth(c, max(70, int(w * wt / tot)))

    def _setup_player(self):
        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.audio_out.setVolume(0.7)
        self.player.setAudioOutput(self.audio_out)
        self.vol_slider.valueChanged.connect(lambda v: self.audio_out.setVolume(max(0.0, min(1.0, v / 100.0))))
        self.pos_slider.sliderPressed.connect(lambda: setattr(self, '_is_seeking', True))
        self.pos_slider.sliderReleased.connect(self._on_seek_done)
        self.pos_slider.sliderMoved.connect(lambda v: self.pos_cur.setText(self._fmt_ms(v)))
        self.speed_combo.currentIndexChanged.connect(
            lambda i: self.player.setPlaybackRate(float(self.speed_combo.itemData(i) or 1.0)))
        self.waveform.seek_requested.connect(self._on_wf_seek)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.durationChanged.connect(self._on_dur)
        self.player.positionChanged.connect(self._on_pos)

    def _make_btn(self, text, icon_id, cb, *, enabled=True):
        b = QPushButton(text)
        b.setIcon(self.style().standardIcon(icon_id))
        b.setEnabled(enabled)
        b.clicked.connect(cb)
        return b

    def _on_dur(self, ms):
        self._duration_ms = max(0, ms)
        self.pos_slider.setRange(0, self._duration_ms)
        self.pos_slider.setEnabled(self._duration_ms > 0)
        self.pos_tot.setText(self._fmt_ms(self._duration_ms))

    def _on_pos(self, ms):
        if self._is_seeking:
            return
        ms = max(0, ms)
        self.pos_slider.setValue(ms)
        self.pos_cur.setText(self._fmt_ms(ms))
        self.waveform.set_position_ratio(ms / self._duration_ms if self._duration_ms else 0.0)

    def _on_seek_done(self):
        self._is_seeking = False
        self.player.setPosition(self.pos_slider.value())

    def _on_wf_seek(self, permille):
        if self._duration_ms > 0:
            self.player.setPosition(permille * self._duration_ms // 1000)

    def _on_skip(self):
        cur = self.player.position()
        for s, _ in self._active_ms:
            if s > cur + 250:
                self.player.setPosition(s)
                self.status.setText(f"Skipped silence. Jumped to {self._fmt_ms(s)}.")
                return
        self.status.setText("No more voiced/loud segments found after current position.")

    def _on_state(self, state):
        self.stop_btn.setEnabled(state == QMediaPlayer.PlaybackState.PlayingState)
        if state == QMediaPlayer.PlaybackState.StoppedState:
            self.pos_slider.setValue(0)
            self.pos_cur.setText("0:00")
            self.waveform.set_position_ratio(0.0)

    def _selected(self):
        s = self.stream_spin.value()
        return s, next((t for t in self._parsed_tracks if t.index == s), None)

    def _require_track(self, action):
        s, t = self._selected()
        if not t:
            QMessageBox.warning(self, f"{action} Error", "Selected subsong was not found.")
        return (s, t) if t else None

    def _on_sel(self):
        sel = self.table.selectedItems()
        if sel:
            item = self.table.item(sel[0].row(), 0)
            if item:
                try:
                    v = int(item.text())
                    if v >= 1:
                        self.stream_spin.setValue(v)
                except ValueError:
                    pass

    def _on_dbl(self, item):
        it = self.table.item(item.row(), 0)
        if it:
            try:
                self.stream_spin.setValue(int(it.text()))
                self._on_play()
            except ValueError:
                pass

    def _vgmstream(self) -> str | None:
        settings = getattr(self.handler.app, "settings", {}) if self.handler.app else {}
        p = settings.get("vgmstream_cli_path", "").strip()
        return p or shutil.which("vgmstream-cli") or shutil.which("vgmstream-cli.exe")

    def _prompt_vgmstream_download(self) -> str | None:
        dl = _get_vgmstream_downloader()
        need, latest = dl.status()
        if not need:
            return str(dl.exe_path) if dl.exe_path.exists() else None

        tag_txt = latest or "latest"
        msg = (
            f"VGMStream CLI is required for sound playback but was not found.\n\n"
            f"Would you like to download it now ({tag_txt})?\n"
            f"It will be saved to REasy's downloads folder and configured automatically."
        ) if not dl.exe_path.exists() else (
            f"A newer vgmstream-cli release ({tag_txt}) is available.\n"
            f"Would you like to update it now?"
        )
        if QMessageBox.question(self, "Download VGMStream CLI?", msg,
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return None
        try:
            exe = dl.ensure(auto_download=True, parent_window=self)
        except Exception as e:
            QMessageBox.critical(self, "Download Failed", f"Failed to download vgmstream-cli:\n{e}")
            return None
        if not exe.exists():
            QMessageBox.critical(self, "Download Failed",
                                 "Download completed but the executable was not found.")
            return None

        exe_str = str(exe)
        if self.handler.app:
            settings = getattr(self.handler.app, "settings", None)
            if settings is not None:
                settings["vgmstream_cli_path"] = exe_str
                try:
                    from settings import save_settings
                    save_settings(settings)
                except Exception:
                    pass
        return exe_str

    @staticmethod
    def _rm(path: str | None):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def _reset_waveform(self):
        self._active_ms = []
        self.skip_btn.setEnabled(False)
        self.waveform.clear()

    def _cleanup_audio(self):
        try:
            self.player.setSource(QUrl())
        except RuntimeError:
            pass
        self._rm(self._current_wem)
        self._rm(self._current_wav)
        self._current_wem = self._current_wav = None
        self._reset_waveform()

    def _cleanup_temp_dir(self):
        self._cleanup_audio()
        if self._temp_dir and os.path.exists(self._temp_dir):
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _decode_wem(self, wem_path: str) -> str | None:
        vgs = self._vgmstream()
        if not vgs:
            vgs = self._prompt_vgmstream_download()
            if not vgs:
                return None
        fd, wav = tempfile.mkstemp(dir=self._temp_dir, suffix=".wav")
        os.close(fd)
        r = subprocess.run([vgs, "-o", wav, wem_path], check=False, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(wav) or os.path.getsize(wav) == 0:
            self._rm(wav)
            err = (r.stderr or r.stdout or "").strip() or "Unknown vgmstream error."
            QMessageBox.warning(self, "Decode Error", f"Failed to decode embedded WEM:\n{err}")
            return None
        return wav

    def _decode_track(self, track) -> tuple[str | None, str | None]:
        wem_data = extract_embedded_wem(self.handler.raw_data, track)
        if not wem_data:
            return None, None
        fd, wem = tempfile.mkstemp(dir=self._temp_dir, suffix=".wem")
        os.close(fd)
        with open(wem, "wb") as f:
            f.write(wem_data)
        wav = self._decode_wem(wem)
        if not wav:
            self._rm(wem)
            return None, None
        return wem, wav

    @staticmethod
    def _pick_path(parent, *, title, name, ext, filt):
        p, _ = QFileDialog.getSaveFileName(parent, title, name, filt)
        if not p:
            return None
        return p if p.lower().endswith(ext) else p + ext

    def _write_export(self, path, data, label):
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError as e:
            QMessageBox.warning(self, "Export Error", f"Failed to export {label}:\n{e}")
            self.status.setText(f"{label} export failed.")
            return False
        self.status.setText(f"{label} exported to: {path}")
        return True

    def _on_export_wem(self):
        r = self._require_track("Export")
        if not r:
            return
        s, t = r
        p = self._pick_path(self, title="Export WEM", name=f"subsong_{s:04d}.wem", ext=".wem", filt="WEM Files (*.wem)")
        if not p:
            return
        d = extract_embedded_wem(self.handler.raw_data, t)
        if not d:
            QMessageBox.warning(self, "Export Error", "Failed to extract embedded WEM.")
            return
        self._write_export(p, d, "WEM")

    def _on_export_wav(self):
        r = self._require_track("Export")
        if not r:
            return
        s, t = r
        p = self._pick_path(self, title="Export WAV", name=f"subsong_{s:04d}.wav", ext=".wav", filt="WAV Files (*.wav)")
        if not p:
            return
        self.status.setText("Decoding embedded track for export...")
        tw, twv = self._decode_track(t)
        if not twv:
            QMessageBox.warning(self, "Export Error", "Failed to decode embedded track for export.")
            self.status.setText("WAV export failed.")
            return
        try:
            shutil.copyfile(twv, p)
            self.status.setText(f"WAV exported to: {p}")
        except OSError as e:
            QMessageBox.warning(self, "Export Error", f"Failed to export WAV:\n{e}")
            self.status.setText("WAV export failed.")
        finally:
            self._rm(tw)
            self._rm(twv)

    def _on_export_pck(self):
        p = self._pick_path(self, title="Export Non-Streaming PCK", name="sound_non_streaming.pck",
                            ext=".pck", filt="PCK Files (*.pck)")
        if p:
            self._write_export(p, export_non_streaming_pck(self.handler.raw_data), "Non-streaming PCK")

    def _on_replace(self):
        r = self._require_track("Replace")
        if not r:
            return
        s, t = r
        src, _ = QFileDialog.getOpenFileName(self, "Select Replacement WEM", f"subsong_{s:04d}.wem",
                                             "WEM Files (*.wem);;All Files (*.*)")
        if not src:
            return
        try:
            with open(src, "rb") as f:
                d = f.read()
        except OSError as e:
            QMessageBox.warning(self, "Replace Error", f"Failed to read replacement WEM:\n{e}")
            return
        if not d:
            QMessageBox.warning(self, "Replace Error", "Replacement WEM file is empty.")
            return
        self.handler.replace_track_data(t.source_id, d)
        self.handler.raw_data = self.handler.rebuild()
        self._parsed_tracks = parse_soundbank(self.handler.raw_data).tracks
        self._populate(self._parsed_tracks)
        self.status.setText(f"Replaced source ID {t.source_id} using {os.path.basename(src)}.")

    def _on_play(self):
        self._stop()
        r = self._require_track("Playback")
        if not r:
            return
        _, t = r
        self.status.setText("Decoding embedded track...")
        wem, wav = self._decode_track(t)
        if not wem or not wav:
            self.status.setText("Decode failed.")
            QMessageBox.warning(self, "Playback Error", "Failed to decode embedded audio data.")
            return
        self._current_wem, self._current_wav = wem, wav
        self.player.setSource(QUrl.fromLocalFile(wav))
        self.player.play()
        self._build_waveform(wav)
        n = len(self._active_ms)
        self.status.setText(f"Playing embedded track. {n} activity segment(s) detected. Click waveform or Skip Silence to jump.")

    def _on_analyze(self):
        self.status.setText("Analyzing sound container...")
        try:
            res = parse_soundbank(self.handler.raw_data)
        except Exception as e:
            self.status.setText("Analyze failed.")
            QMessageBox.warning(self, "Analyze Error", f"Failed to parse sound container: {e}")
            return
        self._parsed_tracks = res.tracks
        self._populate(res.tracks)
        self.exp_pck.setVisible((res.container_type or "").lower() == "pck")
        ver = f" version: {res.bank_version}." if res.bank_version is not None else ""
        self.status.setText(f"Analyze complete. {res.container_type.upper()}{ver} Tracks: {len(res.tracks)}")

    def _stop(self):
        self.player.stop()
        self._cleanup_audio()

    def _on_stop(self):
        self._stop()
        self.status.setText("")

    def _build_waveform(self, wav_path: str):
        result = self._read_mono(wav_path)
        if result is None:
            return
        mono, sr = result
        pn = max(1.0, max(mono))
        tw = max(300, self.waveform.width())
        fpb = max(1, len(mono) // tw)
        peaks = [(max(mono[i:i + fpb]) / pn) if i < len(mono) else 0.0
                 for i in range(0, len(mono), fpb)]
        norm = [a / pn for a in mono]
        ranges = self._detect_activity(norm, sr)
        ratios, self._active_ms = [], []
        for s, e in ranges:
            ratios.append((s / len(mono), e / len(mono)))
            self._active_ms.append((int(s * 1000 / max(1, sr)), int(e * 1000 / max(1, sr))))
        self.skip_btn.setEnabled(bool(self._active_ms))
        if not self._active_ms:
            self.status.setText("No voice/activity detected in this track.")
        self.waveform.set_waveform(peaks, ratios)

    def _read_mono(self, wav_path: str) -> tuple[list[float], int] | None:
        try:
            with wave.open(wav_path, "rb") as w:
                sw, ch, sr = w.getsampwidth(), w.getnchannels(), w.getframerate()
                raw = w.readframes(w.getnframes())
        except (wave.Error, OSError) as e:
            self.status.setText(f"Playing extracted embedded track (waveform unavailable: {e}).")
            self._reset_waveform()
            return None
        if sw != 2 or ch <= 0:
            self.status.setText("Playing extracted embedded track (waveform unsupported sample format).")
            self._reset_waveform()
            return None
        sc = len(raw) // 2
        if sc <= 0:
            self._reset_waveform()
            return None
        samples = struct.unpack(f"<{sc}h", raw)
        frames = sc // ch
        if frames <= 0:
            return None
        mono = [sum(abs(samples[i * ch + c]) for c in range(ch)) / ch for i in range(frames)]
        return mono, sr

    def _detect_activity(self, norm: list[float], sr: int) -> list[tuple[int, int]]:
        wms, wsz = 20, max(1, int(sr * 20 / 1000))
        energies = []
        for i in range(0, len(norm), wsz):
            c = norm[i:i + wsz]
            if c:
                energies.append(sum(v * v for v in c) / len(c))
        if not energies:
            return self._fallback_range(norm, sr)
        se = sorted(energies)
        nf = se[max(0, int(len(se) * 0.2) - 1)]
        sp = se[max(0, int(len(se) * 0.9) - 1)]
        bt = min(0.12, max(nf * 2.4, nf + (sp - nf) * 0.18, 0.00045))
        thr = max(0.0002, bt * self._sensitivity)
        rel = max(0.0001, thr * 0.70)
        wins, active, start = [], False, 0
        for i, e in enumerate(energies):
            if not active and e >= thr:
                active, start = True, i
            elif active and e <= rel:
                wins.append((start, i))
                active = False
        if active:
            wins.append((start, len(energies) - 1))
        mg = max(1, int(140 / wms))
        merged: list[list[int]] = []
        for s, e in wins:
            if merged and s - merged[-1][1] <= mg:
                merged[-1][1] = e
            else:
                merged.append([s, e])
        mn = max(1, int(120 / wms))
        ranges = []
        for s, e in merged:
            if e - s + 1 >= mn:
                ranges.append((s * wsz, min(len(norm) - 1, (e + 1) * wsz - 1)))
        return ranges if ranges else self._fallback_range(norm, sr)

    @staticmethod
    def _fallback_range(norm: list[float], sr: int) -> list[tuple[int, int]]:
        if not norm:
            return []
        loud = max(range(len(norm)), key=norm.__getitem__)
        h = max(1, int(sr * 0.35))
        return [(max(0, loud - h), min(len(norm) - 1, loud + h))]

    def _populate(self, tracks):
        self.table.setRowCount(0)
        dc = get_data_chunk(self.handler.raw_data)
        for t in tracks:
            wd = extract_embedded_wem(self.handler.raw_data, t) if t.absolute_offset else (
                extract_embedded_wem_from_data_chunk(dc, t) if dc else b"")
            m = parse_wem_metadata(wd) if wd else None
            row = self.table.rowCount()
            self.table.insertRow(row)
            vals = [
                str(t.index), str(t.source_id),
                self._fmt_dur(m.duration_seconds if m else None),
                m.codec if m else "Unknown",
                str(m.channels) if m and m.channels else "Unknown",
                f"{m.sample_rate} Hz" if m and m.sample_rate else "Unknown",
            ]
            for c, v in enumerate(vals):
                self.table.setItem(row, c, QTableWidgetItem(v))

    @staticmethod
    def _fmt_dur(s: float | None) -> str:
        if s is None:
            return "Unknown"
        m = int(s // 60)
        return f"{m}:{s - m * 60:05.2f}"

    @staticmethod
    def _fmt_ms(ms: int) -> str:
        t = max(0, int(ms // 1000))
        m, s = divmod(t, 60)
        return f"{m}:{s:02d}"
