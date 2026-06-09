#!/bin/bash
#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

set -euo pipefail

base_dir=${PWD##*/}
if test "$base_dir" = "pytusk"; then
    echo "Removing previous documentation artifacts... if any!"
    rm -f doc/source/pytusk*.rst doc/source/modules.rst

    echo "Generating module RSTs"
    sphinx-apidoc -o doc/source pytusk/

    cd doc
    echo "Building HTML"
    if make html; then
        echo "Docs build success"
        cd ..
        exit 0
    else
        echo "Build failed"
        cd ..
        exit 1
    fi
else
    echo "Command must run from pytusk folder."
fi
