#!/usr/bin/env python3
"""
Helper script to split server.py into modular components.
This script extracts sections from server.py and creates separate files.
"""

import re

def extract_section(filepath, start_marker, end_marker=None, end_line=None):
    """Extract a section from the file based on markers."""
    with open(filepath, 'r') as f:
        lines = f.readlines()
    
    start_idx = None
    for i, line in enumerate(lines):
        if start_marker in line:
            start_idx = i
            break
    
    if start_idx is None:
        return None
    
    if end_line:
        end_idx = end_line
    elif end_marker:
        end_idx = None
        for i in range(start_idx + 1, len(lines)):
            if end_marker in lines[i]:
                end_idx = i
                break
        if end_idx is None:
            end_idx = len(lines)
    else:
        end_idx = len(lines)
    
    return ''.join(lines[start_idx:end_idx])

if __name__ == "__main__":
    print("This is a helper script. Run with Python to see available sections.")