from setuptools import setup, Extension
import os

ext_modules = []

if os.path.exists('native/fast_pakresolve.c'):
    ext_modules.append(
        Extension(
            'fast_pakresolve',
            sources=['native/fast_pakresolve.c'],
            extra_compile_args=['/O2'] if os.name == 'nt' else ['-O3']
        )
    )

if os.path.exists('native/fastmesh.c'):
    ext_modules.append(
        Extension(
            'fastmesh',
            sources=['native/fastmesh.c'],
            extra_compile_args=['/O2'] if os.name == 'nt' else ['-O3']
        )
    )

setup(name='reasy-native', version='0.1.0', ext_modules=ext_modules)
