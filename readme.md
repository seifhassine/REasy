# REasy Editor v0.0.5

<p align="center">
  <img src="resources/images/reasy_editor_logo.png" alt="REasy Editor Logo" style="max-width:300px;">
</p>


<br>

**REasy Editor** is a quality-of-life toolkit for RE games that currently supports viewing and editing of some RE Engine files. It also includes miscellaneous tools to speed up your work. 
I have currently rushed the release and the structure will be improved in the upcoming updates. I have currently tested this on RE2 and RE4R files. It should work fine with uvars from other RE engine titles.


<br>
<br>
<p align="center">
   <img src="https://github.com/user-attachments/assets/562f805e-6323-4697-96eb-f2a9d1863baf" alt="REasy Editor Logo" width=60% height=60%">
</p>

<br>


## Features

<br>
<div align="center">
  
| File Type | Viewing | Editing | Tested On |
|-----------|---------|---------|-----------|
| UVAR      | ✅       | ✅       | RE4, RE2  |
| RCOL      | ✅       | ❌       | RE4       |
| SCN       | ✅       | ❌       | RE4       |
  
</div>
<br>
<br>

- **UVAR File Editing:**  
  View and fully modify UVAR files. Hashing and mapping are taken care of automatically.
  
- **RCOL File Viewing (Limited Support):**  
  Only rcol.25 files are supported for now.
  
- **Flexible Variable Management:**  
  - Add new variables with automatic naming that preserves numeric formatting (e.g. "Location47_031" followed by "Location47_032").
  This allows you to add new flags to RE4R for example (which was not possible with existing tools).
  - Deletion of variables is also supported.

- **Search Functionality:**  
  Search all files across directories for:
  - Specific text (UTF-16LE encoded)
  - 32-bit numbers (with hexadecimal display)
  - GUIDs (with conversion from standard format)

- **GUID Converter Tool:**  
  Convert between memory (hyphenated hex) and standard (hyphenated) GUID formats.

- **Dark Mode:**  
  Toggle a dark mode theme that applies to all dialogs and windows.



## Unique Use-Cases:

- **Adding New Flags to RE4R:**  
  I tested adding 22000 new flags (file size went from 2mb to ~16mb), and tried some of them randomly. Game was stable. At 50k added flags, it crashes when a gamesave is triggered. To determine the exact threshold, your testing and feedback are needed. But 20k should be more than enough. (I wrote a guide [here](https://www.nexusmods.com/residentevil42023/articles/346))
  
- **Finding all files where some data is referenced:**
  Ever encountered a flag but don't know in which file it is set? This tool allows you to find all locations where that flag is checked/set.

## Installation

- Run build.bat

### Prerequisites

- Python 3.x (64-bit recommended)
- [Tkinter](https://docs.python.org/3/library/tkinter.html) (usually included with Python)
- [Pillow](https://python-pillow.org/)  
  Install via pip:
  ```bash
  pip install Pillow

## Credits:

@alphazolam for the uvar template.
@TrikzMe for RE's MurMurHash3 
@praydog, for making the RSZ JSON dumps

## License, Contributions:

REasy is under MIT license.
You are wlecome to contribute to the project. I am currently active and will review PRs.

