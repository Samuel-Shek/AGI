#!/bin/zsh
cd -- "${0:A:h}" || exit 1
if /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -B local_web.py start; then
  /usr/bin/open 'http://127.0.0.1:8768'
fi
