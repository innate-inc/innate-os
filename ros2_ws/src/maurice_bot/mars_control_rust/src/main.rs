use rclrs::{Context, CreateBasicExecutor, SpinOptions, Timer, TimerOptions};
use std_msgs::msg::{String as StringMsg, Int32MultiArray, Float64MultiArray};
use geometry_msgs::msg::{Vector3, Twist};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::path::Path;
use std::process::Command;
use std::time::Duration;

#[derive(Debug, Serialize, Deserialize, Clone)]
struct AppConfig {
    minimum_app_version: String,
}

struct AppControlNode {
    _cmd_vel_pub: rclrs::Publisher<Twist>,
    _arm_cmd_pub: rclrs::Publisher<Float64MultiArray>,
    _robot_info_pub: rclrs::Publisher<StringMsg>,
    _joystick_dummy_pub: rclrs::Publisher<Vector3>,
    _joystick_sub: rclrs::Subscription<Vector3>,
    _leader_sub: rclrs::Subscription<Int32MultiArray>,
    _robot_info_timer: Timer,
    
    // Configuration
    data_directory: String,
    app_config: AppConfig,
}

impl AppControlNode {
    fn new(
        context: &Context,
        executor: &mut rclrs::Executor,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let node = executor.create_node("app_control_node")?;

        println!("🦀 App Control Rust node started successfully!");
        println!("📋 Implementing full app.py functionality:");
        println!("   - Joystick -> cmd_vel conversion");
        println!("   - Leader positions -> arm commands");
        println!("   - Robot info publishing with timer");
        println!("   - WiFi SSID detection");
        println!("   - Git version detection");
        println!("   - Configuration file loading");

        // Load configuration
        let app_config = Self::load_app_config()?;
        let data_directory = Self::get_data_directory();

        // Publishers
        let cmd_vel_pub = node.create_publisher::<Twist>("cmd_vel")?;
        let arm_cmd_pub = node.create_publisher::<Float64MultiArray>("mars_arm_commands")?;
        let robot_info_pub = node.create_publisher::<StringMsg>("robot_info")?;
        
        // Dummy publisher for joystick to advertise topic type for rosbridge
        let joystick_dummy_pub = node.create_publisher::<Vector3>("joystick")?;

        // Clone for callbacks
        let cmd_vel_pub_clone = cmd_vel_pub.clone();
        let arm_cmd_pub_clone = arm_cmd_pub.clone();

        // Joystick subscriber
        let joystick_sub = node.create_subscription::<Vector3, _>("joystick", move |msg: Vector3| {
            // Apply deadband and scaling (matching Python version)
            let deadband = 0.1;
            let linear_scale = 0.4;
            let angular_scale = 1.5;
            
            let x = if msg.x.abs() < deadband { 0.0 } else { msg.x };
            let y = if msg.y.abs() < deadband { 0.0 } else { msg.y };
            
            // Create Twist message
            let mut twist = Twist::default();
            twist.linear.x = y * linear_scale;  // Forward/backward
            twist.angular.z = -x * angular_scale;  // Turn (inverted)
            
            // Set other components to zero
            twist.linear.y = 0.0;
            twist.linear.z = 0.0;
            twist.angular.x = 0.0;
            twist.angular.y = 0.0;
            
            // Publish
            let _ = cmd_vel_pub_clone.publish(twist);
        })?;

        // Leader positions subscriber
        let leader_sub = node.create_subscription::<Int32MultiArray, _>("leader_positions", move |msg: Int32MultiArray| {
            let expected_length = 6;
            if msg.data.len() != expected_length {
                eprintln!("Error: Received {} positions; expected {}", msg.data.len(), expected_length);
                return;
            }
            
            // Convert to radians: (position - 2048) * (2 * pi / 4096)
            let positions_rad: Vec<f64> = msg.data.iter().map(|&pos| {
                let pos_f = pos as f64;
                (pos_f - 2048.0) * (2.0 * std::f64::consts::PI / 4096.0)
            }).collect();
            
            // Create and publish Float64MultiArray
            let mut cmd_msg = Float64MultiArray::default();
            cmd_msg.data = positions_rad;
            let _ = arm_cmd_pub_clone.publish(cmd_msg);
        })?;

        // Robot info timer
        let robot_info_timer = {
            let robot_info_pub_clone = robot_info_pub.clone();
            let data_directory_clone = data_directory.clone();
            let app_config_clone = app_config.clone();
            
            node.create_timer_repeating(
                Duration::from_secs(1),
                move |_| {
                    Self::publish_robot_info_callback(
                        &robot_info_pub_clone,
                        &data_directory_clone,
                        &app_config_clone,
                    );
                }
            )?
        };

        Ok(AppControlNode {
            _cmd_vel_pub: cmd_vel_pub,
            _arm_cmd_pub: arm_cmd_pub,
            _robot_info_pub: robot_info_pub,
            _joystick_dummy_pub: joystick_dummy_pub,
            _joystick_sub: joystick_sub,
            _leader_sub: leader_sub,
            _robot_info_timer: robot_info_timer,
            data_directory,
            app_config,
        })
    }

    fn load_app_config() -> Result<AppConfig, Box<dyn std::error::Error>> {
        let home_dir = env::var("HOME").unwrap_or_default();
        let maurice_root = env::var("INNATE_OS_ROOT")
            .unwrap_or_else(|_| format!("{}/innate-os", home_dir));
        let config_file_path = format!("{}/os_config.json", maurice_root);
        
        let config_content = fs::read_to_string(config_file_path)?;
        let app_config: AppConfig = serde_json::from_str(&config_content)?;
        
        Ok(app_config)
    }

    fn get_data_directory() -> String {
        let home_dir = env::var("HOME").unwrap_or_default();
        let maurice_root = env::var("INNATE_OS_ROOT")
            .unwrap_or_else(|_| format!("{}/innate-os", home_dir));
        format!("{}/data", maurice_root)
    }

    fn get_wifi_ssid() -> Option<String> {
        // Use nmcli to get the active WiFi SSID
        let output = Command::new("nmcli")
            .args(&["-t", "-f", "ACTIVE,SSID", "dev", "wifi"])
            .output()
            .ok()?;

        if output.status.success() {
            let stdout = String::from_utf8_lossy(&output.stdout);
            for line in stdout.lines() {
                if line.starts_with("yes:") {
                    let ssid = line.strip_prefix("yes:").unwrap_or("");
                    if !ssid.is_empty() {
                        return Some(ssid.to_string());
                    }
                }
            }
        }
        
        None
    }

    fn get_robot_version() -> Result<String, Box<dyn std::error::Error>> {
        let home_dir = env::var("HOME").unwrap_or_default();
        let maurice_root = env::var("INNATE_OS_ROOT")
            .unwrap_or_else(|_| format!("{}/innate-os", home_dir));

        // Get current branch
        let output = Command::new("git")
            .args(&["branch", "--show-current"])
            .current_dir(&maurice_root)
            .output()?;
        
        if !output.status.success() {
            return Err(format!("Failed to get current git branch: {}", 
                              String::from_utf8_lossy(&output.stderr)).into());
        }
        
        let current_branch = String::from_utf8_lossy(&output.stdout).trim().to_string();

        // Get all tags sorted by version
        let output = Command::new("git")
            .args(&["tag", "--list", "--sort=-version:refname"])
            .current_dir(&maurice_root)
            .output()?;
        
        if !output.status.success() {
            return Err(format!("Failed to get git tags: {}", 
                              String::from_utf8_lossy(&output.stderr)).into());
        }

        let stdout = String::from_utf8_lossy(&output.stdout);
        if stdout.trim().is_empty() {
            return Err("No git tags found - repository must have at least one tag".into());
        }

        let tags: Vec<&str> = stdout.trim().split('\n').collect();
        let latest_tag = tags.first().unwrap_or(&"");

        // If on main branch and exactly on a tag, return the tag
        if current_branch == "main" && !latest_tag.is_empty() {
            let output = Command::new("git")
                .args(&["describe", "--exact-match", "--tags", "HEAD"])
                .current_dir(&maurice_root)
                .output()?;
            
            if output.status.success() {
                return Ok(latest_tag.to_string());
            }
        }

        // Otherwise, return dev version
        if !latest_tag.is_empty() {
            let parts: Vec<&str> = latest_tag.split('.').collect();
            if parts.len() == 3 {
                let major: i32 = parts[0].parse()?;
                let minor: i32 = parts[1].parse()?;
                let patch: i32 = parts[2].parse()?;
                return Ok(format!("{}.{}.{}-dev", major, minor, patch));
            } else {
                return Err(format!("Invalid tag format: {}. Expected format: x.y.z", latest_tag).into());
            }
        }

        Err("Failed to determine robot version".into())
    }

    fn publish_robot_info_callback(
        robot_info_pub: &rclrs::Publisher<StringMsg>,
        data_directory: &str,
        app_config: &AppConfig,
    ) {
        let robot_info_file_path = format!("{}/robot_info.json", data_directory);
        
        let mut data_to_publish = HashMap::new();
        let mut final_json = "{}".to_string();

        // Try to read robot info from file
        if Path::new(&robot_info_file_path).exists() {
            match fs::read_to_string(&robot_info_file_path) {
                Ok(content) => {
                    match serde_json::from_str::<serde_json::Value>(&content) {
                        Ok(json_obj) => {
                            // Extract robot_name if present
                            if let Some(robot_name) = json_obj.get("robot_name").and_then(|v| v.as_str()) {
                                data_to_publish.insert("robot_name", robot_name.to_string());
                            }
                        }
                        Err(e) => {
                            println!("Error parsing JSON from {}: {}", robot_info_file_path, e);
                        }
                    }
                }
                Err(e) => {
                    println!("Error reading file {}: {}", robot_info_file_path, e);
                }
            }
        } else {
            println!("Robot info file not found: {}", robot_info_file_path);
        }

        // Get WiFi SSID
        if let Some(wifi_ssid) = Self::get_wifi_ssid() {
            data_to_publish.insert("wifi_ssid", wifi_ssid);
        }

        // Get robot version
        match Self::get_robot_version() {
            Ok(version) => {
                data_to_publish.insert("version", version);
            }
            Err(e) => {
                println!("Error getting robot version: {}", e);
            }
        }

        // Include minimum app version
        data_to_publish.insert("minimum_app_version", app_config.minimum_app_version.clone());

        // Create final JSON
        if !data_to_publish.is_empty() {
            match serde_json::to_string(&data_to_publish) {
                Ok(json_str) => {
                    final_json = json_str;
                }
                Err(e) => {
                    println!("Error serializing robot info: {}", e);
                }
            }
        }

        // Publish
        let mut msg = StringMsg::default();
        msg.data = final_json;
        
        if let Err(e) = robot_info_pub.publish(msg) {
            println!("Error publishing robot info: {}", e);
        }
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Bootstrap ROS 2
    let context = Context::default_from_env()?;
    let mut executor = context.create_basic_executor();
    
    // Create the node
    let _node = AppControlNode::new(&context, &mut executor)?;

    // Spin
    println!("🚀 Spinning App Control Rust node...");
    let errors = executor.spin(SpinOptions::default());
    if !errors.is_empty() {
        return Err(errors.into_iter().next().unwrap().into());
    }
    
    Ok(())
}
