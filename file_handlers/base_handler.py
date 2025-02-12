from abc import ABC, abstractmethod
import tkinter as tk
from tkinter import ttk

class FileHandler(ABC):
    @classmethod
    @abstractmethod
    def can_handle(cls, data: bytes) -> bool:
        """Return True if this handler can parse the given data."""
        pass

    @abstractmethod
    def read(self, data: bytes):
        """Parse the raw data into internal structures."""
        pass

    @abstractmethod
    def rebuild(self) -> bytes:
        """Rebuild the file and return its raw data."""
        pass

    @abstractmethod
    def populate_treeview(self, tree: ttk.Treeview, parent_id, metadata_map: dict):
        """Populate the tree with a representation of this file."""
        pass

    @abstractmethod
    def get_context_menu(self, tree: tk.Widget, row_id, meta: dict) -> tk.Menu:
        """Return a context menu for the given tree row (or None)."""
        pass

    @abstractmethod
    def handle_edit(self, meta: dict, new_val, old_val, row_id):
        """Handle an edit for a given tree node."""
        pass

    @abstractmethod
    def add_variables(self, target, prefix: str, count: int):
        """
        Add new variables to the given target file object using the provided prefix and count.
        The implementation should update the internal data and rebuild the file.
        """
        pass

    @abstractmethod
    def update_strings(self):
        """Update the internal string data (if necessary) before repopulating the tree."""
        pass