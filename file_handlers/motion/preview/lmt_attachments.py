"""Provisional XX weapon attachments from event group zero."""
from .mhr_attachments import weapon_attachment
from ..lmt_events import lmt_attachment_state


def lmt_weapon_attachment(part, motion, frame, *, default_properties=()):
    state = lmt_attachment_state(motion, frame)
    if part.weapon_role == 'main' and state is not None:
        return next((joint, matrix) for option, joint, matrix in part.attachment_options if option == state)
    return weapon_attachment(part, {}, frame, default_properties=default_properties)
