#!/usr/bin/env python3
r"""Trace RE Engine PAK mount and lookup order with Frida.

Usage:

    python pak_load_trace.py re8.exe
    python pak_load_trace.py "D:\SteamLibrary\steamapps\common\Game\game.exe"

Press Ctrl+C, or exit the game, to print the final lookup order.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from pathlib import Path
from typing import Any

try:
    import frida
except ImportError:
    frida = None


PACK_PATH_OFFSET = 0x8
STRING_CAPACITY_OFFSET = 0x1C
STRING_HEAP_THRESHOLD = 0xC

# Each short anchor is scanned by Frida. The longer pattern is then checked
# manually, avoiding Frida match-pattern length/version differences.
PATTERNS = (
    {
        "name": "Village generation",
        "anchor": "F0 0F C1 05",
        "adjustment": -6,
        "pattern": (
            "80 7D ?? 00 74 ?? F0 0F C1 05 ?? ?? ?? ?? EB ?? "
            "F0 0F C1 05 ?? ?? ?? ?? EB ?? "
            "80 7D ?? 00 74 ?? F0 0F C1 05 ?? ?? ?? ?? EB ?? "
            "F0 0F C1 05 ?? ?? ?? ?? FF C8 89 83 ?? ?? ?? ??"
        ),
        "priority_disp": 54,
        "hook_offset": 52,
        "reader_reg": "rbx",
        "priority_reg": "rax",
    },
    {
        "name": "RE4 generation",
        "anchor": "F0 0F C1 05",
        "adjustment": 0,
        "pattern": (
            "F0 0F C1 05 ?? ?? ?? ?? EB ?? "
            "F0 0F C1 05 ?? ?? ?? ?? EB ?? "
            "F0 0F C1 05 ?? ?? ?? ?? EB ?? "
            "80 7C 24 ?? 00 74 ?? F0 0F C1 05 ?? ?? ?? ?? EB ?? "
            "F0 0F C1 05 ?? ?? ?? ?? FF C8 89 83 ?? ?? ?? ??"
        ),
        "priority_disp": 59,
        "hook_offset": 57,
        "reader_reg": "rbx",
        "priority_reg": "rax",
    },
    {
        "name": "2024 modern generation",
        "anchor": "F0 0F C1 0D",
        "adjustment": -5,
        "pattern": (
            "B9 FF FF FF FF F0 0F C1 0D ?? ?? ?? ?? "
            "FF C9 B8 01 00 00 00 "
            "41 89 8C 24 ?? ?? ?? ?? F0 0F C1 05"
        ),
        "priority_disp": 24,
        "hook_offset": 20,
        "reader_reg": "r12",
        "priority_reg": "rcx",
    },
    {
        "name": "Pragmata generation",
        "anchor": "F0 44 0F C1 3D",
        "adjustment": 0,
        "pattern": (
            "F0 44 0F C1 3D ?? ?? ?? ?? "
            "48 8D 3D ?? ?? ?? ?? 41 FF CF "
            "45 89 BE ?? ?? ?? ?? "
            "F0 44 0F C1 2D ?? ?? ?? ?? "
            "41 FF C5 B3 01 45 89 AE ?? ?? ?? ??"
        ),
        "priority_disp": 22,
        "hook_offset": 40,
        "reader_reg": "r14",
        "priority_from_memory": True,
    },
    {
        "name": "MHST3/Requiem generation",
        "anchor": "F0 0F C1 05",
        "adjustment": 0,
        "pattern": (
            "F0 0F C1 05 ?? ?? ?? ?? FF C8 "
            "89 86 ?? ?? ?? ?? B8 01 00 00 00 "
            "F0 0F C1 05 ?? ?? ?? ?? FF C0 "
            "89 86 ?? ?? ?? ??"
        ),
        "priority_disp": 12,
        "hook_offset": 31,
        "reader_reg": "rsi",
        "priority_from_memory": True,
    },
)


def build_agent(module_name: str) -> str:
    config = json.dumps(
        {
            "module": module_name,
            "patterns": PATTERNS,
            "packPathOffset": PACK_PATH_OFFSET,
            "capacityOffset": STRING_CAPACITY_OFFSET,
            "heapThreshold": STRING_HEAP_THRESHOLD,
        }
    )
    return rf"""
'use strict';

const config = {config};
const deadline = Date.now() + 30000;
let installed = false;
let waitingReported = false;

function findModule(name) {{
    const wanted = name.toLowerCase();
    return Process.enumerateModules().find(
        module => module.name.toLowerCase() === wanted
    ) || null;
}}

function matchesAt(address, pattern) {{
    const tokens = pattern.trim().split(/\s+/);
    try {{
        for (let index = 0; index < tokens.length; ++index) {{
            if (tokens[index] !== '??' &&
                address.add(index).readU8() !== parseInt(tokens[index], 16))
                return false;
        }}
        return true;
    }} catch (_) {{
        return false;
    }}
}}

function scanExecutableRanges(module, pattern) {{
    const ranges = [];
    const seen = new Set();

    if (typeof module.enumerateRanges === 'function') {{
        for (const protection of ['r-x', 'rwx']) {{
            for (const range of module.enumerateRanges(protection)) {{
                const key = range.base.toString() + ':' + range.size;
                if (!seen.has(key)) {{
                    seen.add(key);
                    ranges.push(range);
                }}
            }}
        }}
    }}
    if (ranges.length === 0)
        ranges.push({{ base: module.base, size: module.size }});

    const matches = [];
    for (const range of ranges) {{
        for (const match of Memory.scanSync(range.base, range.size, pattern))
            matches.push(match);
    }}
    return matches;
}}

function readPackPath(reader) {{
    try {{
        const value = reader.add(config.packPathOffset);
        const capacity = value.add(config.capacityOffset).readU32();
        const characters = capacity >= config.heapThreshold
            ? value.readPointer()
            : value;
        return characters.readUtf16String();
    }} catch (error) {{
        return '<unreadable PackReader::mPackPath: ' + error + '>';
    }}
}}

function fail(message, module, target = null) {{
    send({{
        type: 'fatal',
        message: message,
        base: module.base.toString(),
        target: target === null ? '<not resolved>' : target.toString()
    }});
    installed = true;
}}

function install() {{
    if (installed)
        return;

    const module = findModule(config.module);
    if (module === null)
        return;

    const candidates = new Map();
    try {{
        const anchors = [...new Set(config.patterns.map(spec => spec.anchor))];
        for (const anchorPattern of anchors) {{
            for (const anchor of scanExecutableRanges(module, anchorPattern)) {{
                for (const spec of config.patterns) {{
                    if (spec.anchor !== anchorPattern)
                        continue;
                    const address = spec.adjustment < 0
                        ? anchor.address.sub(-spec.adjustment)
                        : anchor.address.add(spec.adjustment);
                    if (matchesAt(address, spec.pattern))
                        candidates.set(address.toString(), {{ address, spec }});
                }}
            }}
        }}
    }} catch (error) {{
        fail('semantic scan failed: ' + error, module);
        return;
    }}

    if (candidates.size === 0 && Date.now() < deadline) {{
        if (!waitingReported) {{
            send({{
                type: 'status',
                message: 'waiting for RE Engine PAK code',
                base: module.base.toString()
            }});
            waitingReported = true;
        }}
        return;
    }}
    if (candidates.size !== 1) {{
        fail(
            'semantic signatures produced ' + candidates.size +
            ' matches; exactly one is required',
            module
        );
        return;
    }}

    const selected = candidates.values().next().value;
    const spec = selected.spec;
    const priorityOffset = selected.address
        .add(spec.priority_disp)
        .readS32();
    const target = selected.address.add(spec.hook_offset);
    const range = Process.findRangeByAddress(target);
    if (range === null || range.protection.indexOf('x') === -1) {{
        fail('resolved hook is not executable', module, target);
        return;
    }}

    Interceptor.attach(target, {{
        onEnter() {{
            const reader = this.context[spec.reader_reg];
            let priority = null;
            try {{
                priority = spec.priority_from_memory === true
                    ? reader.add(priorityOffset).readS32()
                    : this.context[spec.priority_reg].toInt32();
            }} catch (_) {{
            }}
            send({{
                type: 'mount',
                path: readPackPath(reader),
                priority: priority
            }});
        }}
    }});

    let instruction = '<unavailable>';
    try {{
        instruction = Instruction.parse(target).toString();
    }} catch (_) {{
    }}
    send({{
        type: 'ready',
        module: module.name,
        base: module.base.toString(),
        target: target.toString(),
        generation: spec.name,
        priorityOffset: priorityOffset,
        instruction: instruction
    }});
    installed = true;
}}

install();
const timer = setInterval(() => {{
    install();
    if (installed)
        clearInterval(timer);
}}, 50);
"""


class TraceState:
    def __init__(self) -> None:
        self.mounts: list[tuple[str, int]] = []
        self.stopped = threading.Event()
        self.lock = threading.Lock()

    def on_message(self, message: dict[str, Any], data: bytes | None) -> None:
        if message["type"] == "error":
            print(f"[agent error] {message.get('stack', message)}", file=sys.stderr)
            return

        payload = message.get("payload", {})
        kind = payload.get("type")
        if kind == "ready":
            print(
                f"[hook] {payload['module']} base={payload['base']} "
                f"target={payload['target']}"
            )
            print(
                f"[hook] {payload['generation']}; "
                f"priority offset=0x{payload['priorityOffset']:X}"
            )
            print(f"[hook] instruction: {payload['instruction']}")
        elif kind == "status":
            print(f"[scan] {payload['message']} (base {payload['base']})")
        elif kind == "fatal":
            print(f"[fatal] {payload['message']}", file=sys.stderr)
            print(
                f"        base={payload['base']} target={payload['target']}",
                file=sys.stderr,
            )
            self.stopped.set()
        elif kind == "mount":
            priority = payload.get("priority")
            if priority is None:
                print(f"[warning] could not read priority: {payload['path']}")
                return
            with self.lock:
                sequence = len(self.mounts) + 1
                self.mounts.append((payload["path"], priority))
                print(
                    f"[mount {sequence:03d}] "
                    f"priority=0x{priority & 0xffffffff:08X} "
                    f"{payload['path']}"
                )

    def print_summary(self) -> None:
        with self.lock:
            mounts = list(self.mounts)

        print(f"\nSuccessful mounts: {len(mounts)}")
        if not mounts:
            print("No successful PAK mounts were observed.")
            return

        print("\nActual successful mount order:")
        for sequence, (path, _) in enumerate(mounts, start=1):
            print(f"{sequence:3d}. {path}")

        print("\nEffective lookup order (highest precedence first):")
        for index, (path, priority) in enumerate(
            sorted(mounts, key=lambda item: item[1]), start=1
        ):
            print(
                f"{index:3d}. [0x{priority & 0xffffffff:08X}] {path}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trace RE Engine PAK mount and lookup order."
    )
    parser.add_argument("exe", type=Path, help="path to the game executable")
    return parser.parse_args()


def discover_steam_appid(executable: Path) -> int | None:
    game_dir = executable.parent
    common_dir = game_dir.parent
    if common_dir.name.casefold() != "common":
        return None

    for manifest in common_dir.parent.glob("appmanifest_*.acf"):
        try:
            text = manifest.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        install = re.search(r'"installdir"\s+"([^"]+)"', text, re.IGNORECASE)
        appid = re.search(r'"appid"\s+"(\d+)"', text, re.IGNORECASE)
        if (
            install is not None
            and appid is not None
            and install.group(1).casefold() == game_dir.name.casefold()
        ):
            return int(appid.group(1))
    return None


def spawn_game(
    device: Any,
    executable: Path,
    state: TraceState,
) -> tuple[Any, Any]:
    executable = executable.resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)

    options: dict[str, Any] = {
        "cwd": str(executable.parent),
        "stdio": "inherit",
    }
    appid = discover_steam_appid(executable)
    if appid is not None:
        print(f"[steam] using AppID {appid}")
        options["env"] = {
            "SteamAppId": str(appid),
            "SteamGameId": str(appid),
        }

    pid = device.spawn([str(executable)], **options)
    print(f"[spawn] pid={pid} executable={executable}")
    session = device.attach(pid)

    def detached(reason: str, crash: Any = None) -> None:
        print(f"[detached] reason={reason}")
        if crash is not None:
            print(f"[detached] crash={crash}")
        state.stopped.set()

    session.on("detached", detached)
    script = session.create_script(build_agent(executable.name))
    script.on("message", state.on_message)
    script.load()
    device.resume(pid)
    return session, script


def main() -> int:
    args = parse_args()
    if frida is None:
        print(
            f'Install Frida with:\n  "{sys.executable}" -m pip install frida',
            file=sys.stderr,
        )
        return 2

    state = TraceState()
    device = frida.get_local_device()
    session = script = None

    try:
        session, script = spawn_game(device, args.exe, state)
        while not state.stopped.wait(0.25):
            pass
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"[error] {error}", file=sys.stderr)
        return 1
    finally:
        state.print_summary()
        if script is not None:
            try:
                script.unload()
            except Exception:
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
