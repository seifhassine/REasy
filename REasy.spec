# -*- mode: python ; coding: utf-8 -*-
import os
import re
import types

import pefile
from PyInstaller.utils.hooks import collect_all, collect_submodules

pefile.gc = types.SimpleNamespace(collect=lambda: None)

datas = [('file_handlers/mesh/blender/reasy_blender.py', 'file_handlers/mesh/blender')]
binaries = [('.cache/gdeflate/libGDeflate.dll', 'tools/runtimes/win-x64/native'), ('.cache/msvc/msvcp140.dll', '.'),
            ('.cache/dxc/dxcompiler.dll', '.')]
hiddenimports = ['fast_pakresolve', 'fast_path_search', 'fast_string_scan', 'fastmesh', 'fastsdftex',
                 'fastmatpreview', 'fastdxil', 'fastdxiltext', 'fastsurface', 'fastgraph', 'fastjcns', 'fastkey',
                 'fastaudio', 'fasttexenc', 'texture2ddecoder']
hiddenimports += collect_submodules('file_handlers')
texture_datas, texture_binaries, texture_imports = collect_all('texture2ddecoder')
datas += texture_datas
binaries += texture_binaries
hiddenimports += texture_imports

a = Analysis(
    ['REasy.py'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)
# Windows ships the UCRT; a bundled copy captures ctypes.CDLL("ucrtbase") and fails to load.
UCRT = re.compile(r'(?i)^(ucrtbase(\.dll)?|api-ms-win-.+\.dll)$')
a.binaries = [entry for entry in a.binaries if not UCRT.match(entry[0])]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='REasy',
    upx=False,
    console=os.environ.get('REASY_DEBUG') == 'true',
    version='version.txt',
    icon=['resources/icons/reasy_editor_logo.ico'],
)
