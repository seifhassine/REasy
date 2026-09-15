from abc import ABC, abstractmethod
from utils.binary_handler import BinaryHandler as FileHandler

class BaseModel(ABC):
    def __init__(self):
        self.start_offset = 0
        
    def read(self, handler: FileHandler, offset: int = None) -> bool:
        if offset is not None:
            handler.seek(offset)
        self.start_offset = handler.tell
        return self.do_read(handler)
        
    def write(self, handler: FileHandler, offset: int = None) -> bool:
        if offset is not None:
            handler.seek(offset)
        self.start_offset = handler.tell
        return self.do_write(handler)
        
    @abstractmethod
    def do_read(self, handler: FileHandler) -> bool:
        pass
        
    @abstractmethod  
    def do_write(self, handler: FileHandler) -> bool:
        pass