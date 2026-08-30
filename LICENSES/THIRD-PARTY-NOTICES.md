# Third-party notices

Organizador is distributed under the MIT License. The Windows package also
contains the following independent open-source components. Each component
remains governed by its own license.

## Qt and Qt for Python

The package uses unmodified, dynamically linked Qt 6 libraries through
PySide6-Essentials and shiboken6. They are used under the GNU Lesser General
Public License version 3. The distribution does not prohibit reverse
engineering for debugging modifications to those libraries. Compatible Qt
DLLs can be replaced in the package's `_internal` directory.

- PySide6-Essentials 6.11.2 and shiboken6 6.11.2
- License: LGPL-3.0-only (the packages also offer GPL/commercial alternatives)
- Source: https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.11.2
- Qt source: https://download.qt.io/archive/qt/6.11/6.11.2/single/
- License texts: `LGPL-3.0.txt` and `GPL-3.0.txt`

No Qt or PySide source files were modified for this distribution.

## Python and packaging

- Python 3.13: Python Software Foundation License Version 2. The exact license
  from the interpreter used to build the package is in `PYTHON-LICENSE.txt`.
- PyInstaller bootloader 6.22.2: GPL-2.0-or-later with the PyInstaller
  bootloader exception. Its exact `COPYING.txt` is collected under `packages`.

## Python libraries

| Component | Version | License | Project |
| --- | --- | --- | --- |
| defusedxml | 0.7.1 | PSF-2.0 | https://github.com/tiran/defusedxml |
| openpyxl | 3.1.5 | MIT | https://openpyxl.readthedocs.io |
| et_xmlfile | 2.0.0 | MIT | https://foss.heptapod.net/openpyxl/et_xmlfile |
| pypdf | 6.16.2 | BSD-3-Clause | https://pypdf.readthedocs.io |
| python-docx | 1.2.0 | MIT | https://github.com/python-openxml/python-docx |
| python-pptx | 1.0.2 | MIT | https://github.com/scanny/python-pptx |
| lxml | 6.1.2 | BSD-3-Clause | https://lxml.de |
| Pillow | 12.3.0 | MIT-CMU | https://python-pillow.github.io |
| XlsxWriter | 3.2.9 | BSD-2-Clause | https://xlsxwriter.readthedocs.io |
| RapidFuzz | 3.14.5 | MIT | https://github.com/rapidfuzz/RapidFuzz |
| watchdog | 6.0.0 | Apache-2.0 | https://github.com/gorakhargosh/watchdog |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |

The build copies the exact license, copying, notice, and author files supplied
by these installed distributions into the `packages` subdirectory. Copyright
notices in those files are retained verbatim.

Qt itself includes additional third-party software. Its authoritative notices
and corresponding source references are published with the Qt 6.11.2 source
distribution linked above.
