"""Shared helpers for tree-item highlighting."""


def model_index_row_path(index):
    """Return an index's stable row path from the model root."""
    path = []
    current = index
    while current.isValid():
        path.append(current.row())
        current = current.parent()
    return tuple(reversed(path))
