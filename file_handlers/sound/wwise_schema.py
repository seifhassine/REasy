"""Versioned Wwise HIRC names and built-in parameter schemas."""

from __future__ import annotations

STRUCTURED_BANK_VERSIONS = frozenset({125, 132, 135, 140, 145, 150})
_WWISE_SILENCE_PLUGIN_ID = 0x00650002

# Built-in IDs used by RE3. Effect/source/device names below come from the
# matching Wwise 2018.1 plug-in XML; codec IDs are Wwise's AKCODECID values.
BNK_STANDARD_CUE_NAMES = {
    0: "No cue-name filter",
    43_573_010: "Entry Cue",
    1_539_036_744: "Exit Cue",
}

_HIRC_BASE_TYPES = {
    0x01: "State",
    0x02: "Sound",
    0x03: "Action",
    0x04: "Event",
    0x05: "Random/Sequence Container",
    0x06: "Switch Container",
    0x07: "Actor-Mixer",
    0x08: "Bus",
    0x09: "Layer Container",
    0x0A: "Music Segment",
    0x0B: "Music Track",
    0x0C: "Music Switch",
    0x0D: "Music Random/Sequence",
    0x0E: "Attenuation",
    0x0F: "Dialogue Event",
}
_HIRC_TYPES = {
    **_HIRC_BASE_TYPES,
    0x10: "Feedback Bus",
    0x11: "Feedback Node",
    0x12: "FX Share Set",
    0x13: "FX Custom",
    0x14: "Auxiliary Bus",
    0x15: "LFO",
    0x16: "Envelope",
    0x17: "Audio Device",
}
_HIRC_TYPES_132 = {
    **_HIRC_BASE_TYPES,
    0x08: "Audio Bus",
    0x10: "FX Share Set",
    0x11: "FX Custom",
    0x12: "Auxiliary Bus",
    0x13: "LFO",
    0x14: "Envelope",
    0x15: "Audio Device",
    0x16: "Time Modulator",
}
_HIRC_LAYOUTS = {
    125: (
        _HIRC_TYPES,
        frozenset({0x08, 0x14}),
        frozenset({0x12, 0x13, 0x17}),
        frozenset({0x15, 0x16}),
    ),
    132: (
        _HIRC_TYPES_132,
        frozenset({0x08, 0x12}),
        frozenset({0x10, 0x11, 0x15}),
        frozenset({0x13, 0x14, 0x16}),
    ),
    # Wwise 2019.2 keeps the v128 HIRC numbering used by Wwise 2018.1.
    135: (
        _HIRC_TYPES_132,
        frozenset({0x08, 0x12}),
        frozenset({0x10, 0x11, 0x15}),
        frozenset({0x13, 0x14, 0x16}),
    ),
    # Wwise 2021.1 keeps the v128 HIRC numbering used by Wwise 2018.1.
    140: (
        _HIRC_TYPES_132,
        frozenset({0x08, 0x12}),
        frozenset({0x10, 0x11, 0x15}),
        frozenset({0x13, 0x14, 0x16}),
    ),
    # Wwise 2022.1 keeps the v128 HIRC numbering used by Wwise 2018.1.
    145: (
        _HIRC_TYPES_132,
        frozenset({0x08, 0x12}),
        frozenset({0x10, 0x11, 0x15}),
        frozenset({0x13, 0x14, 0x16}),
    ),
    # Wwise 2023.1 retains the post-v128 HIRC type numbering.
    150: (
        _HIRC_TYPES_132,
        frozenset({0x08, 0x12}),
        frozenset({0x10, 0x11, 0x15}),
        frozenset({0x13, 0x14, 0x16}),
    ),
}


def _hirc_layout(version: int | None):
    return _HIRC_LAYOUTS.get(version, (_HIRC_TYPES, frozenset(), frozenset(), frozenset()))


def _hirc_type_name(type_id: int, version: int | None) -> str:
    return _hirc_layout(version)[0].get(type_id, f"Unknown 0x{type_id:02X}")


def is_hirc_bus(type_id: int, version: int | None) -> bool:
    return int(type_id) in _hirc_layout(version)[1]


def is_hirc_plugin(type_id: int, version: int | None) -> bool:
    return int(type_id) in _hirc_layout(version)[2]

_ACTION_TYPE_NAMES = {
    0x0100: "Stop",
    0x0200: "Pause",
    0x0300: "Resume",
    0x0400: "Play",
    0x0500: "Play and Continue",
    0x0600: "Mute",
    0x0700: "Unmute",
    0x0800: "Set Pitch",
    0x0900: "Reset Pitch",
    0x0A00: "Set Volume",
    0x0B00: "Reset Volume",
    0x0C00: "Set Bus Volume",
    0x0D00: "Reset Bus Volume",
    0x0E00: "Set LPF",
    0x0F00: "Reset LPF",
    0x1000: "Use State",
    0x1100: "Unuse State",
    0x1200: "Set State",
    0x1300: "Set Game Parameter",
    0x1400: "Reset Game Parameter",
    0x1500: "Event Action",
    0x1600: "Event Action",
    0x1700: "Event Action",
    0x1900: "Set Switch",
    0x1A00: "Bypass FX",
    0x1B00: "Reset Bypass FX",
    0x1C00: "Break",
    0x1D00: "Trigger",
    0x1E00: "Seek",
    0x1F00: "Release Envelope",
    0x2000: "Set HPF",
    0x2100: "Play Event",
    0x2200: "Reset Playlist",
    0x2300: "Play Event (custom)",
    0x3000: "Reset HPF",
    0x3100: "Set FX",
    0x3200: "Reset FX",
    0x3300: "Bypass FX Slot 0",
    0x3400: "Bypass FX Slot 1",
    0x3500: "Bypass FX Slot 2",
    0x3600: "Bypass FX Slot 3",
    0x3700: "Bypass All FX",
}

_ACTION_EXTERNAL_TARGET_KINDS = {
    0x1200: "state",
    0x1300: "game_parameter",
    0x1400: "game_parameter",
    0x1900: "switch",
    0x1D00: "trigger",
}
_ACTION_EVENT_TARGETS = frozenset({0x1500, 0x1600, 0x1700, 0x2100, 0x2300})
_ACTION_SET_VALUE_TYPES = frozenset({
    0x0800, 0x0900, 0x0A00, 0x0B00,
    0x0C00, 0x0D00, 0x0E00, 0x0F00, 0x2000, 0x3000,
})

_AUDIO_CONTAINER_TYPES = frozenset({0x05, 0x06, 0x07, 0x09, 0x0A, 0x0C, 0x0D})

# Wwise bank v125 AkPropID values. These compact bundles hold the
# playback controls users expect to edit (volume, pitch, delay, probability,
# and so on) on Actions, Sounds, and actor/music hierarchy nodes.
BNK_PROPERTY_NAMES = {
    0x00: "Volume (dB)", 0x01: "LFE (dB)", 0x02: "Pitch (cents)", 0x03: "LPF (%)",
    0x04: "HPF (%)", 0x05: "Bus Volume (dB)", 0x06: "Make-up Gain (dB)",
    0x07: "Priority", 0x08: "Priority Distance Offset",
    0x09: "Feedback Volume", 0x0A: "Feedback LPF", 0x0B: "Mute Ratio",
    0x0C: "Pan Left/Right", 0x0D: "Pan Front/Rear", 0x0E: "Center %",
    0x0F: "Delay Time (ms)", 0x10: "Transition Time (ms)", 0x11: "Probability (%)",
    0x12: "Dialogue Mode", 0x13: "User Aux Send 0", 0x14: "User Aux Send 1",
    0x15: "User Aux Send 2", 0x16: "User Aux Send 3",
    0x17: "Game Aux Send", 0x18: "Output Bus Volume",
    0x19: "Output Bus HPF", 0x1A: "Output Bus LPF",
    0x1B: "HDR Bus Threshold", 0x1C: "HDR Bus Ratio",
    0x1D: "HDR Bus Release Time", 0x1E: "HDR Bus Game Parameter",
    0x1F: "HDR Game Parameter Minimum", 0x20: "HDR Game Parameter Maximum",
    0x21: "HDR Active Range", 0x22: "Loop Start", 0x23: "Loop End",
    0x24: "Trim In Time", 0x25: "Trim Out Time", 0x26: "Fade In Time",
    0x27: "Fade Out Time", 0x28: "Fade In Curve", 0x29: "Fade Out Curve",
    0x2A: "Loop Crossfade Duration", 0x2B: "Crossfade Up Curve",
    0x2C: "Crossfade Down Curve", 0x2D: "MIDI Root Note",
    0x2E: "MIDI Play-on-note Type", 0x2F: "MIDI Transposition",
    0x30: "MIDI Velocity Offset", 0x31: "MIDI Key Minimum",
    0x32: "MIDI Key Maximum", 0x33: "MIDI Velocity Minimum",
    0x34: "MIDI Velocity Maximum", 0x35: "MIDI Channel Mask",
    0x36: "Playback Speed", 0x37: "MIDI Tempo Source",
    0x38: "MIDI Target Node", 0x39: "Attached Plug-in FX",
    0x3A: "Loop count (0 = infinite)", 0x3B: "Initial Delay (seconds)",
}
BNK_INTEGER_PROPERTIES = frozenset({
    0x0F, 0x10, 0x12, 0x28, 0x29, 0x2B, 0x2C,
    *range(0x2D, 0x36), 0x37, 0x38, 0x39, 0x3A,
})
BNK_PROPERTY_NAMES_132 = {
    **BNK_PROPERTY_NAMES,
    0x09: "Feedback Volume (deprecated)", 0x0A: "Feedback LPF (deprecated)",
    0x3C: "User Aux Send LPF 0", 0x3D: "User Aux Send LPF 1",
    0x3E: "User Aux Send LPF 2", 0x3F: "User Aux Send LPF 3",
    0x40: "User Aux Send HPF 0", 0x41: "User Aux Send HPF 1",
    0x42: "User Aux Send HPF 2", 0x43: "User Aux Send HPF 3",
    0x44: "Game Aux Send LPF", 0x45: "Game Aux Send HPF",
    0x46: "Attenuation ShortID", 0x47: "Positioning Type Blend",
}
BNK_INTEGER_PROPERTIES_132 = BNK_INTEGER_PROPERTIES | {0x46}
BNK_PROPERTY_NAMES_135 = {
    **BNK_PROPERTY_NAMES_132,
    0x48: "Reflections Bus Volume",
}
BNK_PROPERTY_NAMES_140 = {
    **BNK_PROPERTY_NAMES_135,
    0x49: "Pan Up/Down",
}
BNK_INTEGER_PROPERTIES_140 = BNK_INTEGER_PROPERTIES_132
BNK_PROPERTY_NAMES_150 = {
    0x00: "Volume (dB)", 0x01: "Pitch (cents)", 0x02: "LPF (%)",
    0x03: "HPF (%)", 0x04: "Bus Volume (dB)", 0x05: "Make-up Gain (dB)",
    0x06: "Priority", 0x07: "Mute Ratio",
    0x08: "User Aux Send Volume 0", 0x09: "User Aux Send Volume 1",
    0x0A: "User Aux Send Volume 2", 0x0B: "User Aux Send Volume 3",
    0x0C: "Game Aux Send Volume", 0x0D: "Output Bus Volume",
    0x0E: "Output Bus HPF", 0x0F: "Output Bus LPF",
    0x10: "User Aux Send LPF 0", 0x11: "User Aux Send LPF 1",
    0x12: "User Aux Send LPF 2", 0x13: "User Aux Send LPF 3",
    0x14: "User Aux Send HPF 0", 0x15: "User Aux Send HPF 1",
    0x16: "User Aux Send HPF 2", 0x17: "User Aux Send HPF 3",
    0x18: "Game Aux Send LPF", 0x19: "Game Aux Send HPF",
    0x1A: "Reflections Bus Volume", 0x1B: "HDR Bus Threshold",
    0x1C: "HDR Bus Ratio", 0x1D: "HDR Bus Release Time",
    0x1E: "HDR Active Range", 0x1F: "MIDI Transposition",
    0x20: "MIDI Velocity Offset", 0x21: "Playback Speed",
    0x22: "Initial Delay (seconds)", 0x23: "Pan X (2D)",
    0x24: "Pan Y (2D)", 0x25: "Pan Z (2D)", 0x26: "Pan X (3D)",
    0x27: "Pan Y (3D)", 0x28: "Pan Z (3D)", 0x29: "Center %",
    0x2A: "Positioning Type Blend", 0x2B: "Attenuation Enabled",
    0x2C: "Cone Attenuation Enabled", 0x2D: "Cone Attenuation",
    0x2E: "Cone LPF", 0x2F: "Cone HPF", 0x30: "Bypass FX",
    0x31: "Bypass All FX", 0x32: "Available property 0",
    0x33: "Available property 1", 0x34: "Available property 2",
    0x35: "Maximum Instances", 0x36: "Bypass All Metadata",
    0x37: "Special Transition Value", 0x38: "Priority Distance Offset",
    0x39: "Delay Time (ms)", 0x3A: "Transition Time (ms)",
    0x3B: "Probability (%)", 0x3C: "Dialogue Mode",
    0x3D: "HDR Bus Game Parameter", 0x3E: "HDR Game Parameter Minimum",
    0x3F: "HDR Game Parameter Maximum", 0x40: "Loop Start",
    0x41: "Loop End", 0x42: "Trim In Time", 0x43: "Trim Out Time",
    0x44: "Fade In Time", 0x45: "Fade Out Time", 0x46: "Fade In Curve",
    0x47: "Fade Out Curve", 0x48: "Loop Crossfade Duration",
    0x49: "Crossfade Up Curve", 0x4A: "Crossfade Down Curve",
    0x4B: "MIDI Root Note", 0x4C: "MIDI Play-on-note Type",
    0x4D: "MIDI Key Minimum", 0x4E: "MIDI Key Maximum",
    0x4F: "MIDI Velocity Minimum", 0x50: "MIDI Velocity Maximum",
    0x51: "MIDI Channel Mask", 0x52: "MIDI Tempo Source",
    0x53: "MIDI Target Node", 0x54: "Loop count (0 = infinite)",
    0x55: "Attenuation ShortID",
}
BNK_INTEGER_PROPERTIES_150 = frozenset({
    0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37,
    0x39, 0x3A, 0x3C, 0x3D, 0x3E, 0x3F, 0x46, 0x47, 0x49, 0x4A,
    *range(0x4B, 0x56),
})
BNK_STATE_PROPERTY_NAMES = {
    0x00: "Volume (dB)", 0x01: "LFE (dB)", 0x02: "Pitch (cents)",
    0x03: "LPF (%)", 0x04: "HPF (%)", 0x05: "Bus Volume (dB)",
    0x06: "Initial Delay (seconds)", 0x07: "Make-up Gain (dB)",
    0x08: "Feedback Volume", 0x09: "Feedback LPF", 0x0A: "Feedback Pitch",
    0x0B: "MIDI Transposition", 0x0C: "MIDI Velocity Offset",
    0x0D: "Playback Speed", 0x0E: "Mute Ratio",
    0x0F: "Special Transition Value", 0x10: "Priority",
    0x11: "Maximum Instances", 0x12: "Pan X (2D)", 0x13: "Pan Y (2D)",
    0x14: "Pan X (3D)", 0x15: "Pan Y (3D)", 0x16: "Pan Z (3D)",
    0x17: "Positioning Type", 0x18: "Center %",
    0x19: "Cone Attenuation Enabled", 0x1A: "Cone Attenuation",
    0x1B: "Cone LPF", 0x1C: "Cone HPF", 0x1D: "Bypass FX 0",
    0x1E: "Bypass FX 1", 0x1F: "Bypass FX 2", 0x20: "Bypass FX 3",
    0x21: "Bypass All FX", 0x22: "HDR Bus Threshold",
    0x23: "HDR Bus Release Time", 0x24: "HDR Bus Ratio",
    0x25: "HDR Active Range", 0x26: "Game Aux Send Volume",
    0x27: "User Aux Send Volume 0", 0x28: "User Aux Send Volume 1",
    0x29: "User Aux Send Volume 2", 0x2A: "User Aux Send Volume 3",
    0x2B: "Output Bus Volume", 0x2C: "Output Bus HPF", 0x2D: "Output Bus LPF",
    0xFF: "Capcom extension 0xFF (meaning unconfirmed; preserved exactly)",
}
BNK_STATE_PROPERTY_NAMES_132 = {
    **{
        key: value
        for key, value in BNK_STATE_PROPERTY_NAMES.items()
        if key != 0xFF
    },
    0x08: "Feedback Volume (deprecated)", 0x09: "Feedback LPF (deprecated)",
    0x0A: "Feedback Pitch (deprecated)", 0x10: "Maximum Instances",
    0x11: "Priority", 0x17: "Positioning Type Blend",
    0x2E: "Attenuation Enabled", 0x2F: "User Aux Send LPF 0",
    0x30: "User Aux Send LPF 1", 0x31: "User Aux Send LPF 2",
    0x32: "User Aux Send LPF 3", 0x33: "User Aux Send HPF 0",
    0x34: "User Aux Send HPF 1", 0x35: "User Aux Send HPF 2",
    0x36: "User Aux Send HPF 3", 0x37: "Game Aux Send LPF",
    0x38: "Game Aux Send HPF",
}
BNK_STATE_PROPERTY_NAMES_140 = {
    **{
        key: value
        for key, value in BNK_STATE_PROPERTY_NAMES_132.items()
        if key not in {0x2F, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38}
    },
    0x2F: "Reflections Volume",
    0x30: "User Aux Send LPF 0", 0x31: "User Aux Send LPF 1",
    0x32: "User Aux Send LPF 2", 0x33: "User Aux Send LPF 3",
    0x34: "User Aux Send HPF 0", 0x35: "User Aux Send HPF 1",
    0x36: "User Aux Send HPF 2", 0x37: "User Aux Send HPF 3",
    0x38: "Game Aux Send LPF", 0x39: "Game Aux Send HPF",
    0x3A: "Pan Z (2D)", 0x3B: "Bypass All Metadata",
}
BNK_MODULATOR_PROPERTY_NAMES = {
    0x00: "Scope", 0x01: "Envelope: stop playback", 0x02: "LFO: depth",
    0x03: "LFO: attack time", 0x04: "LFO: frequency", 0x05: "LFO: waveform",
    0x06: "LFO: smoothing", 0x07: "LFO: pulse width", 0x08: "LFO: initial phase",
    0x09: "Envelope: attack time", 0x0A: "Envelope: attack curve",
    0x0B: "Envelope: decay time", 0x0C: "Envelope: sustain level",
    0x0D: "Envelope: sustain time", 0x0E: "Envelope: release time",
    0x0F: "Envelope: trigger on", 0x10: "Time: duration", 0x11: "Time: loops",
    0x12: "Time: playback rate", 0x13: "Time: initial delay",
}
BNK_MODULATOR_INTEGER_PROPERTIES = frozenset({0x00, 0x01, 0x05, 0x0A, 0x0F, 0x11})
BNK_MODULATOR_PROPERTY_NAMES_150 = {
    0x00: "Scope", 0x01: "Envelope: stop playback", 0x02: "LFO: depth",
    0x03: "LFO: attack time", 0x04: "LFO: frequency", 0x05: "LFO: waveform",
    0x06: "LFO: smoothing", 0x07: "LFO: pulse width", 0x08: "LFO: initial phase",
    0x09: "LFO: retrigger", 0x0A: "Envelope: attack time",
    0x0B: "Envelope: attack curve", 0x0C: "Envelope: decay time",
    0x0D: "Envelope: sustain level", 0x0E: "Envelope: sustain time",
    0x0F: "Envelope: release time", 0x10: "Envelope: trigger on",
    0x11: "Time: duration", 0x12: "Time: loops",
    0x13: "Time: playback rate", 0x14: "Time: initial delay",
}
BNK_MODULATOR_INTEGER_PROPERTIES_150 = frozenset({
    0x00, 0x01, 0x05, 0x09, 0x0B, 0x10, 0x12,
})


def property_names(kind: str, bank_version: int | None = 125):
    if kind == "state":
        if bank_version in {135, 140, 145, 150}:
            return BNK_STATE_PROPERTY_NAMES_140
        return BNK_STATE_PROPERTY_NAMES_132 if bank_version == 132 else BNK_STATE_PROPERTY_NAMES
    if kind == "modulator":
        return (
            BNK_MODULATOR_PROPERTY_NAMES_150
            if bank_version == 150 else BNK_MODULATOR_PROPERTY_NAMES
        )
    if bank_version == 150:
        return BNK_PROPERTY_NAMES_150
    if bank_version in {140, 145}:
        return BNK_PROPERTY_NAMES_140
    if bank_version == 135:
        return BNK_PROPERTY_NAMES_135
    return BNK_PROPERTY_NAMES_132 if bank_version == 132 else BNK_PROPERTY_NAMES


def integer_properties(kind: str, bank_version: int | None = 125):
    if kind == "modulator":
        return (
            BNK_MODULATOR_INTEGER_PROPERTIES_150
            if bank_version == 150 else BNK_MODULATOR_INTEGER_PROPERTIES
        )
    if kind == "state":
        return frozenset()
    if bank_version == 150:
        return BNK_INTEGER_PROPERTIES_150
    if bank_version in {135, 140, 145}:
        return BNK_INTEGER_PROPERTIES_140
    return BNK_INTEGER_PROPERTIES_132 if bank_version == 132 else BNK_INTEGER_PROPERTIES

BNK_ATTENUATION_TARGETS = (
    "Dry volume", "Game-defined aux send", "User-defined aux send",
    "Low-pass filter", "Spread", "High-pass filter", "Focus",
)
BNK_ATTENUATION_TARGETS_145 = BNK_ATTENUATION_TARGETS + (
    "Obstruction volume", "Obstruction low-pass filter",
    "Obstruction high-pass filter", "Occlusion volume",
    "Occlusion low-pass filter", "Occlusion high-pass filter",
    "Diffraction volume", "Diffraction low-pass filter",
    "Diffraction high-pass filter", "Transmission volume",
    "Transmission low-pass filter", "Transmission high-pass filter",
)


def attenuation_targets(bank_version: int | None = 125):
    return (
        BNK_ATTENUATION_TARGETS_145
        if (bank_version or 0) >= 142 else BNK_ATTENUATION_TARGETS
    )
BNK_CURVE_SCALING = {0: "None", 2: "Decibels", 3: "Logarithmic", 4: "dB to linear"}
BNK_CURVE_INTERPOLATION = {
    0: "Logarithmic 3", 1: "Sine", 2: "Logarithmic 1", 3: "Inverse S-curve",
    4: "Linear", 5: "S-curve", 6: "Exponential 1", 7: "Reciprocal sine",
    8: "Exponential 3", 9: "Constant",
}

BNK_FX_ENUMS = {
    "eq_filter": ("Low-pass", "High-pass", "Band-pass", "Notch", "Low shelf", "High shelf", "Peaking EQ"),
    "filter_insert": ("Off", "Early reflections", "Reverb", "Early reflections + reverb"),
    "filter_curve": ("Low shelf", "Peaking", "High shelf"),
    "waveform": ("Sine", "Triangle", "Square", "Saw up", "Saw down", "Random"),
    "phase_mode": ("Left-right", "Front-rear", "Circular", "Random"),
    "meter_mode": ("Peak", "RMS"),
    "meter_scope": ("Global", "Game Object"),
    "delay_input": ("Left or right", "Center", "Downmix", "None"),
    "delay_filter": ("None", "Low shelf", "Peaking EQ", "High shelf", "Low-pass", "High-pass", "Band-pass", "Notch"),
    "harmonizer_input": ("As input", "Center", "Stereo", "3.0", "4.0", "5.0", "Left only"),
    "source_channel": {4: "Mono", 8: "LFE"},
    "tone_sweep": ("Linear", "Logarithmic"),
    "tone_waveform": ("Sine", "Triangle", "Square", "Sawtooth", "White noise", "Pink noise"),
    "tone_mode": ("Fixed duration", "Envelope"),
    "guitar_filter": ("Low shelf", "Peaking", "High shelf", "Low-pass", "High-pass", "Band-pass", "Notch"),
    "distortion": ("None", "Overdrive", "Heavy", "Fuzz", "Clip"),
    "hybrid_quality": {8: "Low", 16: "High"},
    "convolution_mode": {0: "Reverb", 1: "Filter"},
    "convolution_block_size": ("256 samples", "512 samples", "1024 samples"),
    "matrix_delays": {4: "Performance", 8: "Balanced", 12: "Quality", 16: "Maximum quality"},
    "matrix_mode": {0: "Default delay lengths", 1: "Custom delay lengths"},
    "pitch_input": (
        "As input", "Mono / center", "Stereo", "Left / right / center",
        "Left / right / surround", "Left / right / center / surround",
    ),
    "synth_frequency": ("Base frequency", "MIDI note"),
    "synth_operation": ("Mix", "Ring modulation"),
    "synth_noise": ("White noise", "Pink noise", "Red noise", "Purple noise"),
    "synth_waveform": ("Sine", "Triangle", "Square", "Sawtooth"),
    "futz_slope": ("12 dB/octave", "24 dB/octave"),
    "futz_distortion": (
        "Sat 1", "Sat 2", "Fuzz", "Lo-Fi", "Soft",
        "Stun", "Ouch", "Hard", "Nuke", "Clip",
    ),
    "futz_intensity": ("Original", "Tuned"),
    "futz_chop": ("Single", "Multi"),
    "futz_eq": ("High-pass", "EQ", "Low-pass"),
    "futz_bit_depth": {
        0: "Off", **{index: str(24 - index) for index in range(1, 23)},
    },
    "futz_downsample": (
        "Off", "24000 Hz", "16000 Hz", "12000 Hz", "9600 Hz", "8000 Hz",
        "6857 Hz", "6000 Hz", "5333 Hz", "4800 Hz", "4364 Hz", "4000 Hz",
        "3692 Hz", "3424 Hz", "3200 Hz", "3000 Hz", "2824 Hz", "2667 Hz",
        "2526 Hz", "2400 Hz", "2286 Hz", "2182 Hz", "2087 Hz", "2000 Hz",
        "1920 Hz", "1846 Hz", "1778 Hz", "1714 Hz", "1655 Hz", "1600 Hz",
        "1548 Hz", "1500 Hz", "1455 Hz",
    ),
    "ml1_mode": ("Clean", "Soft", "Smart", "Dynamic", "Loud", "Crush"),
    "system_mix": {0: "Game-defined", 1: "Mix to main", 2: "Mix to passthrough"},
    "mastering_eq_filter": {
        1: "Low-pass resonant", 2: "High-pass resonant", 3: "Peak",
        4: "High shelf", 5: "Low shelf", 6: "Low-pass one-pole",
        7: "High-pass one-pole",
    },
    "mastering_link": {0: "No link", 1: "All channels", 2: "Partial link"},
    "mastering_limiter": {0: "Soft", 1: "Hard", 2: "Advanced"},
    "time_stretch_stereo": {0: "Left Right", 1: "Center Cut"},
    "time_stretch_mode": {0: "Classic", 1: "Transient Preserving"},
}


def _fx_fields(prefix, fields):
    return tuple((f"{prefix}: {label}", storage, *enum) for label, storage, *enum in fields)


_EQ_BAND = (
    ("Filter type", "u32", "eq_filter"), ("Gain (dB)", "f32"),
    ("Frequency (Hz)", "f32"), ("Q factor", "f32"), ("Enabled", "bool"),
)
_GUITAR_EQ_BAND = (
    ("Filter type", "u32", "guitar_filter"), ("Gain (dB)", "f32"),
    ("Frequency (Hz)", "f32"), ("Q factor", "f32"), ("Enabled", "bool"),
)
_HARMONIZER_VOICE = (
    ("Enabled", "bool"), ("Pitch factor", "f32"), ("Gain", "f32"),
    ("Filter type", "u32", "delay_filter"), ("Filter gain", "f32"),
    ("Filter frequency", "f32"), ("Filter Q factor", "f32"),
)
_ROOMVERB_RTPC = tuple((name, "f32") for name in (
    "Decay time", "High-frequency damping", "Diffusion", "Stereo width",
    "Filter 1 gain", "Filter 1 frequency", "Filter 1 Q factor",
    "Filter 2 gain", "Filter 2 frequency", "Filter 2 Q factor",
    "Filter 3 gain", "Filter 3 frequency", "Filter 3 Q factor",
    "Front level", "Rear level", "Center level", "LFE level", "Dry level",
    "Early-reflection level", "Reverb level",
))
_ROOMVERB_ALGORITHM = tuple((name, "f32") for name in (
    "Density delay minimum", "Density delay maximum", "Density delay randomization",
    "Room-shape minimum", "Room-shape maximum", "Diffusion delay scale",
    "Diffusion delay maximum", "Diffusion delay randomization", "DC filter cutoff",
    "Reverb-unit input delay", "Reverb-unit input-delay randomization",
))
_MASTERING_EQ = tuple(
    field
    for band in range(1, 7)
    for field in (
        (f"Parametric EQ band {band}: Filter mode", "u32", "mastering_eq_filter"),
        (f"Parametric EQ band {band}: Frequency", "f32"),
        (f"Parametric EQ band {band}: Gain", "f32"),
        (f"Parametric EQ band {band}: Resonance", "f32"),
    )
)
_MASTERING_COMPRESSOR = tuple(
    (f"Compressor band {band}: {name}", "f32")
    for band in range(1, 5)
    for name in ("Threshold", "Ratio", "Attack", "Release", "Makeup gain", "Knee")
)

# Exact scalar parameter blocks observed in registered legacy Wwise schemas.
BNK_FX_SCHEMAS = {
    0x00000403: ("Capcom Wave Data Bridge", (
        ("Wave data plug-in index", "u32"),
    )),
    0x00BA0003: ("Mastering Suite", (
        ("Parametric EQ enabled", "bool"), ("Compressor enabled", "bool"),
        ("Master volume enabled", "bool"), ("Limiter enabled", "bool"),
        ("Parametric EQ band count", "u32"),
    ) + tuple(
        (f"Parametric EQ band {band} enabled", "bool") for band in range(1, 7)
    ) + _MASTERING_EQ + (
        ("Compressor band count", "u32"),
        ("Compressor link strength", "f32"),
        ("Compressor stereo pairs linked", "bool"),
        ("Compressor link mode", "u32", "mastering_link"),
    ) + tuple(
        (f"Compressor band {band} enabled", "bool") for band in range(1, 5)
    ) + tuple(
        (f"Compressor crossover {number} frequency", "f32")
        for number in range(1, 4)
    ) + _MASTERING_COMPRESSOR + tuple(
        (f"Master volume: {channel}", "f32") for channel in (
            "Left", "Right", "Center", "Surround left", "Surround right",
            "Back left", "Back right", "Height front left",
            "Height front right", "Height back left", "Height back right", "LFE",
        )
    ) + (
        ("Limiter mode", "u32", "mastering_limiter"),
        ("Limiter threshold", "f32"), ("Limiter attack", "f32"),
        ("Limiter release", "f32"), ("Limiter output gain", "f32"),
        ("Limiter channels linked", "bool"),
    )),
    0x007F0003: ("Wwise Convolution Reverb", (
        ("Pre-delay", "f32"), ("Rear delay", "f32"),
        ("Output spread", "f32"), ("Center input level", "f32"),
        ("LFE input level", "f32"), ("Input spread", "f32"),
        ("Front output level", "f32"), ("Rear output level", "f32"),
        ("Center output level", "f32"), ("LFE output level", "f32"),
        ("Dry level", "f32"), ("Reverb level", "f32"),
        ("Reverb type", "u32", "convolution_mode"),
        ("Input threshold", "f32"),
    )),
    0x00021033: ("iZotope Hybrid Reverb", (
        ("Decay time", "f32"), ("Decay low frequency", "f32"),
        ("Decay high frequency", "f32"), ("Decay low ratio", "f32"),
        ("Decay mid ratio", "f32"), ("Decay high ratio", "f32"),
        ("Quality", "u32", "hybrid_quality"),
        ("Early reflection gain", "f32"), ("Tail gain", "f32"),
        ("Predelay front", "f32"), ("Predelay rear", "f32"),
        ("Front wet gain", "f32"), ("Front dry gain", "f32"),
        ("Rear wet gain", "f32"), ("Rear dry gain", "f32"),
    )),
    0x00650002: ("Wwise Silence", (
        ("Duration (seconds)", "f32"),
        ("Random duration minimum (seconds)", "f32"),
        ("Random duration maximum (seconds)", "f32"),
    )),
    0x00640002: ("Wwise Sine", (
        ("Frequency (Hz)", "f32"), ("Gain (dB)", "f32"),
        ("Duration (seconds)", "f32"), ("Channel", "u32", "source_channel"),
    )),
    0x00660002: ("Wwise Tone Generator", (
        ("Gain (dB)", "f32"), ("Start frequency (Hz)", "f32"),
        ("Stop frequency (Hz)", "f32"),
        ("Start-frequency random minimum", "f32"),
        ("Start-frequency random maximum", "f32"),
        ("Sweep frequency", "bool"), ("Sweep type", "u32", "tone_sweep"),
        ("Stop-frequency random minimum", "f32"),
        ("Stop-frequency random maximum", "f32"),
        ("Waveform", "u32", "tone_waveform"),
        ("Duration mode", "u32", "tone_mode"),
        ("Fixed duration (seconds)", "f32"),
        ("Attack duration (seconds)", "f32"),
        ("Decay duration (seconds)", "f32"),
        ("Sustain duration (seconds)", "f32"),
        ("Sustain level", "f32"), ("Release duration (seconds)", "f32"),
        ("Channel", "u32", "source_channel"),
    )),
    0x00940002: ("Wwise Synth One", (
        ("Frequency mode", "u8", "synth_frequency"),
        ("Base frequency", "f32"),
        ("Operation mode", "u8", "synth_operation"),
        ("Output level", "f32"), ("Noise shape", "u8", "synth_noise"),
        ("Noise level", "f32"), ("FM amount", "f32"),
        ("Oversampling", "bool"),
        ("Oscillator 1 waveform", "u8", "synth_waveform"),
        ("Oscillator 1 invert", "bool"),
        ("Oscillator 1 transpose", "i32"),
        ("Oscillator 1 level", "f32"), ("Oscillator 1 PWM", "f32"),
        ("Oscillator 2 waveform", "u8", "synth_waveform"),
        ("Oscillator 2 invert", "bool"),
        ("Oscillator 2 transpose", "i32"),
        ("Oscillator 2 level", "f32"), ("Oscillator 2 PWM", "f32"),
    )),
    0x00690003: ("Wwise Parametric EQ", _fx_fields("Band 1", _EQ_BAND) + _fx_fields("Band 2", _EQ_BAND) + _fx_fields("Band 3", _EQ_BAND) + (("Output level", "f32"), ("Process LFE", "bool"))),
    0x006A0003: ("Wwise Delay", (
        ("Delay time", "f32"), ("Feedback", "f32"), ("Wet/dry mix", "f32"),
        ("Output level", "f32"), ("Feedback enabled", "bool"), ("Process LFE", "bool"),
    )),
    0x006C0003: ("Wwise Compressor", (
        ("Threshold", "f32"), ("Ratio", "f32"), ("Attack", "f32"),
        ("Release", "f32"), ("Output gain", "f32"),
        ("Process LFE", "bool"), ("Channel link", "bool"),
    )),
    0x006D0003: ("Wwise Expander", (
        ("Threshold", "f32"), ("Ratio", "f32"), ("Attack", "f32"),
        ("Release", "f32"), ("Output gain", "f32"),
        ("Process LFE", "bool"), ("Channel link", "bool"),
    )),
    0x006E0003: ("Wwise Peak Limiter", (
        ("Threshold", "f32"), ("Ratio", "f32"), ("Look-ahead", "f32"),
        ("Release", "f32"), ("Output level", "f32"),
        ("Process LFE", "bool"), ("Channel link", "bool"),
    )),
    0x00671003: ("McDSP ML1 Mastering Limiter", (
        ("Output ceiling", "f32"), ("Threshold", "f32"),
        ("Knee", "f32"), ("Release", "f32"),
        ("Mode", "u32", "ml1_mode"),
    )),
    0x006E1003: ("McDSP FutzBox", (
        ("Filters enabled", "bool"),
        ("Low-pass slope", "u32", "futz_slope"),
        ("Low-pass frequency", "f32"), ("Low-pass Q factor", "f32"),
        ("High-pass slope", "u32", "futz_slope"),
        ("High-pass frequency", "f32"), ("High-pass Q factor", "f32"),
        ("Distortion enabled", "bool"),
        ("Distortion mode", "u32", "futz_distortion"),
        ("Distortion amount", "f32"), ("Distortion intensity", "f32"),
        ("Distortion rectify", "f32"),
        ("Distortion intensity mode", "u32", "futz_intensity"),
        ("Distortion wobble", "f32"), ("Distortion chop amount", "f32"),
        ("Distortion chop mode", "u32", "futz_chop"),
        ("EQ enabled", "bool"), ("EQ filter type", "u32", "futz_eq"),
        ("EQ frequency", "f32"), ("EQ Q factor", "f32"), ("EQ gain", "f32"),
        ("Noise generator enabled", "bool"), ("Noise level", "f32"),
        ("Noise low-pass frequency", "f32"),
        ("Noise high-pass frequency", "f32"),
        ("Noise threshold", "f32"), ("Noise range", "f32"),
        ("Noise recovery", "f32"), ("SIM enabled", "bool"),
        ("SIM type", "u32"), ("SIM tuning", "f32"),
        ("Gate enabled", "bool"), ("Gate threshold", "f32"),
        ("Gate range", "f32"), ("Gate attack time", "f32"),
        ("Gate hold time", "f32"), ("Gate release time", "f32"),
        ("Lo-Fi enabled", "bool"),
        ("Lo-Fi bit depth", "u32", "futz_bit_depth"),
        ("Lo-Fi downsample", "u32", "futz_downsample"),
        ("Lo-Fi filter", "f32"), ("Input gain", "f32"),
        ("Output gain", "f32"), ("Balance", "f32"), ("Version", "u32"),
    )),
    0x00730003: ("Wwise Matrix Reverb", (
        ("Reverb time", "f32"), ("High-frequency ratio", "f32"),
        ("Number of delays", "u32", "matrix_delays"),
        ("Dry level", "f32"), ("Wet level", "f32"),
        ("Pre-delay", "f32"), ("Process LFE", "bool"),
        ("Delay-length mode", "u32", "matrix_mode"),
    )),
    0x00760003: ("Wwise RoomVerb", _ROOMVERB_RTPC + (
        ("Early reflections enabled", "bool"), ("Early-reflection pattern", "u32"),
        ("Reverb delay", "f32"), ("Room size", "f32"),
        ("Early-reflection front/back delay", "f32"), ("Density", "f32"),
        ("Room shape", "f32"), ("Reverb units", "u32"),
        ("Tone controls enabled", "bool"),
        ("Filter 1 insertion", "u32", "filter_insert"), ("Filter 1 curve", "u32", "filter_curve"),
        ("Filter 2 insertion", "u32", "filter_insert"), ("Filter 2 curve", "u32", "filter_curve"),
        ("Filter 3 insertion", "u32", "filter_insert"), ("Filter 3 curve", "u32", "filter_curve"),
        ("Input center level", "f32"), ("Input LFE level", "f32"),
    ) + _ROOMVERB_ALGORITHM),
    0x007D0003: ("Wwise Flanger", (
        ("Delay time", "f32"), ("Dry level", "f32"), ("Feed-forward level", "f32"),
        ("Feedback level", "f32"), ("Modulation depth", "f32"),
        ("LFO frequency", "f32"), ("LFO waveform", "u32", "waveform"),
        ("LFO smoothing", "f32"), ("LFO pulse width", "f32"),
        ("Phase offset", "f32"), ("Phase mode", "u32", "phase_mode"),
        ("Phase spread", "f32"), ("Output level", "f32"), ("Wet/dry mix", "f32"),
        ("Enable LFO", "bool"), ("Process center", "bool"), ("Process LFE", "bool"),
    )),
    0x007E0003: ("Wwise Guitar Distortion",
        _fx_fields("Pre-EQ band 1", _GUITAR_EQ_BAND)
        + _fx_fields("Pre-EQ band 2", _GUITAR_EQ_BAND)
        + _fx_fields("Pre-EQ band 3", _GUITAR_EQ_BAND)
        + _fx_fields("Post-EQ band 1", _GUITAR_EQ_BAND)
        + _fx_fields("Post-EQ band 2", _GUITAR_EQ_BAND)
        + _fx_fields("Post-EQ band 3", _GUITAR_EQ_BAND)
        + (
            ("Distortion type", "u32", "distortion"), ("Drive", "f32"),
            ("Tone", "f32"), ("Rectification", "f32"),
            ("Output level (dB)", "f32"), ("Wet/dry mix", "f32"),
        )
    ),
    0x00810003: ("Wwise Meter", (
        ("Attack", "f32"), ("Release", "f32"), ("Minimum", "f32"),
        ("Maximum", "f32"), ("Hold", "f32"), ("Mode", "u8", "meter_mode"),
        ("Scope", "u8", "meter_scope"), ("Apply downstream volume", "bool"),
        ("Game Parameter ShortID", "u32"),
    )),
    0x00820003: ("Wwise Time Stretch", (
        ("Window size", "u32"), ("Time stretch (%)", "f32"),
        ("Time stretch random (%)", "f32"), ("Pitch shift (cents)", "f32"),
        ("Pitch shift random (cents)", "f32"), ("Output gain (dB)", "f32"),
        ("Quality level", "f32"),
        ("Stereo processing", "i16", "time_stretch_stereo"),
        ("Stretch mode", "i16", "time_stretch_mode"),
    )),
    0x00830003: ("Wwise Tremolo", (
        ("LFO depth", "f32"), ("LFO frequency", "f32"),
        ("LFO waveform", "u32", "waveform"), ("LFO smoothing", "f32"),
        ("LFO pulse width", "f32"), ("Phase offset", "f32"),
        ("Phase mode", "u32", "phase_mode"), ("Phase spread", "f32"),
        ("Output gain", "f32"), ("Process center", "bool"), ("Process LFE", "bool"),
    )),
    0x00870003: ("Wwise Stereo Delay", _fx_fields("Left", (
        ("Input", "u32", "delay_input"), ("Delay time", "f32"),
        ("Feedback", "f32"), ("Crossfeed", "f32"),
    )) + _fx_fields("Right", (
        ("Input", "u32", "delay_input"), ("Delay time", "f32"),
        ("Feedback", "f32"), ("Crossfeed", "f32"),
    )) + (
        ("Filter type", "u32", "delay_filter"), ("Filter gain", "f32"),
        ("Filter frequency", "f32"), ("Filter Q factor", "f32"),
        ("Dry level", "f32"), ("Wet level", "f32"), ("Front/rear balance", "f32"),
        ("Enable feedback", "bool"), ("Enable crossfeed", "bool"),
    )),
    0x008A0003: ("Wwise Harmonizer", _fx_fields("Voice 1", _HARMONIZER_VOICE) + _fx_fields("Voice 2", _HARMONIZER_VOICE) + (
        ("Input type", "u32", "harmonizer_input"), ("Dry level", "f32"),
        ("Wet level", "f32"), ("Window size", "u32"),
        ("Process LFE", "bool"), ("Synchronize dry signal", "bool"),
    )),
    0x00880003: ("Wwise Pitch Shifter", (
        ("Input", "u32", "pitch_input"), ("Dry level", "f32"),
        ("Wet level", "f32"), ("Delay time", "f32"),
        ("Process LFE", "bool"), ("Delay dry signal", "bool"),
        ("Pitch shift", "f32"), ("Filter type", "u32", "delay_filter"),
        ("Filter gain", "f32"), ("Filter frequency", "f32"),
        ("Filter Q factor", "f32"),
    )),
    0x008B0003: ("Wwise Gain", (("Full-band gain", "f32"), ("LFE gain", "f32"))),
    0x00C80002: ("Wwise Audio Input", (("Gain (dB)", "f32"),)),
    0x00AE0007: ("System Output", (
        ("Allow 3D audio", "bool"),
        ("Headphone mix channel configuration", "u32"),
        ("Speaker mix channel configuration", "u32"),
        ("Allow system audio objects", "bool"),
        ("Minimum system audio objects", "u16"),
    )),
    0x00AA1137: ("Microsoft Spatial Sound", ()),
    0x00B40007: ("Vibration", ()),
    0x00B50007: ("No Output", ()),
    0x03840009: ("Wwise System Output Settings", (
        ("Mix behavior", "u32", "system_mix"),
    )),
}

# AudioEnginePropertyID values from the matching Audiokinetic plug-in XMLs.
# These IDs are independent of each plug-in's serialized parameter order and
# must therefore be resolved in the context of the plug-in that owns them.
BNK_FX_PROPERTY_NAMES = {
    0x00770002: {
        0: "Wind Speed", 1: "Wind Speed Random", 2: "Wind Speed Automate",
        3: "Direction", 4: "Direction Random", 5: "Direction Automate",
        6: "Variability", 7: "Variability Random", 8: "Variability Automate",
        9: "Gustiness", 10: "Gustiness Random", 11: "Gustiness Automate",
        20: "Frequency Shift", 21: "Frequency Shift Random",
        22: "Frequency Shift Automate", 23: "Q Factor Shift",
        24: "Q Factor Shift Random", 25: "Q Factor Shift Automate",
        26: "Gain Offset", 27: "Gain Offset Random", 28: "Gain Offset Automate",
        40: "Channels", 42: "Duration", 43: "Duration Random",
        44: "Minimum Distance", 45: "Roll-off Factor",
        46: "Dynamic Range", 48: "Playback Rate",
    },
    0x00780002: {
        0: "Object Speed", 1: "Object Speed Random", 2: "Object Speed Automate",
        20: "Frequency Shift", 21: "Frequency Shift Random",
        22: "Frequency Shift Automate", 23: "Q Factor Shift",
        24: "Q Factor Shift Random", 25: "Q Factor Shift Automate",
        26: "Gain Offset", 27: "Gain Offset Random", 28: "Gain Offset Automate",
        40: "Channels", 42: "Duration", 43: "Duration Random",
        44: "Minimum Distance", 45: "Roll-off Factor", 46: "Noise Color",
        47: "Point Time Random", 48: "Point Speed Random",
        49: "Distance Attenuation Enable", 50: "Playback Rate",
        51: "Oversampling", 52: "Dynamic Range",
    },
    0x00AB0003: {
        0: "Speed of Sound", 1: "Center Ratio", 2: "Max Reflections",
        3: "Dry", 4: "Output Level", 5: "Max Distance",
        6: "Base Texture Frequency", 7: "Fade-out Frame Count",
        8: "Distance Smoothing", 9: "Smoothing Type",
        10: "Pitch Threshold", 11: "Distance Threshold",
        12: "Threshold Mode", 13: "Output Config",
    },
    0x00730003: {
        0: "Reverb Time", 1: "High-frequency Ratio",
        2: "Number of Delays", 3: "Dry Level", 4: "Wet Level",
        5: "Pre-delay", 6: "Process LFE", 7: "Delay Lengths Mode",
        **{8 + index: f"Delay Time {index + 1}" for index in range(16)},
    },
    0x006E1003: {
        0: "Distortion Enable", 1: "Distortion Mode",
        2: "Distortion Amount", 3: "Distortion Intensity",
        4: "Distortion Rectify", 5: "Distortion Intensity Mode",
        6: "Distortion Wobble", 7: "Distortion Chop Amount",
        8: "Distortion Chop Mode", 10: "Gate Enable", 11: "Gate Threshold",
        12: "Gate Range", 13: "Gate Attack Time", 14: "Gate Hold Time",
        15: "Gate Release Time", 20: "SIM Enable", 21: "SIM Type",
        22: "SIM Tuning", 30: "Lo-Fi Enable", 31: "Lo-Fi Bit Depth",
        32: "Lo-Fi Downsample", 33: "Lo-Fi Filter", 40: "Input Gain",
        41: "Output Gain", 42: "Balance", 50: "Filters Enable",
        51: "Low-pass Slope", 52: "Low-pass Frequency", 53: "Low-pass Q Factor",
        54: "High-pass Slope", 55: "High-pass Frequency",
        56: "High-pass Q Factor", 60: "EQ Enable", 61: "EQ Filter Type",
        62: "EQ Frequency", 63: "EQ Q Factor", 64: "EQ Gain",
        70: "Noise Enable", 71: "Noise Level", 72: "Noise Low-pass Frequency",
        73: "Noise High-pass Frequency", 74: "Noise Threshold",
        75: "Noise Range", 76: "Noise Recovery",
    },
    0x00671003: {
        0: "Output Ceiling", 1: "Threshold", 2: "Knee",
        3: "Release", 4: "Mode",
    },
    0x00880003: {
        0: "Input", 1: "Process LFE", 2: "Delay Dry Signal",
        3: "Dry Level", 4: "Wet Level", 5: "Delay Time",
        6: "Pitch Shift", 7: "Filter Type", 8: "Filter Gain",
        9: "Filter Frequency", 10: "Filter Q Factor",
    },
    0x00940002: {
        1: "Frequency Mode", 2: "Base Frequency", 3: "Operation Mode",
        4: "Output Level", 5: "Noise Shape", 6: "Noise Level",
        7: "Oscillator 1 Waveform", 8: "Oscillator 1 Transpose",
        9: "Oscillator 1 Level", 10: "Oscillator 1 PWM",
        11: "Oscillator 1 Invert", 12: "Oscillator 2 Waveform",
        13: "Oscillator 2 Transpose", 14: "Oscillator 2 Level",
        15: "Oscillator 2 PWM", 16: "Oscillator 2 Invert",
        17: "FM Amount", 18: "Oversampling",
    },
    0x00BA0003: {
        0: "Parametric EQ Enable", 1: "Compressor Enable",
        2: "Master Volume Enable", 3: "Limiter Enable",
        100: "Parametric EQ Band Count",
        **{101 + band: f"Parametric EQ Band {band + 1} Enable" for band in range(6)},
        **{
            110 + band * 10 + offset: f"Parametric EQ Band {band + 1} {name}"
            for band in range(6)
            for offset, name in enumerate(("Filter Mode", "Frequency", "Resonance", "Gain"))
        },
        200: "Compressor Band Count", 201: "Compressor Link Mode",
        202: "Compressor Link Strength", 203: "Compressor Stereo Pairs Linked",
        **{204 + band: f"Compressor Band {band + 1} Enable" for band in range(4)},
        **{208 + cross: f"Compressor Crossover {cross + 1} Frequency" for cross in range(3)},
        **{
            220 + band * 10 + offset: f"Compressor Band {band + 1} {name}"
            for band in range(4)
            for offset, name in enumerate((
                "Threshold", "Ratio", "Attack", "Release", "Makeup Gain", "Knee",
            ))
        },
        300: "Master Volume Front Left", 301: "Master Volume Front Right",
        302: "Master Volume Center", 303: "Master Volume LFE",
        304: "Master Volume Surround Left", 305: "Master Volume Surround Right",
        306: "Master Volume Back Left", 307: "Master Volume Back Right",
        308: "Master Volume Height Front Left",
        309: "Master Volume Height Front Right",
        310: "Master Volume Height Back Left",
        311: "Master Volume Height Back Right",
        400: "Limiter Mode", 401: "Limiter Threshold", 402: "Limiter Attack",
        403: "Limiter Release", 404: "Limiter Output Gain",
        405: "Limiter Channels Linked",
    },
    0x00690003: {
        0: "Band 1 Filter Type", 1: "Band 1 Gain", 2: "Band 1 Frequency",
        3: "Band 1 Quality Factor", 4: "Band 1 Enable",
        5: "Band 2 Filter Type", 6: "Band 2 Gain", 7: "Band 2 Frequency",
        8: "Band 2 Quality Factor", 9: "Band 2 Enable",
        10: "Band 3 Filter Type", 11: "Band 3 Gain", 12: "Band 3 Frequency",
        13: "Band 3 Quality Factor", 14: "Band 3 Enable",
        15: "Output Gain", 16: "Process LFE",
    },
    0x006C0003: {
        0: "Threshold", 1: "Ratio", 2: "Attack Time", 3: "Release Time",
        4: "Output Gain", 5: "Process LFE", 6: "Channel Link",
    },
    0x00760003: {
        0: "Early-reflection Pattern", 1: "Pre-delay", 2: "Room Size",
        3: "Early-reflection Rear Delay", 4: "Enable Early Reflections",
        10: "Decay Time", 11: "High-frequency Damping", 12: "Density",
        13: "Room Shape", 14: "Quality", 15: "Diffusion",
        16: "Stereo Width", 20: "Enable Tone Controls",
        21: "Filter 1 Insertion", 22: "Filter 1 Curve", 23: "Filter 1 Gain",
        24: "Filter 1 Frequency", 25: "Filter 1 Q Factor",
        26: "Filter 2 Insertion", 27: "Filter 2 Curve", 28: "Filter 2 Gain",
        29: "Filter 2 Frequency", 30: "Filter 2 Q Factor",
        31: "Filter 3 Insertion", 32: "Filter 3 Curve", 33: "Filter 3 Gain",
        34: "Filter 3 Frequency", 35: "Filter 3 Q Factor",
        40: "Center Input Level", 41: "LFE Input Level",
        50: "Front Level", 51: "Rear Level", 52: "Center Level",
        53: "LFE Level", 60: "Dry Level", 61: "Early-reflection Level",
        62: "Reverb Level",
    },
    0x007E0003: {
        0: "Pre-EQ Band 1 Filter Type", 1: "Pre-EQ Band 1 Gain",
        2: "Pre-EQ Band 1 Frequency", 3: "Pre-EQ Band 1 Q Factor",
        4: "Pre-EQ Band 1 Enable", 10: "Pre-EQ Band 2 Filter Type",
        11: "Pre-EQ Band 2 Gain", 12: "Pre-EQ Band 2 Frequency",
        13: "Pre-EQ Band 2 Q Factor", 14: "Pre-EQ Band 2 Enable",
        20: "Pre-EQ Band 3 Filter Type", 21: "Pre-EQ Band 3 Gain",
        22: "Pre-EQ Band 3 Frequency", 23: "Pre-EQ Band 3 Q Factor",
        24: "Pre-EQ Band 3 Enable", 30: "Post-EQ Band 1 Filter Type",
        31: "Post-EQ Band 1 Gain", 32: "Post-EQ Band 1 Frequency",
        33: "Post-EQ Band 1 Q Factor", 34: "Post-EQ Band 1 Enable",
        40: "Post-EQ Band 2 Filter Type", 41: "Post-EQ Band 2 Gain",
        42: "Post-EQ Band 2 Frequency", 43: "Post-EQ Band 2 Q Factor",
        44: "Post-EQ Band 2 Enable", 50: "Post-EQ Band 3 Filter Type",
        51: "Post-EQ Band 3 Gain", 52: "Post-EQ Band 3 Frequency",
        53: "Post-EQ Band 3 Q Factor", 54: "Post-EQ Band 3 Enable",
        60: "Distortion Type", 61: "Distortion Drive", 62: "Distortion Tone",
        63: "Rectification", 64: "Output Gain", 65: "Wet/Dry Mix",
    },
    0x00810003: {
        0: "Attack Time", 1: "Release Time", 2: "Mode", 4: "Minimum",
        5: "Maximum", 6: "Hold", 7: "Apply Downstream Volume",
        8: "RTPC Scope", 9: "Infinite Hold",
    },
    0x00820003: {
        0: "Window Size", 1: "Time Stretch", 2: "Output Gain",
        3: "Time Stretch Random", 4: "Pitch Shift",
        5: "Pitch Shift Random", 6: "Quality Level",
        7: "Stereo Processing", 8: "Stretch Mode",
    },
    0x008A0003: {
        0: "Input", 1: "Process LFE", 2: "Delay Dry Signal",
        3: "Dry Level", 4: "Wet Level", 5: "Window Size",
        6: "Voice 1 Enable", 7: "Voice 1 Pitch", 8: "Voice 1 Gain",
        9: "Voice 1 Filter Type", 10: "Voice 1 Filter Gain",
        11: "Voice 1 Filter Frequency", 12: "Voice 1 Filter Q Factor",
        13: "Voice 2 Enable", 14: "Voice 2 Pitch", 15: "Voice 2 Gain",
        16: "Voice 2 Filter Type", 17: "Voice 2 Filter Gain",
        18: "Voice 2 Filter Frequency", 19: "Voice 2 Filter Q Factor",
    },
    0x03840009: {0: "Mix Behavior"},
}

# Reflect's AudioEnginePropertyID table changed in Wwise 2022.1 and again in
# 2023.1. Keep each table scoped to the bank generation that serialized it.
_REFLECT_PROPERTY_NAMES_145 = {
    0: "Speed of Sound", 1: "Distance Warping", 2: "Diffraction Warping",
    3: "Center Percentage", 4: "Output Channel Configuration", 5: "Wet",
    6: "Distance Smoothing", 7: "Smoothing Type", 8: "Threshold Mode",
    9: "Pitch Threshold", 10: "Distance Threshold", 11: "Max Reflections",
    12: "Dry", 13: "Max Distance", 14: "Base Texture Frequency",
    16: "Curve Usage Mask", 17: "Phasing Suppression",
}

_REFLECT_PROPERTY_NAMES_150 = {
    **{key: value for key, value in _REFLECT_PROPERTY_NAMES_145.items() if key != 17},
    17: "Fusing Time",
    18: "Decorrelation Strength", 19: "Decorrelation Algorithm",
    20: "Decorrelation Strength Source",
    21: "Decorrelation Max Reflection Order", 22: "Stereo Decorrelation",
    23: "Decorrelation Window Width", 24: "Hardware Acceleration",
}


def fx_property_names(plugin_id: int, bank_version: int | None = None):
    """Return the plug-in property table for the bank generation."""

    if bank_version is not None and bank_version >= 150 and plugin_id == 0x00AB0003:
        return _REFLECT_PROPERTY_NAMES_150
    if bank_version is not None and bank_version >= 145 and plugin_id == 0x00AB0003:
        return _REFLECT_PROPERTY_NAMES_145
    return BNK_FX_PROPERTY_NAMES.get(plugin_id)

BNK_PLUGIN_NAMES = {
    0x00010001: "Wwise PCM",
    0x00020001: "Wwise ADPCM",
    0x00040001: "Wwise Vorbis",
    0x00140001: "WEM Opus",
    0x00100001: "Wwise MIDI",
    0x01A01052: "Crankcase Audio REV Model Player",
    0x00770002: "SoundSeed Air Wind",
    0x00780002: "SoundSeed Air Woosh",
    0x00AB0003: "Wwise Reflect",
    0x00B30007: "Controller Speaker",
    0x00AA1137: "Microsoft Spatial Sound",
    **{plugin_id: schema[0] for plugin_id, schema in BNK_FX_SCHEMAS.items()},
}

__all__ = [
    "BNK_ATTENUATION_TARGETS",
    "BNK_ATTENUATION_TARGETS_145",
    "BNK_CURVE_INTERPOLATION",
    "BNK_CURVE_SCALING",
    "BNK_FX_ENUMS",
    "BNK_FX_PROPERTY_NAMES",
    "BNK_FX_SCHEMAS",
    "BNK_PLUGIN_NAMES",
    "BNK_INTEGER_PROPERTIES",
    "BNK_INTEGER_PROPERTIES_132",
    "BNK_MODULATOR_INTEGER_PROPERTIES",
    "BNK_MODULATOR_PROPERTY_NAMES",
    "BNK_STANDARD_CUE_NAMES",
    "BNK_PROPERTY_NAMES",
    "BNK_PROPERTY_NAMES_132",
    "BNK_STATE_PROPERTY_NAMES",
    "BNK_STATE_PROPERTY_NAMES_132",
    "STRUCTURED_BANK_VERSIONS",
    "attenuation_targets",
    "fx_property_names",
    "integer_properties",
    "is_hirc_bus",
    "is_hirc_plugin",
    "property_names",
]
