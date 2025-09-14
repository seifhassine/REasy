import importlib
import os
import sys
import tempfile
import shutil
from pathlib import Path

def _build_native_module(module_name):
    try:
        return importlib.import_module(module_name)
    except Exception:
        pass

    try:
        from setuptools import Distribution, Extension
        from setuptools.command.build_ext import build_ext as _build_ext
    except Exception:
        return None

    src = Path(__file__).resolve().parent.parent / 'native' / f'{module_name}.c'
    if not src.exists():
        return None

    extra_compile_args = ['/O2'] if os.name == 'nt' else ['-O3']
    extra_link_args = []

    ext = Extension(
        module_name,
        sources=[str(src)],
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
    )

    class BuildExt(_build_ext):
        def build_extensions(self):
            super().build_extensions()

    tmpdir = Path(tempfile.mkdtemp())
    try:
        dist = Distribution({'name': module_name, 'ext_modules': [ext]})
        cmd = BuildExt(dist)
        cmd.initialize_options()
        cmd.build_lib = str(tmpdir)
        cmd.build_temp = str(tmpdir / 'tmp')
        cmd.finalize_options()
        cmd.run()


        built = None
        for p in tmpdir.rglob(f'{module_name}.*'):
            if p.suffix in ('.pyd', '.so', '.dll', '.dylib') or '.cpython-' in p.name:
                built = p
                break
        if not built:
            return None

        target_dir = Path(__file__).resolve().parent.parent
        shutil.copy2(built, target_dir / built.name)
        sys.path.insert(0, str(target_dir))
        return importlib.import_module(module_name)
    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def ensure_fast_pakresolve():
    return _build_native_module('fast_pakresolve')


def ensure_fastmesh():
    return _build_native_module('fastmesh')
