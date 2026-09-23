"""Node selection and scalar input shared by all FSM commands."""
import re


def integer(text):
    text = str(text).strip()
    return int(text, 16 if text.lower().lstrip('+-').startswith('0x') else 10)


def parse_value(text):
    text = text.strip()
    if text.lower() in ('true', 'false'):
        return text.lower() == 'true'
    try:
        return integer(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def assignment(text):
    name, separator, value = text.partition('=')
    if not separator or not name.strip():
        raise ValueError('--set requires FIELD=VALUE')
    return text


def identity(node):
    return f'0x{node.id_hash & 0xFFFFFFFF:08X}:{node.ex_id}'


def node_string(name):
    if not name or '\0' in name:
        raise ValueError('Node name must be nonempty and cannot contain NUL')
    data = (name + '\0').encode('utf-16le')
    return data + bytes(-len(data) % 16)


def node_path(document, index):
    names, seen = [], set()
    while index is not None:
        if index in seen:
            raise ValueError('Cycle in node parent references')
        seen.add(index)
        node = document.bhvt.nodes[index]
        names.append(node.name)
        index = document.references.parent_index(node)
    return '.'.join(reversed(names))


def resolve_node(document, selector):
    """Resolve one node; never choose the first of several matching identities."""
    nodes = document.bhvt.nodes
    if selector.startswith('index:'):
        index = integer(selector[6:])
        if not 0 <= index < len(nodes):
            raise ValueError(f'Node index out of range: {index}')
        return index
    if selector.startswith('name:'):
        matches = [i for i, node in enumerate(nodes) if node.name == selector[5:]]
    elif selector.lower().startswith('0x'):
        parts = selector.split(':')
        if len(parts) > 2:
            raise ValueError(f'Invalid node identity: {selector}')
        value = integer(parts[0])
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError(f'Node hash outside u32: {selector}')
        ex = integer(parts[1]) if len(parts) == 2 else None
        matches = [i for i, node in enumerate(nodes)
                   if node.id_hash & 0xFFFFFFFF == value and (ex is None or node.ex_id == ex)]
    elif selector.startswith('path:'):
        matches = [i for i in range(len(nodes)) if node_path(document, i) == selector[5:]]
    else:
        matches = [i for i, node in enumerate(nodes) if node.name == selector]
        if not matches:
            matches = [i for i in range(len(nodes)) if node_path(document, i) == selector]
    if len(matches) != 1:
        choices = ', '.join(f'index:{i} ({identity(nodes[i])})' for i in matches[:12])
        raise ValueError(f'Node {selector!r} resolved to {len(matches)} nodes'
                         + (f'; use {choices}' if choices else ''))
    return matches[0]


def state_indexes(text):
    selected = set()
    for part in text.split(','):
        match = re.fullmatch(r'\s*(\d+)(?:\s*-\s*(\d+))?\s*', part)
        if not match:
            raise ValueError(f'Invalid state selection: {text}')
        first = int(match[1])
        last = int(match[2]) if match[2] else first
        if last < first:
            raise ValueError(f'Reversed state range: {part}')
        selected.update(range(first, last + 1))
    return sorted(selected)
