import platform
import sys
from pathlib import Path

from utils.app_paths import application_root
lazy from PySide6.QtCore import QCoreApplication, QThread
lazy from PySide6.QtWidgets import QApplication, QMessageBox


def shader_tool_directory(name):
    return application_root() / "downloads" / name


class ShaderToolDownloadCancelled(Exception):
    pass


_SPIRV_HEADERS_REVISION = "496543121ce6419f23d6fa5d7194ba66c36212d2"

PACKAGES = {
    "dxc": (
        "Microsoft DirectX Shader Compiler",
        (("https://github.com/microsoft/DirectXShaderCompiler/releases/download/v1.9.2607/dxc_2026_07_29.zip",
          "a1dfb116ba3eeae6a1582291b53a8e7bf65ad760676bd3194685c8f7367cd241",
          ("bin/x64/dxcompiler.dll", "bin/x64/dxil.dll", "LICENSE-LLVM.txt", "LICENSE-MS.txt")),),
    ),
    # Khronos publishes SPIR-V Tools binaries only through CI storage.
    "spirv_tools": (
        "Khronos SPIR-V Tools",
        (("https://storage.googleapis.com/spirv-tools/artifacts/prod/graphics_shader_compiler/spirv_tools/"
          "windows-vs2022-amd64-release/continuous/212/20260918-092032/install.zip",
          "c64a00c40585150153a12e6d22799ee1b6c8c84bf9140e7c39559fad1124628d",
          ("install/bin/spirv-val.exe", "install/bin/spirv-opt.exe", "install/bin/SPIRV-Tools-shared.dll")),
         (f"https://codeload.github.com/KhronosGroup/SPIRV-Headers/zip/{_SPIRV_HEADERS_REVISION}",
          "b33782a6ccd124e6e02634f6382456b0aba2242c6ec5b68d07be20ee77a5b6bc",
          tuple(f"SPIRV-Headers-{_SPIRV_HEADERS_REVISION}/{path}" for path in (
              "include/spirv/unified1/spirv.core.grammar.json",
              "include/spirv/unified1/extinst.glsl.std.450.grammar.json", "LICENSE")))),
    ),
}


def prompt_shader_tool_download(name):
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"} or sys.maxsize <= 2**32:
        return False
    from tools.github_downloader import _download_and_extract_archive

    app = QApplication.instance()
    if not isinstance(app, QApplication) or QThread.currentThread() != app.thread():
        return False
    title, archives = PACKAGES[name]
    directory = shader_tool_directory(name)
    parent = app.activeWindow()
    message = QCoreApplication.translate(
        "ShaderTools", "{tool} is required for this operation. Download it to REasy's downloads folder now?"
    ).format(tool=title)
    if QMessageBox.question(parent, QCoreApplication.translate("ShaderTools", "Download required shader tool?"), message,
                            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
        raise ShaderToolDownloadCancelled("Shader tool download cancelled.")
    for url, checksum, files in archives:
        if not all((directory / Path(file).name).is_file() for file in files):
            _download_and_extract_archive(url, directory, title,
                                          parent_window=parent, files=files, sha256=checksum)
    return True
