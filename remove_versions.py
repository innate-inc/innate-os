#!/usr/bin/env python3
"""
Simple script to remove version specifications from requirements.txt files.
Creates a new file with just package names.
"""

import re
import sys
from pathlib import Path

def remove_versions_from_requirements(input_file, output_file=None):
    """
    Remove version specifications from a requirements file.
    
    Args:
        input_file: Path to the input requirements file
        output_file: Path to the output file (optional, defaults to input_file_no_versions.txt)
    """
    input_path = Path(input_file)
    
    if not input_path.exists():
        print(f"Error: File {input_file} does not exist")
        return False
    
    if output_file is None:
        output_file = input_path.parent / f"{input_path.stem}_no_versions.txt"
    
    # Read the input file
    with open(input_path, 'r') as f:
        lines = f.readlines()
    
    processed_lines = []
    
    for line in lines:
        line = line.strip()
        
        # Skip empty lines and comments
        if not line or line.startswith('#'):
            processed_lines.append(line)
            continue
        
        # Handle git+https URLs - keep them as is
        if line.startswith('git+'):
            # Extract just the package name from git URLs
            if '@' in line:
                # Extract package name from git URL
                package_name = line.split('/')[-1].split('@')[0]
                if '.git' in package_name:
                    package_name = package_name.replace('.git', '')
                processed_lines.append(package_name)
            else:
                processed_lines.append(line)
            continue
        
        # Remove version specifications (==, >=, <=, >, <, ~=, !=)
        # Also handle extras like package[extra]==version
        cleaned_line = re.sub(r'[><=!~]=?[^,\s]*', '', line)
        
        # Remove any trailing commas or whitespace
        cleaned_line = cleaned_line.rstrip(',').strip()
        
        # Handle cases where the package name might have extras [extra]
        # Keep the extras but remove version specs
        if cleaned_line:
            processed_lines.append(cleaned_line)
    
    # Write the output file
    with open(output_file, 'w') as f:
        for line in processed_lines:
            f.write(line + '\n')
    
    print(f"Processed {len(lines)} lines")
    print(f"Output written to: {output_file}")
    return True

def main():
    if len(sys.argv) < 2:
        input_file = "requirements.ubuntu.txt"
        print(f"No input file specified, using default: {input_file}")
    else:
        input_file = sys.argv[1]
    
    output_file = None
    if len(sys.argv) >= 3:
        output_file = sys.argv[2]
    
    success = remove_versions_from_requirements(input_file, output_file)
    if not success:
        sys.exit(1)

if __name__ == "__main__":
    main() 