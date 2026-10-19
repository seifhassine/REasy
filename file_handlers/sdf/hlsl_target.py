from __future__ import annotations

from pathlib import Path

lazy from . import dxc
lazy from .hlsl import compile_hlsl_file_detailed, compile_hlsl_source_detailed


def uses_dxc(target: str) -> bool:
    """D3DCompile only takes Shader Model 5 and earlier."""
    return "_6_" in str(target)


def compile_hlsl_for_target(
    source: str | bytes = "",
    *,
    entry_point: str,
    target: str,
    path: str | Path | None = None,
    debug: bool = False,
    warnings_as_errors: bool = False,
):
    """D3DCompile resolves #include from the file's folder."""
    if uses_dxc(target):
        options = {"entry_point": entry_point, "target": target, "extra_arguments": ("-Zi", "-Od") if debug else ()}
        if path is None:
            return dxc.compile_hlsl_dxc(source, **options)
        return dxc.compile_hlsl_dxc(Path(path).read_bytes(), source_name=str(path), **options)
    if path is not None:
        return compile_hlsl_file_detailed(
            path, entry_point=entry_point, target=target, debug=debug, warnings_as_errors=warnings_as_errors,
        )
    return compile_hlsl_source_detailed(
        source, entry_point=entry_point, target=target, warnings_as_errors=warnings_as_errors,
    )


__all__ = ["compile_hlsl_for_target", "uses_dxc"]
