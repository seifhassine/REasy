# REasy Editor v0.4.9 ![GitHub all releases](https://img.shields.io/github/downloads/seifhassine/REasy/total)

<p align="center">
  <img src="resources/images/reasy_editor_logo.png" alt="REasy Editor Logo" style="max-width:300px;">
</p>


<br>

**REasy Editor** is a quality-of-life toolkit for RE games that currently supports viewing and editing of some RE Engine files. It also includes miscellaneous tools to speed up your work. 
Supports RSZ files (SCN, PFB, User) from all games, as well as UVAR, MSG files.

REasy GUI is currently available in English and Chinese (limited) 


<br>
<br>

<p align="center">
   <img src="https://github.com/user-attachments/assets/64a84918-ad58-47ee-8186-e0e7a5a1ace0" alt="REasy Editor Logo" width=70%">
</p>

<br>


## Features

<br>
<div align="center">
  
| File Type | Support | Tested On |
|-----------|---------|-----------|
| UVAR      | ✅       | Most Titles  |
| RCOL      | ❌ <sub><sup>[temporarily disabled]</sup></sub>       | RE4       |
| SCN       | ✅       | Most titles       |
| User       | ✅       |Most titles       |
| PFB       | ✅       | Most titles      |
| MSG       | ✅       | RE4      |
| PAK       | ✅       | RE4      |
| CFIL       | ✅       | RE4      |
| MOTBANK       | ✅       | Most titles      |
| EFX       | Coming Soon       |       |
  
</div>
<br>
<br>

- **PAK File Extraction and Creation**  
  - REasy currently has the fastest PAK extraction system.
  - Support for single entry extraction.
  - Regex search in the file list is supported. 

- **UVAR File Editing**
 
- **MSG File Editing**
  
- **RSZ File Viewing and Editing:**  
  - User, PFB and SCN files are supported with advanced editing.
  - Template Manager to export your favorite GameObjects and import them in different files (Exported GameObjects can be found in the "templates" directory in plaintext (JSON)).
  - Clipboard system allowing to copy paste array elements, components and GameObjects across different files (JSON serialized).
  - Community Templates browser where you can download templates from other people, rate them and upload your own for different games (accessible in Template Manager).
  - Up-to-date improved RSZ dumps for all games.
  - Obsolete RSZ file detector (available in >Tools)
  - Support for all versions of RSZ files dating from RE7.
  - And much more..
   
- **RSZ Diff Viewer:**  
  - Allows comparing of RSZ files. Currently, only SCN files are supported. 

- **Search Functionality:**  
  Search all files across directories for:
  - Specific text (UTF-16LE encoded)
  - 32-bit numbers (with hexadecimal display)
  - GUIDs (with conversion from standard format)
  - Specific RSZ field values 
- **Project Manager:**
 
  Ability to create mods and export them as .PAK or Fluffy Manager .ZIP archive (File > Create Project).
- **GUID Converter Tool:**  
  Convert between memory (hyphenated hex) and standard (hyphenated) GUID formats.

-  **Backup System for Files**

-  **Dark Mode**  



## Guides:

- **Wiki:**  
  Accessible [here](https://github.com/seifhassine/REasy-Wiki/blob/main/README.md). Work in progress.
  
- **Adding New Flags to RE4R:**  
  I tested adding 22000 new flags (file size went from 2mb to ~16mb), and tried some of them randomly. Game was stable. At 50k added flags, it crashes when a gamesave is triggered. To determine the exact threshold, your testing and feedback are needed. But 20k should be more than enough. (I wrote a guide [here](https://www.nexusmods.com/residentevil42023/articles/346))

- **RE8:**
  [Modding Weapons and Items with REasy Editor](https://www.nexusmods.com/residentevilvillage/articles/45) by [matalayudasleazy](https://next.nexusmods.com/profile/matalayudasleazy?gameId=3669)

## Correctness

- All RSZ (.user, .pfb, .scn) files from most games are tested before release:
 
    [![Build and Package REasy](https://github.com/seifhassine/REasy/actions/workflows/build.yml/badge.svg)](https://github.com/seifhassine/REasy/actions/workflows/build.yml)


## RSZ Dumps:

- Under [/resources/data/dumps](https://github.com/seifhassine/REasy/tree/master/resources/data/dumps) you will find a list of updated RSZ templates for all games. 

## Installation

- Run build.bat

- Dependencies in requirements.txt

- Python 3.13 is required.

If you want to run REasy on Linux and encounter the error "Aborted" on launch, try installing libxcb-cursor0 using apt-get. 

## Credits:

@alphazolam for the uvar template.

@TrikzMe for RE's MurMurHash3 

@praydog, for making the RSZ JSON dumps and REF. 

@don on Discord for helping out with .exe debugging related stuff.

@shadowcookie for consulting with misc. stuff as well as many updated file format structures. 

## Support REasy:

If you appreciate my work and would like to support the development of the tool, you can support me through this [link](https://linktr.ee/seifhassine)

## License, Contributions:

REasy is under MIT license.
You are wlecome to contribute to the project. I am currently active and will review PRs.

## Third-Party Components

This project uses **[PySide6](https://pypi.org/project/PySide6/)** (Qt for Python), licensed under **LGPL version 3**.
For more information, see:  
- [Qt Licensing Information](https://www.qt.io/licensing/)  
- [LGPL v3 License Text](https://www.gnu.org/licenses/lgpl-3.0.html)
  
## Sponsors
<table>
 <tbody>
  <tr>
   <td align="center"><img alt="[SignPath]" src="https://avatars.githubusercontent.com/u/34448643" height="30"/></td>
   <td>Free code signing on Windows provided by <a href="https://signpath.io/">SignPath.io</a>, certificate by <a href="https://signpath.org/">SignPath Foundation</a></td>
  </tr>
 </tbody>
</table>

