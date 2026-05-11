#!/usr/bin/env python3
import os
import shutil
import json
from pathlib import Path

def sync_results():
    # Get paths
    script_dir = Path(__file__).parent
    source_dir = script_dir.parent / 'results'
    target_dir = script_dir / 'public' / 'results'

    # Create target directory if it doesn't exist
    target_dir.mkdir(parents=True, exist_ok=True)

    print(f"Syncing results from {source_dir} to {target_dir}")

    # Get list of benchmark directories
    benchmark_names = []
    for item in source_dir.iterdir():
        if item.is_dir():
            benchmark_names.append(item.name)
            target_benchmark_dir = target_dir / item.name
            
            # Remove existing directory if it exists
            if target_benchmark_dir.exists():
                shutil.rmtree(target_benchmark_dir)
            
            # Copy directory with all contents
            shutil.copytree(item, target_benchmark_dir)
            print(f"Copied benchmark directory: {item.name}")

    # Write index.json with list of benchmarks
    with open(target_dir / 'index.json', 'w') as f:
        json.dump({'benchmarks': benchmark_names}, f)
    print("Created index.json")

    print("\nSync complete!")
    print(f"\nResults are now available in: {target_dir}")

if __name__ == "__main__":
    sync_results()