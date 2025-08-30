from setuptools import setup, Extension
import os

ext = Extension(
    'fast_pakresolve',
    sources=['native/fast_pakresolve.c'],
    extra_compile_args=['/O2'] if os.name == 'nt' else ['-O3']
)

setup(
    name='fast_pakresolve',
    version='0.1.0',
    ext_modules=[ext],
)

