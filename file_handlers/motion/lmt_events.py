"""Shared XX event interpretation for preview and native WeaponHold export."""


def lmt_attachment_state(motion, frame):
    if not motion.events:
        return None
    remap, records = motion.events[0]
    start, state = 0, None
    for mask, duration in records:
        if start > frame:
            break
        if mask:
            values = {remap[bit] for bit in range(32) if mask & (1 << bit)}
            state = 'body' if 2 in values else None
            if state is None:
                hands = {hand for value, hand in ((1, 'right'), (3, 'left'), (5, 'right'), (29, 'right')) if value in values}
                state = next(iter(hands)) if len(hands) == 1 else None
        start += duration
    return state


def weapon_hold_keys(motion, properties):
    """Change the main weapon while retaining the template's sub-weapon state."""
    frames = {0, motion.end_frame}
    if motion.events:
        start = 0
        for _, duration in motion.events[0][1]:
            if start <= motion.end_frame:
                frames.add(start)
            start += duration
    return _hold_keys([(frame, lmt_attachment_state(motion, frame)) for frame in sorted(frames)], properties)


def sampled_weapon_hold_keys(samples, properties):
    return _hold_keys([(frame, lmt_attachment_state(motion, source_frame))
                       for frame, (motion, source_frame) in enumerate(samples)], properties)


def _hold_keys(states, properties):
    from .errors import MotionWriteError
    defaults = {}
    for hand, prop in properties.items():
        if not prop.keys or any(k.value != prop.keys[0].value for k in prop.keys):
            raise MotionWriteError('LMT import requires a constant native WeaponHold template')
        defaults[hand] = int(prop.keys[0].value)
    if set(defaults) != {'left', 'right'} or any(v not in (1, 2, 3, 4) for v in defaults.values()):
        raise MotionWriteError('Unsupported native WeaponHold template states')
    # Type: None=1, AttachMain=2, AttachSub=3, AttachAll=4.
    result = {'_leftWp': [], '_rightWp': []}
    for frame, state in states:
        values = dict(defaults)
        if state is not None:
            values = {hand: 3 if value in (3, 4) else 1 for hand, value in defaults.items()}
            if state in ('left', 'right'):
                values[state] = 4 if values[state] == 3 else 2
        for hand, value in values.items():
            keys = result[f'_{hand}Wp']
            if not keys or value != keys[-1][1] or frame == states[-1][0]:
                keys.append((frame, value))
    return result
