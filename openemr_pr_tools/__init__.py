# SPDX-License-Identifier: GPL-3.0-or-later
"""The package's name before 0.3.0. Imports written for it still work: each module here is the
same module as in pr_toolbox."""

import sys

import pr_toolbox
from pr_toolbox import common, landing_order, pr_check, pr_health

__version__ = pr_toolbox.__version__

for _name, _module in {"common": common, "landing_order": landing_order, "pr_check": pr_check, "pr_health": pr_health}.items():
    sys.modules[f"{__name__}.{_name}"] = _module
