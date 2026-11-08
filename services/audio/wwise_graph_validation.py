"""Offline scalar/reference cross-check against an existing wwiser XML dump."""
import math
import xml.etree.ElementTree as ET


def verify_wwiser_xml(graph, xml_path, bank_filename):
    document = ET.fromstring(b'<banks>' + xml_path.read_bytes() + b'</banks>')
    bank = next(root for root in document if root.get('filename') == bank_filename)
    by_offset = {int(field.get('offset')): field for field in bank.iter('field')
                 if field.get('offset') and field.get('value') is not None}
    count = 0
    mismatch = []
    for node in graph['nodes']:
        for field in node.get('source_fields', node['fields']):
            if field['storage'] in ('bit', 'bytes', 'string'):
                continue
            offset = node['payload_file_offset'] + field['offset']
            reference = by_offset.get(offset)
            if reference is None:
                mismatch.append({'object': node['id'], 'path': field['path'], 'error': 'absent XML offset'})
                continue
            raw = reference.get('value')
            if field['storage'] in ('f32', 'f64'):
                expected = float(raw)
                equal = math.isclose(expected, field['value'], rel_tol=1e-7, abs_tol=1e-7)
            else:
                expected = int(raw)
                if field['storage'].startswith('u'):
                    bits = int(field['storage'][1:])
                    if -(1 << (bits - 1)) <= expected < 0:
                        expected += 1 << bits
                equal = expected == field['value']
            if not equal:
                mismatch.append({'object': node['id'], 'path': field['path'],
                                 'xml_name': reference.get('name'), 'xml_value': raw, 'value': field['value']})
            count += 1
    return {'compared_scalars': count, 'mismatches': mismatch}
