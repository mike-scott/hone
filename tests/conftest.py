# tests/conftest.py — put the repo root on sys.path so `core` and `node`
# import as packages regardless of pytest's import mode or the cwd.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
