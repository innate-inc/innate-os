#!/usr/bin/env python3

import rosbag2_py
from rosidl_runtime_py.utilities import get_message
from rclpy.serialization import deserialize_message
import argparse
import os
from datetime import datetime
import cv2
import numpy as np
from cv_bridge import CvBridge
import tkinter as tk
from tkinter import filedialog

def nanoseconds_to_datetime(nanoseconds):
    """Convert ROS nanosecond timestamp to readable datetime."""
    seconds = nanoseconds / 1e9
    return datetime.fromtimestamp(seconds)

def extract_chat_dialogue(bag_path, output_file):
    """
    Extract brain/chat_in and brain/chat_out topics from ROS bag and create a dialogue file.
    
    Args:
        bag_path (str): Path to the ROS bag directory
        output_file (str): Path to output text file
    """
    
    # Configure reader
    storage_opts = rosbag2_py.StorageOptions(uri=bag_path, storage_id='sqlite3')
    converter_opts = rosbag2_py.ConverterOptions(
        input_serialization_format='cdr',
        output_serialization_format='cdr'
    )
    
    reader = rosbag2_py.SequentialReader()
    reader.open(storage_opts, converter_opts)
    
    # Get topic ↔ type map
    topics_and_types = {t.name: t.type for t in reader.get_all_topics_and_types()}
    
    # Filter for chat topics
    chat_topics = {topic: msg_type for topic, msg_type in topics_and_types.items() 
                   if 'chat' in topic.lower()}
    
    if not chat_topics:
        print("No chat topics found in the bag file.")
        print("Available topics:", list(topics_and_types.keys()))
        return
    
    print(f"Found chat topics: {list(chat_topics.keys())}")
    
    # Collect all chat messages with timestamps
    chat_messages = []
    
    # Read through the bag
    while reader.has_next():
        topic, serialized_data, timestamp = reader.read_next()
        
        # Only process chat topics
        if topic in chat_topics:
            msg_type = chat_topics[topic]
            msg_cls = get_message(msg_type)
            msg = deserialize_message(serialized_data, msg_cls)
            
            # Determine speaker based on topic
            if 'brain/chat_out' in topic:
                speaker = "Bot"
            elif 'brain/chat_in' in topic:
                speaker = "User"
            else:
                speaker = f"Unknown({topic})"
            
            # Extract message content - this might need adjustment based on your message type
            if hasattr(msg, 'data'):
                content = msg.data
            elif hasattr(msg, 'text'):
                content = msg.text
            elif hasattr(msg, 'message'):
                content = msg.message
            else:
                content = str(msg)
            
            chat_messages.append({
                'timestamp': timestamp,
                'speaker': speaker,
                'content': content,
                'topic': topic
            })
    
    # Sort messages by timestamp
    chat_messages.sort(key=lambda x: x['timestamp'])
    
    # Write dialogue to file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("Chat Dialogue Extracted from ROS Bag\n")
        f.write("=" * 50 + "\n\n")
        
        for msg in chat_messages:
            dt = nanoseconds_to_datetime(msg['timestamp'])
            timestamp_str = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # Remove last 3 digits of microseconds
            
            f.write(f"[{timestamp_str}] {msg['speaker']}: {msg['content']}\n")
            f.write(f"  (Topic: {msg['topic']})\n\n")
    
    print(f"Dialogue extracted to: {output_file}")
    print(f"Total chat messages: {len(chat_messages)}")

def extract_video(bag_path, output_file, fps=30):
    """
    Extract images from /mars/main_camera/left/image_raw/compressed topic and create an MP4 video.
    
    Args:
        bag_path (str): Path to the ROS bag directory
        output_file (str): Path to output MP4 file
        fps (int): Frames per second for the output video
    """
    
    # Configure reader
    storage_opts = rosbag2_py.StorageOptions(uri=bag_path, storage_id='sqlite3')
    converter_opts = rosbag2_py.ConverterOptions(
        input_serialization_format='cdr',
        output_serialization_format='cdr'
    )
    
    reader = rosbag2_py.SequentialReader()
    reader.open(storage_opts, converter_opts)
    
    # Get topic ↔ type map
    topics_and_types = {t.name: t.type for t in reader.get_all_topics_and_types()}
    
    # Find compressed image topics
    image_topics = {topic: msg_type for topic, msg_type in topics_and_types.items() 
                   if 'mars/main_camera/left/image_raw/compressed' in topic or 'mars/arm/image_raw/compressed' in topic}
    
    if not image_topics:
        print("No mars/main_camera/left/image_raw/compressed or mars/arm/image_raw/compressed topics found in the bag file.")
        print("Available topics:", list(topics_and_types.keys()))
        return
    
    print(f"Found compressed image topics: {list(image_topics.keys())}")
    
    bridge = CvBridge()
    video_writer = None
    frame_count = 0
    
    # Read through the bag
    while reader.has_next():
        topic, serialized_data, timestamp = reader.read_next()
        
        # Only process compressed image topics
        if topic in image_topics:
            msg_type = image_topics[topic]
            msg_cls = get_message(msg_type)
            msg = deserialize_message(serialized_data, msg_cls)
            
            try:
                # Convert compressed image message to OpenCV format
                # For CompressedImage messages, we need to decode the data field
                np_arr = np.frombuffer(msg.data, np.uint8)
                cv_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                
                if cv_image is None:
                    print(f"Failed to decode compressed image at frame {frame_count}")
                    continue
                
                # Initialize video writer with first frame dimensions
                if video_writer is None:
                    height, width = cv_image.shape[:2]
                    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                    video_writer = cv2.VideoWriter(output_file, fourcc, fps, (width, height))
                    print(f"Initialized video writer: {width}x{height} at {fps} FPS")
                
                # Write frame to video
                video_writer.write(cv_image)
                frame_count += 1
                
                if frame_count % 100 == 0:
                    print(f"Processed {frame_count} frames...")
                    
            except Exception as e:
                print(f"Error processing compressed image frame {frame_count}: {e}")
                continue
    
    # Clean up
    if video_writer is not None:
        video_writer.release()
        print(f"Video saved to: {output_file}")
        print(f"Total frames processed: {frame_count}")
    else:
        print("No valid compressed image frames found to create video.")

def extract_both(bag_path, chat_output, video_output, fps=30):
    """
    Extract both chat dialogue and video from the bag.
    
    Args:
        bag_path (str): Path to the ROS bag directory
        chat_output (str): Path to output text file
        video_output (str): Path to output MP4 file
        fps (int): Frames per second for the output video
    """
    print("Extracting chat dialogue...")
    extract_chat_dialogue(bag_path, chat_output)
    
    print("\nExtracting video...")
    extract_video(bag_path, video_output, fps)

def select_and_process_bag():
    """
    Open a directory dialog to select a ROS bag directory and process it.
    """
    # Create a root window and hide it
    root = tk.Tk()
    root.withdraw()
    
    print("Opening directory dialog...")
    
    # Open directory dialog
    bag_path = filedialog.askdirectory(
        title="Select ROS bag directory",
        initialdir=os.getcwd()
    )
    
    # Destroy the root window
    root.destroy()
    
    if bag_path:
        print(f"\n📁 Selected bag directory: {bag_path}")
        print(f"🔍 Processing ROS bag...\n")
        
        # Generate output filenames in the same directory
        bag_name = os.path.basename(bag_path)
        chat_output = os.path.join(bag_path, f"{bag_name}_chat_dialogue.txt")
        video_output = os.path.join(bag_path, f"{bag_name}_video.mp4")
        
        # Process both chat and video
        extract_both(bag_path, chat_output, video_output, fps=30)
        
        print("\n" + "=" * 60)
        print("✅ ROS bag processing completed!")
        print(f"📝 Chat dialogue saved to: {chat_output}")
        print(f"🎬 Video saved to: {video_output}")
        
        return True
    else:
        print("No directory selected. Exiting...")
        return False

def main():
    parser = argparse.ArgumentParser(description='Extract chat dialogue and/or video from ROS bag')
    parser.add_argument('--manual', action='store_true',
                       help='Use manual mode with command line arguments instead of dialog')
    parser.add_argument('bag_path', nargs='?', help='Path to the ROS bag directory (manual mode only)')
    parser.add_argument('-o', '--output', default='chat_dialogue.txt', 
                       help='Output text file (manual mode only, default: chat_dialogue.txt)')
    parser.add_argument('-v', '--video', 
                       help='Output MP4 video file (manual mode only)')
    parser.add_argument('--fps', type=int, default=30,
                       help='Frames per second for video output (default: 30)')
    parser.add_argument('--both', action='store_true',
                       help='Extract both chat and video (manual mode only)')
    
    args = parser.parse_args()
    
    # Default behavior: use dialog to select bag and process both
    if not args.manual:
        print("🔍 ROS BAG PROCESSOR")
        print("This tool extracts chat dialogue and video from ROS bags.")
        print("You'll be prompted to select a ROS bag directory.\n")
        
        try:
            select_and_process_bag()
        except KeyboardInterrupt:
            print("\n\nOperation cancelled by user.")
        except Exception as e:
            print(f"\nUnexpected error: {e}")
            return 1
        
        return 0
    
    # Manual mode (original behavior)
    if not args.bag_path:
        print("Error: bag_path is required when using --manual mode.")
        return 1
        
    if not os.path.exists(args.bag_path):
        print(f"Error: Bag path '{args.bag_path}' does not exist.")
        return 1
    
    if args.both:
        # Auto-generate video filename based on chat output
        video_output = args.output.replace('.txt', '.mp4')
        if video_output == args.output:  # If no .txt extension, just add .mp4
            video_output = args.output + '.mp4'
        extract_both(args.bag_path, args.output, video_output, args.fps)
    elif args.video:
        extract_video(args.bag_path, args.video, args.fps)
    else:
        extract_chat_dialogue(args.bag_path, args.output)
    
    return 0

if __name__ == '__main__':
    main()
