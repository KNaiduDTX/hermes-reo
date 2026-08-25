# Root conftest — makes repo-root imports work under pytest regardless of CWD
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
