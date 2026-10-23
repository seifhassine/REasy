"""Portable native snapshots for clipboard and user-authored presets."""
import base64
import json

from .efx_file import EfxFile, EfxEntry, EfxAttribute, EfxRecord, EfxBone
from .editing import locate, path_text, tokens


def supported_node(value):
    return isinstance(value, (EfxEntry, EfxAttribute, EfxBone)) or isinstance(value, EfxRecord) and value.classname in ('ExpressionParameter', 'FieldParameter', 'EffectGroup')


def export_template(document, path):
    if not supported_node(locate(document, path).value):
        raise ValueError('Select an entry, attribute, bone, group, or parameter')
    return json.dumps({'format': 'REasy.EFX.Preset', 'format_version': 1,
                       'efx_version': document.version, 'source': path_text(tokens(path)),
                       'document': base64.b64encode(document.build_validated()).decode('ascii')}, ensure_ascii=False, indent=2)


def import_template(text):
    payload = json.loads(text)
    if not isinstance(payload, dict) or payload.get('format') != 'REasy.EFX.Preset' or payload.get('format_version') != 1:
        raise ValueError('Not an REasy EFX preset')
    document = EfxFile.from_bytes(base64.b64decode(payload['document'], validate=True), version=payload['efx_version'])
    path = tokens(payload['source'])
    if not supported_node(locate(document, path).value):
        raise ValueError('Unsupported preset source')
    return document, path
