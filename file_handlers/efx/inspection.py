"""JSON views of native EFX documents (RE Engine containers and World EFX)."""
from file_handlers.efx.efx_file import EfxFile, EfxRecord
from file_handlers.efx.mhw_efx_file import MhwEfxFile


def record_data(value):
    if isinstance(value, EfxFile):
        return document_data(value)
    if isinstance(value, EfxRecord):
        result = {name: record_data(item.value) for name, item in value.fields.items()}
        if value.name is not None:
            result['name'] = value.name
        return result
    if isinstance(value, (tuple, list)):
        return [record_data(item) for item in value]
    return value


def document_data(document):
    if isinstance(document, MhwEfxFile):
        return document.document
    def entry_data(position, entry):
        return {"position": position, "index": entry.index, "name": entry.name, "assignment": entry.assignment,
                "attributes": [{"position": ai, "type_id": attribute.type_id, "type": attribute.name,
                                "unique_id": attribute.unique_id,
                                "parent_bone": attribute.parent_bone.name if attribute.parent_bone else None,
                                "fields": record_data(attribute.body)} for ai, attribute in enumerate(entry.attributes)]}

    return {"format": "RE_EFX", "version": document.version, "dimension": document.header[1],
            "header_u32": [value & 0xFFFFFFFF for value in document.header],
            "bones": [{"name": bone.name, "hash": bone.name_hash, "value": bone.value} for bone in document.bones],
            "expression_parameters": record_data(document.expression_parameters),
            "field_parameters": record_data(document.field_parameters),
            "entries": [entry_data(i, entry) for i, entry in enumerate(document.entries)],
            "actions": [entry_data(i, entry) for i, entry in enumerate(document.actions)],
            "groups": record_data(document.groups), "user_variables": record_data(document.uvar_groups)}
