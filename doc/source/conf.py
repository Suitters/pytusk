#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

"""Sphinx configuration for pytusk documentation."""

import os
import sys

sys.path.insert(0, os.path.abspath("../.."))
import pytusk

project = "pytusk"
copyright = "Frank V. Castellucci"
author = "Frank V. Castellucci"
version = pytusk.version.__version__
release = version

extensions = ["sphinx.ext.autodoc", "sphinx.ext.napoleon"]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "sphinx_rtd_theme"
html_theme_options = {
    "sticky_navigation": False,
    "navigation_depth": 4,
}
autodoc_default_options = {
    "members": True,
    "member-order": "bysource",
    "special-members": "__init__",
    "undoc-members": True,
    "exclude-members": "__weakref__",
}
html_static_path = []
