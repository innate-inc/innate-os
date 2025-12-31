#!/usr/bin/env python3
"""
Test script for Training Job Tracker Node via ROS services.

This script tests the training functionality by calling ROS services
on the training_job_tracker_node.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import rclpy
from rclpy.node import Node
from brain_messages.srv import (
    SubmitTrainingJob,
    GetTrainingJobStatus,
    ListTrainingJobs,
    DownloadTrainingModel,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TrainingNodeTester(Node):
    """Test client for Training Job Tracker Node."""
    
    def __init__(self):
        super().__init__('training_node_tester')
        
        # Create service clients
        self.submit_job_client = self.create_client(SubmitTrainingJob, '/training/submit_job')
        self.get_job_status_client = self.create_client(GetTrainingJobStatus, '/training/get_job_status')
        self.list_jobs_client = self.create_client(ListTrainingJobs, '/training/list_jobs')
        self.download_model_client = self.create_client(DownloadTrainingModel, '/training/download_model')
        
        # Wait for services to be available
        logger.info("Waiting for training services...")
        self.submit_job_client.wait_for_service(timeout_sec=10.0)
        self.get_job_status_client.wait_for_service(timeout_sec=10.0)
        self.list_jobs_client.wait_for_service(timeout_sec=10.0)
        self.download_model_client.wait_for_service(timeout_sec=10.0)
        logger.info("✓ All services available")
    
    def submit_job(self, primitive_path: str = None, file_path: str = None, training_params: dict = None):
        """Submit a training job via ROS service."""
        request = SubmitTrainingJob.Request()
        
        if primitive_path:
            request.primitive_path = primitive_path
        elif file_path:
            request.file_path = file_path
        else:
            logger.error("Must provide either primitive_path or file_path")
            return None
        
        request.training_params_json = json.dumps(training_params or {})
        
        logger.info(f"Submitting job via ROS service...")
        future = self.submit_job_client.call_async(request)
        rclpy.spin_until_future_complete(self, future)
        
        if future.done():
            response = future.result()
            if response.success:
                logger.info(f"✓ Job submitted: {response.job_id}")
                return response.job_id
            else:
                logger.error(f"✗ Error: {response.error_message}")
                return None
        return None
    
    def get_job_status(self, job_id: str):
        """Get job status via ROS service."""
        request = GetTrainingJobStatus.Request()
        request.job_id = job_id
        
        future = self.get_job_status_client.call_async(request)
        rclpy.spin_until_future_complete(self, future)
        
        if future.done():
            response = future.result()
            if response.success:
                return json.loads(response.status_json)
            else:
                logger.error(f"Error: {response.error_message}")
                return None
        return None
    
    def list_jobs(self, status_filter: str = None, limit: int = 100):
        """List all jobs via ROS service."""
        request = ListTrainingJobs.Request()
        request.status_filter = status_filter or ""
        request.limit = limit
        
        future = self.list_jobs_client.call_async(request)
        rclpy.spin_until_future_complete(self, future)
        
        if future.done():
            response = future.result()
            if response.success:
                return json.loads(response.jobs_json)
            else:
                logger.error(f"Error: {response.error_message}")
                return []
        return []
    
    def download_model(self, job_id: str, filename: str = "model.ckpt"):
        """Download model via ROS service."""
        request = DownloadTrainingModel.Request()
        request.job_id = job_id
        request.filename = filename
        
        future = self.download_model_client.call_async(request)
        rclpy.spin_until_future_complete(self, future)
        
        if future.done():
            response = future.result()
            if response.success:
                logger.info(f"✓ Model downloaded: {response.output_path}")
                return response.output_path
            else:
                logger.error(f"Error: {response.error_message}")
                return None
        return None


def main():
    parser = argparse.ArgumentParser(description="Test Training Job Tracker Node via ROS")
    parser.add_argument("--upload-primitive", type=str, help="Upload primitive folder (e.g., primitives/minecraft_wave)")
    parser.add_argument("--upload", type=str, help="Upload single file")
    parser.add_argument("--status", type=str, help="Get job status")
    parser.add_argument("--list-jobs", action="store_true", help="List all jobs")
    parser.add_argument("--download", type=str, help="Download model for job ID")
    parser.add_argument("--filename", type=str, default="model.ckpt", help="Filename to download")
    parser.add_argument("--batch-size", type=int, default=96, help="Training batch size")
    parser.add_argument("--max-steps", type=int, default=120000, help="Max training steps")
    parser.add_argument("--status-filter", type=str, help="Filter jobs by status (running, completed, etc.)")
    parser.add_argument("--limit", type=int, default=100, help="Limit number of jobs to list")
    
    args = parser.parse_args()
    
    rclpy.init()
    tester = TrainingNodeTester()
    
    try:
        if args.upload_primitive:
            training_params = {
                "batch_size": args.batch_size,
                "max_steps": args.max_steps,
            }
            job_id = tester.submit_job(primitive_path=args.upload_primitive, training_params=training_params)
            if job_id:
                print(f"\n✓ Job submitted: {job_id}")
                print(f"Check status: python test_training_client.py --status {job_id}")
        
        elif args.upload:
            training_params = {
                "batch_size": args.batch_size,
                "max_steps": args.max_steps,
            }
            job_id = tester.submit_job(file_path=args.upload, training_params=training_params)
            if job_id:
                print(f"\n✓ Job submitted: {job_id}")
                print(f"Check status: python test_training_client.py --status {job_id}")
        
        elif args.status:
            status = tester.get_job_status(args.status)
            if status:
                print(f"\nJob Status:")
                print(f"  Job ID: {status.get('job_id')}")
                print(f"  Status: {status.get('status')}")
                if status.get('error_message'):
                    print(f"  Error: {status.get('error_message')}")
        
        elif args.list_jobs:
            jobs = tester.list_jobs(status_filter=args.status_filter, limit=args.limit)
            print(f"\nFound {len(jobs)} job(s):")
            for job in jobs:
                job_id = job.get("job_id", "unknown")
                status = job.get("status", "unknown")
                created = job.get("created_at", "unknown")
                print(f"  {job_id[:8]}... | {status:12} | Created: {created}")
        
        elif args.download:
            tester.download_model(args.download, args.filename)
        
        else:
            parser.print_help()
            print("\nExample usage:")
            print("  # Submit primitive folder")
            print("  python test_training_client.py --upload-primitive primitives/minecraft_wave")
            print("\n  # Check job status")
            print("  python test_training_client.py --status <job_id>")
            print("\n  # List all jobs")
            print("  python test_training_client.py --list-jobs")
            print("\n  # Download model")
            print("  python test_training_client.py --download <job_id>")
    
    finally:
        tester.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
