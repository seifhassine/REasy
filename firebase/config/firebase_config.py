"""Firebase configuration for REasy community template sharing functionality."""

import os


class FirebaseConfig:
    """Static helper that surfaces the Firebase-Web SDK config used across the app.

    The content is **public‑key material only** – it is safe to ship in any client.
    """

    _FIREBASE_CONFIG = {
        "apiKey":            "change-me",
        "authDomain":        "reasy-community.firebaseapp.com",
        "projectId":         "reasy-community",
        "storageBucket":     "reasy-community.firebasestorage.app", 
        "messagingSenderId": "825197408046",
        "appId":             "change-me",
        "measurementId":     "G-7GBQ31P3L7",
    }

    @staticmethod
    def get_config():
        config = dict(FirebaseConfig._FIREBASE_CONFIG)  
        return config