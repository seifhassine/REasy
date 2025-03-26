import json
from PySide6.QtCore import QObject, QUrl, QUrlQuery, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PySide6.QtWidgets import QMessageBox

class TranslationManager(QObject):
    """Utility class for translating text using Google Translate API"""
    
    translation_completed = Signal(str, str, object) 
    
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
            self.translation_completed.emit(self.current_text, "", self.current_context)
            reply.deleteLater()
            return
        
        try:
            response_data = reply.readAll().data().decode('utf-8')
            json_data = json.loads(response_data)
            
            # The response format is a nested array where the translation is in json_data[0][0][0]
            if json_data and isinstance(json_data, list) and len(json_data) > 0 and isinstance(json_data[0], list) and len(json_data[0]) > 0:
                translation = json_data[0][0][0]
                self.translation_completed.emit(self.current_text, translation, self.current_context)
            else:
                self.translation_completed.emit(self.current_text, "", self.current_context)
                
        except Exception as e:
            print(f"Error processing translation: {str(e)}")
            self.translation_completed.emit(self.current_text, "", self.current_context)
        
        reply.deleteLater()

def show_translation_error(parent, message):
    """Show translation error dialog"""
    QMessageBox.warning(parent, "Translation Error", message)

def show_translation_result(parent, translation):
    """Show translation result dialog"""
    QMessageBox.information(parent, "Translation", f"Translation: {translation}")
