from __future__ import annotations

from enum import Enum, auto
from typing import Callable

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP
from PySide6.QtWidgets import QMessageBox


_ALLOW_THIS_CHANGE = QT_TRANSLATE_NOOP(
    "AiActionPolicy",
    "Allow this change",
)
_ALLOW_ALL_PROMPT_CHANGES = QT_TRANSLATE_NOOP(
    "AiActionPolicy",
    "Allow all for this prompt",
)
_CANCEL = QT_TRANSLATE_NOOP(
    "AiActionPolicy",
    "Cancel",
)


def _tr(source: str) -> str:
    return QCoreApplication.translate("AiActionPolicy", source)


class AiChangeDecision(Enum):
    CANCEL = auto()
    ALLOW_ONCE = auto()
    ALLOW_PROMPT = auto()


class AiActionPolicy:
    """Share one explicit authorization scope across a user's prompt."""

    def __init__(self, parent):
        self.parent = parent
        self._allow_all_for_request = False

    @property
    def allows_all_changes(self) -> bool:
        return self._allow_all_for_request

    def begin_request(self) -> None:
        self._allow_all_for_request = False

    def reset(self) -> None:
        self._allow_all_for_request = False

    def request(
        self,
        decision_provider: Callable[[], AiChangeDecision],
    ) -> bool:
        if self._allow_all_for_request:
            return True
        decision = decision_provider()
        if decision is AiChangeDecision.ALLOW_PROMPT:
            self._allow_all_for_request = True
        return decision in {
            AiChangeDecision.ALLOW_ONCE,
            AiChangeDecision.ALLOW_PROMPT,
        }

    def show_confirmation(
        self,
        title: str,
        message: str,
    ) -> AiChangeDecision:
        dialog = QMessageBox(self.parent)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle(title)
        dialog.setText(message)
        allow_once = dialog.addButton(
            _tr(_ALLOW_THIS_CHANGE),
            QMessageBox.ButtonRole.AcceptRole,
        )
        allow_prompt = dialog.addButton(
            _tr(_ALLOW_ALL_PROMPT_CHANGES),
            QMessageBox.ButtonRole.ApplyRole,
        )
        cancel = dialog.addButton(
            _tr(_CANCEL),
            QMessageBox.ButtonRole.RejectRole,
        )
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()

        clicked = dialog.clickedButton()
        if clicked is allow_once:
            return AiChangeDecision.ALLOW_ONCE
        if clicked is allow_prompt:
            return AiChangeDecision.ALLOW_PROMPT
        return AiChangeDecision.CANCEL
