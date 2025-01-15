#!/bin/zsh

# Check if the session exists
tmux has-session -t discovery 2>/dev/null

# If the session doesn't exist, create it
if [ $? != 0 ]; then
    # Create a new session named discovery
    tmux new-session -d -s discovery
    
    # Send the FastDDS discovery command to the session
    tmux send-keys -t discovery "fastdds discovery -i 0 -p 11811" C-m
fi