#!/usr/bin/env python3
"""
Parse ROS monitor log file and create a table with one row per iteration.
Columns: Timestamp, SSHD, discovery-server, ros-app, ble-provisioner, WiFi IP,
         Nodes (count), Topics (count), Services (count)
"""

import re
from tabulate import tabulate


def parse_log_file(filepath):
    """Parse the log file and extract data for each iteration."""
    
    iterations = []
    current_iteration = {}
    current_section = None
    section_line_count = 0
    
    with open(filepath, 'r') as f:
        for line in f:
            line_stripped = line.strip()
            
            # Check for iteration marker with timestamp
            if '=== Iteration' in line_stripped:
                # Save previous iteration if exists (with final Services count)
                if current_iteration:
                    if current_section == 'Services':
                        current_iteration['Services'] = section_line_count
                    iterations.append(current_iteration)
                
                # Extract timestamp from line like: [1969-12-31 16:00:46] === Iteration 1 ===
                timestamp_match = re.search(r'\[(.*?)\]', line_stripped)
                iteration_match = re.search(r'Iteration (\d+)', line_stripped)
                
                current_iteration = {
                    'timestamp': timestamp_match.group(1) if timestamp_match else 'N/A',
                    'iteration': int(iteration_match.group(1)) if iteration_match else 0,
                    'SSHD': '',
                    'discovery-server': '',
                    'ros-app': '',
                    'ble-provisioner': '',
                    'WiFi IP': '',
                    'Nodes': 0,
                    'Topics': 0,
                    'Services': 0
                }
                current_section = None
                section_line_count = 0
                continue
            
            # Check for section headers
            if line_stripped.startswith('--- System Status ---'):
                current_section = 'System Status'
                section_line_count = 0
                continue
            elif line_stripped.startswith('--- Nodes ---'):
                current_section = 'Nodes'
                section_line_count = 0
                continue
            elif line_stripped.startswith('--- Topics ---'):
                # Save Nodes count before switching sections
                if current_iteration and current_section == 'Nodes':
                    current_iteration['Nodes'] = section_line_count
                current_section = 'Topics'
                section_line_count = 0
                continue
            elif line_stripped.startswith('--- Services ---'):
                # Save Topics count before switching sections
                if current_iteration and current_section == 'Topics':
                    current_iteration['Topics'] = section_line_count
                current_section = 'Services'
                section_line_count = 0
                continue
            
            # Count lines in current section (lines that start with numbers)
            if current_section in ['Nodes', 'Topics', 'Services']:
                # Check if line starts with whitespace then a number (the item number)
                if re.match(r'^\s*\d+\s', line):
                    section_line_count += 1
            
            # Parse system status lines
            if current_section == 'System Status' and line_stripped and ':' in line_stripped:
                parts = line_stripped.split(':', 1)
                if len(parts) == 2:
                    service = parts[0].strip()
                    status = parts[1].strip()
                    
                    if service in current_iteration:
                        current_iteration[service] = status
        
        # Don't forget the last iteration and its final Services count
        if current_iteration:
            if current_section == 'Services':
                current_iteration['Services'] = section_line_count
            iterations.append(current_iteration)
    
    return iterations


def create_table(iterations):
    """Create formatted table from the parsed data."""
    
    # Prepare table data
    table_data = []
    for it in iterations:
        row = [
            it['iteration'],
            it['timestamp'],
            it['SSHD'],
            it['discovery-server'],
            it['ros-app'],
            it['ble-provisioner'],
            it['WiFi IP'],
            it['Nodes'],
            it['Topics'],
            it['Services']
        ]
        table_data.append(row)
    
    headers = [
        'Iter',
        'Timestamp',
        'SSHD',
        'discovery-server',
        'ros-app',
        'ble-provisioner',
        'WiFi IP',
        'Nodes',
        'Topics',
        'Services'
    ]
    
    print(tabulate(table_data, headers=headers, tablefmt='grid'))
    print(f"\nTotal iterations: {len(iterations)}")


def main():
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python analyze_ros_log.py <log_file>")
        sys.exit(1)
    
    filepath = sys.argv[1]
    
    print(f"\nAnalyzing {filepath}...\n")
    
    iterations = parse_log_file(filepath)
    create_table(iterations)


if __name__ == "__main__":
    main()