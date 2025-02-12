#!/usr/bin/env python3
import os
import sys
import json
import struct
import uuid
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import queue
import threading
import concurrent.futures
from PIL import Image, ImageTk

from file_handlers.factory import get_handler_for_data

# ----------------- User Settings -----------------
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".reasy_editor_settings.json")
DEFAULT_SETTINGS = {"dark_mode": True}

GLOBAL_DARK_MODE = False

# ----------------- Custom Toplevel Subclass -----------------
class AppToplevel(tk.Toplevel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_app_icon(self)
        self.update_dark_mode()

    def update_dark_mode(self):
        if GLOBAL_DARK_MODE:
            self.configure(bg="#2b2b2b")
        else:
            self.configure(bg="SystemButtonFace")

# ----------------- Getting Paths -----------------
def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# ----------------- Helper for Setting the Icon -----------------
def set_app_icon(window):
    icon_path = resource_path("reasy_editor_logo.ico")
    try:
        img = Image.open(icon_path)
        photo = ImageTk.PhotoImage(img)
        window.iconphoto(True, photo)
        window._icon = photo
    except Exception as e:
        print("Failed to set window icon using Pillow:", e)

tk.Toplevel = AppToplevel

# ----------------- Settings Load/Save -----------------
def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r") as f:
                settings = json.load(f)
            for key, val in DEFAULT_SETTINGS.items():
                settings.setdefault(key, val)
            return settings
    except Exception as e:
        print("Error loading settings:", e)
    return DEFAULT_SETTINGS.copy()

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f)
    except Exception as e:
        print("Error saving settings:", e)

class CustomNotebook(ttk.Notebook):
    """
    A custom ttk.Notebook with close buttons on each tab.
    """
    def __init__(self, *args, **kwargs):
        ttk.Style().theme_use("clam")
        super().__init__(*args, **kwargs)
        self._active = None
        self._create_custom_style()
        self.bind("<ButtonPress-1>", self._on_close_press, True)
        self.bind("<ButtonRelease-1>", self._on_close_release, True)

    def _create_custom_style(self):
        style = ttk.Style()
        self.close_img = tk.PhotoImage(file=resource_path("close.png"))
        try:
            style.element_create(
                "Custom.Close", "image", self.close_img, border=4, sticky=''
            )
        except Exception as e:
            print("Error creating Custom.Close element:", e)
        style.layout("Custom.TNotebook.Tab", [
            ("Custom.TNotebook.tab", {
                "sticky": "nswe",
                "children": [
                    ("Custom.TNotebook.padding", {"side": "top", "sticky": "nswe",
                        "children": [
                            ("Custom.TNotebook.focus", {"side": "top", "sticky": "nswe",
                                "children": [
                                    ("Custom.TNotebook.label", {"side": "left", "sticky": ""}),
                                    ("Custom.Close", {"side": "right", "sticky": ""})
                                ]
                            })
                        ]
                    })
                ]
            })
        ])
        self.configure(style="Custom.TNotebook")

    def _on_close_press(self, event):
        element = self.identify(event.x, event.y)
        if "Custom.Close" in element:
            index = self.index("@%d,%d" % (event.x, event.y))
            self.state(["pressed"])
            self._active = index

    def _on_close_release(self, event):
        if not self.instate(["pressed"]):
            return
        element = self.identify(event.x, event.y)
        index = self.index("@%d,%d" % (event.x, event.y))
        if "Custom.Close" in element and self._active == index:
            self.forget(index)
        self.state(["!pressed"])
        self._active = None

# ----------------- FileTab Class -----------------
class FileTab:
    def __init__(self, parent_notebook, filename=None, data=None):
        self.parent_notebook = parent_notebook
        self.frame = ttk.Frame(parent_notebook)
        self.filename = filename
        self.handler = None
        self.metadata_map = {}
        self.undo_stack = []
        self.redo_stack = []
        self.search_results = []
        self.current_result_index = 0

        # Create status bar
        self.status_var = tk.StringVar(value="No file loaded")
        self.status_bar = ttk.Label(self.frame, textvariable=self.status_var, anchor="w", padding=5)
        self.status_bar.pack(side="bottom", fill="x")

        # Create TreeView
        tree_frame = ttk.Frame(self.frame)
        tree_frame.pack(side="top", fill="both", expand=True)

        self.tree = ttk.Treeview(tree_frame, columns=("value",), show="tree headings")
        self.tree.heading("#0", text="Field Name")
        self.tree.heading("value", text="Value")
        self.tree.column("#0", width=240, anchor="w", stretch=True)
        self.tree.column("value", width=400, anchor="w", stretch=True)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Button-3>", self.show_context_menu)

        # Find-dialog variables
        self.find_window = None
        self.search_entry = None
        self.result_frame = None
        self.result_listbox = None
        self.result_scrollbar = None
        self.result_label = None
        self.case_var = tk.IntVar(value=0)  # 0: case-insensitive / 1: case-sensitive

        # You can set a default dark mode flag for FileTab as well:
        self.dark_mode = False

        if data:
            self.load_file(filename, data)

    def load_file(self, filename, data):
        self.filename = filename
        self.handler = get_handler_for_data(data)
        self.handler.app = self
        self.handler.refresh_tree_callback = self.refresh_tree
        self.handler.read(data)
        self.refresh_tree()
        self.status_var.set(f"Loaded: {filename}")

    def refresh_tree(self):
        if self.handler:
            self.handler.update_strings()
        # Clear existing tree items
        for child_id in self.tree.get_children():
            self.tree.delete(child_id)
        self.metadata_map.clear()
        if self.handler:
            self.handler.populate_treeview(self.tree, "", self.metadata_map)
        self.frame.update_idletasks()

    def show_context_menu(self, event):
        clicked_row = self.tree.identify_row(event.y)
        if not clicked_row:
            return
        meta = self.metadata_map.get(clicked_row)
        if self.handler:
            menu = self.handler.get_context_menu(self.tree, clicked_row, meta)
            if menu:
                menu.post(event.x_root, event.y_root)

    def on_double_click(self, event):
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if not row_id or col_id != "#1":
            return
        old_val = self.tree.set(row_id, "value")
        meta = self.metadata_map.get(row_id, {})
        x0, y0, width, height = self.tree.bbox(row_id, col_id)
        entry = tk.Entry(self.tree, font=("Segoe UI", 10))
        entry.place(x=x0, y=y0, width=width, height=height)
        entry.insert(0, str(old_val))
        entry.focus()

        def commit(_event=None):
            new_val = entry.get()
            entry.destroy()
            if new_val == old_val:
                return
            state = self.save_tree_state()
            if self.handler:
                self.handler.handle_edit(meta, new_val, old_val, row_id)
                self.refresh_tree()
                self.restore_tree_state(state)

        entry.bind("<FocusOut>", commit)
        entry.bind("<Return>", commit)

    def save_tree_state(self):
        state = {"yview": self.tree.yview(), "expansion": {}}

        def save_node(node, path):
            state["expansion"][path] = self.tree.item(node, "open")
            for child in self.tree.get_children(node):
                child_text = self.tree.item(child, "text")
                save_node(child, path + (child_text,))

        for node in self.tree.get_children(""):
            node_text = self.tree.item(node, "text")
            save_node(node, (node_text,))
        selected = self.tree.selection()
        if selected:
            state["selected"] = self.tree.item(selected[0], "text")
        else:
            state["selected"] = None
        return state

    def restore_tree_state(self, state):
        if "yview" in state:
            self.tree.yview_moveto(state["yview"][0])
        if "expansion" in state:
            expansion = state["expansion"]

            def restore_node(node, path):
                if path in expansion and expansion[path]:
                    self.tree.item(node, open=True)
                for child in self.tree.get_children(node):
                    child_text = self.tree.item(child, "text")
                    restore_node(child, path + (child_text,))
            for node in self.tree.get_children(""):
                node_text = self.tree.item(node, "text")
                restore_node(node, (node_text,))
        if state.get("selected"):
            for node in self.tree.get_children(""):
                if self.tree.item(node, "text") == state["selected"]:
                    self.tree.selection_set(node)
                    self.tree.focus(node)
                    break

    def on_save(self):
        if not self.handler:
            messagebox.showerror("Error", "No file loaded.")
            return
        data = self.handler.rebuild()
        filename = filedialog.asksaveasfilename(
            title="Save File As...",
            defaultextension=".uvar.3",
            filetypes=[("All Files", "*.*")]
        )
        if not filename:
            return
        with open(filename, "wb") as out_file:
            out_file.write(data)
        messagebox.showinfo("Saved", f"File saved to {filename}")
        self.status_var.set(f"Saved: {filename}")

    def reload_file(self):
        if not self.filename:
            messagebox.showerror("Error", "No file currently loaded.")
            return
        try:
            with open(self.filename, "rb") as f:
                data = f.read()
            self.handler = get_handler_for_data(data)
            self.handler.app = self
            self.handler.refresh_tree_callback = self.refresh_tree
            self.handler.read(data)
            self.refresh_tree()
            self.status_var.set(f"Reloaded: {self.filename}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to reload file: {e}")

    def copy_to_clipboard(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        value = self.tree.set(selected[0], "value")
        if value:
            self.tree.winfo_toplevel().clipboard_clear()
            self.tree.winfo_toplevel().clipboard_append(value)
            messagebox.showinfo("Copied", f"Copied: {value}")

    # ----------------- Find Dialog Methods -----------------
    def create_dialog(self, title, geometry=None):
        top = self.parent_notebook.winfo_toplevel()
        win = tk.Toplevel(top)
        win.title(title)
        if geometry:
            win.geometry(geometry)
        bg_color = win.cget("bg")
        return win, bg_color

    def get_style_options(self):
        if self.dark_mode:
            return {"fg": "white", "bg": "#2b2b2b"}
        else:
            return {}

    def open_find_dialog(self):
        
        if self.find_window is not None and tk.Toplevel.winfo_exists(self.find_window):
            self.find_window.lift()
            return

        self.find_window, bg = self.create_dialog("Find in Tree", "350x200")
        opts = self.get_style_options()
        tk.Label(self.find_window, text="Find:", font=("Segoe UI", 10), **opts).pack(pady=5)
        self.search_entry = tk.Entry(self.find_window, width=30, font=("Segoe UI", 10))
        self.search_entry.pack(pady=5)
        case_check = tk.Checkbutton(
            self.find_window,
            text="Case Sensitive",
            variable=self.case_var,
            onvalue=1,
            offvalue=0,
            font=("Segoe UI", 10),
            **opts,
            selectcolor=("gray" if self.dark_mode else "lightgray")
        )
        case_check.pack(pady=2)
        button_frame = tk.Frame(self.find_window, bg=bg)
        button_frame.pack(pady=10)
        tk.Button(button_frame, text="Find Previous", command=self.find_previous, font=("Segoe UI", 10)).pack(side="left", padx=5)
        tk.Button(button_frame, text="Find Next", command=self.find_next, font=("Segoe UI", 10)).pack(side="left", padx=5)
        tk.Button(button_frame, text="Find All", command=self.find_all, font=("Segoe UI", 10)).pack(side="left", padx=5)
        self.result_frame = tk.Frame(self.find_window, bg=bg)
        self.result_frame.pack(fill="both", expand=True)

    def find_all(self):
        if not self.search_entry:
            messagebox.showerror("Error", "Find dialog not open.", parent=self.find_window)
            return
        search_text = self.search_entry.get().strip()
        if not search_text:
            return
        case_sensitive = (self.case_var.get() == 1)
        self.search_results = []  # Clear previous results
        for item in self.tree.get_children(""):
            self._search_node(item, search_text, case_sensitive)
        # Rebuild the results frame
        for widget in self.result_frame.winfo_children():
            widget.destroy()
        bg = self.find_window.cget("bg")
        self.result_frame.config(bg=bg)
        if not self.search_results:
            messagebox.showinfo("Search", f'No results for "{search_text}"', parent=self.find_window)
            return
        opts = self.get_style_options()
        self.result_label = tk.Label(self.result_frame, text=f"Results: {len(self.search_results)}", font=("Segoe UI", 10), **opts)
        self.result_label.pack(pady=2)
        self.result_listbox = tk.Listbox(self.result_frame, height=10, font=("Segoe UI", 10))
        self.result_listbox.pack(side="left", fill="both", expand=True)
        self.result_scrollbar = ttk.Scrollbar(self.result_frame, orient="vertical", command=self.result_listbox.yview)
        self.result_scrollbar.pack(side="right", fill="y")
        self.result_listbox.configure(yscrollcommand=self.result_scrollbar.set)
        for item in self.search_results:
            key_text = self.tree.item(item, "text")
            value = str(self.tree.set(item, "value"))
            self.result_listbox.insert("end", f"{key_text}: {value}")
        self.result_listbox.bind("<Double-1>", self._jump_to_selected)
        self.current_result_index = 0
        self._highlight_result(self.current_result_index)

    def find_next(self):
        if not self.search_results:
            self.find_all()
        if self.search_results:
            self.current_result_index = (self.current_result_index + 1) % len(self.search_results)
            self._highlight_result(self.current_result_index)

    def find_previous(self):
        if not self.search_results:
            self.find_all()
        if self.search_results:
            self.current_result_index = (self.current_result_index - 1) % len(self.search_results)
            self._highlight_result(self.current_result_index)

    def _search_node(self, item, search_text, case_sensitive):
        value = str(self.tree.set(item, "value"))
        cmp_value = value if case_sensitive else value.lower()
        cmp_search = search_text if case_sensitive else search_text.lower()
        if cmp_search in cmp_value:
            self.search_results.append(item)
        for child in self.tree.get_children(item):
            self._search_node(child, search_text, case_sensitive)

    def _highlight_result(self, index):
        if self.search_results:
            item = self.search_results[index]
            self.tree.selection_set(item)
            self.tree.focus(item)
            self.tree.see(item)

    def _jump_to_selected(self, event):
        selected_indices = self.result_listbox.curselection()
        if selected_indices:
            idx = selected_indices[0]
            self._highlight_result(idx)

# ----------------- Main Application -----------------
class REasyEditorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("REasy Editor v0.0.3")
        self.filename = None
        self.handler = None
        self.search_results = []
        self.current_result_index = 0
        self.undo_stack = []
        self.redo_stack = []
        self.settings = load_settings()
        self.dark_mode = self.settings.get("dark_mode", False)
        self._update_global_dark_mode()

        self.style = ttk.Style()
        default_font = ("Segoe UI", 10)
        self.style.configure("Treeview", font=default_font, rowheight=22)
        self.style.configure("TLabel", font=default_font)
        self.style.configure("TButton", font=default_font)

        # Create a Notebook for multiple tabs
        self.notebook = CustomNotebook(root)
        self.notebook.pack(side="top", fill="both", expand=True)
        self.tabs = {}  # Mapping of tab frame to FileTab

        # Menubar
        menubar = tk.Menu(root)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open...", command=self.on_open, accelerator="Ctrl+O")
        filemenu.add_command(label="Save", command=self.on_save, accelerator="Ctrl+S")
        filemenu.add_command(label="Reload", command=self.reload_file, accelerator="Ctrl+R")
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=root.quit)
        menubar.add_cascade(label="File", menu=filemenu)

        self.editmenu = tk.Menu(menubar, tearoff=0)
        self.editmenu.add_command(label="Copy", accelerator="Ctrl+C", command=self.copy_to_clipboard)
        self.editmenu.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        self.editmenu.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        menubar.add_cascade(label="Edit", menu=self.editmenu)
        self.update_undo_redo_state()

        findmenu = tk.Menu(menubar, tearoff=0)
        findmenu.add_command(label="Find", command=self.open_find_dialog, accelerator="Ctrl+F")
        findmenu.add_command(label="Search Directory for GUID", command=self.search_directory_for_guid, accelerator="Ctrl+G")
        findmenu.add_command(label="Search Directory for Text", command=self.search_directory_for_text, accelerator="Ctrl+T")
        findmenu.add_command(label="Search Directory for Number", command=self.search_directory_for_number, accelerator="Ctrl+N")
        menubar.add_cascade(label="Find", menu=findmenu)

        viewmenu = tk.Menu(menubar, tearoff=0)
        viewmenu.add_command(label="Toggle Dark Mode", command=self.toggle_dark_mode, accelerator="Ctrl+D")
        menubar.add_cascade(label="View", menu=viewmenu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="GUID Converter", command=self.open_guid_converter)
        menubar.add_cascade(label="Tools", menu=tools_menu)

        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=helpmenu)

        root.config(menu=menubar)

        # Keyboard shortcuts
        root.bind("<Control-n>", lambda _: self.search_directory_for_number())
        root.bind("<Control-t>", lambda _: self.search_directory_for_text())
        root.bind("<Control-f>", lambda _: self.open_find_dialog())
        root.bind("<Control-g>", lambda _: self.search_directory_for_guid())
        root.bind("<Control-c>", lambda _: self.copy_to_clipboard())
        root.bind("<Control-z>", lambda _: self.undo())
        root.bind("<Control-y>", lambda _: self.redo())
        root.bind("<Control-d>", lambda _: self.toggle_dark_mode())
        root.bind("<Control-o>", lambda _: self.on_open())
        root.bind("<Control-s>", lambda _: self.on_save())
        root.bind("<Control-r>", lambda _: self.reload_file())

        self.find_window = None
        self.search_entry = None
        self.result_frame = None
        self.result_listbox = None
        self.result_scrollbar = None
        self.result_label = None
        self.case_var = tk.IntVar(value=0)

        self.set_dark_mode(self.dark_mode)
        self.bind_notebook()

    def _update_global_dark_mode(self):
        global GLOBAL_DARK_MODE
        GLOBAL_DARK_MODE = self.dark_mode

    def update_undo_redo_state(self):
        # Placeholder for undo/redo state management.
        pass

    def undo(self, *_):
        pass

    def redo(self, *_):
        pass

    # ----------------- Dialog for asking max file size -----------------
    def _ask_max_size_bytes(self):
        max_size_mb = simpledialog.askfloat(
            "Max File Size",
            "Enter maximum file size in MB (leave blank for no limit):",
            parent=self.root
        )
        return max_size_mb * 1024 * 1024 if max_size_mb is not None else None

    def open_find_dialog(self):
        active_tab = self.get_active_tab()
        if active_tab and hasattr(active_tab, "open_find_dialog"):
            active_tab.open_find_dialog()
        else:
            messagebox.showerror("Error", "No active tab for searching.", parent=self.root)

    def search_directory_for_number(self):
        dirpath = filedialog.askdirectory(title="Select Directory for Number Search")
        if not dirpath:
            return
        search_number = simpledialog.askinteger(
            "Number Search",
            "Enter number to search for (32-bit integer):",
            parent=self.root
        )
        if search_number is None:
            return
        max_size_bytes = self._ask_max_size_bytes()
        try:
            search_bytes = struct.pack("<I", search_number)
        except Exception as e:
            messagebox.showerror("Error", f"Could not convert number: {e}")
            return
        search_hex = search_bytes.hex().upper()
        result_label_text = f"Files containing number {search_number} (Hex: {search_hex}):"
        self._search_directory_common(dirpath, [search_bytes], "Number Search Progress", result_label_text, max_size_bytes)

    def search_directory_for_text(self):
        dirpath = filedialog.askdirectory(title="Select Directory for Text Search")
        if not dirpath:
            return
        search_text = simpledialog.askstring("Text Search",
                                             "Enter text to search (will be converted to UTF-16LE):",
                                             parent=self.root)
        if not search_text:
            return
        max_size_bytes = self._ask_max_size_bytes()
        pattern1 = search_text.encode("utf-16le")
        pattern2 = pattern1 + b"\x00\x00"
        result_label_text = f"Files containing text '{search_text}':"
        self._search_directory_common(dirpath, [pattern1, pattern2],
                                      "Text Search Progress", result_label_text, max_size_bytes)

    def search_directory_for_guid(self):
        dirpath = filedialog.askdirectory(title="Select Directory for GUID Search")
        if not dirpath:
            return
        guid_str = simpledialog.askstring("GUID Search", "Enter GUID (standard format):", parent=self.root)
        if not guid_str:
            return
        try:
            guid_obj = uuid.UUID(guid_str.strip())
        except Exception as e:
            messagebox.showerror("Error", f"Invalid GUID: {guid_str}\n{e}")
            return
        max_size_bytes = self._ask_max_size_bytes()
        hex_guid = guid_obj.bytes_le.hex()
        search_patterns = [guid_obj.bytes_le, guid_obj.bytes, hex_guid.encode("utf-8")]
        result_label_text = f"Files containing GUID {guid_str}:"
        self._search_directory_common(dirpath, search_patterns,
                                      "GUID Search Progress", result_label_text, max_size_bytes)

    def _search_directory_common(self, dirpath, search_patterns, progress_title, result_label_text, max_size_bytes):
        files_list = []
        for root_dir, _, files in os.walk(dirpath):
            for fname in files:
                files_list.append(os.path.join(root_dir, fname))
        total_files = len(files_list)
        if total_files == 0:
            messagebox.showinfo("Search", "No files found in the selected directory.")
            return
        progress_win, _bg = self.create_dialog(progress_title, "400x150")
        progress_win.title(progress_title)
        progress_frame = ttk.Frame(progress_win)
        progress_frame.pack(pady=10, padx=10, fill="x")
        ttk.Label(progress_frame, text="Searching files...").pack(anchor="w")
        progress_var = tk.DoubleVar(value=0)
        progress_bar = ttk.Progressbar(progress_frame, variable=progress_var, maximum=total_files, length=300)
        progress_bar.pack(pady=5)
        progress_label = ttk.Label(progress_frame, text=f"0 / {total_files}")
        progress_label.pack(anchor="w")
        results_frame = ttk.Frame(progress_win)
        results_frame.pack(fill="both", expand=True, padx=10, pady=5)
        ttk.Label(results_frame, text=result_label_text).pack(anchor="w")
        listbox = tk.Listbox(results_frame, font=("Segoe UI", 10), width=80, height=10)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(results_frame, orient="vertical", command=listbox.yview)
        scrollbar.pack(side="right", fill="y")
        listbox.config(yscrollcommand=scrollbar.set)
        update_queue = queue.Queue()
        done_flag = [False]

        def process_file(fpath):
            if max_size_bytes is not None:
                try:
                    if os.path.getsize(fpath) > max_size_bytes:
                        return None
                except Exception:
                    return None
            try:
                with open(fpath, "rb") as in_file:
                    data = in_file.read()
                for pattern in search_patterns:
                    if pattern in data:
                        return fpath
            except Exception:
                return None
            return None

        def worker():
            count = 0
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future_to_file = {
                    executor.submit(process_file, fpath): fpath for fpath in files_list
                }
                for future in concurrent.futures.as_completed(future_to_file):
                    count += 1
                    update_queue.put(("progress", count, total_files))
                    try:
                        result = future.result()
                        if result is not None:
                            update_queue.put(("result", result))
                    except Exception:
                        pass
            update_queue.put(("done",))

        threading.Thread(target=worker, daemon=True).start()

        def poll_queue():
            try:
                while True:
                    msg = update_queue.get_nowait()
                    if msg[0] == "progress":
                        processed, total = msg[1], msg[2]
                        progress_var.set(processed)
                        progress_label.config(text=f"{processed} / {total}")
                    elif msg[0] == "result":
                        listbox.insert("end", msg[1])
                    elif msg[0] == "done":
                        done_flag[0] = True
            except queue.Empty:
                pass
            if not done_flag[0]:
                self.root.after(100, poll_queue)
            else:
                progress_label.config(text=f"Done: {total_files} files processed")
        poll_queue()

    def create_dialog(self, title, geometry=None):
        win = tk.Toplevel(self.root)
        win.title(title)
        if geometry:
            win.geometry(geometry)
        bg_color = win.cget("bg")
        return win, bg_color

    # ----------------- Dark Mode -----------------
    def set_dark_mode(self, state):
        self.dark_mode = state
        if state:
            self.root.configure(bg="#2b2b2b")
            self.style.theme_use("clam")
            self.style.configure(
                "Treeview",
                background="#2b2b2b",
                foreground="white",
                fieldbackground="#2b2b2b"
            )
            self.style.map("Treeview", background=[("selected", "#4a6984")])
        else:
            self.root.configure(bg="SystemButtonFace")
            self.style.theme_use("default")
            self.style.configure(
                "Treeview",
                background="white",
                foreground="black",
                fieldbackground="white"
            )
        self._update_global_dark_mode()

    def toggle_dark_mode(self):
        self.set_dark_mode(not self.dark_mode)
        self.settings["dark_mode"] = self.dark_mode
        save_settings(self.settings)

    def open_guid_converter(self):
        win, bg = self.create_dialog("GUID Converter", "400x200")
        tk.Label(win, text="GUID Memory (in memory order)", font=("Segoe UI", 10), bg=bg, fg=("white" if self.dark_mode else "black")).pack(pady=5)
        mem_entry = tk.Entry(win, width=40, font=("Segoe UI", 10))
        mem_entry.pack(pady=2)
        tk.Label(win, text="GUID Standard (hyphenated)", font=("Segoe UI", 10), bg=bg, fg=("white" if self.dark_mode else "black")).pack(pady=5)
        std_entry = tk.Entry(win, width=40, font=("Segoe UI", 10))
        std_entry.pack(pady=2)
        def mem_to_std():
            mem_str = mem_entry.get().strip()
            mem_str_clean = mem_str.replace("-", "").replace("{", "").replace("}", "").replace(" ", "")
            try:
                if len(mem_str_clean) != 32:
                    raise ValueError("After removing formatting, input must be 32 hex digits.")
                mem_bytes = bytes.fromhex(mem_str_clean)
                guid_obj = uuid.UUID(bytes_le=mem_bytes)
                std_entry.delete(0, tk.END)
                std_entry.insert(0, str(guid_obj))
            except Exception as e:
                messagebox.showerror("Error", f"Conversion error: {e}")
        def std_to_mem():
            guid_str = std_entry.get().strip()
            try:
                guid_obj = uuid.UUID(guid_str)
                mem_bytes = guid_obj.bytes_le
                hex_mem = mem_bytes.hex()
                mem_guid_str = f"{hex_mem[0:8]}-{hex_mem[8:12]}-{hex_mem[12:16]}-{hex_mem[16:20]}-{hex_mem[20:32]}"
                std_entry.delete(0, tk.END)
                std_entry.insert(0, mem_guid_str)
            except Exception as e:
                messagebox.showerror("Error", f"Conversion error: {e}")
        btn_frame = tk.Frame(win, bg=bg)
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="Memory -> Standard", command=mem_to_std, font=("Segoe UI", 10)).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Standard -> Memory", command=std_to_mem, font=("Segoe UI", 10)).pack(side="left", padx=5)

    def show_about(self):
        win, bg = self.create_dialog("About REasy Editor", "450x300")
        tk.Label(win, text="REasy Editor v0.0.3", font=("Segoe UI", 16, "bold"), bg=bg, fg=("white" if self.dark_mode else "black")).pack(pady=(0, 10))
        info_text = (
            "REasy Editor is a quality of life toolkit for modders.\n\n"
            "It supports viewing and full editing of UVAR files.\n\n"
            "For more information and updates, visit my GitHub page:"
        )
        tk.Label(win, text=info_text, font=("Segoe UI", 12), bg=bg, fg=("white" if self.dark_mode else "black"),
                 justify="left", wraplength=380).pack(pady=(0, 10))
        link_label = tk.Label(win, text="http://github.com/seifhassine",
                              font=("Segoe UI", 12, "underline"),
                              bg=bg, fg="blue", cursor="hand2")
        link_label.pack()
        def open_link(_event):
            url = "http://github.com/seifhassine"
            try:
                os.startfile(url)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to open link: {e}")
        link_label.bind("<Button-1>", open_link)
        win.grab_set()
        self.root.wait_window(win)

    # ----------------- Tab Management Methods -----------------
    def add_tab(self, filename=None, data=None):
        new_tab = FileTab(self.notebook, filename, data)
        tab_label = os.path.basename(filename) if filename else "Untitled"
        self.notebook.add(new_tab.frame, text=tab_label)
        self.tabs[new_tab.frame] = new_tab
        self.notebook.select(new_tab.frame)

    def get_active_tab(self):
        current_frame = self.notebook.nametowidget(self.notebook.select())
        return self.tabs.get(current_frame, None)

    def on_open(self):
        filename = filedialog.askopenfilename(
            title="Open File",
            filetypes=[("UVAR Files", ["*.uvar", "*.uvar.3"]), ("All Files", "*.*")]
        )
        if not filename:
            return
        with open(filename, "rb") as opened_file:
            data = opened_file.read()
        self.add_tab(filename, data)

    def on_save(self):
        active_tab = self.get_active_tab()
        if active_tab:
            active_tab.on_save()
        else:
            messagebox.showerror("Error", "No active tab to save.", parent=self.root)

    def reload_file(self):
        active_tab = self.get_active_tab()
        if active_tab:
            active_tab.reload_file()
        else:
            messagebox.showerror("Error", "No active tab to reload.", parent=self.root)

    def on_notebook_click(self, event):
        x, y = event.x, event.y
        element = self.notebook.identify(x, y)
        if "image" in element:
            index = self.notebook.index("@%d,%d" % (x, y))
            self.close_tab(index)
            return "break"

    def close_tab(self, index):
        tab_id = self.notebook.tabs()[index]
        if tab_id in self.tabs:
            del self.tabs[tab_id]
        self.notebook.forget(index)

    def bind_notebook(self):
        self.notebook.bind("<Button-1>", self.on_notebook_click, True)

    # Delegate copy_to_clipboard to the active tab
    def copy_to_clipboard(self, event=None):
        active_tab = self.get_active_tab()
        if active_tab:
            active_tab.copy_to_clipboard()
        else:
            messagebox.showerror("Error", "No active tab.", parent=self.root)

# ----------------- Entry Point -----------------
def main():
    root = tk.Tk()
    root.title("REasy Editor – RE Engine Scripting Toolkit")
    set_app_icon(root)
    root.geometry("800x600")
    app = REasyEditorApp(root)
    if len(sys.argv) > 1:
        with open(sys.argv[1], "rb") as f:
            data = f.read()
        app.add_tab(sys.argv[1], data)
    root.mainloop()

if __name__ == "__main__":
    main()
