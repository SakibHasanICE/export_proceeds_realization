from pathlib import Path

from setuptools import find_packages, setup

setup(
    name="export-proceeds-automation",
    version="0.1.0",
    description="Bank export advice -> ERP Export Proceeds Realization automation",
    packages=find_packages(exclude=["tests"]),
    include_package_data=True,
    package_data={"app.config": ["account_map.json"]},
    install_requires=[l.strip() for l in Path("requirements.txt").read_text().splitlines()
                      if l.strip() and not l.startswith("#") and not l.startswith("pytest")],
    python_requires=">=3.10",
)
