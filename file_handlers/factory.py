def is_handler_type(handler, class_name: str) -> bool:
    return any(cls.__name__ == class_name for cls in type(handler).__mro__)


def _handler_classes():
    from file_handlers.uvar.uvar_handler import UvarHandler
    yield UvarHandler
    from file_handlers.rsz.rsz_handler import RszHandler
    yield RszHandler
    from file_handlers.msg.msg_handler import MsgHandler
    yield MsgHandler
    from file_handlers.cfil.cfil_handler import CfilHandler
    yield CfilHandler
    from file_handlers.motbank.motbank_handler import MotbankHandler
    yield MotbankHandler
    from file_handlers.mcambank.mcambank_handler import McambankHandler
    yield McambankHandler
    from file_handlers.mdf.mdf_handler import MdfHandler
    yield MdfHandler
    from file_handlers.tex.tex_handler import TexHandler
    yield TexHandler
    from file_handlers.tex.dds_handler import DdsHandler
    yield DdsHandler
    from file_handlers.mesh.mesh_handler import MeshHandler
    yield MeshHandler
    from file_handlers.sound.sound_handler import SoundHandler
    yield SoundHandler
    from file_handlers.clip.clip_handler import ClipHandler
    yield ClipHandler
    from file_handlers.uvs.uvs_handler import UvsHandler
    yield UvsHandler
    from file_handlers.rcol.rcol_handler import RcolHandler
    yield RcolHandler
    from file_handlers.fol.fol_handler import FolHandler
    yield FolHandler


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
