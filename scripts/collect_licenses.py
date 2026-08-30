"""Collect exact installed-package license files into a release folder."""

from __future__ import annotations

import argparse
import shutil
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

PACKAGES = (
    "PySide6-Essentials",
    "shiboken6",
    "defusedxml",
    "openpyxl",
    "pypdf",
    "python-docx",
    "python-pptx",
    "RapidFuzz",
    "watchdog",
    "Pillow",
    "lxml",
    "et_xmlfile",
    "XlsxWriter",
    "typing_extensions",
    "pyinstaller",
)
NOTICE_NAMES = ("license", "licence", "copying", "notice", "authors")
MANUALLY_DOCUMENTED_PACKAGES = {"PySide6-Essentials", "shiboken6"}


def main() -> None:
    """Copy package and Python runtime notices without modifying their text."""

    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    packages_dir = args.output / "packages"
    packages_dir.mkdir(exist_ok=True)

    for package_name in PACKAGES:
        try:
            package = distribution(package_name)
        except PackageNotFoundError as exc:
            raise RuntimeError(f"Dependência sem metadados: {package_name}") from exc
        destination = packages_dir / f"{package.metadata['Name']}-{package.version}"
        copied = 0
        for relative in package.files or ():
            filename = relative.name.casefold()
            if not filename.startswith(NOTICE_NAMES):
                continue
            source = Path(package.locate_file(relative))
            if not source.is_file() or "commercial" in filename:
                continue
            destination.mkdir(exist_ok=True)
            target = destination / f"{copied + 1:02}-{relative.name}"
            shutil.copy2(source, target)
            copied += 1
        if copied == 0 and package_name not in MANUALLY_DOCUMENTED_PACKAGES:
            raise RuntimeError(f"Dependência sem aviso de licença: {package_name}")

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError(f"Licença do Python não encontrada: {python_license}")
    shutil.copy2(python_license, args.output / "PYTHON-LICENSE.txt")


if __name__ == "__main__":
    main()
