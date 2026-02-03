from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict
import hashlib
import json
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from firebase.config.firebase_config import FirebaseConfig


_AUTH_PATH = os.path.join(
    Path.home(),
    ".reasy_auth.json" if os.name != "nt" else
    os.path.join(Path.home(), "AppData", "Roaming", "REasy", "auth.json")
)

class RszCommunityTemplateManager:
    REGION: str = "us-east1"
    FUNCTION_PREFIX: str = "api"

    _is_authenticated: bool = False
    _current_user: Dict[str, Any] | None = None
    _auth_token: str | None = None

    @classmethod
    def _cf_root(cls) -> str:
        proj = FirebaseConfig.get_config()["projectId"]
        return f"https://{cls.REGION}-{proj}.cloudfunctions.net"

    @classmethod
    def _url(cls, path: str) -> str:
        prefix = f"/{cls.FUNCTION_PREFIX}" if cls.FUNCTION_PREFIX else ""
        return f"{cls._cf_root()}{prefix}{path}"

    @staticmethod
    def _session() -> requests.Session:
        s = requests.Session()
        retry = Retry(total=5, backoff_factor=1.5,
                      status_forcelist=[408, 429, 500, 502, 503, 504],
                      allowed_methods=["GET", "POST"])
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://",  HTTPAdapter(max_retries=retry))
        return s
    
    @classmethod
    def _save_refresh_token(cls, token: str):
        try:
            os.makedirs(os.path.dirname(_AUTH_PATH), exist_ok=True)
            with open(_AUTH_PATH, "w", encoding="utf-8") as fh:
                json.dump({"refreshToken": token}, fh)
        except Exception as e:
            print("Warning: could not store refresh token:", e)

    @classmethod
    def _delete_refresh_token(cls):
        try:
            if os.path.exists(_AUTH_PATH):
                os.remove(_AUTH_PATH)
        except Exception as e:
            print("Warning: could not delete refresh token:", e)

    @classmethod
    def _refresh_with_token(cls, refresh_token: str, remember: bool = False) -> bool:
        try:
            cfg  = FirebaseConfig.get_config()
            url  = f"https://securetoken.googleapis.com/v1/token?key={cfg['apiKey']}"
            r    = requests.post(url, data={
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh_token,
                }, timeout=20)
            res  = r.json()
            if r.status_code != 200 or "id_token" not in res:
                return False

            cls._is_authenticated = True
            cls._auth_token       = res["id_token"]
            cls._current_user = {
                "uid":          res["user_id"],
                "email":        "",
                "displayName":  "",
                "refreshToken": res["refresh_token"],
            }

            if remember:
                cls._save_refresh_token(res["refresh_token"])
            else:
                cls._delete_refresh_token()
            return True
        except Exception as e:
            print("Silent session restore failed:", e)
            return False

    @classmethod
    def restore_session(cls):
        if cls._is_authenticated:
            return
        try:
            if not os.path.exists(_AUTH_PATH):
                return
            with open(_AUTH_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            token = data.get("refreshToken")
            if token:
                cls._refresh_with_token(token, remember=True)
        except Exception as e:
            print("Could not restore saved login:", e)
            
    @classmethod
    def authenticate(cls, email: str, password: str, remember: bool = False) -> dict:
        try:
            cfg = FirebaseConfig.get_config()
            api_key = cfg["apiKey"]

            sign_in_url = (
                f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
            )
            r = requests.post(
                sign_in_url,
                json={"email": email, "password": password, "returnSecureToken": True},
                timeout=30,
            )
            data = r.json()
            if r.status_code != 200 or "error" in data:
                msg = data.get("error", {}).get("message", "Unknown error")
                return {"success": False, "message": f"Authentication failed: {msg}"}

            id_token  = data["idToken"]
            refresh_t = data["refreshToken"]
            local_id  = data["localId"]

            lookup_url = (
                f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={api_key}"
            )
            r2 = requests.post(lookup_url, json={"idToken": id_token}, timeout=30)
            info = r2.json()
            if r2.status_code != 200 or "users" not in info:
                return {"success": False, "message": "Unable to confirm account state."}

            user_info = info["users"][0]
            if not user_info.get("emailVerified", False):
                verify_url = (
                    f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={api_key}"
                )
                requests.post(
                    verify_url,
                    json={"requestType": "VERIFY_EMAIL", "idToken": id_token},
                    timeout=30,
                )
                return {
                    "success": False,
                    "message": "Please verify your e-mail first – "
                            "we've just sent you a new verification link.",
                }

            cls._is_authenticated = True
            cls._auth_token       = id_token
            cls._current_user = {
                "uid":         local_id,
                "email":       email,
                "displayName": user_info.get("displayName", email.split("@")[0]),
                "refreshToken": refresh_t,
            }
            if remember:
                cls._save_refresh_token(refresh_t)
            else:
                cls._delete_refresh_token()
            return {"success": True, "user": cls._current_user}

        except Exception as exc:
            return {"success": False, "message": f"Login error: {exc}"}


    @classmethod
    def get_current_user(cls):
        return cls._current_user

    @classmethod
    def is_authenticated(cls) -> bool:
        return cls._is_authenticated
    
    @classmethod
    def logout(cls):
        cls._is_authenticated = False
        cls._current_user, cls._auth_token = None, None
        cls._delete_refresh_token()

    @classmethod
    def register_user(cls, email: str, password: str, display_name: str = "") -> dict:
        try:
            cfg = FirebaseConfig.get_config()
            api_key = cfg["apiKey"]

            url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={api_key}"
            payload = {
                "email": email,
                "password": password,
                "returnSecureToken": True,
            }
            r = requests.post(url, json=payload, timeout=30)
            data = r.json()
            if r.status_code != 200 or "error" in data:
                msg = data.get("error", {}).get("message", "unknown error")
                return {"success": False, "message": f"Registration failed: {msg}"}

            if display_name:
                token = data["idToken"]
                url2 = f"https://identitytoolkit.googleapis.com/v1/accounts:update?key={api_key}"
                requests.post(
                    url2,
                    json={"idToken": token, "displayName": display_name, "returnSecureToken": False},
                    timeout=30,
                )
            verify_url = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={api_key}"
            requests.post(verify_url,
                        json={"requestType": "VERIFY_EMAIL", "idToken": data["idToken"]},
                        timeout=30)

            return {"success": True, "message": "Registration successful"}
        except Exception as exc:
            return {"success": False, "message": f"Registration error: {exc}"}

    @classmethod
    def _upload_to_storage(cls, blob: bytes, object_name: str) -> dict:
        bucket = FirebaseConfig.get_config()["storageBucket"]
        url = ("https://firebasestorage.googleapis.com/v0/b/" f"{bucket}/o"
               f"?uploadType=media&name={object_name}")
        hdrs = {"Authorization": f"Firebase {cls._auth_token}",
                "Content-Type": "application/json"}
        r = requests.post(url, headers=hdrs, data=blob, timeout=60)
        if r.status_code != 200:
            try:
                msg = r.json()["error"]["message"]
            except Exception as e:
                print(f"Error parsing error message: {e}")
                msg = r.text
            return {"success": False, "message": f"Storage upload failed: {msg}"}
        return {"success": True, "meta": r.json()}
    
    @classmethod
    def _get_session_with_retry(cls):
        return cls._session()
    
    @classmethod
    def get_api_base_url(cls) -> str:
        base = cls._cf_root()
        if cls.FUNCTION_PREFIX:
            base += f"/{cls.FUNCTION_PREFIX}"
        return base

    @classmethod
    def get_community_templates(cls, *, sort_by="rating",
                                game=None, tag=None, registry=None,
                                search=None, limit=50):
        params = {"sortBy": sort_by, "limit": limit}
        if game:
            params["game"] = game
        if tag:
            params["tag"] = tag
        if registry:
            params["registry"] = registry
        if search:
            params["search"] = search

        r = cls._session().get(
            cls._url("/templates"), params=params,
            headers={"Authorization": f"Bearer {cls._auth_token}"} if cls._auth_token else {},
            timeout=30,
        )
        if r.status_code != 200:
            return []
        return r.json().get("templates", [])

    @classmethod
    def upload_template(cls, template_id: str, game: str) -> dict:
        if not cls.is_authenticated():
            return {"success": False, "message": "Please login first."}

        ALLOWED = {
        "RE2", "RE2 RT", "RE3", "RE3 RT",
        "RE7", "RE7 RT", "RE8", "RE Resistance",
        "RE4", "Onimusha 2",
        "Street Fighter 6", "Devil May Cry 5",
        "Monster Hunter Rise", "Monster Hunter Wilds", "Pragmata",
        "Dragon Dogma 2"}
        if game not in ALLOWED:
            return {"success": False, "message": "Unknown game id."}

        from file_handlers.rsz.rsz_template_manager import RszTemplateManager
        meta = RszTemplateManager.load_metadata()
        tdata = meta.get("templates", {}).get(template_id)
        if not tdata:
            return {"success": False, "message": "Template not found."}

        file_path = tdata["path"]
        registry = tdata["registry"]
        if not os.path.exists(file_path):
            return {"success": False, "message": "File missing on disk."}

        blob = Path(file_path).read_bytes()
        if len(blob) > 5 * 1024 * 1024:
            return {"success": False, "message": "File > 5 MB."}

        uid = cls._current_user["uid"]
        object_name = f"user_uploads/{uid}/{os.path.basename(file_path)}"
        st = cls._upload_to_storage(blob, object_name)
        if not st["success"]:
            return st

        payload = {
            "name":        tdata.get("name", "Unnamed Template"),
            "description": tdata.get("description", ""),
            "tags":        tdata.get("tags", []),
            "registry":    registry,
            "gsPath":      object_name,
            "game":        game,
        }
        r = requests.post(cls._url("/templates"), json=payload,
                          headers={"Authorization": f"Bearer {cls._auth_token}"},
                          timeout=30)
        if r.status_code not in (200, 201):
            msg = (r.json().get("message") if r.headers.get("content-type") == "application/json"
                   else r.text)
            return {"success": False, "message": f"Upload failed: {msg}"}

        return {"success": True,
                "community_id": r.json().get("id"),
                "message": "Template uploaded."}

    @classmethod
    def download_template(cls, community_id: str) -> dict:
        if not cls.is_authenticated():
            return {"success": False,
                    "message": "Please log in before downloading."}

        try:
            api_dl = f"{cls._url(f'/templates/{community_id}/download')}"
            hdrs   = {"Authorization": f"Bearer {cls._auth_token}"}
            r      = cls._session().post(api_dl, headers=hdrs, timeout=30)

            if r.status_code == 429:
                return {"success": False, "message": r.json().get("message", "Download limit reached")}
            if r.status_code != 200:
                return {"success": False, "message": "Download request failed"}

            signed_url = r.json().get("fileUrl")
            if not signed_url:
                return {"success": False, "message": "No URL returned by server"}

            file_r = cls._session().get(signed_url, timeout=60)
            if file_r.status_code != 200:
                return {"success": False, "message": "Could not fetch file"}

            blob = file_r.content

            from file_handlers.rsz.rsz_template_manager import RszTemplateManager

            md5  = hashlib.md5(blob).hexdigest()
            meta = json.loads(blob.decode("utf-8"))
            tname = meta.get("name", "Downloaded Template")

            registry = meta.get("registry", "default")
            dir_ = RszTemplateManager.get_template_directory(registry)
            os.makedirs(dir_, exist_ok=True)

            safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in tname).strip()
            path = os.path.join(dir_, f"{safe}.json")
            i = 1
            while os.path.exists(path):
                path = os.path.join(dir_, f"{safe}_{i}.json")
                i += 1

            Path(path).write_bytes(blob)

            cat = RszTemplateManager.load_metadata()
            cat.setdefault("templates", {})[md5] = {
                "name": tname,
                "description": meta.get("description", ""),
                "tags": meta.get("tags", []),
                "registry": registry,
                "path": path,
                "created": datetime.now().isoformat(),
                "modified": datetime.now().isoformat(),
                "community_id": community_id,
            }
            RszTemplateManager.save_metadata(cat)

            return {"success": True,
                    "template_id": md5,
                    "message": f"Template '{tname}' downloaded."}

        except Exception as exc:
            return {"success": False, "message": f"Download error: {exc}"}

    @classmethod
    def add_rating(cls, community_id, rating):
        if not cls.is_authenticated():
            return {"success": False, "message": "You must be logged in to rate templates"}
            
        if rating < 1 or rating > 5:
            return {"success": False, "message": "Rating must be between 1 and 5"}
            
        try:
            api_url = f"{cls.get_api_base_url()}/templates/{community_id}/rate"
            
            payload = {
                "rating": rating
            }
            
            headers = {
                "Authorization": f"Bearer {cls._auth_token}",
                "Content-Type": "application/json"
            }
            
            response = requests.post(api_url, json=payload, headers=headers)
            
            if response.status_code != 200:
                error_message = "Rating failed"
                try:
                    error_data = response.json()
                    if "message" in error_data:
                        error_message = error_data["message"]
                except Exception as e:
                    print(f"Error parsing error message: {e}")
                return {"success": False, "message": error_message}
                
            return {"success": True, "message": f"Rating ({rating}/5) submitted"}
            
        except Exception as e:
            print(f"Error adding rating: {e}")
            return {"success": False, "message": f"Rating error: {str(e)}"}
    
    @classmethod
    def add_comment(cls, community_id, comment_text):
        if not cls.is_authenticated():
            return {"success": False, "message": "You must be logged in to comment"}
            
        if not comment_text.strip():
            return {"success": False, "message": "Comment cannot be empty"}
            
        try:
            api_url = f"{cls.get_api_base_url()}/templates/{community_id}/comment"
            
            payload = {
                "text": comment_text
            }
            
            headers = {
                "Authorization": f"Bearer {cls._auth_token}",
                "Content-Type": "application/json"
            }
            
            response = requests.post(api_url, json=payload, headers=headers)
            
            if response.status_code != 200:
                error_message = "Comment failed"
                try:
                    error_data = response.json()
                    if "message" in error_data:
                        error_message = error_data["message"]
                except Exception as e:
                    print(f"Error parsing error message: {e}")
                return {"success": False, "message": error_message}
                
            return {"success": True, "message": "Comment added successfully"}
            
        except Exception as e:
            print(f"Error adding comment: {e}")
            return {"success": False, "message": f"Comment error: {str(e)}"}