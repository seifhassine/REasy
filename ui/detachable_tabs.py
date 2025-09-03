from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QTabWidget, QMainWindow, QWidget, QToolButton, QTabBar, QMessageBox


class FloatingTabWindow(QMainWindow):
	def __init__(self, page: QWidget, title: str, notebook: 'CustomNotebook', return_index: int, set_app_icon=None):
		super().__init__()
		if set_app_icon:
			set_app_icon(self)
		self.page = page
		self.notebook = notebook
		self.return_index = return_index
		self.setWindowTitle(title)
		self.setCentralWidget(page)
		self.page.show()
		
		self.file_tab = None
		if hasattr(page, 'parent_tab'):
			self.file_tab = page.parent_tab
		
		self.setFocusPolicy(Qt.StrongFocus)
		main_window = self._get_main_window()
		if main_window is not None:
			self.setStyleSheet(main_window.styleSheet())
			self.setPalette(main_window.palette())
			self.setFont(main_window.font())
		menu = self.menuBar().addMenu("Tab")
		act = QAction("Reattach", self)
		act.triggered.connect(self._reattach_now)
		menu.addAction(act)
		main_window = self._get_main_window()
		if main_window is not None:
			self._mirror_menubar_from(main_window)
			self._create_find_action(main_window)
			actions = self._collect_all_actions(main_window)
			for act in actions:
				self.addAction(act)
				act.setShortcutContext(Qt.ApplicationShortcut)

	def _get_main_window(self):
		return getattr(self.notebook, 'app_instance', None) or self.notebook.window()

	def _reattach_now(self):
		self.close()

	def closeEvent(self, event):
		if self.file_tab and hasattr(self.file_tab, '_find_dialog'):
			try:
				if self.file_tab._find_dialog and self.file_tab._find_dialog.isVisible():
					self.file_tab._find_dialog.close()
			except RuntimeError:
				pass
		
		page = self.centralWidget()
		if page is not None:
			self.takeCentralWidget()
			idx = min(max(self.return_index, 0), self.notebook.count())
			idx = self.notebook.insertTab(idx, page, self.windowTitle())
			self.notebook.setCurrentIndex(idx)
		if self in self.notebook._floating_windows:
			self.notebook._floating_windows.remove(self)
		super().closeEvent(event)

	def _mirror_menubar_from(self, main_window: QMainWindow):
		src_mb = main_window.menuBar()
		dst_mb = self.menuBar()
		for top_action in src_mb.actions():
			src_menu = top_action.menu()
			if not src_menu:
				continue
			if src_menu.title() == "Tab":
				continue
			if src_menu.title() == "Find":
				new_menu = dst_mb.addMenu(src_menu.title())
				if hasattr(self, '_find_action'):
					new_menu.addAction(self._find_action)
				new_menu.addSeparator()
				for act in src_menu.actions():
					if act.objectName() != "find_search":
						if act.isSeparator():
							new_menu.addSeparator()
						else:
							new_menu.addAction(act)
			else:
				new_menu = dst_mb.addMenu(src_menu.title())
				for act in src_menu.actions():
					if act.isSeparator():
						new_menu.addSeparator()
					elif act.menu():
						sub = new_menu.addMenu(act.menu().title())
						for sub_act in act.menu().actions():
							if sub_act.isSeparator():
								sub.addSeparator()
							else:
								sub.addAction(sub_act)
					else:
						new_menu.addAction(act)

	def _create_find_action(self, main_window):
		find_act = QAction("Find", self)
		find_act.setObjectName("find_search_detached")
		
		if hasattr(main_window, 'settings'):
			shortcut = main_window.settings.get("keyboard_shortcuts", {}).get("find_search", "Ctrl+F")
		else:
			shortcut = "Ctrl+F"
		
		find_act.setShortcut(QKeySequence(shortcut))
		find_act.setShortcutContext(Qt.WidgetWithChildrenShortcut)
		find_act.triggered.connect(self.open_find_dialog)
		
		self.addAction(find_act)
		self._find_action = find_act
	
	def _collect_all_actions(self, main_window: QMainWindow):
		actions = []
		for a in main_window.findChildren(QAction):
			if not a.shortcut().isEmpty() or a.parent() == main_window.menuBar():
				if a.objectName() == "find_search":
					continue
				actions.append(a)
		return actions
	
	def open_find_dialog(self):
		if self.file_tab:
			self.file_tab.open_find_dialog()
		else:
			page = self.centralWidget()
			if page:
				main_window = self._get_main_window()
				if main_window and hasattr(main_window, 'tabs'):
					for tab in main_window.tabs.values():
						if hasattr(tab, 'notebook_widget') and tab.notebook_widget == page:
							self.file_tab = tab
							tab.open_find_dialog()
							return
			QMessageBox.warning(self, "Warning", "Cannot open find dialog for this tab")
	
	def keyPressEvent(self, event):
		if event.modifiers() == Qt.ControlModifier and event.key() == Qt.Key_F:
			self.open_find_dialog()
			event.accept()
			return
		
		super().keyPressEvent(event)


class DetachTabBar(QTabBar):
	def __init__(self, notebook: 'CustomNotebook'):
		super().__init__(notebook)
		self._notebook = notebook

	def ensure_button(self, index: int):
		btn = self.tabButton(index, QTabBar.LeftSide)
		if isinstance(btn, QToolButton) and getattr(btn, '_is_detach_button', False):
			return
		button = QToolButton(self)
		button.setAutoRaise(True)
		button.setText('↗')
		button.setToolTip('Detach')
		button._is_detach_button = True
		button.clicked.connect(lambda: self._on_detach_clicked(button))
		self.setTabButton(index, QTabBar.LeftSide, button)

	def _on_detach_clicked(self, button: QToolButton):
		for i in range(self.count()):
			if self.tabButton(i, QTabBar.LeftSide) is button:
				self._notebook.detach_tab(i)
				break

class CustomNotebook(QTabWidget):
	def __init__(self, parent=None):
		super().__init__(parent)
		self.setTabsClosable(True)
		self.setMovable(True)
		self.tabCloseRequested.connect(self.on_tab_close_requested)
		self.dark_mode = False
		self.app_instance = None
		self._floating_windows = []
		self._set_icon_callback = None
		self.setTabBar(DetachTabBar(self))
		self.setTabsClosable(True)
		self.setMovable(True)
		bar = self.tabBar()
		self._refresh_detach_buttons()

	def addTab(self, widget: QWidget, label: str) -> int:
		idx = super().addTab(widget, label)
		self._refresh_detach_buttons()
		return idx

	def insertTab(self, index: int, widget: QWidget, label: str) -> int:
		idx = super().insertTab(index, widget, label)
		self._refresh_detach_buttons()
		return idx

	def removeTab(self, index: int) -> None:
		super().removeTab(index)
		self._refresh_detach_buttons()

	def set_dark_mode(self, is_dark):
		self.dark_mode = is_dark
		if is_dark:
			self.setStyleSheet("QTabWidget { background-color: #2b2b2b; }")
		else:
			self.setStyleSheet("QTabWidget { background-color: white; }")

	def on_tab_close_requested(self, index):
		if self.app_instance:
			self.app_instance.close_tab(index)
		else:
			self.removeTab(index)

	def detach_tab(self, index):
		page = self.widget(index)
		if page is None:
			return
		title = self.tabText(index)
		self.removeTab(index)
		page.setParent(None)
		win = FloatingTabWindow(page, title, self, index, getattr(self, '_set_icon_callback', None))
		self._floating_windows.append(win)
		win.show()
		win.raise_()
		win.activateWindow()
		self._refresh_detach_buttons()
		
		if self.app_instance and hasattr(self.app_instance, '_check_and_close_shared_find_dialog'):
			self.app_instance._check_and_close_shared_find_dialog()


	def _refresh_detach_buttons(self):
		bar = self.tabBar()
		if not isinstance(bar, DetachTabBar):
			return
		for i in range(self.count()):
			bar.ensure_button(i)

 

