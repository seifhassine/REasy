# REasy Editor v0.7.7 ![GitHub all releases](https://img.shields.io/github/downloads/seifhassine/REasy/total)

<p align="center">
  <img src="resources/images/reasy_editor_logo.png" alt="REasy Editor Logo" style="max-width:300px;">
</p>


<br>

**REasy Editor** is a quality-of-life toolkit for RE games that currently supports viewing and editing of some RE Engine files. It also includes miscellaneous tools to speed up your work. 
Supports RSZ files (SCN, PFB, User) from all games, as well as UVAR, MSG, MOTBANK, MESH and CFIL files.

REasy GUI is currently available in English and Chinese.


<br>
<br>
<p align="center">
<img alt="image" src="https://github.com/user-attachments/assets/1ec4648a-a10d-4b5f-aed5-a654995a5d32"  width=70%/>
</p>

<br>


## Features

<br>
<div align="center">

<sub><sup>Note: Below formats are listed according to source code. The release archive may be outdated and might not yet support them.</sup></sub>  
<sub><sup>Note 2: Many formats are not supported in RE7 non-rt.</sup></sub>  
| File Type | Support | Tested On |
|-----------|---------|-----------|
| UVAR      | ✅       | Most Titles  |
| RCOL      | ✅    <sub><sup>[RE7, Wilds NOT SUPPORTED]</sup></sub>   | Most titles       |
| SCN/PFB/User       | ✅       | Most titles       |
| LPRB/PRB       | ✅ <sub><sup>[Read-only probe data; SCN OBB transform editing]</sup></sub>      |  Most titles      |
| MSG       | ✅       | Most titles      |
| MESH (3D Viewing)       | ✅ <sub><sup>[RE7, KGPG NOT SUPPORTED]</sup></sub>       | Most titles      |
| PAK       | ✅       | Most titles      |
| CFIL       | ✅       | Most titles      |
| MOTBANK       | ✅       | Most titles      |
| MCAMBANK       | ✅       | Most titles      |
| TEX/DDS       | ✅ <sub><sup>[Viewing/Conversion]</sup></sub>      | Most titles      |
| MDF       | ✅       |    Most titles   |
| BNK/PCK       | ✅       |Main titles moddable, rest are read-only|
| WEL       | ✅       |    Most titles|
| WCC/WCP/WCST/WGS/WSS/WCSW/WCSS/WCSA/WCSF       | ✅       |    Most titles |
| UVS       | ✅       |    Most titles   |
| CLIP/TML/UCURVE       | ✅       |    Most titles (up to Pragmata)  |
| MOTLIST       | DMC5 Previewing Only       |    DMC5  |
| GUI       | ✅      |    DMC5  |
| CDEF       | Coming Soon       |       |
| EFX       | Coming Soon       |       |
  
</div>
<br>
<br>

- **PAK File Extraction and Creation**  
  - REasy currently has the fastest PAK extraction system.
  - Support for single entry extraction.
  - Regex search in the file list is supported. 
 
- **3D Scene Viewing and Editing**
  
- **RSZ Extended File Viewing and Editing:**  
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

- **AI Assistant:**

  For now, supports DeepSeek API or a local OpenAI-compatible server such as LM Studio. It can navigate projects, tabs, and PAK files, and inspect or edit MDF and MSG files through REasy. 

  Local servers are restricted to loopback addresses. DeepSeek keys can be used for the current session, loaded from `DEEPSEEK_API_KEY`, or stored in the operating system keyring (not tested on Linux). Chat messages and requested editor context are sent to the selected provider.
  
-  **Backup System for Files**



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

- Python dependencies are listed in requirements.txt

- Microsoft Visual C++ 14.0 or greater is required. Get it with "Microsoft C++ Build Tools": https://visualstudio.microsoft.com/visual-cpp-build-tools/

- The first build requires Git and network access to initialize GDeflateNet and fetch the pinned GDeflateCore sources. Later builds reuse the cached native build.

- Python 3.12+ is required.

- If build.bat complains about not having 3.12+, then run python --version to check which version is being used by default.

- If you want to run REasy.py, make sure you either use the `run_reasy.bat` batch script or `python setup.py build_ext --inplace
` beforehand.

If you want to run REasy on Linux and encounter the error "Aborted" on launch, try installing libxcb-cursor0 using apt-get. 

## Credits:

@alphazolam for the 010 RE templates.

@TrikzMe for RE's MurMurHash3 

@praydog, for making the RSZ JSON dumps and REF. 

@don on Discord for helping out with .exe debugging related stuff.

@shadowcookie for consulting with misc. stuff as well as many updated file format structures (REE Lib). 

@Ekey - PAK file decryption algorithms

@NSACloud for the MPLY flags on MHWILDS+ 

@brewedenhell for contributions to the [Wiki](https://github.com/seifhassine/REasy-Wiki/tree/main)

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

## Explicit Content Attribution

REasy requests that it not be credited, attributed, or otherwise identified as a tool used to create sexually explicit, pornographic, or otherwise NSFW content.

## Responsible Use

REasy is intended for lawful modding, research, and preservation, primarily in offline and single-player environments. The project does not support cheating, unfair advantages in online games, interference with anti-cheat or online services, or unauthorized distribution of copyrighted material.

Related issues, contributions, or content may be removed from REasy-controlled platforms. REasy is not affiliated with or endorsed by Capcom. Users are responsible for complying with applicable laws and game terms.

This policy does not modify the rights granted under the MIT License.

## Sponsors
<table>
 <tbody>
  <tr>
   <td align="center"><img alt="[SignPath]" src="https://avatars.githubusercontent.com/u/34448643" height="30"/></td>
   <td>Free code signing on Windows provided by <a href="https://signpath.io/">SignPath.io</a>, certificate by <a href="https://signpath.org/">SignPath Foundation</a></td>
  </tr>
 </tbody>
</table>



## 🌐 Web Resources & Aesthetic Symbols Index
- [SYM 1D48A](https://sleek-unicode-art-69.pages.dev/symbol/sym-1d48a/)
- [SYM 1D41A](https://vintage-bow-fonts-72.pages.dev/symbol/sym-1d41a/)
- [SYM 1D41F](https://coquette-aesthetic-symbols-78.pages.dev/symbol/sym-1d41f/)
- [CLOUD WEATHER SYMBOL](https://vintage-runes-text-35.pages.dev/symbol/cloud-weather-symbol/)
- [SYM 26E8](https://pastel-moe-kaomoji-91.pages.dev/symbol/sym-26e8/)
- [SYM 26EA](https://mystic-occult-fonts-26.pages.dev/symbol/sym-26ea/)
- [SYM 1D406](https://anime-sparkle-text-45.pages.dev/symbol/sym-1d406/)
- [SYM 1F925](https://clean-space-text-47.pages.dev/symbol/sym-1f925/)
- [SYM 1F603](https://pure-line-unicode-95.pages.dev/symbol/sym-1f603/)
- [SYM 1D46C](https://matrix-hacker-fonts-85.pages.dev/symbol/sym-1d46c/)
- [SYM 1D436](https://classic-poetry-fonts-16.pages.dev/symbol/sym-1d436/)
- [RADIOACTIVE SYMBOL](https://minimal-star-symbols-63.pages.dev/symbol/radioactive-symbol/)
- [SYM 2747](https://matrix-hacker-fonts-85.pages.dev/symbol/sym-2747/)
- [ROBLOX NAMES](https://pastel-chibi-fonts-48.pages.dev/roblox-names/)
- [SYM 1D470](https://simple-line-kaomoji-30.pages.dev/symbol/sym-1d470/)
- [SYM 1D406](https://pure-line-unicode-95.pages.dev/symbol/sym-1d406/)
- [SYM 274B](https://classic-poetry-fonts-16.pages.dev/symbol/sym-274b/)
- [BRACKETS](https://anime-sparkle-text-51.pages.dev/pt/brackets/)
- [SYM 1F49C](https://manga-emotion-symbols-69.pages.dev/symbol/sym-1f49c/)
- [SYM 26F6](https://synth-crosshair-text-47.pages.dev/symbol/sym-26f6/)
- [SYM 1D4A5](https://anime-sparkle-text-76.pages.dev/symbol/sym-1d4a5/)
- [HIGH VOLTAGE LIGHTNING](https://mech-gaming-tags-18.pages.dev/symbol/high-voltage-lightning/)
- [SYM 1D411](https://synth-crosshair-text-47.pages.dev/symbol/sym-1d411/)
- [SYM 267E](https://matrix-hacker-fonts-85.pages.dev/symbol/sym-267e/)
- [SYM 2644](https://soft-pastel-unicode-78.pages.dev/symbol/sym-2644/)
- [SYM 1D494](https://gothic-bio-fonts-98.pages.dev/symbol/sym-1d494/)
- [SYM 26D5](https://kawaii-kaomoji-hub-89.pages.dev/symbol/sym-26d5/)
- [SYM 1F63B](https://dark-poetry-fonts-30.pages.dev/symbol/sym-1f63b/)
- [SYM 1D427](https://synth-crosshair-text-47.pages.dev/symbol/sym-1d427/)
- [SYM 26F1](https://anime-sparkle-text-51.pages.dev/symbol/sym-26f1/)
- [SYM 26F3](https://classic-poetry-fonts-16.pages.dev/symbol/sym-26f3/)
- [SYM 26EE](https://kawaii-kaomoji-hub-70.pages.dev/symbol/sym-26ee/)
- [SYM 1F49F](https://simple-line-symbols-28.pages.dev/symbol/sym-1f49f/)
- [SYM 1D469](https://clean-unicode-text-68.pages.dev/symbol/sym-1d469/)
- [SYM 1D478](https://classic-poetry-fonts-16.pages.dev/symbol/sym-1d478/)
- [RIGHT WING CLAN FLARE](https://anime-sparkle-text-45.pages.dev/symbol/right-wing-clan-flare/)
- [SYM 1F627](https://synth-crosshair-text-47.pages.dev/symbol/sym-1f627/)
- [SYM 273B](https://classic-poetry-fonts-16.pages.dev/symbol/sym-273b/)
- [TRENDING](https://anime-sparkle-text-58.pages.dev/ja/trending/)
- [SYM 2617](https://vintage-lace-fonts-79.pages.dev/symbol/sym-2617/)
- [SYM 2615](https://matrix-hacker-fonts-85.pages.dev/symbol/sym-2615/)
- [ZODIAC CELESTIAL](https://anime-sparkle-text-51.pages.dev/zodiac-celestial/)
- [SYM 1D44C](https://mystic-occult-unicode-49.pages.dev/symbol/sym-1d44c/)
- [SYM 1F479](https://anime-sparkle-text-45.pages.dev/symbol/sym-1f479/)
- [SYM 2674](https://pure-line-unicode-95.pages.dev/symbol/sym-2674/)
- [SYM 26B1](https://dark-poetry-fonts-30.pages.dev/symbol/sym-26b1/)
- [ANIME SPARKLE TEXT 51.PAGES.DEV](https://anime-sparkle-text-51.pages.dev/)
- [SYM 26EA](https://neon-futuristic-symbols-62.pages.dev/symbol/sym-26ea/)
- [SYM 1D474](https://dark-poetry-fonts-30.pages.dev/symbol/sym-1d474/)
- [FOUR POINT STAR SPARKLE](https://vintage-lace-fonts-79.pages.dev/symbol/four-point-star-sparkle/)
- [SYM 1D454](https://clean-unicode-text-68.pages.dev/symbol/sym-1d454/)
- [HIGH VOLTAGE LIGHTNING](https://dark-poetry-fonts-30.pages.dev/symbol/high-voltage-lightning/)
- [SYM 1D41F](https://anime-sparkle-text-58.pages.dev/symbol/sym-1d41f/)
- [SYM 260B](https://pure-line-unicode-95.pages.dev/symbol/sym-260b/)
- [SYM 1F60E](https://anime-sparkle-text-58.pages.dev/symbol/sym-1f60e/)
- [BORDERS DIVIDERS](https://synth-crosshair-text-47.pages.dev/pt/borders-dividers/)
- [SYM 1D424](https://anime-sparkle-text-58.pages.dev/symbol/sym-1d424/)
- [HIGH VOLTAGE LIGHTNING](https://vintage-lace-fonts-79.pages.dev/symbol/high-voltage-lightning/)
- [SYM 1F498](https://mystic-occult-fonts-26.pages.dev/symbol/sym-1f498/)
- [SYM 262B](https://dark-poetry-fonts-30.pages.dev/symbol/sym-262b/)
- [SYM 2627](https://matrix-hacker-fonts-85.pages.dev/symbol/sym-2627/)
- [SINGLE EIGHTH MUSICAL NOTE](https://anime-sparkle-text-58.pages.dev/symbol/single-eighth-musical-note/)
- [BRACKETS](https://gothic-bio-fonts-98.pages.dev/ja/brackets/)
- [ZODIAC CELESTIAL](https://synth-crosshair-text-47.pages.dev/ru/zodiac-celestial/)
- [SYM 1D44A](https://classic-poetry-fonts-16.pages.dev/symbol/sym-1d44a/)
- [SYM 2631](https://archival-rune-symbols-42.pages.dev/symbol/sym-2631/)
- [SYM 1D462](https://anime-sparkle-text-58.pages.dev/symbol/sym-1d462/)
- [SYM 26B2](https://anime-sparkle-text-45.pages.dev/symbol/sym-26b2/)
- [SYM 1F920](https://mystic-occult-fonts-26.pages.dev/symbol/sym-1f920/)
- [SYM 26ED](https://matrix-terminal-fonts-30.pages.dev/symbol/sym-26ed/)
- [SYM 2646](https://kawaii-kaomoji-hub-99.pages.dev/symbol/sym-2646/)
- [SYM 2749](https://anime-sparkle-text-45.pages.dev/symbol/sym-2749/)
- [KAOMOJI](https://dark-poetry-fonts-30.pages.dev/vi/kaomoji/)
- [SYM 1F62C](https://matrix-terminal-fonts-30.pages.dev/symbol/sym-1f62c/)
- [SYM 1D43B](https://synth-crosshair-text-47.pages.dev/symbol/sym-1d43b/)
- [SYM 1F914](https://anime-sparkle-text-58.pages.dev/symbol/sym-1f914/)
- [SYM 2666](https://angelic-coquette-text-10.pages.dev/symbol/sym-2666/)
- [SYM 26E4](https://archival-rune-symbols-42.pages.dev/symbol/sym-26e4/)
- [BEAMED EIGHTH NOTES](https://synth-crosshair-text-47.pages.dev/symbol/beamed-eighth-notes/)
- [SYM 1F971](https://matrix-terminal-fonts-30.pages.dev/symbol/sym-1f971/)
- [BLACK HEART](https://vintage-lace-fonts-79.pages.dev/symbol/black-heart/)
- [SYM 1F621](https://anime-sparkle-text-51.pages.dev/symbol/sym-1f621/)
- [SYM 1D447](https://synth-crosshair-text-47.pages.dev/symbol/sym-1d447/)
- [SYM 26D3](https://matrix-hacker-fonts-85.pages.dev/symbol/sym-26d3/)
- [GAMING WEAPONS](https://dark-poetry-fonts-30.pages.dev/ru/gaming-weapons/)
- [SYM 2733](https://matrix-hacker-fonts-85.pages.dev/symbol/sym-2733/)
- [SYM 1F916](https://dark-poetry-fonts-30.pages.dev/symbol/sym-1f916/)
- [SYM 1D4A0](https://anime-sparkle-text-58.pages.dev/symbol/sym-1d4a0/)
- [SYM 2616](https://classic-poetry-fonts-16.pages.dev/symbol/sym-2616/)
- [SYM 1F60B](https://anime-sparkle-text-58.pages.dev/symbol/sym-1f60b/)
- [SYM 26B6](https://matrix-hacker-fonts-85.pages.dev/symbol/sym-26b6/)
- [SYM 26C2](https://matrix-hacker-fonts-85.pages.dev/symbol/sym-26c2/)
- [SYM 26E9](https://cyber-clan-tags-15.pages.dev/symbol/sym-26e9/)
- [SYM 2674](https://anime-sparkle-text-45.pages.dev/symbol/sym-2674/)
- [SYM 1F479](https://gothic-bio-fonts-98.pages.dev/symbol/sym-1f479/)
- [SYM 1F60C](https://anime-sparkle-text-58.pages.dev/symbol/sym-1f60c/)
- [STAR OPERATOR](https://synthwave-text-art-35.pages.dev/symbol/star-operator/)
- [SYM 2742](https://angelic-soft-text-59.pages.dev/symbol/sym-2742/)
- [SYM 2666](https://anime-sparkle-text-45.pages.dev/symbol/sym-2666/)
- [FOUR POINT STAR SPARKLE](https://angelic-coquette-text-10.pages.dev/symbol/four-point-star-sparkle/)
- [LEFT RIGHT EXCHANGE ARROWS](https://vintage-lace-fonts-79.pages.dev/symbol/left-right-exchange-arrows/)
- [SYM 2731](https://pure-line-unicode-95.pages.dev/symbol/sym-2731/)
- [RU](https://dark-poetry-fonts-30.pages.dev/ru/)
- [SYM 1D45E](https://anime-sparkle-text-58.pages.dev/symbol/sym-1d45e/)
- [SYM 1F928](https://gothic-bio-fonts-98.pages.dev/symbol/sym-1f928/)
- [SYM 1D41A](https://vintage-lace-symbols-65.pages.dev/symbol/sym-1d41a/)
- [STARS](https://chibi-heart-symbols-15.pages.dev/vi/stars/)
- [SYM 1F63D](https://pink-ribbon-fonts-28.pages.dev/symbol/sym-1f63d/)
- [SYM 26CE](https://chibi-heart-symbols-15.pages.dev/symbol/sym-26ce/)
- [SYM 2617](https://dark-poetry-fonts-30.pages.dev/symbol/sym-2617/)
- [SYM 1D4A3](https://minimal-star-symbols-20.pages.dev/symbol/sym-1d4a3/)
- [SYM 1D435](https://anime-sparkle-text-58.pages.dev/symbol/sym-1d435/)
- [SYM 26E0](https://matrix-hacker-fonts-85.pages.dev/symbol/sym-26e0/)
- [SYM 2645](https://cute-face-emoticons-66.pages.dev/symbol/sym-2645/)
- [SYM 26DB](https://matrix-hacker-fonts-85.pages.dev/symbol/sym-26db/)
- [COQUETTE BOW RIBBON](https://vintage-lace-fonts-79.pages.dev/symbol/coquette-bow-ribbon/)
- [EIGHT POINTED STAR](https://archival-rune-symbols-42.pages.dev/symbol/eight-pointed-star/)
- [SYM 2613](https://dark-poetry-fonts-30.pages.dev/symbol/sym-2613/)
- [SYM 260B](https://dark-poetry-fonts-30.pages.dev/symbol/sym-260b/)
- [SYM 1F9D0](https://dark-poetry-fonts-30.pages.dev/symbol/sym-1f9d0/)
- [BORDERS DIVIDERS](https://archival-rune-symbols-42.pages.dev/ja/borders-dividers/)
- [SYM 273D](https://pure-line-unicode-95.pages.dev/symbol/sym-273d/)
- [SYM 26CF](https://anime-sparkle-text-76.pages.dev/symbol/sym-26cf/)
- [SYM 1D402](https://anime-sparkle-text-58.pages.dev/symbol/sym-1d402/)
- [SYM 260D](https://dark-poetry-fonts-30.pages.dev/symbol/sym-260d/)
- [SYM 26FE](https://dark-poetry-fonts-30.pages.dev/symbol/sym-26fe/)
- [SYM 26BD](https://mystic-occult-fonts-26.pages.dev/symbol/sym-26bd/)
- [SYM 1F62C](https://anime-sparkle-text-58.pages.dev/symbol/sym-1f62c/)
- [SYM 1D437](https://synth-crosshair-text-47.pages.dev/symbol/sym-1d437/)
- [SYM 1D465](https://anime-sparkle-text-58.pages.dev/symbol/sym-1d465/)
