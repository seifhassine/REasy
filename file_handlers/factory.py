from importlib import import_module


_FORMATS = (
    "uvar.uvar", "rsz.rsz", "msg.msg", "cfil.cfil", "motbank.motbank",
    "mcambank.mcambank", "mdf.mdf", "tex.tex", "tex.dds", "mesh.mesh",
    "sound.sound", "clip.clip", "uvs.uvs", "rcol.rcol", "fol.fol",
)


def is_handler_type(handler, class_name: str) -> bool:
    return any(cls.__name__ == class_name for cls in type(handler).__mro__)


def _handler_classes():
    for path in _FORMATS:
        module = import_module(f"file_handlers.{path}_handler")
        yield getattr(module, f"{path.rsplit('.', 1)[1].title()}Handler")


def get_handler_for_data(data: bytes, filename: str = ""):
    fn = filename.lower()
    if fn.endswith(".wel.11"):
        from file_handlers.wel.wel_handler import WelHandler
        return WelHandler()
    if ".wcc" in fn:
        from file_handlers.wcc.wcc_handler import WccHandler
        return WccHandler()
    for handler_class in _handler_classes():
        if handler_class.can_handle(data):
            return handler_class()
    raise ValueError("Unsupported file type")
