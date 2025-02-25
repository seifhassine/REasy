from PySide6.QtCore import (
    Qt,
    QModelIndex,
)
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QCheckBox,
    QPushButton,
    QPlainTextEdit,
    QListWidget,
    QDialog,
    QSplitter,
    QRadioButton,
)

class BetterFindDialog(QDialog):
    def __init__(self, file_tab, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find in Tree")
        self.resize(400, 300)

        self.file_tab = file_tab
        self.app = file_tab.app

        self.results = []  # Will store tuples of (path, name, value)
        self.current_index = -1

        # Create layout
        layout = QVBoxLayout(self)

        # Search box and options
        search_group = QWidget()
        search_layout = QVBoxLayout(search_group)

        input_layout = QHBoxLayout()
        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText("Enter search text...")
        self.search_entry.returnPressed.connect(self.find_all)
        input_layout.addWidget(QLabel("Search:"))
        input_layout.addWidget(self.search_entry)
        search_layout.addLayout(input_layout)

        # Options row
        options_layout = QHBoxLayout()

        # Radio buttons
        radio_group = QWidget()
        radio_layout = QHBoxLayout(radio_group)
        radio_layout.setContentsMargins(0, 0, 0, 0)
        self.search_name = QRadioButton("Name")
        self.search_value = QRadioButton("Value")
        self.search_both = QRadioButton("Both")
        self.search_both.setChecked(True)
        radio_layout.addWidget(self.search_name)
        radio_layout.addWidget(self.search_value)
        radio_layout.addWidget(self.search_both)
        options_layout.addWidget(radio_group)

        # Case sensitivity checkbox
        self.case_box = QCheckBox("Case sensitive")
        options_layout.addWidget(self.case_box)
        options_layout.addStretch()
        search_layout.addLayout(options_layout)

        layout.addWidget(search_group)

        # Results area with splitter
        results_splitter = QSplitter(Qt.Vertical)

        # Results list
        results_group = QWidget()
        results_layout = QVBoxLayout(results_group)
        results_layout.setContentsMargins(0, 0, 0, 0)
        results_header = QLabel("Results:")
        results_layout.addWidget(results_header)
        self.result_list = QListWidget()
        self.result_list.itemDoubleClicked.connect(self._on_result_selected)
        results_layout.addWidget(self.result_list)
        results_splitter.addWidget(results_group)

        # Preview area
        preview_group = QWidget()
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_header = QLabel("Preview:")
        preview_layout.addWidget(preview_header)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        preview_layout.addWidget(self.preview_text)
        results_splitter.addWidget(preview_group)

        layout.addWidget(results_splitter, 1) 

        # Button area
        button_layout = QHBoxLayout()
        find_all_btn = QPushButton("Find All")
        find_prev_btn = QPushButton("Previous")
        find_next_btn = QPushButton("Next")
        close_btn = QPushButton("Close")

        find_all_btn.clicked.connect(self.find_all)
        find_prev_btn.clicked.connect(self.find_previous)
        find_next_btn.clicked.connect(self.find_next)
        close_btn.clicked.connect(self.close)

        button_layout.addWidget(find_all_btn)
        button_layout.addStretch()
        button_layout.addWidget(find_prev_btn)
        button_layout.addWidget(find_next_btn)
        button_layout.addStretch()
        button_layout.addWidget(close_btn)

        layout.addLayout(button_layout)

        # Status bar
        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

    def find_all(self):
        """Find all occurrences by traversing the tree directly"""
        search_text = self.search_entry.text().strip()
        if not search_text:
            self.status_label.setText("Please enter search text")
            return

        # Get current tree and model
        tree = self.app.get_active_tree()
        if not tree:
            self.status_label.setText("No tree view available")
            return

        model = tree.model()
        if not model:
            self.status_label.setText("Tree has no model")
            return

        # Clear previous results
        self.result_list.clear()
        self.results = []
        self.current_index = -1

        # Settings for search
        case_sensitive = self.case_box.isChecked()
        search_mode = "both"
        if self.search_name.isChecked():
            search_mode = "name"
        elif self.search_value.isChecked():
            search_mode = "value"

        if not case_sensitive:
            search_text = search_text.lower()

        # Recursive search function that doesn't rely on tree triggers
        def search_recursive(parent_index, path=""):
            for row in range(model.rowCount(parent_index)):
                index = model.index(row, 0, parent_index)
                if not index.isValid():
                    continue

                # Get display name
                name = str(index.data(Qt.DisplayRole) or "")
                current_path = path + " > " + name if path else name

                # Try to get value using tree's helper or fallback
                try:
                    if hasattr(tree, "get_value_at_index"):
                        value = tree.get_value_at_index(index)
                    else:
                        # Get directly from model
                        item = index.internalPointer()
                        if (
                            item
                            and hasattr(item, "data")
                            and len(item.data) > 1
                        ):
                            value = str(item.data[1])
                        else:
                            value = str(index.data(Qt.UserRole) or "")
                except:
                    value = ""

                # Apply case sensitivity
                if not case_sensitive:
                    name_lower = name.lower()
                    value_lower = value.lower() if value else ""

                    # Check for matches
                    found = False
                    if search_mode == "name" and search_text in name_lower:
                        found = True
                    elif (
                        search_mode == "value"
                        and value_lower
                        and search_text in value_lower
                    ):
                        found = True
                    elif search_mode == "both" and (
                        search_text in name_lower
                        or (value_lower and search_text in value_lower)
                    ):
                        found = True
                else:
                    # Case sensitive search
                    found = False
                    if search_mode == "name" and search_text in name:
                        found = True
                    elif (
                        search_mode == "value"
                        and value
                        and search_text in value
                    ):
                        found = True
                    elif search_mode == "both" and (
                        search_text in name or (value and search_text in value)
                    ):
                        found = True

                if found:
                    # Store result info (path, name, value, row numbers)
                    self.results.append(
                        {
                            "path": current_path,
                            "name": name,
                            "value": value,
                            "row_path": self._get_row_path(index),
                        }
                    )

                    # Add to list display
                    display_text = f"{name}: {value}" if value else name
                    if len(current_path.split(" > ")) > 3:
                        display_text = "..." + display_text
                    self.result_list.addItem(display_text)

                # Go deeper
                if model.hasChildren(index):
                    search_recursive(index, current_path)

        # Start recursive search
        search_recursive(QModelIndex())

        # Show results
        if self.results:
            self.status_label.setText(f"Found {len(self.results)} matches")
            self.current_index = 0
            self.result_list.setCurrentRow(0)
            self._show_preview(0)
            self._go_to_result(0)
        else:
            self.status_label.setText("No matches found")

    def _get_row_path(self, index):
        """Get path of row indices to reach this item"""
        row_path = []
        current = index
        while current.isValid():
            row_path.insert(0, current.row())
            current = current.parent()
        return row_path

    def _find_index_by_rows(self, row_path):
        """Find a model index by following a path of row numbers with robust error handling"""
        try:
            # Get current app reference first
            if not self.app:
                self.status_label.setText("App reference lost")
                return QModelIndex()

            # Try multiple methods to get a valid tree
            tree = None

            # Method 1: Direct app.get_active_tree()
            try:
                tree = self.app.get_active_tree()
            except RuntimeError:
                pass

            # Method 2: Via current tab
            if not tree:
                try:
                    current_tab = self.app.get_active_tab()
                    if (
                        current_tab
                        and hasattr(current_tab, "viewer")
                        and current_tab.viewer
                    ):
                        tree = getattr(current_tab.viewer, "tree", None)
                    if not tree and current_tab:
                        tree = getattr(current_tab, "tree", None)
                except RuntimeError:
                    pass

            # Final check
            if not tree or not hasattr(tree, "model"):
                self.status_label.setText(
                    "Tree reference invalid - try closing and reopening search"
                )
                return QModelIndex()

            # Get model safely
            try:
                model = tree.model()
                if not model:
                    self.status_label.setText("Tree has no model")
                    return QModelIndex()
            except RuntimeError:
                self.status_label.setText(
                    "Tree deleted during access - try closing and reopening search"
                )
                return QModelIndex()

            # Follow row path
            current = QModelIndex()
            for row in row_path:
                try:
                    current = model.index(row, 0, current)
                    if not current.isValid():
                        return QModelIndex()
                except RuntimeError:
                    self.status_label.setText(
                        "Model access error - try closing and reopening search"
                    )
                    return QModelIndex()

            return current

        except Exception as e:
            self.status_label.setText(f"Error: {str(e)}")
            return QModelIndex()

    def find_next(self):
        """Find the next result"""
        if not self.results:
            self.find_all()
            return

        if self.current_index < len(self.results) - 1:
            self.current_index += 1
        else:
            self.current_index = 0

        self.result_list.setCurrentRow(self.current_index)
        self._show_preview(self.current_index)
        self._go_to_result(self.current_index)

    def find_previous(self):
        """Find the previous result"""
        if not self.results:
            self.find_all()
            return

        if self.current_index > 0:
            self.current_index -= 1
        else:
            self.current_index = len(self.results) - 1

        self.result_list.setCurrentRow(self.current_index)
        self._show_preview(self.current_index)
        self._go_to_result(self.current_index)

    def _show_preview(self, index):
        """Show preview of the selected result"""
        if 0 <= index < len(self.results):
            result = self.results[index]
            self.preview_text.setPlainText(
                f"Path: {result['path']}\n"
                f"Name: {result['name']}\n"
                f"Value: {result['value']}"
            )

    def _go_to_result(self, index):
        """Navigate to the selected result in the tree with better error handling"""
        if 0 <= index < len(self.results):
            try:
                row_path = self.results[index]["row_path"]
                found_index = self._find_index_by_rows(row_path)

                if found_index.isValid():
                    tree = None

                    try:
                        tree = self.app.get_active_tree()
                        if tree:
                            tree.setCurrentIndex(found_index)
                            tree.scrollTo(found_index)
                            self.status_label.setText(
                                f"Result {index + 1} of {len(self.results)}"
                            )
                            return
                    except RuntimeError:
                        pass
                    
                    try:
                        tab = self.app.get_active_tab()
                        if tab:
                            if tab.viewer and hasattr(tab.viewer, "tree"):
                                tab.viewer.tree.setCurrentIndex(found_index)
                                tab.viewer.tree.scrollTo(found_index)
                                self.status_label.setText(
                                    f"Result {index + 1} of {len(self.results)}"
                                )
                                return
                            elif hasattr(tab, "tree"):
                                tab.tree.setCurrentIndex(found_index)
                                tab.tree.scrollTo(found_index)
                                self.status_label.setText(
                                    f"Result {index + 1} of {len(self.results)}"
                                )
                                return
                    except RuntimeError:
                        # Nothing worked
                        pass

                    self.status_label.setText(
                        "Tree view not accessible - close and reopen search to refresh"
                    )
                else:
                    self.status_label.setText(
                        f"Result {index + 1} not found in tree - tree structure changed"
                    )
            except Exception as e:
                self.status_label.setText(f"Navigation error: {str(e)}")

    def _on_result_selected(self, item):
        """Handle double-click on result list item"""
        index = self.result_list.currentRow()
        if 0 <= index < len(self.results):
            self.current_index = index
            self._show_preview(index)
            self._go_to_result(index)

    def showEvent(self, event):
        """When dialog is shown, set focus to search box"""
        super().showEvent(event)
        self.search_entry.setFocus()

    def closeEvent(self, event):
        """Clean up when dialog closes"""
        self.results = []
        self.result_list.clear()
        self.file_tab = None
        self.app = (
            None
        )
        super().closeEvent(event)