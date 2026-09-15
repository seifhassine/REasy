from .texture_handler import TextureViewerHandler


class DdsHandler(TextureViewerHandler):
    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return data.startswith(b"DDS ")

