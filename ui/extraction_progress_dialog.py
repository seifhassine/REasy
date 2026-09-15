from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QProgressBar, 
    QPushButton, QHBoxLayout
)
from PySide6.QtCore import Qt, Signal, QTimer, QElapsedTimer, QObject, Slot


class ProgressSignals(QObject):
    progress_update = Signal(int)
    extraction_complete = Signal()
    extraction_error = Signal(str)


class ExtractionProgressDialog(QDialog):
    
    def __init__(self, total_files: int, parent=None):
        super().__init__(parent)
        self.total_files = total_files
        self.completed_files = 0
        self.cancelled = False
        self.signals = ProgressSignals()
        
        self.setWindowTitle(self.tr("Extracting Files"))
        self.setModal(True)
        self.setMinimumWidth(400)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        
        layout = QVBoxLayout()
        
        self.status_label = QLabel(
            self.tr("Extracting {total_files:,} files...").format(total_files=total_files)
        )
        layout.addWidget(self.status_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(total_files)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        
        self.details_label = QLabel(
            self.tr("{completed_files:,} / {total_files:,} files").format(
                completed_files=0, total_files=total_files
            )
        )
        layout.addWidget(self.details_label)
        
        self.time_label = QLabel(self.tr("Time elapsed: 00:00"))
        layout.addWidget(self.time_label)
        
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        self.cancel_button = QPushButton(self.tr("Cancel"))
        self.cancel_button.clicked.connect(self.cancel_extraction)
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)
        
        self.setLayout(layout)
        
        self.elapsed_timer = QElapsedTimer()
        self.elapsed_timer.start()
        
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_elapsed_time)
        self.update_timer.start(100)
        
        self.signals.progress_update.connect(self.update_progress)
        self.signals.extraction_complete.connect(self.on_extraction_complete)
        self.signals.extraction_error.connect(self.on_extraction_error)
    
    @Slot(int)
    def update_progress(self, files_completed: int):
        self.completed_files += files_completed
        self.progress_bar.setValue(self.completed_files)
        
        percent = (self.completed_files * 100) // self.total_files
        self.details_label.setText(self.tr(
            "{completed_files:,} / {total_files:,} files ({percent}%)"
        ).format(
            completed_files=self.completed_files,
            total_files=self.total_files,
            percent=percent,
        ))
        
        if self.completed_files >= self.total_files:
            self.status_label.setText(self.tr("Extraction complete!"))
            self.cancel_button.setText(self.tr("Close"))
    
    def update_elapsed_time(self):
        elapsed_ms = self.elapsed_timer.elapsed()
        elapsed_sec = elapsed_ms // 1000
        minutes = elapsed_sec // 60
        seconds = elapsed_sec % 60
        self.time_label.setText(self.tr(
            "Time elapsed: {minutes:02d}:{seconds:02d}"
        ).format(minutes=minutes, seconds=seconds))
    
    def cancel_extraction(self):
        if self.completed_files >= self.total_files:
            self.accept()
        else:
            self.cancelled = True
            self.reject()
    
    @Slot()
    def on_extraction_complete(self):
        self.update_timer.stop()
        elapsed_ms = self.elapsed_timer.elapsed()
        elapsed_sec = elapsed_ms // 1000
        minutes = elapsed_sec // 60
        seconds = elapsed_sec % 60
        self.time_label.setText(self.tr(
            "Time elapsed: {minutes:02d}:{seconds:02d}"
        ).format(minutes=minutes, seconds=seconds))
        
        self.status_label.setText(self.tr("Extraction complete!"))
        self.cancel_button.setText(self.tr("Close"))
        self.progress_bar.setValue(self.total_files)
        self.details_label.setText(self.tr(
            "{completed_files:,} / {total_files:,} files (100%)"
        ).format(completed_files=self.total_files, total_files=self.total_files))
        self.completed_files = self.total_files
    
    @Slot(str)
    def on_extraction_error(self, error_msg: str):
        self.update_timer.stop()
        self.status_label.setText(self.tr("Error: {error}").format(error=error_msg))
        self.cancel_button.setText(self.tr("Close"))
    
    def closeEvent(self, event):
        if self.completed_files < self.total_files:
            self.cancelled = True
        self.update_timer.stop()
        super().closeEvent(event)
