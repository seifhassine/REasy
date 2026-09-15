from enum import IntFlag


class TransformChannels(IntFlag):
    NONE = 0
    TRANSLATION = 1
    ROTATION = 2
    SCALE = 4
    ALL = TRANSLATION | ROTATION | SCALE
