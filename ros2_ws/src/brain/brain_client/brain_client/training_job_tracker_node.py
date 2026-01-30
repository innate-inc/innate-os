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
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
import json
import os
import shutil
import uuid
import traceback

from brain_messages.srv import (
    SubmitTrainingJob,
    GetTrainingJobStatus,
    GetTrainingJobStatusByName,
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
            '/home/jetson1/innate-os/skills'
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
        # Pass the ROS logger so adapters can use it for logging
        try:
            self.proxy = ProxyClient(logger=self.get_logger())
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
        self.get_job_status_by_name_srv = self.create_service(
            GetTrainingJobStatusByName,
            "/training/get_job_status_by_name",
            self._handle_get_job_status_by_name
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
        self.logger.info("  - /training/get_job_status_by_name")
        self.logger.info("  - /training/list_jobs")
        self.logger.info("  - /training/download_model")
        
        # Create a timer to keep the node alive and check tracker status
        # This also allows ROS to handle shutdown gracefully
        self.timer = self.create_timer(60.0, self._check_tracker_status)
    
    def _run_async_in_thread(self, coro_func, timeout: float = 30.0):
        """
        Run an async function in a separate thread with its own event loop.
        
        Args:
            coro_func: Async function to run (will be called with no arguments)
            timeout: Maximum time to wait for completion
            
        Returns:
            Tuple of (result, error) - one will be None
        """
        result_container = {"result": None, "error": None}
        
        def run_in_thread():
            """Run async function in thread with new event loop."""
            loop = None
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result_container["result"] = loop.run_until_complete(coro_func())
            except Exception as e:
                result_container["error"] = e
            finally:
                if loop:
                    try:
                        # Cancel any remaining tasks
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        # Give tasks a chance to clean up
                        if pending:
                            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    except Exception:
                        pass
                    finally:
                        loop.close()
        
        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        
        if thread.is_alive():
            return None, TimeoutError(f"Operation timed out after {timeout} seconds")
        
        return result_container["result"], result_container["error"]
    
    def _calculate_number_of_samples(self, primitive_path: str) -> int:
        """
        Calculate the total number of samples from dataset_metadata.json.
        
        For each episode, calculates duration from start_timestamp and end_timestamp,
        then multiplies by data_frequency to get number of frames/samples.
        Sums over all episodes.
        
        Args:
            primitive_path: Path to primitive folder
            
        Returns:
            Total number of samples (timesteps) across all episodes, or 0 if error
        """
        try:
            dataset_metadata_path = Path(primitive_path) / "data" / "dataset_metadata.json"
            
            if not dataset_metadata_path.exists():
                self.logger.warning(f"  dataset_metadata.json not found at {dataset_metadata_path}")
                return 0
            
            with open(dataset_metadata_path, 'r') as f:
                dataset_metadata = json.load(f)
            
            data_frequency = dataset_metadata.get("data_frequency", 20)  # Default to 20 Hz
            episodes = dataset_metadata.get("episodes", [])
            
            if not episodes:
                self.logger.warning(f"  No episodes found in dataset_metadata.json")
                return 0
            
            total_samples = 0
            
            for episode_info in episodes:
                start_timestamp_str = episode_info.get("start_timestamp")
                end_timestamp_str = episode_info.get("end_timestamp")
                
                if not start_timestamp_str or not end_timestamp_str:
                    self.logger.warning(f"  Episode {episode_info.get('episode_id', 'unknown')} missing timestamps")
                    continue
                
                try:
                    # Parse timestamps (ISO format: "2025-12-30T08:11:28")
                    # Try fromisoformat first (Python 3.7+), fall back to manual parsing
                    try:
                        start_time = datetime.fromisoformat(start_timestamp_str)
                        end_time = datetime.fromisoformat(end_timestamp_str)
                    except (ValueError, AttributeError):
                        # Fallback: parse manually if fromisoformat not available
                        # Format: "YYYY-MM-DDTHH:MM:SS"
                        start_time = datetime.strptime(start_timestamp_str, "%Y-%m-%dT%H:%M:%S")
                        end_time = datetime.strptime(end_timestamp_str, "%Y-%m-%dT%H:%M:%S")
                    
                    # Calculate duration in seconds
                    duration_seconds = (end_time - start_time).total_seconds()
                    
                    # Calculate number of samples for this episode
                    episode_samples = int(duration_seconds * data_frequency)
                    total_samples += episode_samples
                    
                    self.logger.debug(f"  Episode {episode_info.get('episode_id', 'unknown')}: "
                                    f"{duration_seconds:.2f}s * {data_frequency}Hz = {episode_samples} samples")
                    
                except Exception as e:
                    self.logger.warning(f"  Error parsing timestamps for episode {episode_info.get('episode_id', 'unknown')}: {e}")
                    continue
            
            self.logger.info(f"  Calculated total samples: {total_samples} from {len(episodes)} episode(s) "
                           f"(data_frequency={data_frequency}Hz)")
            return total_samples
            
        except Exception as e:
            self.logger.error(f"  Error calculating number of samples: {e}")
            return 0
    
    def _calculate_batch_size(self, num_samples: int) -> Tuple[int, int]:
        """
        Calculate optimal batch size and world size based on number of samples.

        The function tries world_size in [8, 4, 2, 1] (high to low), each time:
        - Compute samples_per_worker = num_samples / (world_size * no_of_cycles)
        - If samples_per_worker >= 4, batch_size = largest multiple of 4 <= min(96, samples_per_worker)
        - If none fit, fall back to batch_size=1, world_size=1

        Args:
            num_samples: Total samples in the dataset

        Returns:
            (batch_size, world_size)
        """
        if num_samples <= 0:
            self.logger.warning("  Invalid number of samples, using default batch_size=96, world_size=8")
            return 96, 8

        no_of_cycles = 64
        self.logger.info(f"  Calculating batch size for {num_samples} samples")
        for world_size in [8, 4, 2]:
            samples_per_worker = num_samples / (world_size * no_of_cycles)
            self.logger.debug(f"  world_size={world_size}, no_of_cycles={no_of_cycles}, samples_per_worker={samples_per_worker:.2f}")
            if samples_per_worker >= 4:
                max_batch = min(96, int(samples_per_worker))
                batch_size = (max_batch // 4) * 4
                self.logger.info(f"  ✓ Batch size calculated: {batch_size}, world_size={world_size} (samples_per_worker={samples_per_worker:.2f})")
                return batch_size, world_size

        # If nothing fit above, fall back to batch_size=1, world_size=1
        self.logger.debug("  Too few samples for normal batch sizes, using batch_size=1, world_size=1")
        self.logger.info("  ✓ Batch size calculated: 1, world_size=1 (samples too few)")
        return 1, 1
    
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
        required_files = ["act_policy_final.onnx", "dataset_stats.pt"]
        
        # Get max_steps from training_params to check for final step .pth file
        training_params = job_status.get("training_params", {})
        if isinstance(training_params, str):
            try:
                training_params = json.loads(training_params)
            except json.JSONDecodeError:
                training_params = {}
        
        max_steps = training_params.get("max_steps")
        if max_steps:
            required_files.append(f"act_policy_step_{max_steps}.pth")
        
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
                required_files = ["act_policy_final.onnx", "dataset_stats.pt"]
                
                # Get max_steps from training_params to download final step .pth file
                training_params = job_status.get("training_params", {})
                if isinstance(training_params, str):
                    try:
                        training_params = json.loads(training_params)
                    except json.JSONDecodeError:
                        training_params = {}
                
                max_steps = training_params.get("max_steps")
                if max_steps:
                    required_files.append(f"act_policy_step_{max_steps}.pth")
                
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
                            
                            # Check if already downloaded using job_data (now includes primitive_name)
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
        request_id = str(uuid.uuid4())[:8]  # Short unique ID for this request
        call_stack = ''.join(traceback.format_stack()[-3:-1])  # Get caller info
        self.logger.info(f"📤 [REQUEST {request_id}] Received submit_job service request")
        self.logger.info(f"  [REQUEST {request_id}] Caller stack: {call_stack}")
        
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
            
            # Get primitive_name from request (required for proper job tracking)
            primitive_name = request.primitive_name if request.primitive_name else None
            
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
                # Extract primitive_name from path if not provided (fallback)
                if not primitive_name:
                    primitive_name = Path(path_to_upload).name
                    self.logger.info(f"  Extracted primitive_name from path: {primitive_name}")
                self.logger.info(f"  Uploading primitive folder: {path_to_upload}")
                self.logger.info(f"  Primitive name: {primitive_name}")
            elif request.file_path:
                path_to_upload = request.file_path
                # Resolve relative paths relative to INNATE_OS_ROOT
                if not Path(path_to_upload).is_absolute():
                    path_to_upload = os.path.join(innate_os_root, path_to_upload)
                path_to_upload = os.path.abspath(path_to_upload)
                is_primitive = False
                # Extract primitive_name from filename if not provided (fallback)
                if not primitive_name:
                    filename = Path(path_to_upload).name
                    if filename.endswith('.tar.gz'):
                        primitive_name = filename[:-7]  # Remove .tar.gz
                    else:
                        primitive_name = Path(path_to_upload).stem  # Remove extension
                    self.logger.info(f"  Extracted primitive_name from filename: {primitive_name}")
                self.logger.info(f"  Uploading single file: {path_to_upload}")
                self.logger.info(f"  Primitive name: {primitive_name}")
            else:
                self.logger.error("  Missing required parameter: must provide either primitive_path or file_path")
                response.success = False
                response.error_message = "Must provide either primitive_path or file_path"
                return response
            
            # Ensure primitive_name is set (should always be set by now via request or extraction)
            if not primitive_name:
                self.logger.error("  Could not determine primitive_name")
                response.success = False
                response.error_message = "Could not determine primitive_name. Please provide it explicitly."
                return response
            
            # Validate path exists
            if not Path(path_to_upload).exists():
                self.logger.error(f"  Path does not exist: {path_to_upload}")
                response.success = False
                response.error_message = f"Path does not exist: {path_to_upload}"
                return response
            
            # Calculate batch size and world size dynamically from dataset_metadata.json (only for primitive folders)
            if is_primitive:
                self.logger.info("  Calculating batch size and world size from dataset...")
                num_samples = self._calculate_number_of_samples(path_to_upload)
                
                if num_samples > 0:
                    calculated_batch_size, calculated_world_size = self._calculate_batch_size(num_samples)
                    
                    # Override batch_size and world_size in training_params with calculated values
                    user_batch_size = training_params.get("batch_size")
                    user_world_size = training_params.get("world_size")
                    
                    if user_batch_size:
                        self.logger.info(f"  Overriding user-provided batch_size={user_batch_size} "
                                       f"with calculated batch_size={calculated_batch_size}")
                    else:
                        self.logger.info(f"  Setting calculated batch_size={calculated_batch_size} "
                                       f"(not provided by user)")
                    
                    if user_world_size:
                        self.logger.info(f"  Overriding user-provided world_size={user_world_size} "
                                       f"with calculated world_size={calculated_world_size}")
                    else:
                        self.logger.info(f"  Setting calculated world_size={calculated_world_size} "
                                       f"(not provided by user)")
                    
                    training_params["batch_size"] = calculated_batch_size
                    training_params["world_size"] = calculated_world_size
                else:
                    self.logger.warning("  Could not calculate batch size from dataset, using provided values or defaults")
                    if "batch_size" not in training_params:
                        training_params["batch_size"] = 96  # Default fallback
                        self.logger.info(f"  Using default batch_size=96")
                    if "world_size" not in training_params:
                        training_params["world_size"] = 8  # Default fallback
                        self.logger.info(f"  Using default world_size=8")
            else:
                # For single file uploads, we can't read dataset_metadata.json
                # Use provided batch_size/world_size or defaults
                if "batch_size" not in training_params:
                    training_params["batch_size"] = 96  # Default fallback
                    self.logger.info(f"  Using default batch_size=96 (single file upload, cannot calculate from dataset)")
                if "world_size" not in training_params:
                    training_params["world_size"] = 8  # Default fallback
                    self.logger.info(f"  Using default world_size=8 (single file upload, cannot calculate from dataset)")
            
            # Get job_id and upload_url immediately by requesting upload permission (non-blocking)
            # This allows us to return quickly and let the upload continue in background
            async def request_permission():
                self.logger.info(f"  [REQUEST {request_id}] Calling request_upload_permission...")
                async with self.proxy.training as client:
                    if is_primitive:
                        filename = f"{primitive_name}.tar.gz"
                    else:
                        filename = Path(path_to_upload).name
                    
                    permission = await client.request_upload_permission(
                        filename=filename,
                        content_type="application/gzip" if is_primitive else "application/octet-stream",
                        training_params=training_params,
                        primitive_name=primitive_name,  # Send as top-level field for server
                    )
                    job_id_from_permission = permission["job_id"]
                    self.logger.info(f"  [REQUEST {request_id}] Got job_id: {job_id_from_permission}")
                    return job_id_from_permission, permission["upload_url"]
            
            result, error = self._run_async_in_thread(request_permission, timeout=10.0)
            
            if error:
                raise error
            if result is None:
                raise RuntimeError("Failed to get job_id from upload permission request")
            
            job_id, upload_url = result
            self.logger.info(f"  [REQUEST {request_id}] Using job_id: {job_id} for upload")
            
            # Capture variables in local scope to prevent closure issues with concurrent uploads
            # Python closures capture by reference, so we need to capture values explicitly
            upload_job_id = job_id
            upload_url_local = upload_url
            upload_path = path_to_upload
            upload_primitive_name = primitive_name
            upload_is_primitive = is_primitive
            upload_training_params = training_params.copy() if training_params else {}
            
            # Now start the actual upload in background (non-blocking)
            # Pass job_id and upload_url to avoid duplicate job creation
            async def submit_upload():
                async with self.proxy.training as client:
                    if upload_is_primitive:
                        result = await client.upload_primitive_folder(
                            primitive_path=upload_path,
                            job_id=upload_job_id,
                            upload_url=upload_url_local,
                            primitive_name=upload_primitive_name,
                            training_params=upload_training_params,
                        )
                    else:
                        upload_params = upload_training_params.copy()
                        if upload_primitive_name:
                            upload_params["primitive_name"] = upload_primitive_name
                        
                        result = await client.upload_file_resumable(
                            file_path=upload_path,
                            job_id=upload_job_id,
                            upload_url=upload_url_local,
                            filename=Path(upload_path).name,
                            training_params=upload_params,
                        )
                    
                    if result["job_id"] != upload_job_id:
                        self.logger.warning(f"  Job ID mismatch: expected {upload_job_id}, got {result['job_id']}")
                    
                    await client.notify_upload_complete(upload_job_id)
                    self.logger.info(f"✅ Upload completed successfully for job {upload_job_id}")
            
            def run_upload():
                """Run upload in background thread."""
                _, error = self._run_async_in_thread(submit_upload, timeout=7200.0)  # 2 hour timeout
                if error:
                    self.logger.error(f"❌ Error during background upload for job {upload_job_id}: {error}")
                    self.logger.error(f"  Traceback: {traceback.format_exc()}")
            
            self.logger.info(f"  Starting background upload for job {job_id}...")
            self.logger.info(f"  Note: Large uploads may take >10 minutes. Upload continues in background.")
            threading.Thread(target=run_upload, daemon=True).start()
            
            # Return immediately with job_id - upload continues in background
            response.success = True
            response.job_id = job_id
            self.logger.info(f"✅ Job submitted successfully via service: {job_id} (upload in progress)")
            
        except Exception as e:
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
            
            async def get_status():
                async with self.proxy.training as client:
                    return await client.get_job_status(request.job_id)
            
            status, error = self._run_async_in_thread(get_status, timeout=30.0)
            
            if error:
                raise error
            if status is None:
                raise RuntimeError("Query completed but no status returned")
            
            job_status = status.get("status", "unknown")
            self.logger.info(f"✅ Retrieved job status: {job_status}")
            
            response.success = True
            response.status_json = json.dumps(status)
            
        except Exception as e:
            self.logger.error(f"❌ Error getting job status: {e}")
            self.logger.error(f"  Traceback: {traceback.format_exc()}")
            response.success = False
            response.error_message = str(e)
        
        return response
    
    def _handle_get_job_status_by_name(self, request: GetTrainingJobStatusByName.Request, response: GetTrainingJobStatusByName.Response) -> GetTrainingJobStatusByName.Response:
        """ROS service handler for getting job status by primitive name."""
        self.logger.info(f"📊 Received get_job_status_by_name service request for primitive: {request.primitive_name}")
        
        try:
            if not request.primitive_name:
                self.logger.error("  Missing required parameter: primitive_name")
                response.success = False
                response.error_message = "primitive_name required"
                return response
            
            async def get_status():
                async with self.proxy.training as client:
                    return await client.get_job_status_by_name(request.primitive_name)
            
            status, error = self._run_async_in_thread(get_status, timeout=30.0)
            
            if error:
                raise error
            if status is None:
                raise RuntimeError("Query completed but no status returned")
            
            job_status = status.get("status", "unknown")
            self.logger.info(f"✅ Retrieved job status for {request.primitive_name}: {job_status}")
            
            response.success = True
            response.status_json = json.dumps(status)
            
        except Exception as e:
            self.logger.error(f"❌ Error getting job status by name: {e}")
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
            
            async def list_jobs():
                async with self.proxy.training as client:
                    return await client.list_jobs(status_filter=status_filter, limit=limit)
            
            jobs, error = self._run_async_in_thread(list_jobs, timeout=30.0)
            
            if error:
                raise error
            if jobs is None:
                raise RuntimeError("Query completed but no jobs returned")
            
            self.logger.info(f"✅ Retrieved {len(jobs)} job(s) from proxy")
            if jobs:
                for job in jobs[:5]:  # Log first 5
                    job_id = job.get("job_id", "unknown")
                    status = job.get("status", "unknown")
                    primitive_name = job.get("primitive_name", "N/A")
                    self.logger.debug(f"    - {job_id[:8]}... | {status} | primitive: {primitive_name}")
                if len(jobs) > 5:
                    self.logger.debug(f"    ... and {len(jobs) - 5} more")
            
            response.success = True
            response.jobs_json = json.dumps(jobs)
            
        except Exception as e:
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
            
            async def download():
                async with self.proxy.training as client:
                    self.logger.debug(f"  Getting job status for {request.job_id}...")
                    job_status = await client.get_job_status(request.job_id)
                    self.logger.info(f"  Job status: {job_status.get('status')}")
                    self.logger.info(f"  Starting model download...")
                    await self._download_model(request.job_id, job_status)
                    return job_status
            
            job_status, error = self._run_async_in_thread(download, timeout=300.0)
            
            if error:
                raise error
            if job_status is None:
                raise RuntimeError("Download completed but no job_status returned")
            
            primitive_name = self._extract_primitive_name(job_status, request.job_id)
            output_path = self.download_dir / primitive_name / "ckpts" / filename
            
            self.logger.info(f"✅ Model downloaded successfully to: {output_path}")
            
            response.success = True
            response.output_path = str(output_path)
            
        except Exception as e:
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
