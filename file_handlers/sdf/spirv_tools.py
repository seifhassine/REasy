import os
from pathlib import Path
import subprocess

from .spirv import SpirvError
from tools.shader_tools import PAGES, place_msvc_runtime, prompt_shader_tool_download
from utils.app_paths import downloads_directory


def find_spirv_tool(name):
    if name not in {"spirv-val", "spirv-opt"}:
        raise ValueError("Unsupported SPIR-V tool")
    executable = name + (".exe" if os.name == "nt" else "")
    candidates = [Path(value) / executable for value in (
        os.environ.get("REASY_SPIRV_TOOLS_DIR"),
        str(downloads_directory("spirv_tools")),
        str(Path(os.environ["VULKAN_SDK"]) / "Bin") if os.environ.get("VULKAN_SDK") else None,
    ) if value]
    for path in candidates:
        if path.is_file():
            return path
    raise SpirvError(f"{name} is required for native Vulkan material authoring. "
                     "REasy will offer to download it when needed, or get it from "
                     f"{PAGES['spirv_tools']} and put the files in {downloads_directory('spirv_tools')}.")


def run_spirv_tool(name, module, *arguments):
    try:
        executable = find_spirv_tool(name)
    except SpirvError:
        if not prompt_shader_tool_download("spirv_tools"):
            raise
        executable = find_spirv_tool(name)
    if executable.parent == downloads_directory("spirv_tools"):
        place_msvc_runtime(executable.parent)
    result = subprocess.run([str(executable), *arguments, "-"], input=module,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise SpirvError(f"{name}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def validate_spirv(module):
    # Vulkan 1.1 enables relaxed block layout, the DX-layout compiler contract.
    run_spirv_tool("spirv-val", module, "--target-env", "vulkan1.1")


def finalize_spirv(module, *, preserve_bindings=False):
    validate_spirv(module)
    result = run_spirv_tool("spirv-opt", module, "--target-env=vulkan1.1",
                            *(("--preserve-bindings",) if preserve_bindings else ()),
                            "--preserve-interface", "--preserve-spec-constants",
                            "--inline-entry-points-exhaustive", "--eliminate-dead-functions",
                            "--eliminate-dead-code-aggressive", "--compact-ids", "-o", "-")
    validate_spirv(result)
    return result
