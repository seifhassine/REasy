# REasy Editor

![REasy Editor Logo](reasy_editor_logo.png)

**REasy Editor** is a quality-of-life toolkit for RE games that currently supports viewing and limited editing of UVAR files. It also includes miscellaneous tools to speed up your work. 

## Features

- **UVAR File Editing:**  
  Open and modify (carefully) UVAR files including header fields and variables. 
  
- **Flexible Variable Management:**  
  Add new variables with automatic naming that preserves numeric formatting (e.g. "Location47_031" followed by "Location47_032").
  This allows you to add new flags to the game.

- **Search Functionality:**  
  Search within UVAR files and across directories for:
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
