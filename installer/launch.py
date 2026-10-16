#!/usr/bin/env python3
"""Entry point for the compact source bundle; installed runtime stays compatible."""

from pathlib import Path
import runpy
import sys

sys.dont_write_bytecode = True
source = Path(__file__).resolve().parent / '.misbot'
if source.is_symlink() or not (source / 'misbot.py').is_file():
    print('Комплект неполный. Распакуйте проверенный архив Misbot заново.', file=sys.stderr)
    raise SystemExit(2)
sys.path.insert(0, str(source))
sys.argv[0] = str(source / 'misbot.py')
runpy.run_path(str(source / 'misbot.py'), run_name='__main__')
