"""
HelloML API Package

AI voice agent platform with phone provisioning and RAG capabilities.
"""

import os

# Read version from VERSION file
version_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'VERSION')
with open(version_file, 'r') as f:
    __version__ = f.read().strip()
