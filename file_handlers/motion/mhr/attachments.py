"""The WeaponHold track uses snow.player.WeaponHold.Type, not ConstType."""


def weapon_hold_properties(motion):
    found = {}
    def visit(node):
        if node.name.rsplit('.', 1)[-1] == 'WeaponHold':
            for prop in node.properties:
                if prop.name in ('_leftWp', '_rightWp'):
                    found['left' if prop.name == '_leftWp' else 'right'] = prop
        for child in node.children:
            visit(child)
    for sequence in motion.sequences:
        visit(sequence.clip.root)
    return found


def weapon_attachment(part, properties, frame, *, default_properties=()):
    if not properties:
        properties = dict(default_properties)
    if not part.weapon_role or not properties:
        return part.parent_joint, part.local_transform
    wanted = 2 if part.weapon_role == 'main' else 3  # AttachMain / AttachSub
    states = {}
    for hand, prop in properties.items():
        keys = [key for key in prop.keys if key.frame <= frame]
        if keys:
            states[hand] = int(keys[-1].value)
    for hand in ('left', 'right'):
        if states.get(hand) in (wanted, 4):  # AttachAll
            return next((joint, matrix) for option, joint, matrix in part.attachment_options if option == hand)
    if states and all(state in (1, 2, 3) for state in states.values()):
        return next((joint, matrix) for option, joint, matrix in part.attachment_options if option == 'body')
    return part.parent_joint, part.local_transform
