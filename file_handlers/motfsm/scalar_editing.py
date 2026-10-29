"""Fixed-width FSM edits with exact unrelated-byte preservation."""
from .motfsm_file import MotfsmFile


def open_document(source):
    document = MotfsmFile()
    document.read(source.read_bytes())
    return document

def save_edits(document, edits):
    edits = list(edits)
    for binding, value in edits:
        print(f'{binding.name}: {binding.value} -> {value}')
        document.edit_field(binding, value)
    output = document.rebuild()
    expected = bytearray(document.source)
    for binding, _ in edits:
        expected[binding.offset:binding.offset + binding.size] = binding.encode(binding.value)
    if output != bytes(expected):
        raise ValueError('Rebuild changed bytes outside the requested fields')
    reopened = MotfsmFile()
    reopened.read(output)
    if reopened.rebuild() != output:
        raise ValueError('Reopened candidate does not rebuild identically')
    return output
