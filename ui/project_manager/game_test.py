from __future__ import annotations

import os
import re
import signal
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMessageBox

lazy from file_handlers.pak.utils import filepath_hash
lazy from file_handlers.pak.writer import write_pak, written_entry_hashes
lazy from services.game_install import (chains_later_patch, game_processes, pak_mods_enabled, patch_slot, play_games,
                                        reframework_commits, steam_app_id, window_owners)
lazy from settings import save_settings

lazy from .pak_status_dialog import run_with_progress
lazy from .project_config import load_project_config, project_files

REFRAMEWORK_NIGHTLY = "https://github.com/praydog/REFramework-nightly/releases"
MARKS =(".reasy/play_test", ".reasy/play_test_placeholder")


def _left_behind(folder: Path) -> list[tuple[Path, bool]]:
    marks = {filepath_hash(name): bool(placeholder) for placeholder, name in enumerate(MARKS)}
    found = [(part, False) for part in (*folder.glob("*.reasy-part"), *folder.glob("pak_mods/**/*.reasy-part"))]
    for pak in (*folder.glob("re_chunk_000.pak.patch_*.pak"), *folder.glob("pak_mods/**/*.pak")):
        if hits := written_entry_hashes(pak) & marks.keys():
            found.append((pak, marks[hits.pop()]))
    return found


def _remove(pak: Path, placeholder: bool) -> str:
    try:
        if chains_later_patch(pak):
            write_pak([], pak)
        elif placeholder:
            pak.write_bytes(b"")
        else:
            pak.unlink(missing_ok=True)
    except OSError as error:
        return str(error)
    return ""


class GameTestRun(QObject):
    changed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll)
        self.pak: Path | None = None
        self.game = ""
        self.started = self.stuck = self._placeholder = self._shown = False
        self._exe = None
        self._missed = self._hidden = 0

    def _tell(self, text: str) -> None:
        QMessageBox.warning(self.parent(), self.tr("Play"), text)

    def start(self, game: str, game_dir: str | None, project_dir: str, settings: dict) -> None:
        if not game_dir:
            return self._tell(self.tr("Choose the game folder in the PAK view first: the test PAK goes there."))
        root, needs = Path(game_dir), play_games()[game]
        exe = root / needs["exe"]
        if not exe.is_file():
            return self._tell(self.tr("{exe} is not in the game folder:\n{folder}").format(exe=exe.name, folder=root))
        if "reframework" in needs:
            dll, need = root / "dinput8.dll", needs["reframework"]
            commits = reframework_commits(dll) if dll.is_file() else None
            if commits is None:
                return self._tell(self.tr("REFramework is not installed in the game folder. {game} loads PAKs from "
                                          "pak_mods with REFramework commit {need} or later:\n{url}").format(
                    game=game, need=need, url=REFRAMEWORK_NIGHTLY))
            if commits < need:
                return self._tell(self.tr("The game folder has REFramework commit {have}. {game} loads PAKs from "
                                          "pak_mods from commit {need} on; update REFramework:\n{url}").format(
                    have=commits, game=game, need=need, url=REFRAMEWORK_NIGHTLY))
            if not pak_mods_enabled(root):
                return self._tell(self.tr("REFramework's PAK Directory Loading is off in this game "
                                          "(re2_fw_config.txt). Turn it on in REFramework's menu, then try again."))
        if game_processes(exe):
            return self._tell(self.tr("{game} is already running. Close it first: the game reads its PAKs when it "
                                      "starts.").format(game=game))
        errors = []
        for folder in dict.fromkeys([*settings["test_pak_folders"], str(root)]):
            for pak, placeholder in _left_behind(Path(folder)):
                if error := _remove(pak, placeholder):
                    errors.append((folder, f"{pak}: {error}"))
        settings["test_pak_folders"] = list(dict.fromkeys([str(root), *(folder for folder, _ in errors)]))
        save_settings(settings)
        if errors:
            return self._tell(self.tr("REasy's earlier test PAKs could not be removed:\n{errors}").format(
                errors="\n".join(text for _, text in errors)))
        config = load_project_config(project_dir)
        name = re.sub(r"(?i)\.pak$", "", config.get("pak_name") or Path(project_dir).name)
        pak = root / "pak_mods" / f"REasy test - {name}.pak" if "reframework" in needs else patch_slot(root)
        placeholder = "reframework" not in needs and pak.is_file()
        part, label = pak.with_name(pak.name + ".reasy-part"), self.tr("Packing {name}…").format(name=pak.name)

        def pack(bridge):
            bridge.text.emit(label)
            with TemporaryDirectory() as temp:
                (mark := Path(temp, "mark")).write_bytes(b"")
                write_pak([*project_files(project_dir), (MARKS[placeholder], mark)], part,
                          config.get("pak_compression", "deflate"), bridge.prog.emit)

        try:
            pak.parent.mkdir(exist_ok=True)
            run_with_progress(self.parent(), self.tr("Play"), pack)
            os.replace(part, pak)
        except (OSError, ValueError) as error:
            _remove(part, False)
            return self._tell(str(error))
        try:
            os.startfile(f"steam://rungameid/{app}" if (app := steam_app_id(root)) else exe, cwd=root)
        except OSError as error:
            _remove(pak, placeholder)
            return self._tell(str(error))
        self.pak, self.game, self.started, self.stuck, self._placeholder = pak, game, False, False, placeholder
        self._exe, self._missed, self._hidden, self._shown = exe, 0, 0, False
        self._timer.start()
        self.changed.emit()

    def _poll(self) -> None:
        pids = game_processes(self._exe)
        windowed = bool(pids) and not window_owners().isdisjoint(pids)
        self._missed = 0 if pids else self._missed + 1
        self._shown = self._shown or windowed
        self._hidden = self._hidden + 1 if pids and self._shown and not windowed else 0
        if pids and not self.started:
            self.started = True
            self.changed.emit()
        elif self.started and self._missed == 3:
            self.finish()
        elif self.stuck != (self._hidden >= 10):
            self.stuck = not self.stuck
            self.changed.emit()

    def end_game(self) -> None:
        try:
            for pid in game_processes(self._exe):
                os.kill(pid, signal.SIGTERM)
        except OSError as error:
            if game_processes(self._exe):
                return self._tell(self.tr("{game} could not be ended:\n{error}").format(game=self.game, error=error))
        self.stuck, self._hidden = False, 0
        self.changed.emit()

    def finish(self) -> None:
        self._timer.stop()
        pak, self.pak, self.started, self.stuck = self.pak, None, False, False
        self.changed.emit()
        if error := _remove(pak, self._placeholder):
            self._tell(self.tr("The test PAK could not be removed, so the game loads it until you press Play again:\n"
                               "{path}\n\n{error}").format(path=pak, error=error))

    def confirm_quit(self) -> bool:
        if self.pak and not self.started:
            self.finish()
        return not self.pak or QMessageBox.question(self.parent(), self.tr("Play"), self.tr(
            "{game} is running with REasy's test PAK. If REasy quits now, the PAK stays in the game folder until you "
            "press Play again:\n{path}\n\nQuit anyway?").format(game=self.game, path=self.pak)
        ) == QMessageBox.Yes
