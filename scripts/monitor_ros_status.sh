#!/bin/bash
# Monitor ROS nodes, topics, and services startup status
# Logs output to /tmp/ros_monitor.log

LOG_FILE="/tmp/ros_monitor.log"

# Clear previous log
> "$LOG_FILE"

for i in {1..100}; do
    {
        ros2 node list > /tmp/ros_nodes 2>&1 & 
        ros2 topic list > /tmp/ros_topics 2>&1 & 
        ros2 service list > /tmp/ros_services 2>&1 & 

        wait
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Iteration $i ===" 
        
        echo '--- System Status ---'
        echo "SSHD: $(systemctl is-active sshd)"
        echo "discovery-server: $(systemctl is-active discovery-server.service)"
        echo "ros-app: $(systemctl is-active ros-app.service)"
        echo "ble-provisioner: $(systemctl is-active ble-provisioner.service)"
        WIFI_IP=$(ip addr show wlan0 2>/dev/null | grep "inet " | awk '{print $2}')
        if [ -n "$WIFI_IP" ]; then
            echo "WiFi IP: $WIFI_IP"
        else
            echo "WiFi IP: NOT CONFIGURED"
        fi
        echo ''
        
        
        
        echo '--- Nodes ---' 
        nl /tmp/ros_nodes 
        echo ''
        
        echo '--- Topics ---' 
        nl /tmp/ros_topics 
        echo ''
        
        echo '--- Services ---' 
        nl /tmp/ros_services 
        echo ''
        
        rm -f /tmp/ros_nodes /tmp/ros_topics /tmp/ros_services
        #sleep 1
    } | tee -a "$LOG_FILE"
done

echo "Monitor complete. Full log saved to: $LOG_FILE"
