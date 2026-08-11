class GuiFormatError(ValueError):
    """Raised when a GUIR structure is malformed or unsupported."""


class GuiWriteError(ValueError):
    """Raised when an edit cannot be represented by the proven GUIR model."""


class GuiSceneError(ValueError):
    """Raised for an unresolved/cyclic graph or invalid animation request."""


class GuiAssetError(ValueError):
    """Raised when a referenced GUI dependency cannot be resolved or decoded."""
