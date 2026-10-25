import importlib
import importlib.util
import os
import sys
import tempfile
import shutil
import hashlib
from pathlib import Path


class NativeModuleError(ImportError):
    pass


def _build_native_module(module_name):
    # Already imported: no need to read and hash the source again.
    if module_name in sys.modules:
        return sys.modules[module_name]
    src = Path(__file__).resolve().parent.parent / 'native' / f'{module_name}.c'
    cache = src.parent / '.cache' / sys.implementation.cache_tag
    cached = cache / (module_name + '-' + hashlib.sha256(src.read_bytes()).hexdigest()[:16]) if src.exists() else None
    if module_name not in sys.modules and cached is not None and cached.is_dir():
        for path in cached.glob(module_name + '.*'):
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is not None:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    sys.modules[module_name] = module
                    return module
            except (ImportError, OSError):
                continue
    try:
        spec = importlib.util.find_spec(module_name)
        origin = Path(spec.origin) if spec is not None and spec.origin else None
        compiled_is_current = (
            src.exists()
            and origin is not None
            and origin.exists()
            and origin.stat().st_mtime >= src.stat().st_mtime
        )
        if (
            module_name in sys.modules
            or not src.exists()
            or compiled_is_current
        ):
            return importlib.import_module(module_name)
    except (ImportError, OSError):
        pass

    if not src.exists():
        raise NativeModuleError(f"{module_name} is not built and its source native/{module_name}.c is missing")
    try:
        from setuptools import Distribution, Extension
        from setuptools.command.build_ext import build_ext as _build_ext
    except ImportError as exc:
        raise NativeModuleError(f"{module_name} is not built, and building it needs setuptools: {exc}") from exc

    extra_compile_args = ['/O2'] if os.name == 'nt' else ['-O3']
    extra_link_args = []

    ext = Extension(
        module_name,
        sources=[str(src)],
        extra_compile_args=extra_compile_args,
        extra_link_args=extra_link_args,
        py_limited_api=module_name == 'fastsdftex',
    )

    tmpdir = Path(tempfile.mkdtemp())
    try:
        dist = Distribution({'name': module_name, 'ext_modules': [ext]})
        cmd = _build_ext(dist)
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
            raise NativeModuleError(f"building {module_name} produced no extension module")

        target_dir = Path(__file__).resolve().parent.parent
        try:
            shutil.copy2(built, target_dir / built.name)
        except PermissionError:
            # Windows keeps extension modules locked while another viewer is using them.
            cached.mkdir(parents=True, exist_ok=True)
            shutil.copy2(built, cached / built.name)
            target_dir = cached
        sys.path.insert(0, str(target_dir))
        return importlib.import_module(module_name)
    except NativeModuleError:
        raise
    except Exception as exc:
        raise NativeModuleError(
            f"{module_name} could not be built ({exc}); build the native extensions with "
            "'setup.py build_ext --inplace'") from exc
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def ensure_fast_pakresolve():
    return _build_native_module('fast_pakresolve')


def ensure_fast_string_scan():
    return _build_native_module('fast_string_scan')


def ensure_fast_path_search():
    return _build_native_module('fast_path_search')


def ensure_fastmesh():
    return _build_native_module('fastmesh')


def ensure_fastmotion():
    return _build_native_module('fastmotion')


def ensure_fastsdftex():
    return _build_native_module('fastsdftex')


def ensure_fastkey():
    return _build_native_module('fastkey')
