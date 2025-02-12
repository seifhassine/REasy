from file_handlers.uvar_handler import UvarHandler
from file_handlers.base_handler import FileHandler

def get_handler_for_data(data: bytes) -> FileHandler:
    for handler_class in [UvarHandler]:
        if handler_class.can_handle(data):
            return handler_class()
    raise ValueError("Unsupported file type")