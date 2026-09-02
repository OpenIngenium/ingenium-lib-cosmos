import os
from setuptools import setup, find_packages


def get_dependencies():
    deps=[
        'requests>=2.31.0',
        'urllib3>=1.26.0',
    ]
    return deps

setup(
    name='ing_lib_cosmos',
    packages=find_packages(),
    install_requires=get_dependencies(),
    description='Python libraries supporting OpenIngenium and COSMOS',
    version='0.1.0',
    python_requires='>=3.10',
    entry_points = {
    },
    author='Christopher Swan',
    author_email='open-ingenium@jpl.nasa.gov',
    url='https://github.com/OpenIngenium/ingenium-lib-cosmos',
    classifiers=[
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
        'Programming Language :: Python :: 3.14',
    ],
)