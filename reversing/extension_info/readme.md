# RE Games Extension Info Dumper

This tool extracts `{extension_string : number}` pairs from a game executable by finding known UTF-16 “anchors” (strings) and reading the first two integer args passed to the function that consumes them.

## How it works

1. **Anchors**: You provide one or more known extension strings (e.g., `"fbxskel"`, `"lmap"`). The tool finds their UTF-16LE occurrences in **data** sections (you don't need to give it all extensions).
2. **Next call**: It scans **code** for spots that load one of those anchors into **RCX** and takes the **very next call** — that’s the callee we care about.
3. **Callsites**: It indexes all callsites of that callee and looks for patterns (mov, lea, etc..) to get what's being assigned to RCX (holds string) and EDX (version number)

**Output**: If an extension is missing from the result, re-run the script and use it as an extra argument `-ext "your extension"`. That will also allow the script to find some other missing extensions.


## Run

```bash
1- Prepare your enviroment
2- pip install -r requirements.txt
3- python extension_dumper.py <game.exe> -ext "name1" [-ext "name2" ...] [-o output.json]

Alternatively you can feed it a file containing some known extension names, using `-F` or `--ext-file`
```

<br>
<sup>Should support all games. Handles instruction patterns from both old and new builds.</sup>
  
<sup>Sometimes you will some extensions, and wrong versions along with them. If that happens, try with different extension names, and preferably 4 characters long or more.</sup>
