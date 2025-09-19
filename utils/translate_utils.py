import json
from collections import deque
from PySide6.QtCore import QObject, QUrl, QUrlQuery, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PySide6.QtWidgets import QMessageBox

class TranslationManager(QObject):
    """Utility class for translating text using Google Translate API"""
    
    translation_completed = Signal(str, object) 
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.network_manager = None
        self.default_target_language = "en"
        
    def translate_text(self, text, source_lang="auto", target_lang=None, context=None):
        """
        Translate text using Google Translate API
        
        Args:
            text: Text to translate
            source_lang: Source language code (default: auto-detect)
            target_lang: Target language code (overrides default if provided)
            context: Optional context object passed back in the signal
        """
        if not text or text.strip() == "":
            return False
 
        if not self.network_manager:
            self.network_manager = QNetworkAccessManager(self)
            self.network_manager.finished.connect(self._handle_translation_response)
        
        self.current_context = context
        self.current_text = text
        
        base_url = "https://translate.googleapis.com/translate_a/single"
        query = QUrlQuery()
        query.addQueryItem("client", "gtx")
        query.addQueryItem("sl", source_lang)
        query.addQueryItem("tl", target_lang)
        query.addQueryItem("dt", "t")
        query.addQueryItem("q", text)
        
        url = QUrl(base_url)
        url.setQuery(query)
        
        request = QNetworkRequest(url)
        self.network_manager.get(request)
        return True
        
    def _handle_translation_response(self, reply):
        """Handle the response from the translation API"""
        if reply.error() != QNetworkReply.NoError:
            self.translation_completed.emit("", self.current_context)
            reply.deleteLater()
            return
        
        try:
            response_data = reply.readAll().data().decode('utf-8')
            json_data = json.loads(response_data)
            
            # The response format is a nested array where each entry contains a translated segment.
            translation = ""
            if json_data and isinstance(json_data, list) and json_data:
                segments = json_data[0] if isinstance(json_data[0], list) else []
                translated_parts = []
                for segment in segments:
                    if isinstance(segment, list) and segment:
                        part = segment[0]
                        if isinstance(part, str):
                            translated_parts.append(part)
                if translated_parts:
                    translation = "".join(translated_parts)

            if translation:
                self.translation_completed.emit(translation, self.current_context)
            else:
                self.translation_completed.emit("", self.current_context)

        except Exception as e:
            print(f"Error processing translation: {str(e)}")
            self.translation_completed.emit("", self.current_context)

        reply.deleteLater()


class TranslationBatcher(QObject):
    """Minimal helper that batches translation requests under a size limit."""

    def __init__(
        self,
        translation_manager: TranslationManager,
        parent=None,
        *,
        char_limit: int = 2500,
        marker_prefix: str = "__REASY_ENTRY_",
        marker_suffix: str = "__::",
    ):
        super().__init__(parent)
        self.translation_manager = translation_manager
        self.char_limit = char_limit
        self.marker_prefix = marker_prefix
        self.marker_suffix = marker_suffix

        self._batches = deque()
        self._stats = None
        self._apply = None
        self._finish = None
        self._active = False

    def is_running(self) -> bool:
        return self._active

    def start(
        self,
        entries,
        target_lang,
        apply_callback,
        finish_callback,
        *,
        initial_skipped: int = 0,
    ):
        if self._active:
            return False, {"error": "A batch translation is already running.", "skipped": initial_skipped}

        batches, overflow_skipped = self._prepare_batches(entries, target_lang)
        skipped = initial_skipped + overflow_skipped

        if not batches:
            return False, {"error": "Nothing to translate.", "skipped": skipped}

        self._batches = deque(batches)
        total = sum(len(batch["entries"]) for batch in batches)
        self._stats = {"total": total, "success": 0, "failed": 0, "skipped": skipped, "requests": 0}
        self._apply = apply_callback
        self._finish = finish_callback
        self._active = True

        self._dispatch_next()
        return True, {"skipped": skipped}

    def handle_response(self, translated_text, context) -> bool:
        if not context or context.get("batch_runner") is not self:
            return False

        entries = context.get("entries") or []
        entry_count = len(entries)

        if not self._stats:
            self._dispatch_next()
            return True

        if not translated_text:
            self._stats["failed"] += entry_count
            self._dispatch_next()
            return True

        values = self._extract_values(translated_text, entry_count)
        if not values or len(values) != entry_count:
            self._stats["failed"] += entry_count
            self._dispatch_next()
            return True

        success = 0
        for entry, value in zip(entries, values):
            if not value.strip():
                continue
            if self._apply and not self._apply(entry, value):
                continue
            success += 1

        self._stats["success"] += success
        self._stats["failed"] += entry_count - success
        self._dispatch_next()
        return True

    # Internal helpers -------------------------------------------------

    def _dispatch_next(self):
        if not self._batches:
            self._finish_run()
            return

        batch = self._batches.popleft()
        entries = batch["entries"]
        target_lang = batch.get("target_lang") or self.translation_manager.default_target_language
        context = {"batch_runner": self, "entries": entries}
        success = self.translation_manager.translate_text(
            text=batch["text"],
            source_lang="auto",
            target_lang=target_lang,
            context=context,
        )

        if success:
            self._stats["requests"] += 1
        else:
            self._stats["failed"] += len(entries)
            self._dispatch_next()

    def _finish_run(self):
        stats = dict(self._stats) if self._stats else {}

        self._batches.clear()
        self._stats = None
        self._apply = None
        finish = self._finish
        self._finish = None
        self._active = False

        if finish:
            finish(stats)

    def _prepare_batches(self, entries, target_lang):
        batches = []
        current_entries = []
        current_lines = []
        current_length = 0
        overflow_skipped = 0

        for entry in entries:
            text = (entry.get("text") or "").strip()
            marker = self._build_marker(len(current_entries))
            line = f"{marker} {text}" if text else marker
            line_length = len(line)

            if line_length > self.char_limit:
                overflow_skipped += 1
                continue

            projected = line_length if not current_lines else current_length + 1 + line_length

            if current_entries and projected > self.char_limit:
                batches.append(self._make_batch(current_entries, target_lang, current_lines))
                current_entries = []
                current_lines = []
                current_length = 0
                marker = self._build_marker(0)
                line = f"{marker} {text}" if text else marker
                line_length = len(line)
                if line_length > self.char_limit:
                    overflow_skipped += 1
                    continue
                projected = line_length

            current_entries.append(entry)
            current_lines.append(line)
            current_length = projected

        if current_entries:
            batches.append(self._make_batch(current_entries, target_lang, current_lines))

        return batches, overflow_skipped

    def _make_batch(self, entries, target_lang, prepared_lines):
        return {
            "entries": list(entries),
            "target_lang": target_lang,
            "text": "\n".join(prepared_lines),
        }

    def _extract_values(self, translated_text, count):
        normalized = translated_text.replace("\r\n", "\n").replace("\r", "\n")
        pos = 0
        values = []

        for idx in range(count):
            marker = self._build_marker(idx)
            start = normalized.find(marker, pos)
            if start == -1:
                return None
            start += len(marker)
            if start < len(normalized) and normalized[start] == " ":
                start += 1

            next_marker = self._build_marker(idx + 1) if idx + 1 < count else None
            end = normalized.find(next_marker, start) if next_marker else len(normalized)
            if end == -1:
                end = len(normalized)

            value = normalized[start:end].strip()
            values.append(value)
            pos = end

        return values

    def _build_marker(self, index: int) -> str:
        return f"{self.marker_prefix}{index}{self.marker_suffix}"

def show_translation_error(parent, message):
    """Show translation error dialog"""
    QMessageBox.warning(parent, "Translation Error", message)

def show_translation_result(parent, translation):
    """Show translation result dialog"""
    QMessageBox.information(parent, "Translation", f"Translation: {translation}")
