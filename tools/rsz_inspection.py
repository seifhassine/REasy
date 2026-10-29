"""Public import compatibility for the native RSZ inspection API."""
from file_handlers.rsz.inspection import scalar, field_record, fields_record

__all__ = ['scalar', 'field_record', 'fields_record']
