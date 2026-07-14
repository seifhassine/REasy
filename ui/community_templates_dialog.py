from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QLineEdit, QTextEdit, QPushButton, QComboBox,
    QWidget, QSplitter, QMessageBox, QTabWidget,
    QFrame, QGridLayout, QScrollArea, QStackedWidget,
    QFormLayout, QCheckBox
)

from file_handlers.rsz.rsz_template_manager import RszTemplateManager
from file_handlers.rsz.rsz_community_template_manager import RszCommunityTemplateManager
from datetime import datetime


_NO_RESULT = object()


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


class QStarWidget(QWidget):
    """Custom star rating widget that allows setting and displaying ratings"""
    ratingChanged = Signal(int)
    
    def __init__(self, max_stars=5, initial_rating=0, readonly=False):
        super().__init__()
        self.max_stars = max_stars
        self.rating = initial_rating
        self.readonly = readonly
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        
        self.star_buttons = []
        for i in range(max_stars):
            btn = QPushButton("★")
            btn.setFixedSize(20, 20)
            
            if readonly:
                btn.setEnabled(False)
            else:
                btn.clicked.connect(lambda checked, idx=i+1: self._set_rating(idx))
                
            layout.addWidget(btn)
            self.star_buttons.append(btn)
        
        self.setRating(initial_rating)
    
    def setRating(self, rating):
        self.rating = min(max(0, rating), self.max_stars)
        self._update_stars()
    
    def _set_rating(self, rating):
        if not self.readonly and self.rating != rating:
            self.rating = rating
            self._update_stars()
            self.ratingChanged.emit(rating)
    
    def _update_stars(self):
        filled  = int(self.rating)
        partial = self.rating - filled

        for i, btn in enumerate(self.star_buttons):
            if i < filled:
                btn.setText("★")
                if self.readonly:
                    btn.setStyleSheet("QPushButton { border: none; color: #ffd700; }")
                else:
                    btn.setStyleSheet("QPushButton { border: none; color: #ffd700; }"
                                      "QPushButton:hover { color: #ffcc00; border: 1px solid #ffcc00; }")
            elif i == filled and partial >= 0.5:
                btn.setText("⯨")            
                if self.readonly:
                    btn.setStyleSheet("QPushButton { border: none; color: #ffd700; }")
                else:
                    btn.setStyleSheet("QPushButton { border: none; color: #ffd700; }"
                                      "QPushButton:hover { color: #ffcc00; border: 1px solid #ffcc00; }")
            else:
                btn.setText("☆")
                if self.readonly:
                    btn.setStyleSheet("QPushButton { border: none; color: #ccc; }")
                else:
                    btn.setStyleSheet("QPushButton { border: none; color: #ccc; }"
                                     "QPushButton:hover { color: #ffcc00; border: 1px solid #ffcc00; }")

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
        
        header_layout = QHBoxLayout()
        header_label = QLabel(self.tr("REasy Community Templates"))
        header_label.setStyleSheet("font-size: 24px; font-weight: bold;")
        header_layout.addWidget(header_label, 0, Qt.AlignCenter)
        login_layout.addLayout(header_layout)
        
        form_container = QWidget()
        form_container.setMaximumWidth(400)
        form_layout = QVBoxLayout(form_container)
        
        self.auth_tabs = QTabWidget()
        
        login_tab = QWidget()
        login_tab_layout = QFormLayout(login_tab)
        
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
        login_tab_layout.addRow("", self.login_button)
        
        self.login_status = QLabel("")

        self.login_status.setStyleSheet("color: red;")
        login_tab_layout.addRow("", self.login_status)
        
        self.auth_tabs.addTab(login_tab, self.tr("Login"))
        
        register_tab = QWidget()
        register_tab_layout = QFormLayout(register_tab)
        
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
        register_tab_layout.addRow("", self.register_button)
        
        self.register_status = QLabel("")

        self.register_status.setStyleSheet("color: red;")
        register_tab_layout.addRow("", self.register_status)
        
        self.auth_tabs.addTab(register_tab, self.tr("Register"))
        
        form_layout.addWidget(self.auth_tabs)
        
        login_layout.addWidget(form_container, 0, Qt.AlignCenter)
        login_layout.addStretch()
    
    def _create_main_ui(self):
        """Build the main (post-login) UI."""
        main_layout = QVBoxLayout(self.main_page)

        user_bar = QHBoxLayout()
        self.user_status_label = QLabel()
        user_bar.addWidget(self.user_status_label)
        user_bar.addStretch()
        self.logout_button = QPushButton(self.tr("Logout"))
        user_bar.addWidget(self.logout_button)
        main_layout.addLayout(user_bar)

        header = QHBoxLayout()

        header.addWidget(QLabel(self.tr("Game:")))
        self.game_combo = QComboBox()
        self.game_combo.addItems(self.GAMES)
        header.addWidget(self.game_combo)

        header.addSpacing(15)
        header.addWidget(QLabel(self.tr("Sort:")))
        self.sort_combo = QComboBox()
        self.sort_combo.addItem(self.tr("Rating"),    "rating")
        self.sort_combo.addItem(self.tr("Date"),      "date")
        self.sort_combo.addItem(self.tr("Downloads"), "downloads")
        header.addWidget(self.sort_combo)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(self.tr("Search community templates…"))
        header.addWidget(self.search_input, 1)

        self.upload_button = QPushButton(self.tr("Upload Template"))
        header.addWidget(self.upload_button)
        main_layout.addLayout(header)
        
        splitter = QSplitter(Qt.Horizontal, self.main_page)
        self.template_list = QListWidget()
        splitter.addWidget(self.template_list)
        
        details_widget = QWidget()
        details_layout = QVBoxLayout(details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)
        
        self.template_info = QWidget()
        info_layout = QVBoxLayout(self.template_info)
        
        self.template_name = QLabel()
        self.template_name.setStyleSheet("font-size: 16px; font-weight: bold;")
        info_layout.addWidget(self.template_name)
        
        self.template_meta = QLabel()
        info_layout.addWidget(self.template_meta)
        
        self.template_description = QLabel()
        self.template_description.setWordWrap(True)
        info_layout.addWidget(self.template_description)
        
        rating_frame = QFrame()
        rating_layout = QHBoxLayout(rating_frame)
        rating_layout.setContentsMargins(0, 10, 0, 10)
        
        self.rating_label = QLabel(self.tr("Rating:"))
        rating_layout.addWidget(self.rating_label)
        
        self.star_widget = QStarWidget(5, 0)
        rating_layout.addWidget(self.star_widget)
        
        self.rating_count = QLabel(self.tr("(0 ratings)"))
        rating_layout.addWidget(self.rating_count)
        
        rating_layout.addStretch()
        
        self.download_button = QPushButton(self.tr("Download"))
        rating_layout.addWidget(self.download_button)
        
        info_layout.addWidget(rating_frame)
        
        tabs = QTabWidget()
        
        comments_widget = QWidget()
        comments_layout = QVBoxLayout(comments_widget)
        
        self.comments_area = QScrollArea()
        self.comments_area.setWidgetResizable(True)
        self.comments_container = QWidget()
        self.comments_layout = QVBoxLayout(self.comments_container)
        self.comments_layout.setAlignment(Qt.AlignTop)
        self.comments_area.setWidget(self.comments_container)
        comments_layout.addWidget(self.comments_area)
        
        comment_input_layout = QHBoxLayout()
        self.comment_input = QTextEdit()
        self.comment_input.setPlaceholderText(self.tr("Add your comment..."))
        self.comment_input.setMaximumHeight(80)
        comment_input_layout.addWidget(self.comment_input)
        
        self.submit_comment_button = QPushButton(self.tr("Submit"))
        self.submit_comment_button.setFixedWidth(80)
        comment_input_layout.addWidget(self.submit_comment_button, alignment=Qt.AlignBottom)
        
        comments_layout.addLayout(comment_input_layout)
        
        tabs.addTab(comments_widget, self.tr("Comments"))
        
        metadata_widget = QWidget()
        metadata_layout = QGridLayout(metadata_widget)
        
        metadata_layout.addWidget(QLabel(self.tr("Registry:")), 0, 0)
        self.registry_label = QLabel()
        metadata_layout.addWidget(self.registry_label, 0, 1)
        
        metadata_layout.addWidget(QLabel(self.tr("Tags:")), 1, 0)
        self.tags_label = QLabel()
        metadata_layout.addWidget(self.tags_label, 1, 1)
        
        metadata_layout.addWidget(QLabel(self.tr("Uploaded by:")), 2, 0)
        self.uploader_label = QLabel()
        metadata_layout.addWidget(self.uploader_label, 2, 1)
        
        metadata_layout.addWidget(QLabel(self.tr("Upload date:")), 3, 0)
        self.date_label = QLabel()
        metadata_layout.addWidget(self.date_label, 3, 1)
        
        metadata_layout.addWidget(QLabel(self.tr("Downloads:")), 4, 0)
        self.downloads_label = QLabel()
        metadata_layout.addWidget(self.downloads_label, 4, 1)
        
        tabs.addTab(metadata_widget, self.tr("Details"))
        
        info_layout.addWidget(tabs)
        details_layout.addWidget(self.template_info)
        
        splitter.addWidget(details_widget)
        splitter.setSizes([300, 600])
        
        main_layout.addWidget(splitter)

        self.template_info.setVisible(False)
        
        self.no_template_label = QLabel(self.tr("Select a template from the list to view details"))
        self.no_template_label.setAlignment(Qt.AlignCenter)
        self.no_template_label.setStyleSheet("color: #888; font-size: 14px;")
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
            self.login_status.setText(self.tr("Please enter your email address"))
            return
            
        if not password:
            self.login_status.setText(self.tr("Please enter your password"))
            return
        
        self.login_status.setText(self.tr("Logging in..."))
        self.login_status.setStyleSheet("color: blue;")
        self.login_button.setEnabled(False)
        
        remember = self.remember_me.isChecked()
        
        def on_login_complete(result):
            self.login_button.setEnabled(True)
            
            if result["success"]:
                self.login_status.setText("")
                self._show_main_page()
            else:
                self.login_status.setText(result["message"])
                self.login_status.setStyleSheet("color: red;")
        
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
            self.register_status.setText(self.tr("Please enter your email address"))
            return
            
        if not password:
            self.register_status.setText(self.tr("Please enter a password"))
            return
            
        if password != password_confirm:
            self.register_status.setText(self.tr("Passwords do not match"))
            return
            
        if len(password) < 6:
            self.register_status.setText(self.tr("Password must be at least 6 characters"))
            return
        
        self.register_status.setText(self.tr("Registering..."))
        self.register_status.setStyleSheet("color: blue;")
        self.register_button.setEnabled(False)
        
        def on_register_complete(result):
            self.register_button.setEnabled(True)
            
            if result["success"]:
                self.register_status.setText(self.tr("Registration successful! You can now log in."))
                self.register_status.setStyleSheet("color: green;")
                self.auth_tabs.setCurrentIndex(0)  
                
                self.login_email.setText(email)
                self.login_password.setText("")

            else:
                self.register_status.setText(result["message"])
                self.register_status.setStyleSheet("color: red;")
        
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
        self.template_list.addItem(loading_item)

        def show_list(templates):
            self.template_list.clear()
            if not templates:
                empty_item = QListWidgetItem(self.tr("No templates found"))
                empty_item.setData(self._SENTINEL_ROLE, True)
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
        
        def on_template_details_loaded(template_data):
            self.template_name.setText(template_data.get("name", self.tr("Unnamed Template")))
            
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
                ratings_arr = template_data.get("ratings", [])
                avg_rating = sum(ratings_arr) / len(ratings_arr) if ratings_arr else 0
            rating_count = len(template_data.get("ratings", []))
            self.star_widget.setRating(avg_rating)
            self.rating_count.setText(
                self.tr("({count} ratings)").format(count=rating_count)
            )
            
            self.registry_label.setText(registry)
            self.tags_label.setText(tags if tags else self.tr("None"))

            uploader = (template_data.get("uploaderName")
                        or self.tr("Unknown"))
            self.uploader_label.setText(uploader)

            created_at = template_data.get("createdAt")  
            
            created_at_str = self.tr("Unknown date")
            
            timestamp = datetime.fromtimestamp(created_at["_seconds"])
            created_at_str = timestamp.strftime("%Y-%m-%d %H:%M")
            
            meta_text += self.tr(" | Uploaded by: {uploader}").format(uploader=uploader)
            meta_text += self.tr(" on {date}").format(date=created_at_str)
            self.template_meta.setText(meta_text)

            self.date_label.setText(created_at_str)
            
            self.downloads_label.setText(str(template_data.get("downloadCnt", 0)))
            
            self._load_comments(template_data.get("comments", []))
            
            self.template_info.setVisible(True)
        
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

        self.detail_thread.result_ready.connect(on_template_details_loaded)
        self.detail_thread.start()
    
    def _load_comments(self, comments):
        """Load and display comments for the selected template"""
        while self.comments_layout.count():
            item = self.comments_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        if not comments:
            no_comments = QLabel(self.tr("No comments yet. Be the first to comment!"))
            self.comments_layout.addWidget(no_comments)
            return
        
        try:
            from datetime import datetime
            sorted_comments = sorted(comments, key=lambda c: c.get("timestamp", datetime.min), reverse=True)
        except Exception as e:
            print(f"Error sorting comments: {e}")
            sorted_comments = comments
        
        for comment in sorted_comments:
            comment_frame = QFrame()
            comment_frame.setFrameShape(QFrame.StyledPanel)
            comment_frame.setStyleSheet("border-radius: 5px;")
            
            comment_layout = QVBoxLayout(comment_frame)
            
            header_layout = QHBoxLayout()
            username = QLabel(comment.get("username", self.tr("Anonymous")))
            username.setStyleSheet("font-weight: bold;")
            header_layout.addWidget(username)
            
            timestamp = comment.get("timestamp")
            if timestamp:
                try:
                    if isinstance(timestamp, dict) and "_seconds" in timestamp:
                        dt = datetime.fromtimestamp(timestamp["_seconds"])
                    elif isinstance(timestamp, (int, float)):
                        dt = datetime.fromtimestamp(timestamp)
                    elif hasattr(timestamp, "strftime"):
                        dt = timestamp
                    else:
                        dt = None
                    if dt:
                        date_str = dt.strftime("%Y-%m-%d %H:%M")
                        time_label = QLabel(date_str)
                        time_label.setStyleSheet("color: #888;")
                        header_layout.addWidget(time_label)
                except Exception as e:
                    print(f"Error formatting timestamp: {e}")
            
            header_layout.addStretch()
            comment_layout.addLayout(header_layout)
            
            text = QLabel(comment.get("text", ""))
            text.setWordWrap(True)
            comment_layout.addWidget(text)
            
            self.comments_layout.addWidget(comment_frame)
        
        self.comments_layout.addStretch()
    
    def _on_sort_changed(self):
        """Handle sort criteria change"""
        self._load_templates()
    
    def _on_search_text_changed(self):
        """Handle search text change with debounce"""
        from PySide6.QtCore import QTimer
        
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
                self, self.tr("Login Required"),
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
        lay.addWidget(QLabel(self.tr("<b>Pick the local template</b>:")))

        tpl_list = QListWidget()
        for tpl in local_templates:
            it = QListWidgetItem(tpl.get("name", self.tr("Unnamed")))
            it.setData(Qt.UserRole, tpl.get("id"))
            tpl_list.addItem(it)
        lay.addWidget(tpl_list)

        lay.addWidget(QLabel(self.tr("<b>Select the game this template belongs to</b>:")))
        game_combo = QComboBox()
        game_combo.addItems(self.GAMES)
        lay.addWidget(game_combo)

        btn_box = QHBoxLayout()
        btn_box.addStretch()
        cancel_btn = QPushButton(self.tr("Cancel"))
        upload_btn = QPushButton(self.tr("Upload"))
        upload_btn.setEnabled(False)
        btn_box.addWidget(cancel_btn)
        btn_box.addWidget(upload_btn)
        lay.addLayout(btn_box)

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

        busy = QDialog(self, Qt.WindowTitleHint | Qt.WindowSystemMenuHint)
        busy.setModal(True)
        busy.setWindowTitle(self.tr("Uploading"))
        QVBoxLayout(busy).addWidget(QLabel(self.tr("Uploading template, please wait…")))
        busy.show()

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
    def _download_template(self):
        """Download the selected template"""
        if not self.current_community_id:
            return
            
        loading_dialog = QDialog(self)
        loading_dialog.setWindowTitle(self.tr("Downloading"))
        loading_dialog.setModal(True)
        loading_layout = QVBoxLayout(loading_dialog)
        loading_layout.addWidget(QLabel(self.tr("Downloading template from community...")))
        loading_dialog.show()
        
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
                self, self.tr("Login Required"), self.tr("You must be logged in to comment.")
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
                self, self.tr("Login Required"),
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
