import json
import os
import struct
import time
import uuid

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalSocket

lazy from app_config import GAME_TITLES
lazy from utils.resource_file_utils import _RESOURCE_SUFFIX

CLIENT_ID = "1575223836054397000"
PIPE = "discord-ipc-0"
HANDSHAKE, FRAME, CLOSE, PING, PONG = range(5)


class DiscordPresence(QObject):
    def __init__(self, window):
        super().__init__(window)
        self._started = int(time.time())
        self._socket = QLocalSocket(self)
        self._socket.connected.connect(self._handshake)
        self._socket.readyRead.connect(self._read)
        QTimer(self, interval=5000, timeout=self._refresh).start()
        self._refresh()

    def _refresh(self):
        window = self.parent()
        if not window.settings["discord_presence"]:
            self._socket.abort()
        elif self._socket.state() == QLocalSocket.UnconnectedState:
            self._ready, self._sent, self._buffer = False, None, b""
            self._socket.connectToServer(PIPE)
        elif self._ready and (activity := self._activity(window)) != self._sent:
            self._sent = activity
            self._send(FRAME, {"cmd": "SET_ACTIVITY", "args": {"pid": os.getpid(), "activity": activity},
                               "nonce": str(uuid.uuid4())})

    def _activity(self, window):
        tab = window.get_active_tab()
        path = tab and (tab.pak_source_path or tab.filename)
        activity = {
            "state": GAME_TITLES[window.current_game or window.settings["game_version"]],
            "timestamps": {"start": self._started},
            "buttons": [{"label": self.tr("Get REasy"), "url": "https://github.com/seifhassine/REasy"}],
        }
        if path:
            name = _RESOURCE_SUFFIX.sub("", os.path.basename(path))
            activity["details"] = self.tr("Editing {name}").format(name=name)[:128]
        return activity

    def _handshake(self):
        self._send(HANDSHAKE, {"v": 1, "client_id": CLIENT_ID})

    def _send(self, op, payload):
        data = json.dumps(payload).encode()
        self._socket.write(struct.pack("<II", op, len(data)) + data)

    def _read(self):
        self._buffer += self._socket.readAll().data()
        while len(self._buffer) >= 8:
            op, size = struct.unpack_from("<II", self._buffer)
            if len(self._buffer) < 8 + size:
                return
            message, self._buffer = json.loads(self._buffer[8:8 + size]), self._buffer[8 + size:]
            if op == PING:
                self._send(PONG, message)
            elif op == CLOSE:
                print(f"Discord presence: {message['message']}")
            elif message["evt"] == "READY":
                self._ready = True
                self._refresh()
            elif message["evt"] == "ERROR":
                print(f"Discord presence: {message['data']['message']}")
