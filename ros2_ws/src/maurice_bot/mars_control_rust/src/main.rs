use rclrs::{Context, CreateBasicExecutor, SpinOptions};
use std_msgs::msg::{String as StringMsg, Int32MultiArray, Float64MultiArray};
use geometry_msgs::msg::{Vector3, Twist};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Bootstrap ROS 2
    let context = Context::default_from_env()?;
    let mut executor = context.create_basic_executor();
    let node = executor.create_node("app_control_node")?;

    println!("🦀 App Control Rust node started successfully!");
    println!("📋 Implementing basic app.py functionality:");
    println!("   - Joystick -> cmd_vel conversion");
    println!("   - Leader positions -> arm commands"); 
    println!("   - Robot info publishing (timer simplified)");

    // Publisher for velocity commands (Twist) on /cmd_vel
    let cmd_vel_pub = node.create_publisher::<Twist>("cmd_vel")?;
    let cmd_vel_handle = cmd_vel_pub.clone();

    // Publisher for leader arm commands (Float64MultiArray) on /mars/arm/commands
    let arm_cmd_pub = node.create_publisher::<Float64MultiArray>("mars_arm_commands")?;
    let arm_cmd_handle = arm_cmd_pub.clone();

    // Publisher for robot info
    let robot_info_pub = node.create_publisher::<StringMsg>("robot_info")?;
    let robot_info_handle = robot_info_pub.clone();

    // Subscriber on joystick that converts to cmd_vel
    let _joystick_sub = node.create_subscription::<Vector3, _>("joystick", move |msg: Vector3| {
        println!("Received joystick: x={}, y={}, z={}", msg.x, msg.y, msg.z);
        
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
        
        println!("Publishing cmd_vel: linear.x={:.2}, angular.z={:.2}", 
                 twist.linear.x, twist.angular.z);
        
        // Publish (ignore error for brevity)
        let _ = cmd_vel_handle.publish(twist);
    })?;

    // Subscriber on leader_positions that converts to arm commands
    let _leader_sub = node.create_subscription::<Int32MultiArray, _>("leader_positions", move |msg: Int32MultiArray| {
        println!("Received leader positions: {:?}", msg.data);
        
        let expected_length = 6;
        if msg.data.len() != expected_length {
            println!("Error: Received {} positions; expected {}", msg.data.len(), expected_length);
            return;
        }
        
        // Convert to radians: (position - 2048) * (2 * pi / 4096)
        let positions_rad: Vec<f64> = msg.data.iter().map(|&pos| {
            let pos_f = pos as f64;
            (pos_f - 2048.0) * (2.0 * std::f64::consts::PI / 4096.0)
        }).collect();
        
        println!("Publishing arm commands (rad): {:?}", positions_rad);
        
        // Create and publish Float64MultiArray
        let mut cmd_msg = Float64MultiArray::default();
        cmd_msg.data = positions_rad;
        let _ = arm_cmd_handle.publish(cmd_msg);
    })?;

    // Note: Timer implementation simplified out for now 
    // In full implementation would publish robot info every 1 second

    // Spin
    let errors = executor.spin(SpinOptions::default());
    if !errors.is_empty() {
        return Err(errors.into_iter().next().unwrap().into());
    }
    Ok(())
}
