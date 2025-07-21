#!/usr/bin/env python3

import rosbag2_py
from rosidl_runtime_py.utilities import get_message
from rclpy.serialization import deserialize_message
import argparse
import os
from datetime import datetime

def nanoseconds_to_datetime(nanoseconds):
    """Convert ROS nanosecond timestamp to readable datetime."""
    seconds = nanoseconds / 1e9
    return datetime.fromtimestamp(seconds)

def extract_chat_dialogue(bag_path, output_file):
    """
    Extract chat_in and chat_out topics from ROS bag and create a dialogue file.
    
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
            if 'chat_out' in topic:
                speaker = "Bot"
            elif 'chat_in' in topic:
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

def main():
    parser = argparse.ArgumentParser(description='Extract chat dialogue from ROS bag')
    parser.add_argument('bag_path', help='Path to the ROS bag directory')
    parser.add_argument('-o', '--output', default='chat_dialogue.txt', 
                       help='Output text file (default: chat_dialogue.txt)')
    
    args = parser.parse_args()
    
    if not os.path.exists(args.bag_path):
        print(f"Error: Bag path '{args.bag_path}' does not exist.")
        return
    
    extract_chat_dialogue(args.bag_path, args.output)

if __name__ == '__main__':
    main()
