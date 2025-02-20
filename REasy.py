#!/usr/bin/env python3
from tkinter.scrolledtext import ScrolledText
import os, sys, json, struct, uuid, tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import queue, threading, concurrent.futures, weakref, mmap
from PIL import Image, ImageTk
from file_handlers.factory import get_handler_for_data
from settings import load_settings, save_settings
import sys
from ui.console_logger import StdoutRedirector, setup_console_logging, ConsoleRedirector
import tkinter.dnd as dnd 
from tkinterdnd2 import TkinterDnD 


# Cache resource paths
_RESOURCE_CACHE = {}


class AppToplevel(tk.Toplevel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        set_app_icon(self)
        self.apply_theme()

    def apply_theme(self):
        dark = getattr(self.master, "dark_mode", False)
        self.configure(bg=("#2b2b2b" if dark else "white"))


def create_standard_dialog(root, title, geometry=None):
    win = tk.Toplevel(root)
    win.title(title)
    if geometry:
        win.geometry(geometry)
    dark = getattr(root, "dark_mode", False)
    bg = root.cget("bg")
    win.configure(bg=bg)
    return win, bg


def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        # PyInstaller creates atemp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    
    full_path = os.path.join(base_path, relative_path)
    if not os.path.exists(full_path):
        # Fallback to checking relative to working directory
        full_path = os.path.join(os.getcwd(), relative_path)
        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Could not find resource: {relative_path}")
            
    return full_path


def set_app_icon(window):
    try:
        icon_path = resource_path("resources/icons/reasy_editor_logo.ico")
        img = Image.open(icon_path)
        photo = ImageTk.PhotoImage(img)
        window.iconphoto(True, photo)
        window._icon = photo
    except IOError as e:
        print("Failed to set window icon:", e)


tk.Toplevel = AppToplevel


class CustomNotebook(ttk.Notebook):
    def __init__(self, *args, **kwargs):
        ttk.Style().theme_use("clam")
        super().__init__(*args, **kwargs)
        self._active = None
        
        self.dark_mode = False
        style = ttk.Style()
        style.configure('Custom.TNotebook', background='white')
        self.configure(style='Custom.TNotebook')
        
        try:
            close_path = resource_path("resources/icons/close.png")
            self.close_img = tk.PhotoImage(file=close_path)
            self._create_custom_style()
        except Exception as e:
            print(f"Warning: Failed to load close button image: {e}")
            self.close_img = None
            
        self.bind("<ButtonPress-1>", self._on_close_press, True)
        self.bind("<ButtonRelease-1>", self._on_close_release, True)

    def set_dark_mode(self, is_dark):
        """Update notebook background color based on dark mode setting"""
        self.dark_mode = is_dark
        style = ttk.Style()
        bg_color = "#2b2b2b" if is_dark else "white"
        style.configure('Custom.TNotebook', background=bg_color)

    def _create_custom_style(self):
        style = ttk.Style()
        self.close_img = tk.PhotoImage(file=resource_path("resources/icons/close.png"))
        try:
            style.element_create(
                "Custom.Close", "image", self.close_img, border=4, sticky=""
            )
        except Exception as e:
            print("Error creating Custom.Close element:", e)
        style.layout(
            "Custom.TNotebook.Tab",
            [
                (
                    "Custom.TNotebook.tab",
                    {
                        "sticky": "nswe",
                        "children": [
                            (
                                "Custom.TNotebook.padding",
                                {
                                    "side": "top",
                                    "sticky": "nswe",
                                    "children": [
                                        (
                                            "Custom.TNotebook.focus",
                                            {
                                                "side": "top",
                                                "sticky": "nswe",
                                                "children": [
                                                    (
                                                        "Custom.TNotebook.label",
                                                        {"side": "left", "sticky": ""},
                                                    ),
                                                    (
                                                        "Custom.Close",
                                                        {"side": "right", "sticky": ""},
                                                    ),
                                                ],
                                            },
                                        )
                                    ],
                                },
                            )
                        ],
                    },
                )
            ],
        )
        self.configure(style="Custom.TNotebook")

    def _on_close_press(self, event):
        if "Custom.Close" in self.identify(event.x, event.y):
            self._active = self.index("@%d,%d" % (event.x, event.y))
            self.state(["pressed"])

    def _on_close_release(self, event):
        if not self.instate(["pressed"]):
            return
        if "Custom.Close" in self.identify(event.x, event.y):
            index = self.index("@%d,%d" % (event.x, event.y))
            if self.app_instance:
                self.app_instance.close_tab(index)
            else:
                self.forget(index)
        self.state(["!pressed"])
        self._active = None


class FileTab:
    def __init__(self, parent_notebook, filename=None, data=None, app=None):
        self.parent_notebook = parent_notebook
        self.frame = ttk.Frame(parent_notebook)
        self.filename = filename
        self.handler = None
        self.metadata_map = {}
        self.search_results = []
        self.current_result_index = 0
        self.modified = False
        self.app = app
        self.status_var = tk.StringVar(value="No file loaded")
        self.status_bar = ttk.Label(
            self.frame, textvariable=self.status_var, anchor="w", padding=5
        )
        self.status_bar.pack(side="bottom", fill="x")
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
        self.find_window = None
        self.search_entry = None
        self.result_frame = None
        self.result_listbox = None
        self.result_scrollbar = None
        self.result_label = None
        self.case_var = tk.IntVar(value=0)
        self.dark_mode = self.app.dark_mode if self.app else False
        if data:
            self.load_file(filename, data)
                
    def load_file(self, filename, data):
        self.filename = filename
        self.handler = get_handler_for_data(data)
        self.handler.app = self.app
        self.handler.refresh_tree_callback = self.refresh_tree
        self.handler.read(data)
        self.refresh_tree()
        self.status_var.set(
            f"Loaded{' (Read Only)' if not self.handler.supports_editing() else ''}: {filename}"
        )
        
    def update_tab_title(self):
        tab_title = os.path.basename(self.filename) if self.filename else "Untitled"
        if self.modified:
            tab_title += " *"
        self.parent_notebook.tab(self.frame, text=tab_title)
        if self.app:
            self.app.root.title(f"REasy Editor - {tab_title}")

    def refresh_tree(self):
        if self.handler:
            self.handler.update_strings()
        for child in self.tree.get_children():
            self.tree.delete(child)
        self.metadata_map.clear()
        if self.handler:
            self.handler.populate_treeview(self.tree, "", self.metadata_map)
        self.frame.update_idletasks()

    def show_context_menu(self, event):
        clicked = self.tree.identify_row(event.y)
        if not clicked:
            return
        meta = self.metadata_map.get(clicked)
        if self.handler:
            menu = self.handler.get_context_menu(self.tree, clicked, meta)
            if menu:
                menu.post(event.x_root, event.y_root)

    def on_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row or self.tree.identify_column(event.x) != "#1":
            return
        old_val = self.tree.set(row, "value")
        meta = self.metadata_map.get(row, {})
        bbox = self.tree.bbox(row, "#1")
        if not bbox:
            return
        x0, y0, width, height = bbox
        entry = tk.Entry(self.tree, font=("Segoe UI", 10))
        entry.place(x=x0, y=y0, width=width, height=height)
        entry.insert(0, str(old_val))
        entry.focus()

        def commit(_event=None):
            new_val = entry.get()
            entry.destroy()
            if new_val != old_val and self.handler:
                state = self.handler.save_tree_state(self.tree)
                self.handler.handle_edit(meta, new_val, old_val, row)
                self.refresh_tree()
                self.handler.restore_tree_state(self.tree, state)
                self.modified = True
                self.update_tab_title()

        entry.bind("<FocusOut>", commit)
        entry.bind("<Return>", commit)

    def save_tree_state(self):
        active = self.get_active_tab()
        if active:
            return active.handler.save_tree_state(active.tree)
        return {}

    def restore_tree_state(self, state):
        active = self.get_active_tab()
        if active:
            active.handler.restore_tree_state(active.tree, state)

    def on_save(self):
        if not self.handler:
            messagebox.showerror("Error", "No file loaded")
            return
        if not self.handler.supports_editing():
            messagebox.showinfo(
                "Read Only", "Editing not supported for this file type."
            )
            return
        try:
            data = self.handler.rebuild()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to rebuild file: {e}")
            return
        fname = filedialog.asksaveasfilename(
            title="Save File As...",
            defaultextension=(os.path.splitext(self.filename)[1] if self.filename else ""),
            filetypes=[("All Files", "*.*")],
        )
        if not fname:
            return
        try:
            with open(fname, "wb") as f:
                f.write(data)
        except IOError as e:
            messagebox.showerror("Error", f"Failed to save file: {e}")
            return
        messagebox.showinfo("Saved", f"File saved to {fname}")
        self.status_var.set(f"Saved: {fname}")
        self.modified = False
        self.update_tab_title()

    def reload_file(self):
        if self.modified:
            ans = messagebox.askyesnocancel(
                "Unsaved Changes",
                f"File {os.path.basename(self.filename)} has unsaved changes.\nSave before reloading?",
                parent=self.app.root,
            )
            if ans is None:
                return
            if ans:
                self.on_save()
        if not self.filename:
            messagebox.showerror("Error", "No file currently loaded.")
            return
        try:
            with open(self.filename, "rb") as f:
                data = f.read()
            self.handler = get_handler_for_data(data)
            self.handler.app = self.app
            self.handler.refresh_tree_callback = self.refresh_tree
            self.handler.read(data)
            self.refresh_tree()
            self.status_var.set(f"Reloaded: {self.filename}")
            self.modified = False
            self.update_tab_title()
        except IOError as e:
            messagebox.showerror("Error", f"Failed to reload file: {e}")

    def copy_to_clipboard(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        value = self.tree.set(sel[0], "value")
        if value:
            self.tree.winfo_toplevel().clipboard_clear()
            self.tree.winfo_toplevel().clipboard_append(value)
            messagebox.showinfo("Copied", f"Copied: {value}")

    def open_find_dialog(self):
        if self.find_window is not None and tk.Toplevel.winfo_exists(self.find_window):
            self.find_window.lift()
            return
        parent = self.parent_notebook.winfo_toplevel()
        self.find_window, bg = create_standard_dialog(self.app.root, "Find in Tree", "350x200")
        opts = self.get_style_options()
        tk.Label(
            self.find_window, text="Find:", font=("Segoe UI", 10), bg=bg, fg=opts["fg"]
        ).pack(pady=5)
        self.search_entry = tk.Entry(
            self.find_window,
            width=30,
            font=("Segoe UI", 10),
            bg=bg,
            fg=opts["fg"],
            insertbackground=opts["fg"],
        )
        self.search_entry.pack(pady=5)
        tk.Checkbutton(
            self.find_window,
            text="Case Sensitive",
            variable=self.case_var,
            onvalue=1,
            offvalue=0,
            font=("Segoe UI", 10),
            bg=bg,
            fg=opts["fg"],
            selectcolor=("gray" if self.app.dark_mode else "lightgray"),
        ).pack(pady=2)
        button_frame = tk.Frame(self.find_window, bg=bg)
        button_frame.pack(pady=10)
        tk.Button(
            button_frame,
            text="Find Previous",
            command=self.find_previous,
            font=("Segoe UI", 10),
            bg=bg,
            fg=opts["fg"],
        ).pack(side="left", padx=5)
        tk.Button(
            button_frame,
            text="Find Next",
            command=self.find_next,
            font=("Segoe UI", 10),
            bg=bg,
            fg=opts["fg"],
        ).pack(side="left", padx=5)
        tk.Button(
            button_frame,
            text="Find All",
            command=self.find_all,
            font=("Segoe UI", 10),
            bg=bg,
            fg=opts["fg"],
        ).pack(side="left", padx=5)
        self.result_frame = tk.Frame(self.find_window, bg=bg)
        self.result_frame.pack(fill="both", expand=True)

    def find_all(self):
        if not self.search_entry:
            messagebox.showerror("Error", "Find dialog not open.", parent=self.find_window)
            return
        search_text = self.search_entry.get().strip()
        if not search_text:
            return
        case_sensitive = self.case_var.get() == 1
        self.search_results = []
        cmp_search = search_text if case_sensitive else search_text.lower()
        stack = list(self.tree.get_children(""))
        while stack:
            item = stack.pop()
            val = str(self.tree.set(item, "value"))
            cmp_val = val if case_sensitive else val.lower()
            if cmp_search in cmp_val:
                self.search_results.append(item)
            stack.extend(self.tree.get_children(item))
        for widget in self.result_frame.winfo_children():
            widget.destroy()
        bg = self.find_window.cget("bg")
        self.result_frame.config(bg=bg)
        if not self.search_results:
            messagebox.showinfo("Search", f'No results for "{search_text}"', parent=self.find_window)
            return
        opts = self.get_style_options()
        tk.Label(
            self.result_frame,
            text=f"Results: {len(self.search_results)}",
            font=("Segoe UI", 10),
            bg=bg,
            fg=opts["fg"],
        ).pack(pady=2)
        self.result_listbox = tk.Listbox(
            self.result_frame,
            height=10,
            font=("Segoe UI", 10),
            bg=("#2b2b2b" if self.app.dark_mode else "white"),
            fg=("white" if self.app.dark_mode else "black"),
        )
        self.result_listbox.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(self.result_frame, orient="vertical", command=self.result_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.result_listbox.configure(yscrollcommand=scrollbar.set)
        for item in self.search_results:
            self.result_listbox.insert("end", f"{self.tree.item(item, 'text')}: {self.tree.set(item, 'value')}")
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

    def _highlight_result(self, index):
        if self.search_results:
            item = self.search_results[index]
            self.tree.selection_set(item)
            self.tree.focus(item)
            self.tree.see(item)

    def _jump_to_selected(self, event):
        sel = self.result_listbox.curselection()
        if sel:
            self._highlight_result(sel[0])

    def get_style_options(self):
        dark = self.app.dark_mode if self.app else False
        return {"fg": "white", "bg": "#2b2b2b"} if dark else {"fg": "black", "bg": "white"}


class REasyEditorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("REasy Editor v0.0.5")
        self.root.protocol("WM_DELETE_WINDOW", self.on_quit)
        self.settings = load_settings()
        self.dark_mode = self.settings.get("dark_mode", False)
        self.root.dark_mode = self.dark_mode
        self.style = ttk.Style()
        default_font = ("Segoe UI", 10)
        self.style.configure("Treeview", font=default_font, rowheight=22)
        self.style.configure("TLabel", font=default_font)
        self.style.configure("TButton", font=default_font)
        self.notebook = CustomNotebook(root)
        self.notebook.pack(side="top", fill="both", expand=True)
        self.notebook.app_instance = self
        self.notebook.set_dark_mode(self.dark_mode) 
        self.tabs = weakref.WeakValueDictionary()

        def new_on_close_release(event):
            widget = event.widget
            x, y = event.x, event.y
            if "Custom.Close" in widget.identify(x, y):
                self.close_tab(widget.index("@%d,%d" % (x, y)))
            widget.state(["!pressed"])
            widget._active = None

        self.notebook._on_close_release = new_on_close_release
        menubar = tk.Menu(root)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open...", command=self.on_open, accelerator="Ctrl+O")
        filemenu.add_command(label="Save", command=self.on_save, accelerator="Ctrl+S")
        filemenu.add_command(label="Reload", command=self.reload_file, accelerator="Ctrl+R")
        filemenu.add_separator()
        filemenu.add_command(label="Settings", command=self.open_settings_dialog)
        filemenu.add_command(label="Exit", command=root.quit)
        menubar.add_cascade(label="File", menu=filemenu)
        self.editmenu = tk.Menu(menubar, tearoff=0)
        self.editmenu.add_command(label="Copy", accelerator="Ctrl+C", command=self.copy_to_clipboard)
        menubar.add_cascade(label="Edit", menu=self.editmenu)
        findmenu = tk.Menu(menubar, tearoff=0)
        findmenu.add_command(label="Find", command=self.open_find_dialog, accelerator="Ctrl+F")
        findmenu.add_command(label="Search Directory for GUID", command=self.search_directory_for_guid, accelerator="Ctrl+G")
        findmenu.add_command(label="Search Directory for Text", command=self.search_directory_for_text, accelerator="Ctrl+T")
        findmenu.add_command(label="Search Directory for Number", command=self.search_directory_for_number, accelerator="Ctrl+N")
        menubar.add_cascade(label="Find", menu=findmenu)
        viewmenu = tk.Menu(menubar, tearoff=0)
        viewmenu.add_command(label="Toggle Dark Mode", command=self.toggle_dark_mode, accelerator="Ctrl+D")
        viewmenu.add_command(label="Toggle Debug Console", command=lambda: self.toggle_debug_console(not self.settings.get("show_debug_console", True)), accelerator="Ctrl+Shift+D")
        menubar.add_cascade(label="View", menu=viewmenu)
        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="GUID Converter", command=self.open_guid_converter)
        menubar.add_cascade(label="Tools", menu=tools_menu)
        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=helpmenu)
        root.config(menu=menubar)
        for key, cmd in {
            "<Control-n>": self.search_directory_for_number,
            "<Control-t>": self.search_directory_for_text,
            "<Control-f>": self.open_find_dialog,
            "<Control-g>": self.search_directory_for_guid,
            "<Control-c>": self.copy_to_clipboard,
            "<Control-d>": self.toggle_dark_mode,
            "<Control-o>": self.on_open,
            "<Control-s>": self.on_save,
            "<Control-r>": self.reload_file,
        }.items():
            root.bind(key, lambda _e, cmd=cmd: cmd())
        self.find_window = None
        self.case_var = tk.IntVar(value=0)
        self.apply_theme()
        self.bind_notebook()
        self.console_frame = tk.Frame(self.root, bg="#333333")
        self.console_frame.pack(side="bottom", fill="both")
        self.console = ScrolledText(
            self.console_frame,
            height=8,
            bg="#000000",
            fg="#00FF00",
            font=("Courier New", 10),
            state="disabled",
            wrap="none",
        )

        if self.settings.get("show_debug_console", True):
            setup_console_logging(self.console)
            sys.stdout = ConsoleRedirector(self.console, sys.stdout)
            sys.stderr = ConsoleRedirector(self.console, sys.stderr)
            sys.stdout = StdoutRedirector(self.console)
            self.console.pack(fill="both", expand=True)
            setup_console_logging(self.console)
            print("Debug console started.")
        else:
            self.console_frame = None
            self.console_text = None

        self.setup_drag_and_drop()

    def setup_drag_and_drop(self):
        self.root.drop_target_register('DND_Files')
        self.root.dnd_bind('<<Drop>>', self.on_drop)

    def on_drop(self, event):
        files = self.root.tk.splitlist(event.data)
        for f in files:
            f = f.strip('{}')
            if os.path.isfile(f):
                try:
                    with open(f, "rb") as file_obj:
                        data = file_obj.read()
                    self.add_tab(f, data)
                except Exception as e:
                    messagebox.showerror("Drop Error", f"Could not open {f}\n{e}")

    def handle_missing_json(self):
        messagebox.showwarning(
            "Missing JSON",
            "A valid JSON file is required for processing RCOL files.\nPlease select a JSON file.",
            parent=self.root,
        )
        json_path = filedialog.askopenfilename(
            title="Select JSON file",
            filetypes=[("JSON files", "*.json")],
            parent=self.root,
        )
        if not json_path or not os.path.exists(json_path):
            messagebox.showerror(
                "Invalid JSON",
                "A valid JSON file must be selected to process RCOL files.",
                parent=self.root,
            )
            return None
        return json_path

    def apply_theme(self):
        self.root.configure(bg=("#2b2b2b" if self.dark_mode else "white"))
        if self.dark_mode:
            self.style.theme_use("clam")
            self.style.configure(
                "Treeview",
                background="#2b2b2b",
                foreground="white",
                fieldbackground="#2b2b2b",
            )
            self.style.map("Treeview", background=[("selected", "#4a6984")])
        else:
            self.style.theme_use("default")
            self.style.configure(
                "Treeview",
                background="white",
                foreground="black",
                fieldbackground="white",
            )

    def on_quit(self):
        for tab in list(self.tabs.values()):
            if tab.modified:
                ans = messagebox.askyesnocancel(
                    "Unsaved Changes",
                    f"File {os.path.basename(tab.filename)} has unsaved changes.\nQuit without saving?",
                    parent=self.root,
                )
                if ans is None:
                    return
                if not ans:
                    tab.on_save()
                else:
                    tab.modified = False
                    tab.update_tab_title()
        self.root.destroy()

    def _ask_max_size_bytes(self):
        ms = simpledialog.askfloat(
            "Max File Size",
            "Enter maximum file size in MB (leave blank for no limit):",
            parent=self.root,
        )
        return ms * 1024 * 1024 if ms is not None else None

    def open_find_dialog(self):
        active = self.get_active_tab()
        if active and hasattr(active, "open_find_dialog"):
            active.open_find_dialog()
        else:
            messagebox.showerror("Error", "No active tab for searching.", parent=self.root)

    def open_settings_dialog(self):
        win, bg = create_standard_dialog(self.root, "Settings", "400x250")
        tk.Label(
            win,
            text="RCOL JSON Path:",
            font=("Segoe UI", 10),
            bg=bg,
            fg=("white" if self.dark_mode else "black"),
        ).pack(pady=5)
        json_var = tk.StringVar(value=self.settings.get("rcol_json_path", ""))
        json_entry = tk.Entry(win, textvariable=json_var, width=40, font=("Segoe UI", 10))
        json_entry.pack(pady=5)
        tk.Button(
            win,
            text="Browse...",
            font=("Segoe UI", 10),
            command=lambda: json_var.set(
                filedialog.askopenfilename(
                    title="Select JSON file",
                    filetypes=[("JSON Files", "*.json")],
                    parent=win,
                )
            ),
        ).pack(pady=5)
        dark_var = tk.BooleanVar(value=self.dark_mode)
        tk.Checkbutton(
            win,
            text="Dark Mode",
            variable=dark_var,
            font=("Segoe UI", 10),
            bg=bg,
            fg=("white" if self.dark_mode else "black"),
            selectcolor=("gray" if self.dark_mode else "lightgray"),
        ).pack(pady=5)
        debug_var = tk.BooleanVar(value=self.settings.get("debug_console"))
        tk.Checkbutton(
            win,
            text="Show Debug Console",
            variable=debug_var,
            font=("Segoe UI", 10),
            bg=bg,
            fg=("white" if self.dark_mode else "black"),
            selectcolor=("gray" if self.dark_mode else "lightgray"),
        ).pack(pady=5)
        button_frame = tk.Frame(win, bg=bg)
        button_frame.pack(pady=10)

        def on_ok():
            new_json_path = json_var.get().strip()
            if not new_json_path or not os.path.exists(new_json_path):
                messagebox.showerror("Error", "Please select a valid JSON file.", parent=win)
                return
            self.settings["rcol_json_path"] = new_json_path
            self.dark_mode = dark_var.get()
            self.settings["dark_mode"] = self.dark_mode
            self.settings["debug_console"] = debug_var.get()
            save_settings(self.settings)
            self.set_dark_mode(self.dark_mode)
            if hasattr(self, "toggle_debug_console"):
                self.toggle_debug_console(debug_var.get())
            win.destroy()

        def on_cancel():
            win.destroy()

        tk.Button(button_frame, text="OK", font=("Segoe UI", 10), command=on_ok).pack(side="left", padx=5)
        tk.Button(button_frame, text="Cancel", font=("Segoe UI", 10), command=on_cancel).pack(side="left", padx=5)

    def search_directory_for_number(self):
        dpath = filedialog.askdirectory(title="Select Directory for Number Search")
        if not dpath:
            return
        snum = simpledialog.askinteger(
            "Number Search",
            "Enter number to search for (32-bit integer):",
            parent=self.root,
        )
        if snum is None:
            return
        max_bytes = self._ask_max_size_bytes()
        try:
            sbytes = struct.pack("<I", snum)
        except Exception as e:
            messagebox.showerror("Error", f"Could not convert number: {e}")
            return
        shex = sbytes.hex().upper()
        rtext = f"Files containing number {snum} (Hex: {shex}):"
        self._search_directory_common(dpath, [sbytes], "Number Search Progress", rtext, max_bytes)

    def search_directory_for_text(self):
        dpath = filedialog.askdirectory(title="Select Directory for Text Search")
        if not dpath:
            return
        stext = simpledialog.askstring("Text Search", "Enter text to search (UTF-16LE):", parent=self.root)
        if not stext:
            return
        max_bytes = self._ask_max_size_bytes()
        p1 = stext.encode("utf-16le")
        p2 = p1 + b"\x00\x00"
        rtext = f"Files containing text '{stext}':"
        self._search_directory_common(dpath, [p1, p2], "Text Search Progress", rtext, max_bytes)

    def search_directory_for_guid(self):
        dpath = filedialog.askdirectory(title="Select Directory for GUID Search")
        if not dpath:
            return
        gstr = simpledialog.askstring("GUID Search", "Enter GUID (standard format):", parent=self.root)
        if not gstr:
            return
        try:
            gobj = uuid.UUID(gstr.strip())
        except Exception as e:
            messagebox.showerror("Error", f"Invalid GUID: {gstr}\n{e}")
            return
        max_bytes = self._ask_max_size_bytes()
        hex_guid = gobj.bytes_le.hex()
        spats = [gobj.bytes_le, gobj.bytes, hex_guid.encode("utf-8")]
        rtext = f"Files containing GUID {gstr}:"
        self._search_directory_common(dpath, spats, "GUID Search Progress", rtext, max_bytes)

    def _search_directory_common(self, dpath, patterns, ptitle, rtext, max_bytes):
        if max_bytes is None:
            messagebox.showinfo("Search", "Search cancelled.", parent=self.root)
            return

        flist = [os.path.join(r, f) for r, _, fs in os.walk(dpath) for f in fs]
        total = len(flist)
        if total == 0:
            messagebox.showinfo("Search", "No files found.", parent=self.root)
            return

        pwin, bg = create_standard_dialog(self.root, ptitle, "400x250")
        cancel_event = threading.Event()
        pwin.cancel_search = cancel_event
        pwin.protocol("WM_DELETE_WINDOW", lambda: (cancel_event.set(), pwin.destroy()))
        pwin.update_idletasks()

        pframe = tk.Frame(pwin, bg=bg)
        pframe.pack(pady=10, padx=10, fill="x")
        status_label = tk.Label(pframe, text="Gathering files...", bg=bg, fg=("white" if self.dark_mode else "black"))
        status_label.pack(anchor="w")
        pvar = tk.DoubleVar(value=0)
        pbar = ttk.Progressbar(pframe, variable=pvar, maximum=total, length=300)
        pbar.pack(pady=5)
        plabel = tk.Label(pframe, text=f"0 / {total} files processed, 0 skipped", bg=bg, fg=("white" if self.dark_mode else "black"))
        plabel.pack(anchor="w")
        stop_button = tk.Button(pframe, text="Stop Search", command=lambda: cancel_event.set(), bg=bg, fg=("white" if self.dark_mode else "black"))
        stop_button.pack(anchor="e", pady=5)

        rframe = tk.Frame(pwin, bg=bg)
        rframe.pack(fill="both", expand=True, padx=10, pady=5)
        tk.Label(rframe, text=rtext, bg=bg, fg=("white" if self.dark_mode else "black")).pack(anchor="w")
        lbox = tk.Listbox(rframe, font=("Segoe UI", 10), width=80, height=10, bg=("#2b2b2b" if self.dark_mode else "white"),
                           fg=("white" if self.dark_mode else "black"), selectbackground=("#4a6984" if self.dark_mode else "SystemHighlight"))
        lbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(rframe, orient="vertical", command=lbox.yview)
        sb.pack(side="right", fill="y")
        lbox.config(yscrollcommand=sb.set)

        uq = queue.Queue()
        done_flag = [False]
        skipped_count = [0]

        def process_file(fp):
            if cancel_event.is_set():
                return None
            try:
                if max_bytes is not None and os.path.getsize(fp) > max_bytes:
                    skipped_count[0] += 1
                    return None
            except IOError:
                return None
            try:
                with open(fp, "rb") as f:
                    data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                    found = any(data.find(p) != -1 for p in patterns)
                    data.close()
                    if found:
                        return fp
            except IOError:
                return None
            return None

        def search_worker():
            count = 0
            cpu_count = os.cpu_count() or 4
            max_workers = max(1, int(cpu_count * 0.6))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = {ex.submit(process_file, fp): fp for fp in flist}
                for future in concurrent.futures.as_completed(futures):
                    if cancel_event.is_set():
                        uq.put(("cancelled", count, total))
                        return
                    count += 1
                    uq.put(("progress", count, total))
                    try:
                        res = future.result()
                        if res is not None:
                            uq.put(("result", res))
                    except Exception:
                        pass
                uq.put(("done", count, total))

        def start_search():
            threading.Thread(target=search_worker, daemon=True).start()

        self.root.after(0, lambda: status_label.config(text=f"{total} files gathered. Searching..."))
        start_search()

        def poll_queue():
            try:
                while True:
                    msg = uq.get_nowait()
                    if msg[0] == "progress":
                        pvar.set(msg[1])
                        if plabel.winfo_exists():
                            plabel.config(text=f"{msg[1]} / {msg[2]} files processed, {skipped_count[0]} skipped")
                    elif msg[0] == "result":
                        if lbox.winfo_exists():
                            lbox.insert("end", msg[1])
                    elif msg[0] == "done":
                        done_flag[0] = True
                        final_count = msg[1]
                        if plabel.winfo_exists():
                            plabel.config(text=f"Done: {final_count} / {msg[2]} files processed, {skipped_count[0]} skipped")
                    elif msg[0] == "cancelled":
                        done_flag[0] = True
                        final_count = msg[1]
                        if plabel.winfo_exists():
                            plabel.config(text=f"Cancelled: {final_count} / {msg[2]} files processed, {skipped_count[0]} skipped")
            except queue.Empty:
                pass
            if not pwin.winfo_exists():
                done_flag[0] = True
                return
            if not done_flag[0]:
                self.root.after(100, poll_queue)

        poll_queue()

    def set_dark_mode(self, state):
        self.dark_mode = state
        if state:
            self.root.configure(bg="#2b2b2b")
            self.style.theme_use("clam")
            self.style.configure(
                "Treeview",
                background="#2b2b2b",
                foreground="white",
                fieldbackground="#2b2b2b",
            )
            self.style.map("Treeview", background=[("selected", "#4a6984")])
        else:
            self.root.configure(bg="white")
            self.style.theme_use("default")
            self.style.configure(
                "Treeview",
                background="white",
                foreground="black",
                fieldbackground="white",
            )
        self.notebook.set_dark_mode(state) 
        self.apply_theme()

    def toggle_dark_mode(self):
        self.set_dark_mode(not self.dark_mode)
        self.settings["dark_mode"] = self.dark_mode
        save_settings(self.settings)

    def toggle_debug_console(self, show: bool):
        """Toggle debug console visibility"""
        if show:
            if not hasattr(self, "console_frame") or self.console_frame is None:
                self.console_frame = tk.Frame(self.root, bg=self.root.cget("bg"))
                self.console_frame.pack(side="bottom", fill="both", expand=False)
                self.console = ScrolledText( 
                    self.console_frame,
                    bg="black",
                    fg="lime",
                    font=("Consolas", 10),
                    height=8,
                    wrap="none",
                )
                self.console.pack(fill="both", expand=True)
                setup_console_logging(self.console)
                sys.stdout = StdoutRedirector(self.console)
                sys.stderr = ConsoleRedirector(self.console, sys.stderr)
        else:
            if hasattr(self, "console_frame") and self.console_frame is not None:
                self.console_frame.destroy()
                self.console_frame = None
                self.console = None
                sys.stdout = sys.__stdout__
                sys.stderr = sys.__stderr__
        
        self.settings["show_debug_console"] = show
        save_settings(self.settings)

    def open_guid_converter(self):
        win, bg = create_standard_dialog(self.root, "GUID Converter", "400x200")
        tk.Label(
            win,
            text="GUID Memory (in memory order)",
            font=("Segoe UI", 10),
            bg=bg,
            fg=("white" if self.dark_mode else "black"),
        ).pack(pady=5)
        mem_entry = tk.Entry(win, width=40, font=("Segoe UI", 10))
        mem_entry.pack(pady=2)
        tk.Label(
            win,
            text="GUID Standard (hyphenated)",
            font=("Segoe UI", 10),
            bg=bg,
            fg=("white" if self.dark_mode else "black"),
        ).pack(pady=5)
        std_entry = tk.Entry(win, width=40, font=("Segoe UI", 10))
        std_entry.pack(pady=2)

        def mem_to_std():
            ms = mem_entry.get().strip().replace("-", "").replace("{", "").replace("}", "").replace(" ", "")
            try:
                if len(ms) != 32:
                    raise ValueError("Must be 32 hex digits.")
                mb = bytes.fromhex(ms)
                std_entry.delete(0, tk.END)
                std_entry.insert(0, str(uuid.UUID(bytes_le=mb)))
            except Exception as e:
                messagebox.showerror("Error", f"Conversion error: {e}")

        def std_to_mem():
            try:
                g = uuid.UUID(std_entry.get().strip())
                hex_mem = g.bytes_le.hex()
                std_entry.delete(0, tk.END)
                std_entry.insert(0, f"{hex_mem[0:8]}-{hex_mem[8:12]}-{hex_mem[12:16]}-{hex_mem[16:20]}-{hex_mem[20:32]}")
            except Exception as e:
                messagebox.showerror("Error", f"Conversion error: {e}")

        btn_frame = tk.Frame(win, bg=bg)
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="Memory -> Standard", command=mem_to_std, font=("Segoe UI", 10)).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Standard -> Memory", command=std_to_mem, font=("Segoe UI", 10)).pack(side="left", padx=5)

    def show_about(self):
        win, bg = create_standard_dialog(self.root, "About REasy Editor", "450x250")
        win.resizable(False, False)
        tk.Label(
            win,
            text="REasy Editor v0.0.5",
            font=("Segoe UI", 16, "bold"),
            bg=bg,
            justify='center',
            fg=("white" if self.dark_mode else "black"),
        ).pack(pady=(0, 10))
        info = (
            "REasy Editor is a quality of life toolkit for modders.\n\n"
            "It supports viewing and full editing of UVAR files.\n\n"
            "Viewing of rcol.25 and scn fiels is also supported.\n\n"
            "For more information, visit my GitHub page:"
        )
        tk.Label(
            win,
            text=info,
            font=("Segoe UI", 12),
            bg=bg,
            fg=("white" if self.dark_mode else "black"),
            justify="center",
            wraplength=380,
        ).pack(pady=(0, 10))
        link = tk.Label(
            win,
            text="http://github.com/seifhassine",
            font=("Segoe UI", 12, "underline"),
            bg=bg,
            fg="blue",
            cursor="hand2",
        )
        link.pack()
        link.bind("<Button-1>", lambda _e: os.startfile("http://github.com/seifhassine"))
        win.grab_set()
        self.root.wait_window(win)

    def add_tab(self, filename=None, data=None):
        if filename:
            abs_fn = os.path.abspath(filename)
            for tab in self.tabs.values():
                if tab.filename and os.path.abspath(tab.filename) == abs_fn:
                    if tab.modified:
                        ans = messagebox.askyesnocancel(
                            "Unsaved Changes",
                            f"The file {os.path.basename(filename)} has unsaved changes.\nSave before reopening?",
                            parent=self.root,
                        )
                        if ans is None:
                            return
                        elif ans:
                            tab.on_save()
                        else:
                            tab.modified = False
                            tab.update_tab_title()
                    self.notebook.select(tab.frame)
                    return
        new_tab = FileTab(self.notebook, filename, data, app=self)
        tab_label = os.path.basename(filename) if filename else "Untitled"
        self.notebook.add(new_tab.frame, text=tab_label)
        self.tabs[new_tab.frame._w] = new_tab
        self.notebook.select(new_tab.frame)

    def get_active_tab(self):
        return self.tabs.get(self.notebook.select(), None)

    def on_open(self):
        fn = filedialog.askopenfilename(
            title="Open File",
            filetypes=[("UVAR, SCN, RCOL Files", ["*.uvar", "*.uvar.*", "*scn.20", "*.rcol.25"]), ("All Files", "*.*")],
        )
        if not fn:
            return
        with open(fn, "rb") as f:
            data = f.read()
        self.add_tab(fn, data)

    def on_save(self):
        active = self.get_active_tab()
        if active:
            active.on_save()
        else:
            messagebox.showerror("Error", "No active tab to save.", parent=self.root)

    def reload_file(self):
        active = self.get_active_tab()
        if active:
            active.reload_file()
        else:
            messagebox.showerror("Error", "No active tab to reload.", parent=self.root)

    def on_notebook_click(self, event):
        x, y = event.x, event.y
        if "image" in self.notebook.identify(x, y):
            self.close_tab(self.notebook.index("@%d,%d" % (x, y)))
            return "break"

    def close_tab(self, index):
        tab_frame = self.notebook.tabs()[index]
        tab = self.tabs.get(tab_frame)
        if tab and tab.modified:
            ans = messagebox.askyesnocancel(
                "Unsaved Changes",
                f"The file {os.path.basename(tab.filename)} has unsaved changes.\nSave before closing?",
                parent=self.root,
            )
            if ans is None:
                return
            elif ans:
                tab.on_save()
            else:
                tab.modified = False
                tab.update_tab_title()
        del self.tabs[tab_frame]
        self.notebook.forget(index)

    def bind_notebook(self):
        self.notebook.bind("<Button-1>", self.on_notebook_click, True)

    def copy_to_clipboard(self, event=None):
        active = self.get_active_tab()
        if active:
            active.copy_to_clipboard()
        else:
            messagebox.showerror("Error", "No active tab.", parent=self.root)


def main():
    root = TkinterDnD.Tk()  
    root.title("REasy Editor – RE Engine Scripting Toolkit")
    set_app_icon(root)
    root.geometry("800x600")
    app = REasyEditorApp(root)

    def on_closing():
        for tab in list(app.tabs.values()):
            if tab.modified:
                if not messagebox.askyesnocancel(
                    "Unsaved Changes",
                    f"The file {os.path.basename(tab.filename)} has unsaved changes. Quit without saving?",
                    parent=root,
                ):
                    return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_closing)
    if len(sys.argv) > 1:
        with open(sys.argv[1], "rb") as f:
            data = f.read()
        app.add_tab(sys.argv[1], data)
    root.mainloop()


if __name__ == "__main__":
    main()
