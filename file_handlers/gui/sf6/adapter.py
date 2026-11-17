"""See docs/internals/gui-editor.md, Stage."""

from __future__ import annotations

from ..dmc5.adapter import Dmc5GuiAdapter

lazy from ..dmc5.stage import Dmc5GuiStage
lazy from .interaction import Sf6GuiInteraction
lazy from .text_animation import TextAnimations


class Sf6GuiAdapter(Dmc5GuiAdapter):
    def interaction(self, stage: Dmc5GuiStage) -> Sf6GuiInteraction:
        return Sf6GuiInteraction(stage)

    def text_animations(self, profile, load) -> TextAnimations:
        return TextAnimations(profile, load)


SF6_GUI_ADAPTER = Sf6GuiAdapter()
