"""
Pytest configuration file for Maurice robot controller tests.
"""

import os
import sys

# Add the parent directory to the path so we can import the modules
parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, parent_dir)
