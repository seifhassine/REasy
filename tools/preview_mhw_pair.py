"""Compare a World LMT and its Rise MOTLIST export using the same RE mesh."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer, Qt, QCoreApplication
from PySide6.QtWidgets import QApplication, QMainWindow, QTabWidget

from ui.scene.opengl_setup import configure_default_surface_format
from ui.styles import get_color_scheme, get_main_stylesheet
from file_handlers.motion.motlist_handler import MotListHandler
from file_handlers.motion.preview.mhw_lmt_preview import MhwLmtPreview
from file_handlers.motion.preview.widget import MotListPreviewWidget
from file_handlers.motion.preview.target import load_re_engine_mesh_target


class MotionPairWindow(QMainWindow):
    def __init__(self, world, rise, mesh, motion_id=60, frame=0):
        super().__init__()
        self.setStyleSheet(get_main_stylesheet(get_color_scheme()))
        self.setWindowTitle(f'REasy · World / Rise · {mesh.name}')
        self.resize(1500, 900)
        self.tabs = QTabWidget(self)
        self.setCentralWidget(self.tabs)
        self.previews = []
        target = load_re_engine_mesh_target(str(mesh), mesh.read_bytes())
        for path, preview_class, label in ((world, MhwLmtPreview, 'World LMT'),
                                           (rise, MotListPreviewWidget, 'Rise MOTLIST')):
            handler = MotListHandler()
            handler.filepath = str(path)
            handler.read(path.read_bytes())
            preview = preview_class(handler)
            self.select_motion(preview, motion_id)
            preview.set_target(target)
            if not preview.controller.ready:
                raise ValueError(f'{path}: {preview.status_label.text()}')
            preview.playback._on_frame_changed(frame)
            self.previews.append(preview)
            self.tabs.addTab(preview, label)
        self._previous_tab = 0
        self.tabs.currentChanged.connect(self._switch_preview)

    @staticmethod
    def select_motion(preview, motion_id):
        index = next((i for i, entry in enumerate(preview._motions)
                      if entry.motion_id == motion_id), None)
        if index is None:
            raise ValueError(f'{preview.handler.filepath}: motion ID {motion_id} is absent')
        preview.motion_browser.animation_list.setCurrentRow(index)

    def _switch_preview(self, index):
        previous, current = self.previews[self._previous_tab], self.previews[index]
        previous.playback.stop()
        self.select_motion(current, previous.current_entry.motion_id)
        current.playback._on_frame_changed(previous.controller.current_frame)
        a, b = previous.viewport.freecam, current.viewport.freecam
        b.pos, b.yaw, b.pitch = a.pos.copy(), a.yaw, a.pitch
        b.base_speed, b.speed = a.base_speed, a.speed
        current.viewport.update()
        self._previous_tab = index

    def closeEvent(self, event):
        for preview in self.previews:
            preview.cleanup()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('world', type=Path)
    parser.add_argument('rise', type=Path)
    parser.add_argument('mesh', type=Path)
    parser.add_argument('--motion', type=int, default=60)
    parser.add_argument('--frame', type=float, default=0)
    parser.add_argument('--capture-dir', type=Path)
    args = parser.parse_args()
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    configure_default_surface_format()
    app = QApplication([])
    app.setStyle('Fusion')
    window = MotionPairWindow(args.world, args.rise, args.mesh, args.motion, args.frame)
    window.show()
    if args.capture_dir:
        args.capture_dir.mkdir(parents=True, exist_ok=True)

        def capture(index):
            preview = window.previews[index]
            if not preview.controller.ready or preview._scene_renderer._target_mesh is None:
                print(preview.status_label.text(), flush=True)
                window.close()
                app.exit(1)
                return
            print(f'{window.tabs.tabText(index)}: {preview.status_label.text()}', flush=True)
            window.grab().save(str(args.capture_dir / f'{index}_motion_{args.motion}.png'))
            if index == 0:
                window.tabs.setCurrentIndex(1)
                QTimer.singleShot(1500, lambda: capture(1))
            else:
                window.close()
                app.quit()

        QTimer.singleShot(1500, lambda: capture(0))
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
