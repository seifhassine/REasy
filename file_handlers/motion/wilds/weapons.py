"""Wilds weapon classes and the hunter-skeleton joints that carry their models.

``app.HunterDef.JointName`` is the runtime's own joint dictionary (see the dump
in ``tests/TESTFILE/il2cpp_dump.json``).  It names the hand attach points
``WpHandL`` -> ``L_Wep`` / ``WpHandR`` -> ``R_Wep``, the secondary weapon hands
``WpSubHandL`` -> ``L_Wep_Sub`` / ``WpSubHandR`` -> ``R_Wep_Sub`` and, for the
shield, ``WpSubShield`` -> ``R_Shield``.  ``R_Shield`` is the only attach point
that is not a hand: it is a child of ``R_Forearm``.

Rise models the same split with a separate shield mesh for short sword, lance,
gun lance and charge axe (``swd`` part in the main hand, ``sld`` part on the
right arm).  Those shields must follow the Wilds shield joint, not the hand the
other weapon parts inherit from ``R_Wep``.
"""
from __future__ import annotations

import re

# Directory order of natives/stm/weapon/wpXX, i.e. app.WeaponDef.TYPE:
# 0 LONG_SWORD, 1 SHORT_SWORD, 2 TWIN_SWORD, 3 TACHI, 4 HAMMER, 5 WHISTLE,
# 6 LANCE, 7 GUN_LANCE, 8 SLASH_AXE, 9 CHARGE_AXE, 10 ROD, 11 BOW,
# 12 HEAVY_BOWGUN, 13 LIGHT_BOWGUN.
WILDS_WEAPONS = ('greatsword', 'shortsword', 'dualblades', 'longsword', 'hammer',
                 'horn', 'lance', 'gunlance', 'slashaxe', 'chargeaxe',
                 'insectglaive', 'bow', 'heavybowgun', 'lightbowgun')

# Families whose Rise model carries a separate shield mesh (`*_sld001`).
SHIELD_WEAPONS = frozenset({'shortsword', 'lance', 'gunlance', 'chargeaxe'})

# Rise hunter joints that carry weapon parts, keyed by the Wilds joint that must
# drive them.  Both rigs keep the main weapon in the left hand; the right side is
# either the second weapon part (twin blade, sheath, quiver) or the shield.
_HAND_ATTACH = {'L_Weapon_00': 'L_Wep', 'R_Weapon_00': 'R_Wep'}
_SHIELD_ATTACH = {'L_Weapon_00': 'L_Wep', 'R_Weapon_00': 'R_Shield'}

WILDS_WEAPON_ATTACH = {
    'greatsword': _HAND_ATTACH,
    'shortsword': _SHIELD_ATTACH,  # ss_sld001 shield
    'dualblades': _HAND_ATTACH,
    'longsword': _HAND_ATTACH,     # ls_saya003 sheath, worn on the body
    'hammer': _HAND_ATTACH,
    'horn': _HAND_ATTACH,
    'lance': _SHIELD_ATTACH,       # l_sld001 shield
    'gunlance': _SHIELD_ATTACH,    # gl_sld001 shield
    'slashaxe': _HAND_ATTACH,
    'chargeaxe': _SHIELD_ATTACH,   # ca_sld001 shield
    'insectglaive': _HAND_ATTACH,
    'bow': _HAND_ATTACH,           # b_ydt001 quiver, worn on the body
    'heavybowgun': _HAND_ATTACH,
    'lightbowgun': _HAND_ATTACH,
}


def wilds_weapon_family(name) -> str | None:
    """Return the weapon family of a ``wp09_00`` style Wilds list name."""
    match = re.match(r'wp(\d{2})(?:_|\.|$)', str(name).strip(), re.IGNORECASE)
    if match is None:
        return None
    index = int(match[1])
    return WILDS_WEAPONS[index] if index < len(WILDS_WEAPONS) else None


def weapon_attach(family) -> dict[str, str]:
    """Return ``{rise joint: wilds joint}`` weapon carriers for one family.

    Unknown families fall back to the plain hand attach, which is what every
    single-part weapon uses.
    """
    return dict(WILDS_WEAPON_ATTACH.get(family, _HAND_ATTACH))
