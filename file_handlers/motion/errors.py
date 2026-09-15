class MotionCodecError(ValueError):
    """Base class for motion codec failures."""


class MotionParseError(MotionCodecError):
    """Input bytes do not describe the selected motion profile."""


class MotionValidationError(MotionCodecError):
    """A semantic graph cannot be represented by the selected profile."""


class MotionWriteError(MotionCodecError):
    """A deterministic layout could not be completed."""
