from __future__ import annotations

import keyring

SERVICE_NAME = "REasy AI Assistant"
ACCOUNT_NAME = "DeepSeek API key"


class CredentialStoreError(RuntimeError):
    pass


class DeepSeekCredentialStore:
    """Store the DeepSeek key in Windows Credential Locker or Linux Secret Service."""

    unavailable_reason = "No supported operating-system keyring is available."

    def __init__(self, backend=keyring):
        self._backend = backend
        self.available = self._backend_is_usable()

    def _backend_is_usable(self) -> bool:
        if self._backend is None:
            return False
        get_keyring = getattr(self._backend, "get_keyring", None)
        if get_keyring is None:
            return True
        try:
            return float(getattr(get_keyring(), "priority", 0)) > 0
        except Exception:
            return False

    def load(self) -> str | None:
        key = self._call("get_password")
        return str(key).strip() if key else None

    def save(self, key: str) -> None:
        key = str(key or "").strip()
        if not key:
            raise CredentialStoreError("Enter an API key before remembering it.")
        self._call("set_password", key)

    def delete(self) -> None:
        self._call("delete_password")

    def _call(self, method: str, *args):
        if not self.available:
            raise CredentialStoreError(self.unavailable_reason)
        try:
            return getattr(self._backend, method)(SERVICE_NAME, ACCOUNT_NAME, *args)
        except Exception as exc:
            raise CredentialStoreError(
                "The operating-system keyring could not be accessed."
            ) from exc
