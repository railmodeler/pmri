"""``python -m pmri_pmri`` entry point.

Equivalent to the ``pmri`` script installed by pyproject's
``console_scripts``, provided for the "pre-install editable, test
without a global shim" workflow and for Windows users who prefer
``python -m`` over putting ``%USERPROFILE%\\.local\\Scripts`` on PATH.
"""
from __future__ import annotations

import sys

from pmri_pmri.cli import main

sys.exit(main())
