#!/usr/bin/env python3
import os
import glob
import h5py
import cv2
import numpy as np

# Hard-coded directory containing the episode_x.h5 files
directory_path = "/home/vignesh/maurice-prod/data/Pick up leaf"

# Get list of all episode_*.h5 files (sorted by name)
h5_files = sorted(glob.glob(os.path.join(directory_path, "episode_*.h5")))

for h5_file in h5_files:
    print(f"Processing {h5_file} ...")
    try:
        # Open the file in read/write mode so we can modify it in-place.
        with h5py.File(h5_file, 'r+') as f:
            if "observations/images" not in f:
                print(f"  -> No 'observations/images' group found in {h5_file}. Skipping.")
                continue

            images_group = f["observations/images"]
            # Using list() to iterate keys as we'll be modifying the group.
            for camera in list(images_group.keys()):
                ds = images_group[camera]
                data = ds[()]
                print(f"  Processing camera: {camera}, original shape: {data.shape}")

                # Resize image(s) based on the shape of the dataset
                if data.ndim == 3:
                    # Single image: (height, width, channels)
                    resized = cv2.resize(data, (640, 480))
                elif data.ndim == 4:
                    # Multiple images: (num_images, height, width, channels)
                    resized_list = []
                    for i in range(data.shape[0]):
                        resized_img = cv2.resize(data[i], (640, 480))
                        resized_list.append(resized_img)
                    resized = np.stack(resized_list)
                else:
                    print(f"  -> Unexpected shape {data.shape} for camera {camera}. Skipping.")
                    continue

                # Delete the old dataset and create a new one with the resized data
                del images_group[camera]
                images_group.create_dataset(camera, data=resized, compression="gzip")
                print(f"  Updated camera: {camera} with new shape: {resized.shape}")
    except Exception as e:
        print(f"Error processing {h5_file}: {e}")

print("All episodes processed.")
