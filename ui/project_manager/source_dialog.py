from __future__ import annotations
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QRadioButton, QDialogButtonBox

class SelectSourceDialog(QDialog):
    def __init__(self, parent, game_name: str,
                 unpacked_checked: bool = True,
                 paks_checked: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Project Source")
        self._rb_unpacked = QRadioButton("Unpacked game directory (natives/*)")
        self._rb_paks     = QRadioButton("Game directory containing .pak files")
        self._rb_unpacked.setChecked(bool(unpacked_checked))
        self._rb_paks.setChecked(bool(paks_checked))

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Choose project source for {game_name}:"))
        lay.addWidget(self._rb_unpacked)
        lay.addWidget(self._rb_paks)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        lay.addWidget(btns)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

    def choose_paks(self) -> bool:
        return self._rb_paks.isChecked()

    @staticmethod
    def prompt(parent, game_name: str,
               unpacked_checked: bool = True,
               paks_checked: bool = False) -> bool | None:
        dlg = SelectSourceDialog(parent, game_name, unpacked_checked, paks_checked)
        return dlg.choose_paks() if dlg.exec() == QDialog.Accepted else None