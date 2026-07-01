# RE Engine PAK Load Trace

Traces the successful PAK mount order and effective lookup priority at runtime.

## Setup

```
py -3.14 -m pip install -r requirements.txt
```

## Usage

Run the script from the game directory:

```
py -3.14 pak_load_trace.py re8.exe
```

The script automatically spawns the game, detects its Steam AppID when
available, scans for the appropriate PAK code, and installs the hook. Exit the
game or press `Ctrl+C` to print:

- Actual successful mount order
- Effective lookup order, highest precedence first

Signatures are included for the Village, RE4, 2024 modern, Pragmata, and
MHST3/Requiem engine generations. Unsupported or changed builds fail safely
instead of hooking an ambiguous address.
