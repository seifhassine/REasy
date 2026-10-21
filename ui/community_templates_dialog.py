import math

from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt, Signal, QThread, QTimer, QPointF, QRectF, QSize
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QLineEdit, QTextEdit, QPushButton, QComboBox,
    QWidget, QSplitter, QMessageBox, QTabWidget,
    QFrame, QGridLayout, QScrollArea, QStackedWidget,
    QFormLayout, QCheckBox, QSizePolicy, QDialogButtonBox, QProgressDialog
)

lazy from file_handlers.rsz.rsz_template_manager import RszTemplateManager
lazy from file_handlers.rsz.rsz_community_template_manager import RszCommunityTemplateManager
lazy from ui.editor_widgets import editor_strip, search_field, styled_label
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role
from datetime import datetime


_NO_RESULT = sentinel("_NO_RESULT")
LOGIN_REQUIRED_TITLE = QT_TRANSLATE_NOOP(
    "CommunityTemplatesDialog", "Login Required"
)


class _ResultWorker(QThread):
    """Run one callable and deliver its result back to the UI thread."""

    result_ready = Signal(object)

    def __init__(self, operation, *args, discard_if_interrupted=False):
        super().__init__()
        self._operation = operation
        self._args = args
        self._discard_if_interrupted = discard_if_interrupted

    def run(self):
        if self._discard_if_interrupted and self.isInterruptionRequested():
            return
        result = self._operation(*self._args)
        if result is _NO_RESULT:
            return
        if self._discard_if_interrupted and self.isInterruptionRequested():
            return
        self.result_ready.emit(result)


def _fetch_community_templates(game, sort_by, query):
    try:
        return RszCommunityTemplateManager.get_community_templates(
            sort_by=sort_by,
            game=game,
            search=query or None,
        )
    except Exception as e:
        print("Template loading error:", e)
        return []


def _fetch_template_details(community_id):
    try:
        api_url = (
            f"{RszCommunityTemplateManager.get_api_base_url()}"
            f"/templates/{community_id}"
        )

        headers = {}
        if RszCommunityTemplateManager._auth_token:
            headers["Authorization"] = (
                f"Bearer {RszCommunityTemplateManager._auth_token}"
            )

        import requests

        response = requests.get(api_url, headers=headers)
        if response.status_code != 200:
            return _NO_RESULT
        return response.json()
    except Exception as e:
        print(f"Error loading template details: {e}")
        return _NO_RESULT


def _star_path(rect: QRectF) -> QPainterPath:
    center, outer = rect.center(), rect.width() / 2
    points = []
    for index in range(10):
        radius = outer if index % 2 == 0 else outer * 0.45
        angle = math.radians(-90 + index * 36)
        points.append(QPointF(center.x() + radius * math.cos(angle), center.y() + radius * math.sin(angle)))
    path = QPainterPath()
    path.addPolygon(QPolygonF(points))
    path.closeSubpath()
    return path


class QStarWidget(QWidget):
    ratingChanged = Signal(int)
    _STAR = 16
    _GAP = 3

    def __init__(self, max_stars=5, initial_rating=0, readonly=False):
        super().__init__()
        self.max_stars = max_stars
        self.rating = initial_rating
        self.readonly = readonly
        self._hover = 0
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setRating(initial_rating)

    def sizeHint(self):
        return QSize(self.max_stars * (self._STAR + self._GAP) - self._GAP, self._STAR + 4)

    def setRating(self, rating):
        self.rating = min(max(0, rating), self.max_stars)
        self.update()

    def _set_rating(self, rating):
        if not self.readonly and self.rating != rating:
            self.rating = rating
            self.update()
            self.ratingChanged.emit(rating)

    def _star_at(self, x: float) -> int:
        index = int(x // (self._STAR + self._GAP)) + 1
        return index if 1 <= index <= self.max_stars else 0

    def enterEvent(self, event):
        self.setCursor(Qt.ArrowCursor if self.readonly else Qt.PointingHandCursor)
        self.setToolTip("" if self.readonly else self.tr("Click a star to rate this template"))
        super().enterEvent(event)

    def mouseMoveEvent(self, event):
        hover = 0 if self.readonly else self._star_at(event.position().x())
        if hover != self._hover:
            self._hover = hover
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._hover:
            self._hover = 0
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            star = self._star_at(event.position().x())
            if star:
                self._set_rating(star)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        colors = current_colors()
        filled = QColor(colors["warning"])
        if self._hover:
            filled = filled.lighter(120)
        outline = QPen(QColor(colors["text_subtle"]), 1.2)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        shown = self._hover or self.rating
        whole, partial = int(shown), shown - int(shown)
        top = (self.height() - self._STAR) / 2
        for index in range(self.max_stars):
            rect = QRectF(index * (self._STAR + self._GAP), top, self._STAR, self._STAR)
            path = _star_path(rect)
            if index < whole:
                painter.fillPath(path, filled)
                continue
            if index == whole and partial >= 0.5:
                painter.save()
                painter.setClipRect(QRectF(rect.left(), rect.top(), rect.width() / 2, rect.height()))
                painter.fillPath(path, filled)
                painter.restore()
            painter.strokePath(path, outline)
        painter.end()

class CommunityTemplatesDialog(QDialog):
    """Dialog for browsing, downloading, rating, and commenting on community templates"""
    template_downloaded = Signal(str) 
    _SENTINEL_ROLE = Qt.UserRole + 1
    
    def __init__(self, parent=None):
        super().__init__(parent)
        RszCommunityTemplateManager.restore_session()
        self.setWindowTitle(self.tr("Community Template Browser"))
        self.resize(900, 700)
        self.setMinimumSize(700, 500)
        
        self.current_community_id = None
        
        self._create_ui()
        self._connect_signals()
        
        self._check_login_status()

    def closeEvent(self, event):
        for attr in dir(self):
            obj = getattr(self, attr)
            if isinstance(obj, QThread) and obj.isRunning():
                obj.quit()
                obj.wait()
        super().closeEvent(event)

    def _create_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.stacked_widget = QStackedWidget()
        
        self.login_page = QWidget()
        self._create_login_ui()
        self.stacked_widget.addWidget(self.login_page)
        
        self.main_page = QWidget()
        self._create_main_ui()
        self.stacked_widget.addWidget(self.main_page)
        
        main_layout.addWidget(self.stacked_widget)
        
        self.stacked_widget.setCurrentWidget(self.login_page)
    
    def _create_login_ui(self):
        """Create the login/register UI"""
        login_layout = QVBoxLayout(self.login_page)
        login_layout.setContentsMargins(16, 16, 16, 16)
        login_layout.addStretch(1)

        form_container = QWidget()
        form_container.setFixedWidth(380)
        form_layout = QVBoxLayout(form_container)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(12)

        header_label = styled_label(self.tr("REasy Community Templates"), heading="title")
        header_label.setAlignment(Qt.AlignCenter)
        form_layout.addWidget(header_label)

        self.auth_tabs = QTabWidget()

        login_tab = QWidget()
        login_tab_layout = self._auth_form(login_tab)

        self.login_email = QLineEdit()
        self.login_email.setPlaceholderText(self.tr("Email address"))
        login_tab_layout.addRow(self.tr("Email:"), self.login_email)
        
        self.login_password = QLineEdit()
        self.login_password.setPlaceholderText(self.tr("Password"))
        self.login_password.setEchoMode(QLineEdit.Password)
        login_tab_layout.addRow(self.tr("Password:"), self.login_password)
        
        self.remember_me = QCheckBox(self.tr("Remember me"))
        login_tab_layout.addRow("", self.remember_me)
        
        self.login_button = QPushButton(self.tr("Login"))
        self.login_button.setProperty("variant", "primary")
        login_tab_layout.addRow("", self.login_button)

        self.login_status = styled_label("", tone="danger")
        self.login_status.setWordWrap(True)
        login_tab_layout.addRow("", self.login_status)

        self.auth_tabs.addTab(login_tab, self.tr("Login"))

        register_tab = QWidget()
        register_tab_layout = self._auth_form(register_tab)

        self.register_email = QLineEdit()
        self.register_email.setPlaceholderText(self.tr("Email address"))
        register_tab_layout.addRow(self.tr("Email:"), self.register_email)
        
        self.register_username = QLineEdit()
        self.register_username.setPlaceholderText(self.tr("Display name"))
        register_tab_layout.addRow(self.tr("Display Name:"), self.register_username)
        
        self.register_password = QLineEdit()
        self.register_password.setPlaceholderText(self.tr("Password"))
        self.register_password.setEchoMode(QLineEdit.Password)
        register_tab_layout.addRow(self.tr("Password:"), self.register_password)
        
        self.register_password_confirm = QLineEdit()
        self.register_password_confirm.setPlaceholderText(self.tr("Confirm password"))
        self.register_password_confirm.setEchoMode(QLineEdit.Password)
        register_tab_layout.addRow(self.tr("Confirm:"), self.register_password_confirm)
        
        self.register_button = QPushButton(self.tr("Register"))
        self.register_button.setProperty("variant", "primary")
        register_tab_layout.addRow("", self.register_button)

        self.register_status = styled_label("", tone="danger")
        self.register_status.setWordWrap(True)
        register_tab_layout.addRow("", self.register_status)

        self.auth_tabs.addTab(register_tab, self.tr("Register"))

        form_layout.addWidget(self.auth_tabs)

        login_layout.addWidget(form_container, 0, Qt.AlignHCenter)
        login_layout.addStretch(2)

    @staticmethod
    def _auth_form(tab):
        form = QFormLayout(tab)
        form.setContentsMargins(4, 12, 4, 4)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return form

    @staticmethod
    def _set_status(label, text, tone="danger"):
        label.setText(text)
        set_style_role(label, tone=tone)

    def _create_main_ui(self):
        """Build the main (post-login) UI."""
        main_layout = QVBoxLayout(self.main_page)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        toolbar, header = editor_strip("toolbar", (8, 6, 8, 6), 6)

        header.addWidget(QLabel(self.tr("Game:")))
        self.game_combo = QComboBox()
        self.game_combo.addItems(self.GAMES)
        header.addWidget(self.game_combo)

        header.addSpacing(6)
        header.addWidget(QLabel(self.tr("Sort:")))
        self.sort_combo = QComboBox()
        self.sort_combo.addItem(self.tr("Rating"),    "rating")
        self.sort_combo.addItem(self.tr("Date"),      "date")
        self.sort_combo.addItem(self.tr("Downloads"), "downloads")
        header.addWidget(self.sort_combo)

        self.search_input = search_field(QLineEdit())
        self.search_input.setPlaceholderText(self.tr("Search community templates…"))
        header.addWidget(self.search_input, 1)

        self.upload_button = QPushButton(line_icon("export"), self.tr("Upload Template"))
        header.addWidget(self.upload_button)
        main_layout.addWidget(toolbar)

        splitter = QSplitter(Qt.Horizontal, self.main_page)
        splitter.setChildrenCollapsible(False)
        list_panel = QWidget()
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(8, 8, 4, 8)
        self.template_list = QListWidget()
        list_layout.addWidget(self.template_list)
        splitter.addWidget(list_panel)

        details_widget = QWidget()
        details_layout = QVBoxLayout(details_widget)
        details_layout.setContentsMargins(4, 8, 8, 8)

        self.template_info = QWidget()
        info_layout = QVBoxLayout(self.template_info)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(6)

        self.template_name = styled_label(heading="title")
        info_layout.addWidget(self.template_name)

        self.template_meta = styled_label(tone="muted")
        self.template_meta.setWordWrap(True)
        info_layout.addWidget(self.template_meta)

        self.template_description = QLabel()
        self.template_description.setWordWrap(True)
        info_layout.addWidget(self.template_description)

        rating_frame = QFrame()
        rating_layout = QHBoxLayout(rating_frame)
        rating_layout.setContentsMargins(0, 4, 0, 4)
        rating_layout.setSpacing(8)

        self.rating_label = styled_label(self.tr("Rating:"), tone="muted")
        rating_layout.addWidget(self.rating_label)

        self.star_widget = QStarWidget(5, 0)
        rating_layout.addWidget(self.star_widget)

        self.rating_count = styled_label(self.tr("(0 ratings)"), tone="subtle")
        rating_layout.addWidget(self.rating_count)

        rating_layout.addStretch()

        self.download_button = QPushButton(self.tr("Download"))
        self.download_button.setProperty("variant", "primary")
        rating_layout.addWidget(self.download_button)

        info_layout.addWidget(rating_frame)

        tabs = QTabWidget()

        comments_widget = QWidget()
        comments_layout = QVBoxLayout(comments_widget)
        comments_layout.setContentsMargins(0, 6, 0, 0)
        comments_layout.setSpacing(6)

        self.comments_area = QScrollArea()
        self.comments_area.setWidgetResizable(True)
        self.comments_container = QWidget()
        self.comments_layout = QVBoxLayout(self.comments_container)
        self.comments_layout.setContentsMargins(0, 0, 4, 0)
        self.comments_layout.setSpacing(6)
        self.comments_layout.setAlignment(Qt.AlignTop)
        self.comments_area.setWidget(self.comments_container)
        comments_layout.addWidget(self.comments_area)

        comment_input_layout = QHBoxLayout()
        comment_input_layout.setSpacing(6)
        self.comment_input = QTextEdit()
        self.comment_input.setPlaceholderText(self.tr("Add your comment..."))
        self.comment_input.setMaximumHeight(72)
        comment_input_layout.addWidget(self.comment_input)

        self.submit_comment_button = QPushButton(line_icon("message"), self.tr("Submit"))
        comment_input_layout.addWidget(self.submit_comment_button, alignment=Qt.AlignBottom)

        comments_layout.addLayout(comment_input_layout)

        tabs.addTab(comments_widget, self.tr("Comments"))

        metadata_widget = QWidget()
        metadata_layout = QGridLayout(metadata_widget)
        metadata_layout.setContentsMargins(0, 10, 0, 0)
        metadata_layout.setHorizontalSpacing(16)
        metadata_layout.setVerticalSpacing(6)
        metadata_layout.setColumnStretch(1, 1)
        metadata_layout.setRowStretch(5, 1)

        self.registry_label = QLabel()
        self.tags_label = QLabel()
        self.uploader_label = QLabel()
        self.date_label = QLabel()
        self.downloads_label = QLabel()
        for row, (caption, value) in enumerate((
            (self.tr("Registry:"), self.registry_label),
            (self.tr("Tags:"), self.tags_label),
            (self.tr("Uploaded by:"), self.uploader_label),
            (self.tr("Upload date:"), self.date_label),
            (self.tr("Downloads:"), self.downloads_label),
        )):
            key = styled_label(caption, tone="muted")
            metadata_layout.addWidget(key, row, 0)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            metadata_layout.addWidget(value, row, 1)

        tabs.addTab(metadata_widget, self.tr("Details"))

        info_layout.addWidget(tabs)
        details_layout.addWidget(self.template_info)

        splitter.addWidget(details_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 600])

        main_layout.addWidget(splitter, 1)

        account_bar, account = editor_strip("footer", spacing=6)
        self.user_status_label = styled_label(tone="muted")
        account.addWidget(self.user_status_label)
        account.addStretch()
        self.logout_button = QPushButton(self.tr("Logout"))
        self.logout_button.setProperty("variant", "ghost")
        self.logout_button.setProperty("compact", True)
        account.addWidget(self.logout_button)
        main_layout.addWidget(account_bar)

        self.template_info.setVisible(False)

        self.no_template_label = styled_label(self.tr("Select a template from the list to view details"), tone="subtle")
        self.no_template_label.setAlignment(Qt.AlignCenter)
        details_layout.addWidget(self.no_template_label)
    
    def _on_game_changed(self):
        """User switched the game filter → reload list."""
        self._load_templates()
        
    def _connect_signals(self):
        """Connect all UI signals to handlers"""
        self.login_button.clicked.connect(self._handle_login)
        self.register_button.clicked.connect(self._handle_register)
        
        self.logout_button.clicked.connect(self._handle_logout)
        self.template_list.itemSelectionChanged.connect(self._on_template_selected)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        self.game_combo.currentIndexChanged.connect(self._on_game_changed)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        self.upload_button.clicked.connect(self._upload_template)
        self.download_button.clicked.connect(self._download_template)
        self.submit_comment_button.clicked.connect(self._submit_comment)
        self.star_widget.ratingChanged.connect(self._submit_rating)
    
    def _check_login_status(self):
        """Check if user is already logged in and show appropriate page"""
        if RszCommunityTemplateManager.is_authenticated():
            self._show_main_page()
        else:
            self.stacked_widget.setCurrentWidget(self.login_page)
    
    def _handle_login(self):
        """Handle the login button click"""
        email = self.login_email.text().strip()
        password = self.login_password.text()
        
        if not email:
            self._set_status(self.login_status, self.tr("Please enter your email address"))
            return

        if not password:
            self._set_status(self.login_status, self.tr("Please enter your password"))
            return

        self._set_status(self.login_status, self.tr("Logging in..."), "info")
        self.login_button.setEnabled(False)

        remember = self.remember_me.isChecked()

        def on_login_complete(result):
            self.login_button.setEnabled(True)

            if result["success"]:
                self.login_status.setText("")
                self._show_main_page()
            else:
                self._set_status(self.login_status, result["message"])
        
        self.login_thread = _ResultWorker(
            RszCommunityTemplateManager.authenticate,
            email,
            password,
            remember,
        )
        self.login_thread.result_ready.connect(on_login_complete)
        self.login_thread.start()
    
    def _handle_register(self):
        """Handle the register button click"""
        email = self.register_email.text().strip()
        password = self.register_password.text()
        password_confirm = self.register_password_confirm.text()
        display_name = self.register_username.text().strip()
        
        if not email:
            self._set_status(self.register_status, self.tr("Please enter your email address"))
            return

        if not password:
            self._set_status(self.register_status, self.tr("Please enter a password"))
            return

        if password != password_confirm:
            self._set_status(self.register_status, self.tr("Passwords do not match"))
            return

        if len(password) < 6:
            self._set_status(self.register_status, self.tr("Password must be at least 6 characters"))
            return

        self._set_status(self.register_status, self.tr("Registering..."), "info")
        self.register_button.setEnabled(False)

        def on_register_complete(result):
            self.register_button.setEnabled(True)

            if result["success"]:
                self._set_status(
                    self.register_status, self.tr("Registration successful! You can now log in."), "success"
                )
                self.auth_tabs.setCurrentIndex(0)

                self.login_email.setText(email)
                self.login_password.setText("")

            else:
                self._set_status(self.register_status, result["message"])
        
        self.register_thread = _ResultWorker(
            RszCommunityTemplateManager.register_user,
            email,
            password,
            display_name,
        )
        self.register_thread.result_ready.connect(on_register_complete)
        self.register_thread.start()
    
    def _handle_logout(self):
        """Handle the logout button click"""
        RszCommunityTemplateManager.logout()
        self.stacked_widget.setCurrentWidget(self.login_page)
    
    def _show_main_page(self):
        """Show the main page and update UI based on login status"""
        self.stacked_widget.setCurrentWidget(self.main_page)
        
        is_logged_in = RszCommunityTemplateManager.is_authenticated()
        user = RszCommunityTemplateManager.get_current_user()
        
        if is_logged_in and user:
            display_name = user.get("displayName", user.get("email", self.tr("User")))
            self.user_status_label.setText(
                self.tr("Logged in as: {name}").format(name=display_name)
            )
            self.upload_button.setEnabled(True)
            self.star_widget.readonly = False
            self.comment_input.setEnabled(True)
            self.submit_comment_button.setEnabled(True)
            
        self._load_templates()
        
    def _load_templates(self):
        """(Re)load the template list for the selected game."""
        old = getattr(self, "_loader", None)
        if isinstance(old, QThread):
            try:
                if old.isRunning():
                    old.requestInterruption()
                    old.quit()
                    old.wait()
            except RuntimeError:
                pass
            self._loader = None

        self.template_list.clear()
        loading_item = QListWidgetItem(self.tr("Loading…"))
        loading_item.setData(self._SENTINEL_ROLE, True)
        self._mute_item(loading_item)
        self.template_list.addItem(loading_item)

        def show_list(templates):
            self.template_list.clear()
            if not templates:
                empty_item = QListWidgetItem(self.tr("No templates found"))
                empty_item.setData(self._SENTINEL_ROLE, True)
                self._mute_item(empty_item)
                self.template_list.addItem(empty_item)
                return
            for t in templates:
                it = QListWidgetItem(t.get("name", self.tr("Untitled")))
                it.setData(Qt.UserRole, t.get("id"))
                avg = t.get("avgRating") or 0
                it.setToolTip(self.tr(
                    "Rating: {rating:.1f}/5\nDownloads: {downloads}"
                ).format(rating=avg, downloads=t.get("downloadCnt", 0)))
                self.template_list.addItem(it)

        self._loader = _ResultWorker(
            _fetch_community_templates,
            self.game_combo.currentText(),
            self.sort_combo.currentData(),
            self.search_input.text().strip(),
            discard_if_interrupted=True,
        )
        self._loader.result_ready.connect(show_list)
        self._loader.finished.connect(self._loader.deleteLater)
        self._loader.finished.connect(lambda: setattr(self, "_loader", None))
        self._loader.start()

    @staticmethod
    def _mute_item(item):
        item.setFlags(Qt.NoItemFlags)
        item.setForeground(QColor(current_colors()["text_subtle"]))

    def _show_community_template_details(self, template_data):
        self.template_name.setText(
            template_data.get("name", self.tr("Unnamed Template"))
        )

        registry = template_data.get("registry", "default")
        tags = ", ".join(template_data.get("tags", []))
        meta_text = self.tr("Registry: {registry}").format(registry=registry)
        if tags:
            meta_text += self.tr(" | Tags: {tags}").format(tags=tags)
        downloads = template_data.get("downloadCnt", 0)
        meta_text += self.tr(" | Downloads: {downloads}").format(downloads=downloads)

        self.template_description.setText(
            template_data.get("description", self.tr("No description provided"))
        )

        avg_rating = template_data.get("avgRating", 0)
        if avg_rating is None:
            ratings = template_data.get("ratings", [])
            avg_rating = sum(ratings) / len(ratings) if ratings else 0
        rating_count = len(template_data.get("ratings", []))
        self.star_widget.setRating(avg_rating)
        self.rating_count.setText(
            self.tr("({count} ratings)").format(count=rating_count)
        )

        self.registry_label.setText(registry)
        self.tags_label.setText(tags if tags else self.tr("None"))
        uploader = template_data.get("uploaderName") or self.tr("Unknown")
        self.uploader_label.setText(uploader)

        created_at_data = template_data.get("createdAt")
        timestamp = datetime.fromtimestamp(created_at_data["_seconds"])
        created_at = timestamp.strftime("%Y-%m-%d %H:%M")
        meta_text += self.tr(" | Uploaded by: {uploader}").format(uploader=uploader)
        meta_text += self.tr(" on {date}").format(date=created_at)
        self.template_meta.setText(meta_text)
        self.date_label.setText(created_at)
        self.downloads_label.setText(str(template_data.get("downloadCnt", 0)))
        self._load_comments(template_data.get("comments", []))
        self.template_info.setVisible(True)

    def _on_template_selected(self):
        """Handle template selection from list"""
        selected_items = self.template_list.selectedItems()
        if (
            not selected_items
            or selected_items[0].data(self._SENTINEL_ROLE)
        ):
            self.template_info.setVisible(False)
            self.no_template_label.setVisible(True)
            self.current_community_id = None
            return
        
            
        item = selected_items[0]
        community_id = item.data(Qt.UserRole)
        self.current_community_id = community_id
        
        self.no_template_label.setVisible(False)

        old = getattr(self, "detail_thread", None)
        if isinstance(old, QThread):
            try:
                if old.isRunning():
                    old.requestInterruption()
                    old.quit()
                    old.wait()
            except RuntimeError:
                pass 
            self.detail_thread = None   

        self.detail_thread = _ResultWorker(_fetch_template_details, community_id)

        self.detail_thread.finished.connect(self.detail_thread.deleteLater)
        self.detail_thread.finished.connect(
            lambda: setattr(self, "detail_thread", None)  
        )

        self.detail_thread.result_ready.connect(self._show_community_template_details)
        self.detail_thread.start()

    def _clear_comments(self):
        while self.comments_layout.count():
            item = self.comments_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    @staticmethod
    def _comment_time_label(timestamp):
        if not timestamp:
            return None
        try:
            if isinstance(timestamp, dict) and "_seconds" in timestamp:
                value = datetime.fromtimestamp(timestamp["_seconds"])
            elif isinstance(timestamp, (int, float)):
                value = datetime.fromtimestamp(timestamp)
            elif hasattr(timestamp, "strftime"):
                value = timestamp
            else:
                return None
            if not value:
                return None
            return styled_label(value.strftime("%Y-%m-%d %H:%M"), tone="subtle")
        except Exception as e:
            print(f"Error formatting timestamp: {e}")
            return None

    def _comment_frame(self, comment):
        frame = QFrame()
        frame.setProperty("surface", "card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(8)
        username = styled_label(comment.get("username", self.tr("Anonymous")), heading="section")
        header.addWidget(username)
        if time_label := self._comment_time_label(comment.get("timestamp")):
            header.addWidget(time_label)
        header.addStretch()
        layout.addLayout(header)
        text = QLabel(comment.get("text", ""))
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(text)
        return frame

    def _load_comments(self, comments):
        """Load and display comments for the selected template"""
        self._clear_comments()
        
        if not comments:
            no_comments = styled_label(self.tr("No comments yet. Be the first to comment!"), tone="subtle")
            no_comments.setAlignment(Qt.AlignCenter)
            self.comments_layout.addWidget(no_comments)
            return
        
        try:
            from datetime import datetime
            sorted_comments = sorted(comments, key=lambda c: c.get("timestamp", datetime.min), reverse=True)
        except Exception as e:
            print(f"Error sorting comments: {e}")
            sorted_comments = comments
        
        for comment in sorted_comments:
            self.comments_layout.addWidget(self._comment_frame(comment))
        
        self.comments_layout.addStretch()
    
    def _on_sort_changed(self):
        """Handle sort criteria change"""
        self._load_templates()
    
    def _on_search_text_changed(self):
        """Handle search text change with debounce"""
        
        if hasattr(self, "_search_timer"):
            self._search_timer.stop()
        else:
            self._search_timer = QTimer()
            self._search_timer.setSingleShot(True)
            self._search_timer.timeout.connect(self._load_templates)
        
        self._search_timer.start(500)  # 500ms debounce
    
    GAMES = [
        "RE2", "RE2 RT", "RE3", "RE3 RT",
        "RE7", "RE7 RT", "RE8", "RE Resistance",
        "RE4", "Onimusha 2",
        "Street Fighter 6", "Devil May Cry 5",
        "Monster Hunter Rise", "Monster Hunter Wilds", "Monster Hunter Stories 3", "Pragmata",
        "Dragon Dogma 2",
    ]
    

    def _upload_template(self):
        """Select a local template, choose a game, confirm, then upload."""
        if not RszCommunityTemplateManager.is_authenticated():
            QMessageBox.warning(
                self, self.tr(LOGIN_REQUIRED_TITLE),
                self.tr("You must be logged in to upload templates."),
            )
            return

        local_templates = RszTemplateManager.get_template_list()
        if not local_templates:
            QMessageBox.warning(
                self, self.tr("No Templates"),
                self.tr("You don't have any local templates to upload."),
            )
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(self.tr("Upload Template"))
        dlg.setMinimumWidth(450)

        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        lay.addWidget(QLabel(self.tr("<b>Pick the local template</b>:")))

        tpl_list = QListWidget()
        for tpl in local_templates:
            it = QListWidgetItem(tpl.get("name", self.tr("Unnamed")))
            it.setData(Qt.UserRole, tpl.get("id"))
            tpl_list.addItem(it)
        lay.addWidget(tpl_list)

        lay.addSpacing(4)
        lay.addWidget(QLabel(self.tr("<b>Select the game this template belongs to</b>:")))
        game_combo = QComboBox()
        game_combo.addItems(self.GAMES)
        lay.addWidget(game_combo)

        btn_box = QDialogButtonBox()
        cancel_btn = btn_box.addButton(self.tr("Cancel"), QDialogButtonBox.RejectRole)
        upload_btn = btn_box.addButton(self.tr("Upload"), QDialogButtonBox.AcceptRole)
        upload_btn.setProperty("variant", "primary")
        upload_btn.setEnabled(False)
        lay.addSpacing(4)
        lay.addWidget(btn_box)

        tpl_list.itemSelectionChanged.connect(
            lambda: upload_btn.setEnabled(bool(tpl_list.selectedItems()))
        )
        cancel_btn.clicked.connect(dlg.reject)
        upload_btn.clicked.connect(dlg.accept)

        if dlg.exec_() != QDialog.Accepted:
            return

        sel = tpl_list.selectedItems()
        if not sel:
            return
        template_id = sel[0].data(Qt.UserRole)
        template_name = sel[0].text()
        game_id = game_combo.currentText()

        if QMessageBox.question(
                self, self.tr("Confirm Game"),
                self.tr("Upload <b>{template_name}</b> for game <b>{game_id}</b>?").format(
                    template_name=template_name, game_id=game_id
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return

        busy = self._busy_dialog(self.tr("Uploading"), self.tr("Uploading template, please wait…"))

        def _on_done(result: dict):
            busy.close()
            if result.get("success"):
                QMessageBox.information(
                    self, self.tr("Upload Complete"),
                    result.get("message", self.tr("Upload succeeded")),
                )
                self._load_templates()     
            else:
                QMessageBox.warning(
                    self, self.tr("Upload Failed"),
                    result.get("message", self.tr("Unknown error")),
                )

        self.upload_thread = _ResultWorker(
            RszCommunityTemplateManager.upload_template,
            template_id,
            game_id,
        )
        self.upload_thread.result_ready.connect(_on_done)
        self.upload_thread.start()

    def _busy_dialog(self, title, text):
        dialog = QProgressDialog(self)
        dialog.setWindowTitle(title)
        dialog.setLabelText(text)
        dialog.setCancelButton(None)
        dialog.setRange(0, 0)
        dialog.setModal(True)
        dialog.setMinimumWidth(320)
        dialog.show()
        return dialog

    def _download_template(self):
        """Download the selected template"""
        if not self.current_community_id:
            return
            
        loading_dialog = self._busy_dialog(
            self.tr("Downloading"), self.tr("Downloading template from community...")
        )
        
        def on_download_complete(result):
            loading_dialog.close()
            
            if result["success"]:
                QMessageBox.information(self, self.tr("Download Complete"), result["message"])
                self.template_downloaded.emit(result["template_id"])
            else:
                QMessageBox.warning(self, self.tr("Download Failed"), result["message"])
        
        self.download_thread = _ResultWorker(
            RszCommunityTemplateManager.download_template,
            self.current_community_id,
        )
        self.download_thread.result_ready.connect(on_download_complete)
        self.download_thread.start()
    
    def _submit_comment(self):
        """Submit a comment on the selected template"""
        if not RszCommunityTemplateManager.is_authenticated():
            QMessageBox.warning(
                self, self.tr(LOGIN_REQUIRED_TITLE), self.tr("You must be logged in to comment.")
            )
            return
            
        if not self.current_community_id:
            return
            
        comment_text = self.comment_input.toPlainText().strip()
        if not comment_text:
            QMessageBox.warning(
                self, self.tr("Empty Comment"),
                self.tr("Please enter a comment before submitting."),
            )
            return
        
        self.submit_comment_button.setEnabled(False)
        self.submit_comment_button.setText(self.tr("Sending..."))
        
        def on_comment_complete(result):
            self.submit_comment_button.setEnabled(True)
            self.submit_comment_button.setText(self.tr("Submit"))
            
            if result["success"]:
                self.comment_input.clear()
                self._on_template_selected()  
            else:
                QMessageBox.warning(self, self.tr("Comment Failed"), result["message"])
        
        self.comment_thread = _ResultWorker(
            RszCommunityTemplateManager.add_comment,
            self.current_community_id,
            comment_text,
        )
        self.comment_thread.result_ready.connect(on_comment_complete)
        self.comment_thread.start()
    
    def _submit_rating(self, rating):
        """Submit a rating for the selected template"""
        if not RszCommunityTemplateManager.is_authenticated():
            QMessageBox.warning(
                self, self.tr(LOGIN_REQUIRED_TITLE),
                self.tr("You must be logged in to rate templates."),
            )
            return
            
        if not self.current_community_id:
            return
        
        def on_rating_complete(result):
            if result["success"]:
                self._on_template_selected()
            else:
                QMessageBox.warning(self, self.tr("Rating Failed"), result["message"])
        
        old_thread = getattr(self, "rating_thread", None)
        if isinstance(old_thread, QThread) and old_thread.isRunning():
            try:
                old_thread.requestInterruption()
                old_thread.quit()
                old_thread.wait()
            except RuntimeError:
                pass
            
        self.rating_thread = _ResultWorker(
            RszCommunityTemplateManager.add_rating,
            self.current_community_id,
            rating,
        )
        self.rating_thread.result_ready.connect(on_rating_complete)
        self.rating_thread.finished.connect(self.rating_thread.deleteLater)
        self.rating_thread.finished.connect(lambda: setattr(self, "rating_thread", None))
        self.rating_thread.start()
