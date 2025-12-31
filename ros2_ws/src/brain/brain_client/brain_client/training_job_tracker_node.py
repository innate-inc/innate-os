#!/usr/bin/env python3
"""
Training Job Tracker ROS Node

A ROS node that monitors training jobs and automatically downloads models.
Queries proxy service for job status (stateless) and automatically
downloads models when training completes.

This node survives robot restarts - it queries the proxy service
for all incomplete jobs on startup and continues monitoring them.
"""

import rclpy
from rclpy.node import Node
import asyncio
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import json
import os
import shutil
import shutil

from brain_messages.srv import (
    SubmitTrainingJob,
    GetTrainingJobStatus,
    ListTrainingJobs,
    DownloadTrainingModel,
)

from brain_client.client.proxy_client import ProxyClient
from brain_client.logging_config import UniversalLogger


class TrainingJobTrackerNode(Node):
    """
    ROS node that tracks training jobs and downloads models.
    
    Runs as a background service that:
    - Queries proxy for incomplete jobs (stateless)
    - Polls job status with adaptive intervals
    - Automatically downloads models when training completes
    - Survives robot restarts (queries proxy on startup)
    """
    
    def __init__(self):
        super().__init__('training_job_tracker_node')
        
        # Wrap ROS logger with UniversalLogger
        ros_logger = self.get_logger()
        self.logger = UniversalLogger(enabled=True, wrapped_logger=ros_logger)
        
        self.logger.info("🚀 Starting Training Job Tracker Node...")
        
        # Get download directory from parameter or use default
        self.download_dir = Path(self.declare_parameter(
            'download_dir',
            '/home/jetson1/innate-os/primitives'
        ).value)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        
        # Get poll intervals from parameters
        self.poll_interval_running = self.declare_parameter(
            'poll_interval_running',
            300  # 5 minutes
        ).value
        
        self.poll_interval_submitted = self.declare_parameter(
            'poll_interval_submitted',
            120  # 2 minutes
        ).value
        
        self.poll_interval_uploading = self.declare_parameter(
            'poll_interval_uploading',
            60  # 1 minute
        ).value
        
        self.logger.info(f"Download directory: {self.download_dir}")
        self.logger.info(f"Poll intervals - Running: {self.poll_interval_running}s, "
                        f"Submitted: {self.poll_interval_submitted}s, "
                        f"Uploading: {self.poll_interval_uploading}s")
        
        # Create proxy client (credentials from env vars)
        # Similar to how InputManagerNode initializes ProxyClient
        try:
            self.proxy = ProxyClient()
            if self.proxy.is_available():
                self.logger.info(f"✅ Proxy client initialized (URL: {self.proxy.proxy_url[:30]}...)")
                # Verify auth key is set
                if not self.proxy.innate_service_key:
                    self.logger.error("❌ INNATE_SERVICE_KEY is not set!")
                    raise RuntimeError("INNATE_SERVICE_KEY environment variable is not set")
                token_preview = self.proxy.innate_service_key[:20] + "..." if len(self.proxy.innate_service_key) > 20 else self.proxy.innate_service_key
                self.logger.debug(f"Auth key present: {bool(self.proxy.innate_service_key)}, length: {len(self.proxy.innate_service_key)}, preview: {token_preview}")
            else:
                self.logger.error("⚠️ Proxy not configured - check INNATE_PROXY_URL and INNATE_SERVICE_KEY")
                self.proxy = None
        except Exception as e:
            self.logger.error(f"⚠️ Could not initialize proxy client: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            self.proxy = None
        
        if not self.proxy or not self.proxy.is_available():
            raise RuntimeError(
                "Proxy client not available - cannot start training tracker. "
                "Set INNATE_PROXY_URL and INNATE_SERVICE_KEY environment variables."
            )
        
        # Tracker state
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self.exit_event = threading.Event()
        
        # Start async event loop in separate thread
        self.tracker_thread = threading.Thread(target=self._run_tracker_loop, daemon=True)
        self.tracker_thread.start()
        
        self.logger.info("✓ Training Job Tracker started")
        self.logger.info("  - Queries proxy for incomplete jobs (stateless)")
        self.logger.info("  - Automatically downloads models when training completes")
        self.logger.info("  - Survives robot restarts")
        
        # ROS Services for interacting with the node
        self.submit_job_srv = self.create_service(
            SubmitTrainingJob,
            "/training/submit_job",
            self._handle_submit_job
        )
        self.get_job_status_srv = self.create_service(
            GetTrainingJobStatus,
            "/training/get_job_status",
            self._handle_get_job_status
        )
        self.list_jobs_srv = self.create_service(
            ListTrainingJobs,
            "/training/list_jobs",
            self._handle_list_jobs
        )
        self.download_model_srv = self.create_service(
            DownloadTrainingModel,
            "/training/download_model",
            self._handle_download_model
        )
        
        self.logger.info("✅ ROS services registered:")
        self.logger.info("  - /training/submit_job")
        self.logger.info("  - /training/get_job_status")
        self.logger.info("  - /training/list_jobs")
        self.logger.info("  - /training/download_model")
        
        # Create a timer to keep the node alive and check tracker status
        # This also allows ROS to handle shutdown gracefully
        self.timer = self.create_timer(60.0, self._check_tracker_status)
    
    def _extract_primitive_name(self, job_status: Dict, job_id: str = None) -> str:
        """
        Extract primitive name from job status.
        
        Expects server to provide 'primitive_name' field in job status response.
        Falls back to 'unknown' if not available.
        
        Args:
            job_status: Job status dict from server
            job_id: Optional job ID for logging
            
        Returns:
            Primitive name or "unknown" if not found
        """
        primitive_name = job_status.get("primitive_name")
        if primitive_name:
            self.logger.info(f"✓ Found primitive_name: {primitive_name}")
            return primitive_name
        
        # Fallback to unknown if not provided by server
        if job_id:
            self.logger.warning(f"⚠ Server did not provide primitive_name for job {job_id}")
        return "unknown"
    
    def _is_job_downloaded(self, job_id: str, job_status: Dict) -> bool:
        """
        Check if a job's model files have been downloaded locally.
        
        Since we're stateless, we check if model files exist on disk.
        
        Args:
            job_id: Job ID
            job_status: Job status dict with metadata
            
        Returns:
            True if model files exist locally, False otherwise
        """
        # Try to get primitive name from job metadata
        primitive_name = self._extract_primitive_name(job_status, job_id)
        
        # Check if any model files exist in the expected location
        ckpts_dir = self.download_dir / primitive_name / "ckpts"
        if not ckpts_dir.exists():
            return False
        
        # Check for required model files
        # We need both the ONNX model and dataset stats
        required_files = ["act_policy_final.onnx", "dataset_stats.pt"]
        for filename in required_files:
            if not (ckpts_dir / filename).exists():
                return False
        
        return True
    
    async def _download_model(self, job_id: str, job_status: Dict):
        """Download model files for a completed job."""
        # Log available fields for debugging
        self.logger.info(f"📋 Extracting primitive name from job_status (source: get_job_status API response)")
        self.logger.info(f"  Job status fields: {list(job_status.keys())}")
        if "metadata" in job_status:
            self.logger.info(f"  Metadata fields: {list(job_status.get('metadata', {}).keys())}")
        
        # Extract primitive name using helper method
        primitive_name = self._extract_primitive_name(job_status, job_id)
        
        output_dir = self.download_dir / primitive_name / "ckpts"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        self.logger.info(f"Downloading model for job {job_id} to {output_dir} (primitive: {primitive_name})")
        
        try:
            async with self.proxy.training as client:
                # Download required model files
                # Only download the ONNX model and dataset stats (not intermediate checkpoints)
                required_files = ["act_policy_final.onnx", "dataset_stats.pt"]
                downloaded = []
                failed_files = []
                
                for filename in required_files:
                    try:
                        output_path = output_dir / filename
                        await client.download_file(
                            job_id=job_id,
                            filename=filename,
                            output_path=str(output_path),
                        )
                        downloaded.append(filename)
                        self.logger.info(f"✓ Downloaded {filename}")
                    except Exception as e:
                        failed_files.append(filename)
                        self.logger.error(f"✗ Failed to download {filename}: {e}")
                
                if downloaded:
                    self.logger.info(f"✓ Successfully downloaded {len(downloaded)}/{len(required_files)} required file(s) for {primitive_name}")
                    if failed_files:
                        self.logger.warning(f"⚠ Missing files: {', '.join(failed_files)}")
                    
                    # Delete the data folder after successful download (only if all required files downloaded)
                    if len(downloaded) == len(required_files):
                        data_dir = self.download_dir / primitive_name / "data"
                        if data_dir.exists():
                            try:
                                self.logger.info(f"🗑️  Deleting data folder: {data_dir}")
                                shutil.rmtree(data_dir)
                                self.logger.info(f"✓ Successfully deleted data folder for {primitive_name}")
                            except Exception as e:
                                self.logger.warning(f"⚠ Failed to delete data folder: {e}")
                    else:
                        self.logger.info(f"⚠ Not deleting data folder - some files failed to download")
                else:
                    self.logger.error(f"❌ No model files downloaded for job {job_id}")
                    self.logger.error(f"  Tried: {', '.join(required_files)}")
                    
        except Exception as e:
            self.logger.error(f"Failed to download model for job {job_id}: {e}")
    
    async def _poll_loop(self):
        """Main polling loop - queries proxy for jobs (stateless)."""
        self.logger.info("Starting training job polling loop")
        
        # Track last poll time per job to implement adaptive intervals
        last_poll_times: Dict[str, datetime] = {}
        
        # Track when we last checked for completed-but-not-downloaded jobs
        last_completed_check: Optional[datetime] = None
        completed_check_interval = 300  # Check every 5 minutes for completed jobs
        
        while self._running:
            try:
                # Query proxy for incomplete jobs (always fresh from database)
                async with self.proxy.training as client:
                    incomplete_jobs = await client.get_incomplete_jobs()
                
                # Also check for completed jobs that haven't been downloaded
                # (do this less frequently to avoid too many API calls)
                check_completed = False
                if last_completed_check is None:
                    check_completed = True
                else:
                    time_since_check = (datetime.utcnow() - last_completed_check).total_seconds()
                    if time_since_check >= completed_check_interval:
                        check_completed = True
                
                completed_jobs_to_download = []
                if check_completed:
                    try:
                        async with self.proxy.training as client:
                            completed_jobs = await client.list_jobs(status_filter="completed")
                        
                        self.logger.debug(f"Checking {len(completed_jobs)} completed job(s) for downloads")
                        
                        for job_data in completed_jobs:
                            job_id = job_data.get("job_id")
                            if not job_id:
                                continue
                            
                            # Get fresh job status to ensure we have primitive_name for proper check
                            # list_jobs doesn't include primitive_name, so we need get_job_status
                            try:
                                fresh_status = await client.get_job_status(job_id)
                                # Check if already downloaded using fresh status with primitive_name
                                if not self._is_job_downloaded(job_id, fresh_status):
                                    completed_jobs_to_download.append(fresh_status)
                            except Exception as e:
                                self.logger.warning(f"Failed to get job status for {job_id} during download check: {e}")
                                # Fallback: use job_data without primitive_name (will check "unknown" folder)
                                if not self._is_job_downloaded(job_id, job_data):
                                    completed_jobs_to_download.append(job_data)
                        
                        if completed_jobs_to_download:
                            self.logger.info(f"Found {len(completed_jobs_to_download)} completed job(s) not yet downloaded")
                        
                        last_completed_check = datetime.utcnow()
                    except Exception as e:
                        self.logger.error(f"Failed to check completed jobs: {e}")
                
                # Process jobs to monitor/download
                all_jobs_to_process = incomplete_jobs + completed_jobs_to_download
                
                if not all_jobs_to_process:
                    self.logger.debug("No jobs to process")
                    await asyncio.sleep(60)  # Check every minute for new jobs
                    continue
                
                self.logger.info(f"Processing {len(incomplete_jobs)} incomplete job(s) and {len(completed_jobs_to_download)} completed job(s) to download")
                
                async with self.proxy.training as client:
                    for job_data in all_jobs_to_process:
                        if not self._running:
                            break
                        
                        job_id = job_data.get("job_id")
                        if not job_id:
                            continue
                        
                        current_status = job_data.get("status", "pending")
                        
                        # If it's a completed job that needs downloading, download it immediately
                        if current_status == "completed" and job_data in completed_jobs_to_download:
                            if not self._is_job_downloaded(job_id, job_data):
                                self.logger.info(f"Job {job_id} appears completed but not downloaded. Checking status...")
                                try:
                                    # Get fresh status to ensure we have latest info
                                    status = await client.get_job_status(job_id)
                                    fresh_status = status.get("status")
                                    
                                    # Only download if the fresh status is actually "completed"
                                    if fresh_status == "completed":
                                        self.logger.info(f"Job {job_id} confirmed completed. Downloading model...")
                                        await self._download_model(job_id, status)
                                    else:
                                        self.logger.debug(f"Job {job_id} status changed from 'completed' to '{fresh_status}'. Skipping download, will poll normally.")
                                        # Don't continue - let it fall through to normal polling
                                        continue
                                except Exception as e:
                                    self.logger.error(f"Failed to download model for completed job {job_id}: {e}")
                            else:
                                # Already downloaded, skip polling
                                continue
                        
                        # For incomplete jobs, use adaptive polling
                        # Determine poll interval based on status
                        if current_status == "running":
                            poll_interval = self.poll_interval_running
                        elif current_status == "submitted":
                            poll_interval = self.poll_interval_submitted
                        elif current_status == "uploading":
                            poll_interval = self.poll_interval_uploading
                        else:
                            poll_interval = 60  # Default 1 minute
                        
                        # Check if enough time has passed since last poll
                        last_poll = last_poll_times.get(job_id)
                        if last_poll:
                            time_since_poll = (datetime.utcnow() - last_poll).total_seconds()
                            if time_since_poll < poll_interval:
                                continue  # Skip this job, not time to poll yet
                        
                        # Poll the job (get fresh status from proxy)
                        self.logger.debug(f"Polling job {job_id} (status: {current_status})")
                        try:
                            status = await client.get_job_status(job_id)
                            last_poll_times[job_id] = datetime.utcnow()
                            
                            new_status = status.get("status")
                            self.logger.info(f"Job {job_id} status: {current_status} → {new_status}")
                            
                            # If completed and not downloaded, download model
                            if new_status == "completed":
                                if not self._is_job_downloaded(job_id, status):
                                    self.logger.info(f"Job {job_id} completed! Downloading model...")
                                    await self._download_model(job_id, status)
                                else:
                                    self.logger.debug(f"Job {job_id} already downloaded")
                            elif new_status in ["failed", "cancelled"]:
                                self.logger.warning(f"Job {job_id} ended with status: {new_status}")
                                if status.get("error_message"):
                                    self.logger.error(f"Error: {status.get('error_message')}")
                            
                        except Exception as e:
                            self.logger.error(f"Failed to poll job {job_id}: {e}")
                        
                        # Small delay between jobs
                        await asyncio.sleep(1)
                
                # Wait before next polling cycle
                await asyncio.sleep(30)  # Check for new jobs every 30 seconds
                
            except Exception as e:
                self.logger.error(f"Error in polling loop: {e}")
                await asyncio.sleep(60)  # Wait before retrying
    
    def _start_polling(self):
        """Start background polling."""
        if self._running:
            self.logger.warning("Polling already running")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        self.logger.info("Training job tracker started")
    
    def _stop_polling(self):
        """Stop background polling."""
        if not self._running:
            return
        
        self._running = False
        if self._task:
            self._task.cancel()
        self.logger.info("Training job tracker stopped")
    
    def _run_tracker_loop(self):
        """Run the async tracker loop in a separate thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Start polling (creates async task - requires loop to be running)
            async def start_polling():
                self._start_polling()
                # Keep running until exit event
                while not self.exit_event.is_set() and rclpy.ok():
                    await asyncio.sleep(1)
            
            # Run the loop
            loop.run_until_complete(start_polling())
        except Exception as e:
            self.logger.error(f"Error in tracker loop: {e}")
        finally:
            self._stop_polling()
            # Cancel any remaining tasks
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.close()
    
    def _check_tracker_status(self):
        """Periodic check to ensure tracker is still running."""
        if not self._running:
            self.logger.warning("Tracker stopped unexpectedly, restarting...")
            # Note: Can't directly call _start_polling from sync context
            # The thread will restart on next cycle
    
    def _handle_submit_job(self, request: SubmitTrainingJob.Request, response: SubmitTrainingJob.Response) -> SubmitTrainingJob.Response:
        """ROS service handler for submitting a training job."""
        self.logger.info("📤 Received submit_job service request")
        
        try:
            # Parse training params
            training_params = {}
            if request.training_params_json:
                try:
                    training_params = json.loads(request.training_params_json)
                    self.logger.info(f"  Training params: {training_params}")
                except json.JSONDecodeError as e:
                    self.logger.error(f"  Failed to parse training_params_json: {e}")
                    response.success = False
                    response.error_message = f"Invalid JSON in training_params_json: {e}"
                    return response
            
            # Determine if primitive folder or single file
            # Resolve paths relative to INNATE_OS_ROOT (or ~/innate-os)
            innate_os_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
            
            if request.primitive_path:
                path_to_upload = request.primitive_path
                # Resolve relative paths relative to INNATE_OS_ROOT
                if not Path(path_to_upload).is_absolute():
                    path_to_upload = os.path.join(innate_os_root, path_to_upload)
                path_to_upload = os.path.abspath(path_to_upload)
                is_primitive = True
                self.logger.info(f"  Uploading primitive folder: {path_to_upload}")
            elif request.file_path:
                path_to_upload = request.file_path
                # Resolve relative paths relative to INNATE_OS_ROOT
                if not Path(path_to_upload).is_absolute():
                    path_to_upload = os.path.join(innate_os_root, path_to_upload)
                path_to_upload = os.path.abspath(path_to_upload)
                is_primitive = False
                self.logger.info(f"  Uploading single file: {path_to_upload}")
            else:
                self.logger.error("  Missing required parameter: must provide either primitive_path or file_path")
                response.success = False
                response.error_message = "Must provide either primitive_path or file_path"
                return response
            
            # Validate path exists
            if not Path(path_to_upload).exists():
                self.logger.error(f"  Path does not exist: {path_to_upload}")
                response.success = False
                response.error_message = f"Path does not exist: {path_to_upload}"
                return response
            
            # Run async operation in separate thread to avoid blocking ROS executor
            result_container = {"job_id": None, "error": None}
            
            def run_async_upload():
                """Run async upload in separate thread."""
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    async def submit():
                        async with self.proxy.training as client:
                            if is_primitive:
                                result = await client.upload_primitive_folder(
                                    primitive_path=path_to_upload,
                                    primitive_name=Path(path_to_upload).name,
                                    training_params=training_params,
                                )
                            else:
                                result = await client.upload_file_resumable(
                                    file_path=path_to_upload,
                                    filename=Path(path_to_upload).name,
                                    training_params=training_params,
                                )
                            
                            job_id = result["job_id"]
                            # Notify upload complete
                            await client.notify_upload_complete(job_id)
                            return job_id
                    
                    self.logger.info(f"  Starting async upload and submission...")
                    result_container["job_id"] = loop.run_until_complete(submit())
                except Exception as e:
                    result_container["error"] = e
                finally:
                    loop.close()
            
            self.logger.info(f"  Starting upload thread...")
            self.logger.info(f"  Note: Large uploads may take >10 minutes. Zenoh may timeout, but upload continues.")
            upload_thread = threading.Thread(target=run_async_upload, daemon=True)
            upload_thread.start()
            
            # Wait for upload with timeout (upload can take a while)
            # Note: Zenoh has a 600s (10min) query timeout, but we allow up to 1 hour for the actual upload
            upload_thread.join(timeout=3600)  # 1 hour timeout for large uploads
            
            if upload_thread.is_alive():
                # Upload is still running - this is OK, it will complete in background
                # But we can't wait longer for the ROS service response
                self.logger.warning(f"  Upload still in progress after 1 hour. It will continue in background.")
                self.logger.warning(f"  Job ID will be available via polling. Zenoh timeout is expected for long uploads.")
                raise TimeoutError("Upload is taking longer than 1 hour. Check job status via polling.")
            
            if result_container["error"]:
                raise result_container["error"]
            
            if result_container["job_id"] is None:
                raise RuntimeError("Upload completed but no job_id returned")
            
            job_id = result_container["job_id"]
            
            response.success = True
            response.job_id = job_id
            self.logger.info(f"✅ Job submitted successfully via service: {job_id}")
            
        except Exception as e:
            import traceback
            self.logger.error(f"❌ Error submitting job: {e}")
            self.logger.error(f"  Traceback: {traceback.format_exc()}")
            response.success = False
            response.error_message = str(e)
        
        return response
    
    def _handle_get_job_status(self, request: GetTrainingJobStatus.Request, response: GetTrainingJobStatus.Response) -> GetTrainingJobStatus.Response:
        """ROS service handler for getting job status."""
        self.logger.info(f"📊 Received get_job_status service request for job: {request.job_id}")
        
        try:
            if not request.job_id:
                self.logger.error("  Missing required parameter: job_id")
                response.success = False
                response.error_message = "job_id required"
                return response
            
            # Run async operation in separate thread to avoid blocking ROS executor
            result_container = {"status": None, "error": None}
            
            def run_async_query():
                """Run async query in separate thread."""
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    async def get_status():
                        async with self.proxy.training as client:
                            return await client.get_job_status(request.job_id)
                    
                    self.logger.debug(f"  Querying proxy for job status...")
                    result_container["status"] = loop.run_until_complete(get_status())
                except Exception as e:
                    result_container["error"] = e
                finally:
                    loop.close()
            
            query_thread = threading.Thread(target=run_async_query, daemon=True)
            query_thread.start()
            query_thread.join(timeout=30)  # 30 second timeout for status query
            
            if query_thread.is_alive():
                raise TimeoutError("Status query timed out after 30 seconds")
            
            if result_container["error"]:
                raise result_container["error"]
            
            if result_container["status"] is None:
                raise RuntimeError("Query completed but no status returned")
            
            status = result_container["status"]
            
            job_status = status.get("status", "unknown")
            self.logger.info(f"✅ Retrieved job status: {job_status}")
            
            response.success = True
            response.status_json = json.dumps(status)
            
        except Exception as e:
            import traceback
            self.logger.error(f"❌ Error getting job status: {e}")
            self.logger.error(f"  Traceback: {traceback.format_exc()}")
            response.success = False
            response.error_message = str(e)
        
        return response
    
    def _handle_list_jobs(self, request: ListTrainingJobs.Request, response: ListTrainingJobs.Response) -> ListTrainingJobs.Response:
        """ROS service handler for listing all jobs."""
        filter_str = f" (filter: {request.status_filter})" if request.status_filter else ""
        limit_str = f" (limit: {request.limit})" if request.limit > 0 else ""
        self.logger.info(f"📋 Received list_jobs service request{filter_str}{limit_str}")
        
        try:
            status_filter = request.status_filter if request.status_filter else None
            limit = request.limit if request.limit > 0 else None
            
            # Run async operation in separate thread to avoid blocking ROS executor
            result_container = {"jobs": None, "error": None}
            
            def run_async_query():
                """Run async query in separate thread."""
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    async def list_jobs():
                        async with self.proxy.training as client:
                            return await client.list_jobs(status_filter=status_filter, limit=limit)
                    
                    self.logger.debug(f"  Querying proxy for jobs...")
                    result_container["jobs"] = loop.run_until_complete(list_jobs())
                except Exception as e:
                    result_container["error"] = e
                finally:
                    loop.close()
            
            query_thread = threading.Thread(target=run_async_query, daemon=True)
            query_thread.start()
            query_thread.join(timeout=30)  # 30 second timeout for list query
            
            if query_thread.is_alive():
                raise TimeoutError("List jobs query timed out after 30 seconds")
            
            if result_container["error"]:
                raise result_container["error"]
            
            if result_container["jobs"] is None:
                raise RuntimeError("Query completed but no jobs returned")
            
            jobs = result_container["jobs"]
            
            self.logger.info(f"✅ Retrieved {len(jobs)} job(s) from proxy")
            if jobs:
                for job in jobs[:5]:  # Log first 5
                    job_id = job.get("job_id", "unknown")
                    status = job.get("status", "unknown")
                    self.logger.debug(f"    - {job_id[:8]}... | {status}")
                if len(jobs) > 5:
                    self.logger.debug(f"    ... and {len(jobs) - 5} more")
            
            response.success = True
            response.jobs_json = json.dumps(jobs)
            
        except Exception as e:
            import traceback
            self.logger.error(f"❌ Error listing jobs: {e}")
            self.logger.error(f"  Traceback: {traceback.format_exc()}")
            response.success = False
            response.error_message = str(e)
        
        return response
    
    def _handle_download_model(self, request: DownloadTrainingModel.Request, response: DownloadTrainingModel.Response) -> DownloadTrainingModel.Response:
        """ROS service handler for downloading a model."""
        filename = request.filename or "model.ckpt"
        self.logger.info(f"📥 Received download_model service request for job: {request.job_id}, filename: {filename}")
        
        try:
            if not request.job_id:
                self.logger.error("  Missing required parameter: job_id")
                response.success = False
                response.error_message = "job_id required"
                return response
            
            # Run async operation in separate thread to avoid blocking ROS executor
            result_container = {"job_status": None, "error": None}
            
            def run_async_download():
                """Run async download in separate thread."""
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    async def download():
                        async with self.proxy.training as client:
                            self.logger.debug(f"  Getting job status for {request.job_id}...")
                            job_status = await client.get_job_status(request.job_id)
                            self.logger.info(f"  Job status: {job_status.get('status')}")
                            self.logger.info(f"  Starting model download...")
                            await self._download_model(request.job_id, job_status)
                            return job_status
                    
                    result_container["job_status"] = loop.run_until_complete(download())
                except Exception as e:
                    result_container["error"] = e
                finally:
                    loop.close()
            
            download_thread = threading.Thread(target=run_async_download, daemon=True)
            download_thread.start()
            download_thread.join(timeout=300)  # 5 minute timeout for download
            
            if download_thread.is_alive():
                raise TimeoutError("Download timed out after 5 minutes")
            
            if result_container["error"]:
                raise result_container["error"]
            
            if result_container["job_status"] is None:
                raise RuntimeError("Download completed but no job_status returned")
            
            job_status = result_container["job_status"]
            
            primitive_name = self._extract_primitive_name(job_status, request.job_id)
            output_path = self.download_dir / primitive_name / "ckpts" / filename
            
            self.logger.info(f"✅ Model downloaded successfully to: {output_path}")
            
            response.success = True
            response.output_path = str(output_path)
            
        except Exception as e:
            import traceback
            self.logger.error(f"❌ Error downloading model: {e}")
            self.logger.error(f"  Traceback: {traceback.format_exc()}")
            response.success = False
            response.error_message = str(e)
        
        return response
    
    def destroy_node(self):
        """Clean shutdown - stop tracker before destroying node."""
        self.logger.info("Shutting down Training Job Tracker Node...")
        self.exit_event.set()
        self._stop_polling()
        if self.tracker_thread.is_alive():
            self.tracker_thread.join(timeout=5.0)
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    
    node = TrainingJobTrackerNode()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
