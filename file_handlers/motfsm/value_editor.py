"""Named choices that always commit their underlying native values."""
from PySide6.QtWidgets import QComboBox


def choices(field):
    if field.enum_values:
        return field.enum_values
    if field.binding and field.binding.type_name == 'bool':
        return [{'name': 'True', 'value': True}, {'name': 'False', 'value': False}]
    return []


def fill_combo(editor, members, value):
    editor.clear()
    for member in members:
        editor.addItem(member['name'], member['value'])
    index = editor.findData(value)
    if index < 0:
        editor.addItem(str(value), value)
        index = editor.count()-1
    editor.setCurrentIndex(index)


def combo(parent, members, value):
    editor = QComboBox(parent)
    fill_combo(editor, members, value)
    return editor
