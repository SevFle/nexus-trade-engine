from setuptools import find_packages, setup

setup(
    name="nexus-trade-sdk",
    version="0.1.0",
    description="SDK for building Nexus Trade Engine strategy plugins",
    long_description=open("../README.md").read() if __import__("os").path.exists("../README.md") else "",
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
