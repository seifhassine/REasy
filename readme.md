# REasy Editor v0.0.1

<p align="center">
  <img src="reasy_editor_logo.png" alt="REasy Editor Logo" style="max-width:300px;">
</p>

**REasy Editor** is a quality-of-life toolkit for RE games that currently supports viewing and limited editing of UVAR files. It also includes miscellaneous tools to speed up your work. 
I have currently rushed the release and the structure will be improved in the upcoming updates. I have currently only tested and used this on RE4R files.

## Features

- **UVAR File Editing:**  
  Open and modify (carefully) UVAR files including header fields and variables. 
  
- **Flexible Variable Management:**  
  Add new variables with automatic naming that preserves numeric formatting (e.g. "Location47_031" followed by "Location47_032").
  This allows you to add new flags to RE4R for example (which was not possible with existing tools).

- **Search Functionality:**  
  Search all files across directories for:
  - Specific text (UTF-16LE encoded)
  - 32-bit numbers (with hexadecimal display)
  - GUIDs (with conversion from standard format)

- **GUID Converter Tool:**  
  Convert between memory (hyphenated hex) and standard (hyphenated) GUID formats.

- **Dark Mode:**  
  Toggle a dark mode theme that applies to all dialogs and windows.

## Installation

- Run build.bat

### Prerequisites

- Python 3.x (64-bit recommended)
- [Tkinter](https://docs.python.org/3/library/tkinter.html) (usually included with Python)
- [Pillow](https://python-pillow.org/)  
  Install via pip:
  ```bash
  pip install Pillow

### Credits:

@alphazolam for the uvar template.
@TrikzMe for RE's MurMurHash3 

