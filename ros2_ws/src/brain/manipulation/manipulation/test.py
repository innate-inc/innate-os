#! /usr/bin/env python3
import os
import shutil
import json

# Explicitly set the base directory
base_dir = '/home/vignesh/data'

# Original directories containing the files relative to the base directory
paper_dirs = ['Paper1', 'Paper2']
# New combined directory relative to the base directory
combined_dir = 'Paper'
combined_dir_path = os.path.join(base_dir, combined_dir)

# Create the combined directory if it doesn't exist
if not os.path.exists(combined_dir_path):
    os.makedirs(combined_dir_path)

combined_metadata = {
    "task_name": "Paper",
    "task_description": "Paper",
    "mobile_task": True,
    "number_of_episodes": 0,
    "episodes": []
}

all_episodes = []

# Process metadata from both directories
for paper in paper_dirs:
    metadata_path = os.path.join(base_dir, paper, 'metadata.json')
    with open(metadata_path, 'r') as f:
        metadata = json.load(f)
    # Add each episode with its source directory path
    for episode in metadata.get('episodes', []):
        # Add absolute source directory to locate the file
        episode['source_dir'] = os.path.join(base_dir, paper)
        all_episodes.append(episode)

# Sort the episodes by their start timestamp
all_episodes.sort(key=lambda ep: ep['start_timestamp'])

# Copy files and update episode metadata with a new global numbering scheme
for idx, episode in enumerate(all_episodes, start=1):
    source_dir = episode.pop('source_dir')  # Remove the temporary field after use
    old_file_name = episode['file_name']
    new_file_name = f"episode_{idx}.h5"
    
    # Update metadata for this episode
    episode['episode_id'] = idx
    episode['file_name'] = new_file_name
    
    # Define source and destination file paths
    src_file_path = os.path.join(source_dir, old_file_name)
    dst_file_path = os.path.join(combined_dir_path, new_file_name)
    
    # Copy the file to the new combined directory with the new name
    shutil.copy(src_file_path, dst_file_path)
    
    # Append the updated episode metadata
    combined_metadata['episodes'].append(episode)

# Update total number of episodes
combined_metadata['number_of_episodes'] = len(combined_metadata['episodes'])

# Write the combined metadata to the new metadata.json in the combined directory
combined_metadata_path = os.path.join(combined_dir_path, 'metadata.json')
with open(combined_metadata_path, 'w') as f:
    json.dump(combined_metadata, f, indent=4)

print(f"Combined {len(all_episodes)} episodes into '{combined_dir_path}' directory.")
