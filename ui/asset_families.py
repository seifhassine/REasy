from utils.resource_file_utils import resource_version_from_path

lazy from file_handlers.gui.rules import supported_versions

FAMILIES = {
    "mesh": ("cube", "#3fb6d6", "mesh stmesh"),
    "tex": ("image", "#d6604f", "tex rtex sdftex dds uvs"),
    "material": ("palette", "#5fbf5f", "mdf2 mmtr"),
    "shader": ("code", "#4fc1a6", "sdf vsdf csdf"),
    "scene": ("layers", "#e09a3a", "scn fol"),
    "prefab": ("gameobject", "#e09a3a", "pfb"),
    "data": ("variable", "#b07fd6", "user uvar"),
    "motion": ("bone", "#d9825f", "mot motlist motbank motpack mottree jcns mcambank"),
    "timeline": ("clock", "#d6c14f", "clip tml ucurve dlgtml motclip"),
    "logic": ("graph", "#d67fb0", "bhvt fsm fsmv2 motfsm motfsm2 tmlfsm2"),
    "collision": ("box", "#8fa3b0", "rcol cfil"),
    "gui": ("template", "#4fa3d6", "gui gcf"),
    "text": ("text", "#a0a3aa", "msg oft ift"),
    "video": ("play", "#c77fd6", "mov"),
    "sound": ("sound", "#5f8ad9", "spck sbnk pck bnk wel wcc wcp wcst wgs wss wcsw wcss wcsa wcsf wcbk wcggp wcgp wcja "
                                  "wcjm wcjmv wcjr wcmo wcms wcmsw wcmts wcr wcrb wcrd wcswn wcta wcv amix swms wfa "
                                  "wms wpi wtos wtot"),
}
_FAMILY_BY_EXTENSION = {
    extension: family for family, (_icon, _color, extensions) in FAMILIES.items() for extension in extensions.split()
}


def resource_extension(path: str) -> str:
    parts = path.rsplit("/", 1)[-1].lower().split(".")
    for at in range(len(parts) - 1, 1, -1):
        if parts[at].isdigit():
            return parts[at - 1]
    return parts[-1] if len(parts) > 1 else ""


def asset_family(path: str) -> str | None:
    family = _FAMILY_BY_EXTENSION.get(resource_extension(path))
    if family == "gui" and resource_version_from_path(path, "gui") not in (None, *supported_versions()):
        return None
    return family
