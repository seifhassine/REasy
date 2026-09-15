import os
from setuptools import setup, Extension


compile_args = ['/O2'] if os.name == 'nt' else ['-O3']
ext_modules = []
for module_name in ('fast_pakresolve', 'fast_string_scan', 'fastmesh'):
    source_path = f'native/{module_name}.c'
    if os.path.exists(source_path):
        ext_modules.append(
            Extension(
                module_name,
                sources=[source_path],
                extra_compile_args=compile_args,
            )
        )

setup(name='reasy-native', version='0.1.0', ext_modules=ext_modules)
