"""Coupled curve/expression edits: bit sets, packed frames and tangents."""
import math
import struct

from .efx_file import EfxAttribute, EfxRecord, signature, write_value
from .expressions import compile_expression, expression_text

COMMON = 'ReeLib.Efx.Structs.Common.'


def body_of(value):
    body = value.body if isinstance(value, EfxAttribute) else value
    if not isinstance(body, EfxRecord):
        raise ValueError('Select a curve/expression attribute')
    return body


def active_channels(body, field):
    item = body.fields[field]
    limit = len(item.value) * 32
    return [i for i in range(limit) if item.value[i // 32] & (1 << (i % 32))]


def _bit(body, field, channel, enabled):
    item = body.fields[field]
    if type(channel) is not int or not 0 <= channel < len(item.value) * 32:
        raise ValueError('Channel is outside the attribute bit set')
    mask = 1 << (channel % 32)
    item.value[channel // 32] = item.value[channel // 32] | mask if enabled else item.value[channel // 32] & ~mask


def curve_spans(clip):
    spans, fi, ti = [], 0, 0
    for header in clip['clips']:
        end = fi + header['frameCount']
        if header['frameCount'] < 0 or end > len(clip['frames']):
            raise ValueError('Curve header exceeds the frame table')
        te = ti + sum(frame['type'] == 5 for frame in clip['frames'][fi:end])
        if te > len(clip['interpolationData']):
            raise ValueError('Curve is missing Bezier tangent records')
        spans.append((fi, end, ti, te)); fi, ti = end, te
    if fi != len(clip['frames']):
        raise ValueError('Curve frames are not assigned to a channel')
    return spans


def curve_snapshot(body):
    body = body_of(body)
    clip = body['clipData']
    material = 'mdfProperties' in clip.fields
    targets = list(range(len(clip['clips']))) if material else active_channels(body, 'clipBits')
    if len(targets) != len(clip['clips']):
        raise ValueError('Curve channel bit set does not match its clips')
    result = []
    for target, header, (fi, fe, ti, _) in zip(targets, clip['clips'], curve_spans(clip)):
        keys = []
        for frame in clip['frames'][fi:fe]:
            value = struct.unpack('<i', write_value(frame.fields['value'], {}))[0] if header['valueType'] == 3 else frame['value']
            key = {'time': frame['frameTime'], 'value': value, 'interpolation': frame['type']}
            if frame['type'] == 5:
                key['tangents'] = {k: f.value for k, f in clip['interpolationData'][ti].fields.items()}; ti += 1
            keys.append(key)
        result.append({'channel': target, 'value_type': header['valueType'], 'keys': keys})
        if material:
            result[-1]['material'] = {k: f.value for k, f in clip['mdfProperties'][target].fields.items()}
    return result


def edit_curve(body, operation, factory):
    body = body_of(body)
    clip = body['clipData']
    material = 'mdfProperties' in clip.fields
    snapshot = curve_snapshot(body)
    targets = [row['channel'] for row in snapshot]
    channel = operation['channel']
    if type(channel) is not int or channel < 0:
        raise ValueError('Channel must be a nonnegative integer')
    existing = channel in targets
    deleting = operation['op'] == 'curve-delete'
    if deleting and not existing:
        raise ValueError('Curve channel is absent')
    index = targets.index(channel) if existing else sum(t < channel for t in targets)
    if material and not existing and channel != len(targets):
        raise ValueError('Append a new material curve at the next slot')
    if not material:
        _bit(body, 'clipBits', channel, not deleting)
    spans = curve_spans(clip)
    fi, fe, ti, te = spans[index] if existing else (spans[index][0], spans[index][0], spans[index][2], spans[index][2]) if index < len(spans) else (len(clip['frames']), len(clip['frames']), sum(f['type'] == 5 for f in clip['frames']), sum(f['type'] == 5 for f in clip['frames']))
    frames, tangents = [], []
    if not deleting:
        payload = operation['value']
        value_type = payload['value_type']
        if value_type not in (3, 5):
            raise ValueError('Curve values support Int (3) or Float (5)')
        previous = -math.inf
        if not payload['keys']:
            raise ValueError('A curve needs at least one key')
        for key in payload['keys']:
            time, value, interpolation = key['time'], key['value'], key['interpolation']
            if type(time) not in (int, float) or not math.isfinite(time) or time < 0 or time < previous:
                raise ValueError('Curve key times must be finite, nonnegative and ordered')
            if type(interpolation) is not int:
                raise ValueError('Interpolation must be a native integer code')
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError('Curve values must be finite')
            raw = None
            if value_type == 3:
                if type(value) is not int:
                    raise ValueError('Int curves require integer values')
                raw = struct.pack('<i', value)
                value = struct.unpack('<f', raw)[0]
            frame = factory(body.version, COMMON + 'EfxClipFrame', {'frameTime': time, 'type': interpolation, 'value': value})
            if raw is not None:
                frame.fields['value'].raw, frame.fields['value'].original = raw, signature(value)
            frames.append(frame)
            if interpolation == 5:
                tangent = factory(body.version, COMMON + 'EfxClipInterpolationTangents', key['tangents'])
                if any(not math.isfinite(f.value) for f in tangent.fields.values()):
                    raise ValueError('Bezier tangents must be finite')
                tangents.append(tangent)
            elif 'tangents' in key:
                raise ValueError('Only interpolation code 5 stores tangents')
            previous = time
        header = factory(body.version, COMMON + 'EfxClipHeader', {'frameCount': len(frames), 'valueType': value_type})
    clip['frames'][fi:fe], clip['interpolationData'][ti:te] = frames, tangents
    clip['clips'][index:index + int(existing)] = [] if deleting else [header]
    for name in ('frames', 'interpolationData', 'clips'):
        clip.fields[name].elements = None
    if material:
        changing_count = deleting or not existing
        if changing_count and 'indices' not in operation:
            raise ValueError('Material curve insertion/deletion requires the explicit native indices table')
        if 'indices' in operation:
            indices = operation['indices']
            if not isinstance(indices, list) or any(type(v) is not int or not 0 <= v <= 0xffffffff for v in indices):
                raise ValueError('Material indices must be unsigned integers')
            clip['indices'] = list(indices); clip.fields['indices'].elements = None
        prop = [] if deleting else [factory(body.version, COMMON + 'EfxMaterialClip_Struct4', operation['value']['material'])]
        clip['mdfProperties'][index:index + int(existing)] = prop
        clip.fields['mdfProperties'].elements = None
        if 'bits' in operation:
            values = operation['bits']
            if len(values) != len(body['clipBits']) or any(type(v) is not int or not 0 <= v <= 0xffffffff for v in values):
                raise ValueError('Invalid material clip bit set')
            body['clipBits'] = list(values)
    clip['clipDuration'] = max((frame['frameTime'] for frame in clip['frames']), default=0.)


def edit_expression(body, operation, factory, global_parameters):
    body = body_of(body)
    material = operation.get('material', False)
    flat_material = material and 'materialExpressionsList' in body.fields
    field = 'materialExpressionsList' if flat_material else 'materialExpressions' if material else 'expressions'
    expression_list = body[field]
    values = expression_list if flat_material else expression_list['expressions']
    targets = list(range(len(values))) if material else active_channels(body, 'expressionBits')
    if len(targets) != len(values):
        raise ValueError('Expression channel mapping is unresolved; edit its full structure with explicit indices')
    channel = operation['channel']
    existing = channel in targets
    deleting = operation['op'] == 'expression-delete'
    if deleting and not existing:
        raise ValueError('Expression channel is absent')
    if material and (type(channel) is not int or channel < 0 or channel > len(values)):
        raise ValueError('Invalid material expression slot')
    index = targets.index(channel) if existing else sum(t < channel for t in targets)
    if not material:
        _bit(body, 'expressionBits', channel, not deleting)
    if deleting:
        values.pop(index)
    else:
        payload = dict(operation['value'])
        if 'formula' in payload:
            payload.update(compile_expression(payload.pop('formula'), global_parameters))
        expression = factory(body.version, COMMON + ('EFXMaterialExpression' if material else 'EFXExpressionObject'), payload)
        expression_text(expression, global_parameters)
        values[index:index + int(existing)] = [expression]
    (body.fields[field] if flat_material else expression_list.fields['expressions']).elements = None
    if material:
        headers = [name for name in ('materialExpressionCount', 'materialPropertyCount') if name in body.fields]
        if (deleting or not existing) and any(name not in operation.get('headers', {}) for name in headers):
            raise ValueError('Material channel count edits require explicit shader header values: ' + ', '.join(headers))
        if 'headers' in operation:
            from .editing import typed_value
            if set(operation['headers']) - set(headers):
                raise ValueError('Unknown material expression header')
            for name, value in operation['headers'].items():
                item = body.fields[name]
                body[name] = typed_value(item.kind, value, item.definition)
        if (deleting or not existing) and 'indices' not in operation:
            raise ValueError('Material expression insertion/deletion requires the explicit native indices table')
        if 'indices' in operation:
            indices = operation['indices']
            if not isinstance(indices, list) or any(type(v) is not int or not 0 <= v <= 0xffffffff for v in indices):
                raise ValueError('Material indices must be unsigned integers')
            if flat_material:
                body['materialIndices'] = list(indices)
                body.fields['materialIndices'].elements = None
            else:
                expression_list['indices'] = list(indices)


def validate_channels(document):
    from .graph import records
    for attribute in document.all_attributes(recursive=True):
        body = attribute.body
        for _, record in records(body):
            if record.classname.rsplit('.', 1)[-1] in ('EFXExpressionObject', 'EFXMaterialExpression'):
                expression_text(record)
        if 'clipData' in body.fields:
            clip = body['clipData']
            if isinstance(clip, EfxRecord) and 'clips' in clip.fields:
                curve_spans(clip)
                if 'mdfProperties' in clip.fields:
                    if document.version < 3539837 and len(clip['mdfProperties']) != len(clip['clips']):
                        raise ValueError('Material curve metadata must match the clip table')
                elif len(active_channels(body, 'clipBits')) != len(clip['clips']):
                    raise ValueError('Curve channel bit set does not match its clips')
        if 'expressions' in body.fields and 'expressionBits' in body.fields:
            expressions = body['expressions']['expressions']
            if len(active_channels(body, 'expressionBits')) != len(expressions):
                raise ValueError(f'{attribute.name}: expression bit set does not match its channels')
