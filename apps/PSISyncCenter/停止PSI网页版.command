#!/bin/zsh
cd -- "${0:A:h}" || exit 1
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -B local_web.py stop
