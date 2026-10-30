"""Setup for scormxblock XBlock."""

import os
from setuptools import setup


def package_data(pkg, roots):
    """Generic function to find package_data.

    All of the files under each of the `roots` will be declared as package
    data for package `pkg`.

    """
    data = []
    for root in roots:
        for dirname, _, files in os.walk(os.path.join(pkg, root)):
            for fname in files:
                data.append(os.path.relpath(os.path.join(dirname, fname), pkg))

    return {pkg: data}


setup(
    name='scormxblock-xblock',
    # ilmX fork for Open edX Verawood (Python 3.12, Django 5.2, XBlock 6)
    version='1.0.0+ilmx',
    description='SCORM XBlock (ilmX fork, block type "scormxblock")',
    packages=[
        'scormxblock',
    ],
    python_requires='>=3.12',
    install_requires=[
        'XBlock>=2.0',
        'web-fragments',
        'Django>=4.2',
        'WebOb',
        'defusedxml',
    ],
    entry_points={
        'xblock.v1': [
            'scormxblock = scormxblock:ScormXBlock',
        ]
    },
    package_data=package_data("scormxblock", ["static", "public", "translations"]),
    license="Apache",
    classifiers=[
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3.12",
        "Framework :: Django :: 4.2",
        "Framework :: Django :: 5.2",
    ]
)
