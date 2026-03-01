"""
pytest configuration — adds the project root to sys.path so tests can import
bot modules without needing `pip install -e .`.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
