from setuptools import find_packages, setup

_long_description = ""
if __import__("os").path.exists("../README.md"):
    with open("../README.md") as _f:
        _long_description = _f.read()

setup(
    name="nexus-trade-sdk",
    version="0.1.0",
    description="SDK for building Nexus Trade Engine strategy plugins",
    long_description=_long_description,
    long_description_content_type="text/markdown",
    author="Nexus Trade Engine",
    license="MIT",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "pydantic>=2.7.0",
        "pyyaml>=6.0.1",
    ],
    extras_require={
        "dev": [
            "pytest>=8.0.0",
            "pandas>=2.2.0",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3.11",
        "License :: OSI Approved :: MIT License",
        "Topic :: Office/Business :: Financial :: Investment",
    ],
)
