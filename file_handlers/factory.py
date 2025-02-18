from file_handlers.uvar_handler import UvarHandler
from file_handlers.rcol_handler import RcolHandler
from file_handlers.scn_handler import ScnHandler
from file_handlers.base_handler import FileHandler


def get_handler_for_data(data: bytes) -> FileHandler:
    for handler_class in [ScnHandler, RcolHandler, UvarHandler]:
        if handler_class.can_handle(data):
            return handler_class()
    raise ValueError("Unsupported file type")
