import re
lazy from file_handlers.uvar.uvar_handler import UvarHandler
lazy from file_handlers.rsz.rsz_handler import RszHandler
lazy from file_handlers.msg.msg_handler import MsgHandler
lazy from file_handlers.cfil.cfil_handler import CfilHandler
lazy from file_handlers.motbank.motbank_handler import MotbankHandler
lazy from file_handlers.mcambank.mcambank_handler import McambankHandler
lazy from file_handlers.mdf.mdf_handler import MdfHandler
lazy from file_handlers.tex.tex_handler import TexHandler
lazy from file_handlers.tex.dds_handler import DdsHandler
lazy from file_handlers.mesh.mesh_handler import MeshHandler
lazy from file_handlers.stmesh.stmesh_handler import StmeshHandler
lazy from file_handlers.sdftex.sdftex_handler import SdfTexHandler
lazy from file_handlers.sdf.sdf_handler import SdfHandler
lazy from file_handlers.sound.sound_handler import SoundHandler
lazy from file_handlers.clip.clip_handler import ClipHandler
lazy from file_handlers.motion.motlist_handler import MotListHandler
lazy from file_handlers.uvs.uvs_handler import UvsHandler
lazy from file_handlers.gui.gui_handler import GuiHandler
lazy from file_handlers.gcf.gcf_handler import GcfHandler
lazy from file_handlers.ift.ift_handler import IftHandler
lazy from file_handlers.oft.oft_handler import OftHandler
lazy from file_handlers.rcol.rcol_handler import RcolHandler
lazy from file_handlers.fol.fol_handler import FolHandler
lazy from file_handlers.mmtr.mmtr_handler import MmtrHandler
lazy from file_handlers.wel.wel_handler import WelHandler
lazy from file_handlers.mov.mov_handler import MovHandler
lazy from file_handlers.wcc.wcc_handler import WccHandler


def is_handler_type(handler, class_name: str) -> bool:
    return any(cls.__name__ == class_name for cls in type(handler).__mro__)


def _handler_classes():
    yield UvarHandler
    yield RszHandler
    yield MsgHandler
    yield CfilHandler
    yield MotbankHandler
    yield McambankHandler
    yield MdfHandler
    yield TexHandler
    yield DdsHandler
    yield MeshHandler
    yield StmeshHandler
    yield SdfTexHandler
    yield SdfHandler
    yield SoundHandler
    yield ClipHandler
    yield MotListHandler
    yield UvsHandler
    yield GuiHandler
    yield GcfHandler
    yield IftHandler
    yield OftHandler
    yield RcolHandler
    yield FolHandler


def get_handler_for_data(data: bytes, filename: str = ""):
    fn = filename.lower()
    if re.search(r"\.mmtr\.\d+(?:\.[^.]+)*$", fn):
        return MmtrHandler()
    if re.search(r"\.wel\.\d+(?:\.[^.]+)*$", fn):
        return WelHandler()
    if re.search(r"\.mov\.\d+(?:\.[^.]+)*$", fn):
        return MovHandler()
    if re.search(r"\.(?:wcc|wcp|wcst|wgs|wss|wcsw|wcss|wcsa|wcsf)(?:\.\d+)?(?:\.[^.]+)*$", fn):
        return WccHandler()
    for handler_class in _handler_classes():
        if handler_class.can_handle(data):
            return handler_class()
    raise ValueError("Unsupported file type")
