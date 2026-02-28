from file_handlers.uvar.uvar_handler import UvarHandler
from file_handlers.rsz.rsz_handler import RszHandler
from file_handlers.msg.msg_handler import MsgHandler
from file_handlers.cfil.cfil_handler import CfilHandler
from file_handlers.motbank.motbank_handler import MotbankHandler
from file_handlers.mcambank.mcambank_handler import McambankHandler
from file_handlers.motfsm.motfsm_handler import MotfsmHandler
from file_handlers.tex.tex_handler import TexHandler
from file_handlers.tex.dds_handler import DdsHandler
from file_handlers.mesh.mesh_handler import MeshHandler
from file_handlers.mdf.mdf_handler import MdfHandler
from file_handlers.base_handler import FileHandler
from file_handlers.sound.sound_handler import SoundHandler
from file_handlers.uvs.uvs_handler import UvsHandler
from file_handlers.wel.wel_handler import WelHandler
from file_handlers.wcc.wcc_handler import WccHandler


def get_handler_for_data(data: bytes, filename: str = "") -> FileHandler:
    fn = filename.lower()
    if fn.endswith(".wel.11"):
        return WelHandler()
    elif ".wcc" in fn:
        return WccHandler()
    for handler_class in [
        MotfsmHandler,  # Before RszHandler (MOTFSM2 contains embedded RSZ)
        RszHandler,
        MsgHandler,
        UvarHandler,
        CfilHandler,
        MotbankHandler,
        McambankHandler,
        MdfHandler,
        TexHandler,
        DdsHandler,
        MeshHandler,        
        SoundHandler,
        UvsHandler,
    ]:
        if handler_class.can_handle(data):
            return handler_class()
    raise ValueError("Unsupported file type")
