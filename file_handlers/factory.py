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


def get_handler_for_data(data: bytes) -> FileHandler:
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
    ]:
        if handler_class.can_handle(data):
            return handler_class()
    raise ValueError("Unsupported file type")
