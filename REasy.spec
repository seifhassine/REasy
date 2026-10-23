# -*- mode: python ; coding: utf-8 -*-
import re

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [('file_handlers/mesh/blender/reasy_blender.py', 'file_handlers/mesh/blender')]
binaries = [('.cache/gdeflate/libGDeflate.dll', 'tools/runtimes/win-x64/native'), ('.cache/msvc/msvcp140.dll', '.')]
hiddenimports = ['fast_pakresolve', 'fast_path_search', 'fast_string_scan', 'fastmesh', 'fastmotion', 'fastsdftex',
                 'fastmatpreview', 'fastdxil', 'texture2ddecoder']
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
# Audio plays through Qt's Windows Media Foundation backend, so leave out the FFmpeg one.
FFMPEG = re.compile(r'(?i)(ffmpegmediaplugin|avcodec-\d+|avformat-\d+|avutil-\d+|swresample-\d+|swscale-\d+)\.dll$')
# Windows ships the UCRT; a bundled copy captures ctypes.CDLL("ucrtbase") and fails to load.
UCRT = re.compile(r'(?i)^(ucrtbase(\.dll)?|api-ms-win-.+\.dll)$')
a.binaries = [entry for entry in a.binaries if not (FFMPEG.search(entry[0]) or UCRT.match(entry[0]))]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='REasy',
    upx=False,
    console=False,
    version='version.txt',
    icon=['resources/icons/reasy_editor_logo.ico'],
)
