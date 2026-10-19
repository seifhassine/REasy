import os
from setuptools import setup, Extension


compile_args = ['/O2'] if os.name == 'nt' else ['-O3']
ext_modules = []
for module_name in ('fast_pakresolve', 'fast_path_search', 'fast_string_scan', 'fastmesh', 'fastmotion', 'fastsdftex'):
    source_path = f'native/{module_name}.c'
    if os.path.exists(source_path):
        ext_modules.append(
            Extension(
                module_name,
                sources=[source_path],
                extra_compile_args=compile_args,
                py_limited_api=module_name == 'fastsdftex',
            )
        )

if os.name == 'nt':
    ext_modules.append(
        Extension(
            'fastmatpreview',
            sources=['native/fastmatpreview.cpp'],
            extra_compile_args=['/O2', '/EHsc', '/std:c++17'],
            libraries=['d3d12', 'dxgi'],
            py_limited_api=True,
        )
    )
    ext_modules.append(
        Extension(
            'fastdxil',
            sources=['native/fastdxil.cpp'],
            extra_compile_args=['/O2', '/EHsc', '/std:c++17'],
            py_limited_api=True,
        )
    )

setup(name='reasy-native', version='0.1.0', ext_modules=ext_modules)
