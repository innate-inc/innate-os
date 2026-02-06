#!/usr/bin/env python3
"""
File watcher for hot-reloading skills and agents.
Uses watchdog to monitor file changes and triggers reload via ROS service.
"""

import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    Observer = None
    FileSystemEventHandler = object


class HotReloadHandler(FileSystemEventHandler):
    """Handler for file system events that triggers hot reload."""

    def __init__(
        self,
        logger,
        on_skill_changed: Callable[[str], None],
        on_agent_changed: Callable[[str], None],
        debounce_seconds: float = 1.0,
    ):
        super().__init__()
        self.logger = logger
        self.on_skill_changed = on_skill_changed
        self.on_agent_changed = on_agent_changed
        self.debounce_seconds = debounce_seconds
        
        # Track pending reloads with timestamps to debounce
        self._pending_skills: dict[str, float] = {}
        self._pending_agents: dict[str, float] = {}
        self._lock = threading.Lock()
        self._debounce_timer: Optional[threading.Timer] = None

    def on_modified(self, event):
        if event.is_directory:
            return
        self._handle_file_event(event.src_path)

    def on_created(self, event):
        if event.is_directory:
            return
        self._handle_file_event(event.src_path)

    def _handle_file_event(self, file_path: str):
        """Process a file change event."""
        path = Path(file_path)
        
        # Only handle Python files
        if path.suffix != ".py":
            return
        
        # Skip __init__.py and private files
        if path.name.startswith("_") or path.name == "__init__.py":
            return
        
        # Determine if this is a skill or agent based on parent directory
        parent_name = path.parent.name
        item_name = path.stem  # filename without extension
        
        with self._lock:
            now = time.time()
            
            if parent_name == "skills" or "skills" in str(path.parent):
                self._pending_skills[item_name] = now
                self.logger.info(f"📝 Skill file changed: {item_name}")
            elif parent_name == "agents" or "agents" in str(path.parent):
                self._pending_agents[item_name] = now
                self.logger.info(f"📝 Agent file changed: {item_name}")
            else:
                return  # Not in a watched directory
            
            # Schedule debounced reload
            self._schedule_reload()

    def _schedule_reload(self):
        """Schedule a debounced reload."""
        if self._debounce_timer is not None:
            self._debounce_timer.cancel()
        
        self._debounce_timer = threading.Timer(
            self.debounce_seconds, self._execute_reload
        )
        self._debounce_timer.start()

    def _execute_reload(self):
        """Execute the pending reloads."""
        with self._lock:
            skills_to_reload = list(self._pending_skills.keys())
            agents_to_reload = list(self._pending_agents.keys())
            self._pending_skills.clear()
            self._pending_agents.clear()
        
        if skills_to_reload:
            self.logger.info(f"🔄 Hot reloading skills: {skills_to_reload}")
            for skill_name in skills_to_reload:
                try:
                    self.on_skill_changed(skill_name)
                except Exception as e:
                    self.logger.error(f"Error reloading skill {skill_name}: {e}")
        
        if agents_to_reload:
            self.logger.info(f"🔄 Hot reloading agents: {agents_to_reload}")
            for agent_name in agents_to_reload:
                try:
                    self.on_agent_changed(agent_name)
                except Exception as e:
                    self.logger.error(f"Error reloading agent {agent_name}: {e}")


class HotReloadWatcher:
    """
    Watches skills and agents directories for file changes.
    Triggers reload callbacks when Python files are modified.
    """

    def __init__(
        self,
        logger,
        skills_directories: list[str],
        agents_directories: list[str],
        on_reload: Callable[[list[str], list[str]], None],
        debounce_seconds: float = 1.0,
    ):
        """
        Initialize the hot reload watcher.
        
        Args:
            logger: ROS logger instance
            skills_directories: List of skill directories to watch
            agents_directories: List of agent directories to watch
            on_reload: Callback function that takes (skill_names, agent_names)
            debounce_seconds: Time to wait before triggering reload after last change
        """
        self.logger = logger
        self.skills_directories = skills_directories
        self.agents_directories = agents_directories
        self.on_reload = on_reload
        self.debounce_seconds = debounce_seconds
        
        self._observer: Optional[Observer] = None
        self._pending_skills: set[str] = set()
        self._pending_agents: set[str] = set()
        self._lock = threading.Lock()
        self._debounce_timer: Optional[threading.Timer] = None
        self._running = False

    def start(self):
        """Start watching for file changes."""
        if not WATCHDOG_AVAILABLE:
            self.logger.warn(
                "⚠️ watchdog package not installed. Hot reload file watching disabled. "
                "Install with: pip install watchdog"
            )
            return False
        
        if self._running:
            self.logger.warn("Hot reload watcher already running")
            return True
        
        self._observer = Observer()
        
        # Create event handler
        handler = _InternalHandler(
            logger=self.logger,
            on_file_changed=self._on_file_changed,
        )
        
        # Watch all directories
        watched_count = 0
        for directory in self.skills_directories + self.agents_directories:
            if os.path.exists(directory):
                self._observer.schedule(handler, directory, recursive=False)
                self.logger.info(f"👁️ Watching for changes: {directory}")
                watched_count += 1
        
        if watched_count == 0:
            self.logger.warn("No valid directories to watch for hot reload")
            return False
        
        self._observer.start()
        self._running = True
        self.logger.info(f"🔥 Hot reload watcher started ({watched_count} directories)")
        return True

    def stop(self):
        """Stop watching for file changes."""
        if self._debounce_timer is not None:
            self._debounce_timer.cancel()
            self._debounce_timer = None
        
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None
        
        self._running = False
        self.logger.info("Hot reload watcher stopped")

    def _on_file_changed(self, file_path: str, is_skill: bool):
        """Called when a file changes."""
        path = Path(file_path)
        item_name = path.stem
        
        with self._lock:
            if is_skill:
                self._pending_skills.add(item_name)
            else:
                self._pending_agents.add(item_name)
            
            # Cancel existing timer and schedule new one
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            
            self._debounce_timer = threading.Timer(
                self.debounce_seconds, self._execute_reload
            )
            self._debounce_timer.start()

    def _execute_reload(self):
        """Execute pending reloads."""
        with self._lock:
            skills = list(self._pending_skills)
            agents = list(self._pending_agents)
            self._pending_skills.clear()
            self._pending_agents.clear()
        
        if skills or agents:
            self.logger.info(
                f"🔄 Hot reload triggered - skills: {skills}, agents: {agents}"
            )
            try:
                self.on_reload(skills, agents)
            except Exception as e:
                self.logger.error(f"Hot reload failed: {e}")


class _InternalHandler(FileSystemEventHandler):
    """Internal handler for watchdog events."""

    def __init__(self, logger, on_file_changed: Callable[[str, bool], None]):
        super().__init__()
        self.logger = logger
        self.on_file_changed = on_file_changed

    def on_modified(self, event):
        self._handle(event)

    def on_created(self, event):
        self._handle(event)

    def _handle(self, event):
        if event.is_directory:
            return
        
        path = Path(event.src_path)
        
        # Only Python files
        if path.suffix != ".py":
            return
        
        # Skip __init__.py and private files
        if path.name.startswith("_") or path.name == "__init__.py":
            return
        
        # Determine type based on parent directory
        parent = path.parent.name
        is_skill = "skill" in parent.lower()
        
        self.logger.debug(f"File changed: {path.name} (skill={is_skill})")
        self.on_file_changed(str(path), is_skill)
