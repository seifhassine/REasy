from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

from PySide6.QtCore import QObject, Signal

lazy from utils.app_paths import application_root

_BOOKMARKS_VERSION = 1
_BOOKMARK_SCOPES = frozenset(("pak", "project", "unpacked"))
_BookmarkKey = tuple[str, str, str]
_TAG_MAX_LEN = 24
_NATIVE_PREFIX = re.compile(r"^natives/[^/]+/", re.IGNORECASE)

BOOKMARKS_PROJECT_NAME = ".reasy_bookmarks.json"


def display_bookmark_path(path: str) -> str:
    """``path`` without its natives/<platform>/ folder, as the browser trees show it."""
    return _NATIVE_PREFIX.sub("", path, count=1) or path


def bookmarks_path() -> Path:
    return application_root() / "bookmarks.json"


def project_bookmarks_path(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / BOOKMARKS_PROJECT_NAME


def normalize_tag(tag: object) -> str:
    return re.sub(r"\s+", " ", str(tag).strip()).lower()[:_TAG_MAX_LEN].strip()


def normalize_tags(tags) -> tuple[str, ...]:
    if tags is None:
        tags = ()
    elif isinstance(tags, str):
        tags = (tags,)
    elif not isinstance(tags, (list, tuple)):
        raise ValueError("Bookmark tags must be a list of strings.")
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags:
        if not isinstance(raw, str):
            raise ValueError("Bookmark tags must be strings.")
        tag = normalize_tag(raw)
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return tuple(out)


def normalize_note(note: object) -> str:
    if note is None:
        return ""
    if not isinstance(note, str):
        raise ValueError("Bookmark note must be text.")
    return note.strip()


def normalize_pak_path(path: str) -> str:
    """Normalize a PAK path; a single trailing '/' marks a folder bookmark."""
    raw = str(path).replace("\\", "/").strip()
    is_folder = raw.endswith("/")
    parts = [part for part in raw.split("/") if part and part != "."]
    if ".." in parts:
        raise ValueError("Bookmark paths cannot contain '..'.")
    return "/".join(parts).lower() + ("/" if is_folder else "")


def normalize_relative_path(path: str) -> str:
    raw = str(path).replace("\\", "/").strip()
    if raw.startswith("/") or re.match(r"^[a-zA-Z]:", raw):
        raise ValueError("Filesystem bookmark paths must be relative.")
    if raw in (".", "./"):
        return "."
    parts = [part for part in raw.split("/") if part and part != "."]
    if ".." in parts:
        raise ValueError("Bookmark paths cannot contain '..'.")
    return "/".join(parts)


def normalize_scope(scope: object) -> str:
    normalized = str(scope or "").strip().lower()
    if normalized not in _BOOKMARK_SCOPES:
        raise ValueError(f"Unsupported bookmark scope: {normalized or '<empty>'}")
    return normalized


def normalize_bookmark_path(scope: str, path: object) -> str:
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if not isinstance(path, str):
        raise ValueError("Bookmark path must be text.")
    normalized = (
        normalize_pak_path(path)
        if scope == "pak"
        else normalize_relative_path(path)
    )
    if not normalized:
        raise ValueError("Bookmark path cannot be empty.")
    return normalized


def _timestamp(value: object, field_name: str) -> float:
    try:
        result = 0.0 if value is None or value == "" else float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field_name} timestamp.") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Invalid {field_name} timestamp.")
    return result


def normalize_root(value: str | os.PathLike | None) -> str:
    """Normalize a path for comparing bookmark roots with active directories."""
    if not value:
        return ""
    return os.path.normcase(os.path.abspath(os.path.expanduser(os.fspath(value))))


def root_to_relative(value: str | os.PathLike | None) -> str:
    """Store a project root relative to the REasy working folder."""
    if not value:
        return ""
    abs_value = normalize_root(value)
    base = normalize_root(application_root())
    try:
        rel = os.path.relpath(abs_value, base)
    except ValueError:
        return abs_value.replace("\\", "/")
    return "" if rel == "." else rel.replace("\\", "/")


def root_to_absolute(value: str | os.PathLike | None) -> str:
    if not value:
        return ""
    raw = os.fspath(value)
    if os.path.isabs(raw):
        return normalize_root(raw)
    return normalize_root(os.path.join(application_root(), raw))


def _query_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quoted = False
    for character in text:
        if character == '"':
            quoted = not quoted
            if not quoted:
                tokens.append("".join(current))
                current = []
        elif character.isspace() and not quoted:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(character)
    if current or quoted:
        tokens.append("".join(current))
    return [token for token in tokens if token]


class BookmarkQuery(NamedTuple):
    terms: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    excluded_tags: tuple[str, ...] = ()

    def matches(self, bookmark: "Bookmark", haystack: str) -> bool:
        if any(tag not in bookmark.tags for tag in self.tags):
            return False
        if any(tag in bookmark.tags for tag in self.excluded_tags):
            return False
        if any(term not in haystack for term in self.terms):
            return False
        return not any(term in haystack for term in self.excluded)


def parse_query(text: str) -> BookmarkQuery:
    terms: list[str] = []
    tags: list[str] = []
    excluded: list[str] = []
    excluded_tags: list[str] = []
    for token in _query_tokens(text or ""):
        negative = token.startswith("-") and len(token) > 1
        body = token[1:] if negative else token
        if body[:4].lower() == "tag:":
            names = (normalize_tag(part) for part in body[4:].split(","))
            (excluded_tags if negative else tags).extend(name for name in names if name)
        elif negative:
            excluded.append(body.lower())
        else:
            terms.append(body.lower())
    return BookmarkQuery(tuple(terms), tuple(tags), tuple(excluded), tuple(excluded_tags))


@dataclass(frozen=True)
class Bookmark:
    id: str
    path: str
    scope: str  # "pak" | "project" | "unpacked"
    game: str = ""
    root: str = ""  # only meaningful for "project" scope
    tags: tuple[str, ...] = ()
    note: str = ""
    created_at: float = 0.0
    opened_at: float = 0.0

    @property
    def display_path(self) -> str:
        return display_bookmark_path(self.path)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "scope": self.scope,
            "game": self.game,
            "root": root_to_relative(self.root),
            "tags": list(self.tags),
            "note": self.note,
            "created_at": self.created_at,
            "opened_at": self.opened_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Bookmark":
        if not isinstance(data, dict):
            raise ValueError("Bookmark entry must be an object.")
        scope = normalize_scope(data.get("scope") or "pak")
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]).strip(),
            path=normalize_bookmark_path(scope, data.get("path")),
            scope=scope,
            game=str(data.get("game") or "").strip(),
            root=root_to_absolute(str(data.get("root") or "")) if scope == "project" else "",
            tags=normalize_tags(data.get("tags")),
            note=normalize_note(data.get("note")),
            created_at=_timestamp(data.get("created_at"), "created_at"),
            opened_at=_timestamp(data.get("opened_at"), "opened_at"),
        )


_BookmarkCollection = tuple[
    list[Bookmark],
    dict[_BookmarkKey, Bookmark],
    dict[str, Bookmark],
]


class BookmarksStore(QObject):
    """Persistent bookmark store with transactional writes and indexed lookup."""

    changed = Signal()

    def __init__(self, path: Path | str | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.path = Path(path) if path else bookmarks_path()
        self._bookmarks: list[Bookmark] = []
        self._by_key: dict[_BookmarkKey, Bookmark] = {}
        self._by_id: dict[str, Bookmark] = {}
        self._search_blobs: dict[str, str] = {}
        self.load_warnings: list[str] = []
        self.load()

    def load(self) -> None:
        self.load_warnings.clear()
        self._replace_bookmarks([])
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.load_warnings.append(f"Could not read {self.path.name}: {exc}")
            return
        try:
            entries = self._payload_entries(data)
        except ValueError as exc:
            self.load_warnings.append(str(exc))
            return
        bookmarks: list[Bookmark] = []
        invalid = 0
        for entry in entries:
            try:
                bookmarks.append(Bookmark.from_dict(entry))
            except (TypeError, ValueError, OverflowError):
                invalid += 1
        duplicates = len(bookmarks) - len(self._replace_bookmarks(bookmarks))
        if invalid:
            self.load_warnings.append(f"Skipped {invalid} invalid bookmark(s).")
        if duplicates:
            self.load_warnings.append(f"Skipped {duplicates} duplicate bookmark(s).")

    @staticmethod
    def _payload_entries(data: object) -> list[object]:
        if not isinstance(data, dict):
            raise ValueError("Invalid bookmarks file: expected an object.")
        entries = data.get("bookmarks", [])
        if not isinstance(entries, list):
            raise ValueError("Invalid bookmarks file: 'bookmarks' must be a list.")
        return entries

    @staticmethod
    def _payload(bookmarks: list[Bookmark]) -> dict:
        return {
            "version": _BOOKMARKS_VERSION,
            "bookmarks": [bookmark.to_dict() for bookmark in bookmarks],
        }

    @classmethod
    def _write_payload(cls, path: Path, bookmarks: list[Bookmark]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(cls._payload(bookmarks), indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _commit(self, bookmarks: list[Bookmark]) -> None:
        prepared = self._prepare_bookmarks(bookmarks)
        self._write_payload(self.path, prepared[0])
        self._apply_bookmarks(prepared)
        self.changed.emit()

    def _replace_bookmarks(self, bookmarks: list[Bookmark]) -> list[Bookmark]:
        prepared = self._prepare_bookmarks(bookmarks)
        self._apply_bookmarks(prepared)
        return self._bookmarks

    def _prepare_bookmarks(self, bookmarks: list[Bookmark]) -> _BookmarkCollection:
        unique: list[Bookmark] = []
        by_key: dict[_BookmarkKey, Bookmark] = {}
        by_id: dict[str, Bookmark] = {}
        for bookmark in bookmarks:
            key = self._bookmark_key(bookmark)
            if key in by_key:
                continue
            if not bookmark.id or bookmark.id in by_id:
                bookmark = replace(bookmark, id=self._new_id(by_id))
            unique.append(bookmark)
            by_key[key] = bookmark
            by_id[bookmark.id] = bookmark
        return unique, by_key, by_id

    def _apply_bookmarks(self, prepared: _BookmarkCollection) -> None:
        self._bookmarks, self._by_key, self._by_id = prepared
        self._search_blobs = {
            bookmark.id: self._search_blob(bookmark) for bookmark in self._bookmarks
        }

    @staticmethod
    def _new_id(existing: dict[str, Bookmark]) -> str:
        while True:
            candidate = uuid.uuid4().hex[:12]
            if candidate not in existing:
                return candidate

    def _with_replacement(self, replacement: Bookmark) -> list[Bookmark]:
        return [
            replacement if bookmark.id == replacement.id else bookmark
            for bookmark in self._bookmarks
        ]

    @staticmethod
    def _normalized_key(scope: str, path: str, root: str = "", game: str = "") -> _BookmarkKey:
        context = root if scope == "project" else str(game or "").upper()
        return scope, path.lower(), context

    @classmethod
    def _bookmark_key(cls, bookmark: Bookmark) -> _BookmarkKey:
        return cls._normalized_key(
            bookmark.scope,
            bookmark.path,
            bookmark.root,
            bookmark.game,
        )

    @classmethod
    def _key(cls, scope: str, path: str, root: str = "", game: str = "") -> _BookmarkKey:
        scope = normalize_scope(scope)
        path = normalize_bookmark_path(scope, path)
        root = normalize_root(root) if scope == "project" else ""
        return cls._normalized_key(scope, path, root, str(game or "").strip())

    def get(self, scope, path, root="", game="") -> Bookmark | None:
        try:
            return self._by_key.get(self._key(scope, path, root, game))
        except ValueError:
            return None

    def get_by_id(self, bookmark_id: str) -> Bookmark | None:
        return self._by_id.get(bookmark_id)

    def is_bookmarked(self, scope, path, root="", game="") -> bool:
        return self.get(scope, path, root, game) is not None

    def upsert(self, *, scope, path, root="", game="", tags=(), note="") -> Bookmark:
        scope = normalize_scope(scope)
        normalized = normalize_bookmark_path(scope, path)
        root = normalize_root(root) if scope == "project" else ""
        game = str(game or "").strip()
        existing = self._by_key.get(self._normalized_key(scope, normalized, root, game))
        if existing:
            updated = replace(
                existing,
                tags=normalize_tags(tags),
                note=normalize_note(note),
            )
            if updated == existing:
                return existing
            self._commit(self._with_replacement(updated))
            return updated
        bookmark = Bookmark(
            id=self._new_id(self._by_id),
            path=normalized,
            scope=scope,
            game=game,
            root=root,
            tags=normalize_tags(tags),
            note=normalize_note(note),
            created_at=time.time(),
        )
        self._commit([*self._bookmarks, bookmark])
        return bookmark

    def update(self, bookmark_id: str, *, tags=None, note=None) -> Bookmark | None:
        bookmark = self.get_by_id(bookmark_id)
        if bookmark is None:
            return None
        updated = replace(
            bookmark,
            tags=bookmark.tags if tags is None else normalize_tags(tags),
            note=bookmark.note if note is None else normalize_note(note),
        )
        if updated == bookmark:
            return bookmark
        self._commit(self._with_replacement(updated))
        return updated

    def remove(self, bookmark_id: str) -> bool:
        if bookmark_id not in self._by_id:
            return False
        self._commit([bookmark for bookmark in self._bookmarks if bookmark.id != bookmark_id])
        return True

    def touch(self, bookmark_id: str) -> Bookmark | None:
        bookmark = self.get_by_id(bookmark_id)
        if bookmark is None:
            return None
        updated = replace(bookmark, opened_at=time.time())
        self._commit(self._with_replacement(updated))
        return updated

    def adopt_project_bookmarks(self, project_root: str, target: Path | str) -> int:
        root = normalize_root(project_root)
        target = Path(target)
        moving = [
            bookmark
            for bookmark in self._bookmarks
            if bookmark.scope == "project" and bookmark.root == root
        ]
        if not moving:
            return 0
        merged: list[Bookmark] = []
        known: set[_BookmarkKey] = set()
        if target.is_file():
            existing = BookmarksStore(target)
            merged = list(existing._bookmarks)
            known = set(existing._by_key)
        for bookmark in moving:
            key = self._bookmark_key(bookmark)
            if key in known:
                continue
            known.add(key)
            merged.append(bookmark)
        self._write_payload(target, merged)
        self._commit([
            bookmark
            for bookmark in self._bookmarks
            if bookmark.scope != "project" or bookmark.root != root
        ])
        return len(moving)

    def __len__(self) -> int:
        return len(self._bookmarks)

    def all_tags(self, game: str = "", project_root: str = "") -> list[str]:
        return _context_tags(self._bookmarks, game, project_root)

    @staticmethod
    def _search_blob(bookmark: Bookmark) -> str:
        return " ".join([bookmark.display_path, bookmark.note, *bookmark.tags]).lower()

    def _blob(self, bookmark: Bookmark) -> str:
        blob = self._search_blobs.get(bookmark.id)
        if blob is None:
            blob = self._search_blob(bookmark)
            self._search_blobs[bookmark.id] = blob
        return blob

    def matches(self, query: str, tag: str | None = None) -> list[Bookmark]:
        parsed = parse_query(query)
        return [
            bookmark
            for bookmark in self._bookmarks
            if (not tag or tag in bookmark.tags) and parsed.matches(bookmark, self._blob(bookmark))
        ]

    def export_to(self, path: str | Path) -> int:
        self._write_payload(Path(path), self._bookmarks)
        return len(self._bookmarks)

    def import_from(self, path: str | Path) -> int:
        """Merge valid bookmarks from *path* and return the number added."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(self.tr("Could not read file: {error}").format(error=exc)) from exc
        entries = self._payload_entries(data)
        incoming: list[Bookmark] = []
        for index, entry in enumerate(entries, start=1):
            try:
                incoming.append(Bookmark.from_dict(entry))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    self.tr("Invalid bookmark at position {index}: {error}").format(
                        index=index,
                        error=exc,
                    )
                ) from exc

        bookmarks = list(self._bookmarks)
        known = set(self._by_key)
        for bookmark in incoming:
            key = self._bookmark_key(bookmark)
            if key in known:
                continue
            known.add(key)
            bookmarks.append(bookmark)
        added = len(bookmarks) - len(self._bookmarks)
        if added:
            self._commit(bookmarks)
        return added


def _context_tags(bookmarks, game: str, project_root: str) -> list[str]:
    """Frequency-sorted tags of *bookmarks* matching the given game / project."""
    active_game = str(game or "").strip().upper()
    active_root = normalize_root(project_root)
    counts: dict[str, int] = {}
    for bookmark in bookmarks:
        if active_game or active_root:
            if bookmark.scope == "project":
                if active_root and bookmark.root != active_root:
                    continue
            elif active_game and bookmark.game.upper() != active_game:
                continue
        for tag in bookmark.tags:
            counts[tag] = counts.get(tag, 0) + 1
    return [tag for tag, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


class ScopedBookmarksStore(QObject):
    changed = Signal()

    def __init__(
        self,
        project_store: BookmarksStore,
        shared_store: BookmarksStore,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.project_store = project_store
        self.shared_store = shared_store
        self.load_warnings = [*project_store.load_warnings, *shared_store.load_warnings]
        project_store.changed.connect(self.changed)
        shared_store.changed.connect(self.changed)

    def _store_for_scope(self, scope: str) -> BookmarksStore:
        return self.project_store if normalize_scope(scope) == "project" else self.shared_store

    def _store_for_id(self, bookmark_id: str) -> BookmarksStore | None:
        if bookmark_id in self.project_store._by_id:
            return self.project_store
        if bookmark_id in self.shared_store._by_id:
            return self.shared_store
        return None

    def get(self, scope, path, root="", game="") -> Bookmark | None:
        try:
            return self._store_for_scope(scope).get(scope, path, root, game)
        except ValueError:
            return None

    def is_bookmarked(self, scope, path, root="", game="") -> bool:
        return self.get(scope, path, root, game) is not None

    def get_by_id(self, bookmark_id: str) -> Bookmark | None:
        store = self._store_for_id(bookmark_id)
        return store.get_by_id(bookmark_id) if store else None

    def upsert(self, *, scope, path, root="", game="", tags=(), note="") -> Bookmark:
        return self._store_for_scope(scope).upsert(
            scope=scope, path=path, root=root, game=game, tags=tags, note=note
        )

    def update(self, bookmark_id: str, *, tags=None, note=None) -> Bookmark | None:
        store = self._store_for_id(bookmark_id)
        return store.update(bookmark_id, tags=tags, note=note) if store else None

    def remove(self, bookmark_id: str) -> bool:
        store = self._store_for_id(bookmark_id)
        return store.remove(bookmark_id) if store else False

    def touch(self, bookmark_id: str) -> Bookmark | None:
        store = self._store_for_id(bookmark_id)
        return store.touch(bookmark_id) if store else None

    def __len__(self) -> int:
        return len(self.project_store) + len(self.shared_store)

    def all_tags(self, game: str = "", project_root: str = "") -> list[str]:
        return _context_tags(
            [*self.project_store.matches(""), *self.shared_store.matches("")],
            game,
            project_root,
        )

    def matches(self, query: str, tag: str | None = None) -> list[Bookmark]:
        return self.project_store.matches(query, tag) + self.shared_store.matches(query, tag)

    def export_to(self, path: str | Path) -> int:
        bookmarks = self.matches("")
        BookmarksStore._write_payload(Path(path), bookmarks)
        return len(bookmarks)

    def import_from(self, path: str | Path) -> int:
        """Merge valid bookmarks from *path*, routed by scope; return count added."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(self.tr("Could not read file: {error}").format(error=exc)) from exc
        entries = BookmarksStore._payload_entries(data)
        incoming: list[Bookmark] = []
        for index, entry in enumerate(entries, start=1):
            try:
                incoming.append(Bookmark.from_dict(entry))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    self.tr("Invalid bookmark at position {index}: {error}").format(
                        index=index,
                        error=exc,
                    )
                ) from exc

        added = 0
        for store, wanted in (
            (self.project_store, True),
            (self.shared_store, False),
        ):
            known = set(store._by_key)
            merged = list(store._bookmarks)
            for bookmark in incoming:
                if (bookmark.scope == "project") is not wanted:
                    continue
                key = store._bookmark_key(bookmark)
                if key in known:
                    continue
                known.add(key)
                merged.append(bookmark)
            if len(merged) != len(store._bookmarks):
                added += len(merged) - len(store._bookmarks)
                store._commit(merged)
        return added


def resolve_filesystem_target(bookmark: Bookmark, root: str = "") -> str:
    """Absolute path for a project/unpacked bookmark, or '' when missing."""
    if bookmark.scope not in ("project", "unpacked") or not root:
        return ""
    base = Path(root).resolve()
    target = base.joinpath(*bookmark.path.split("/")).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return ""
    return str(target) if target.exists() else ""
