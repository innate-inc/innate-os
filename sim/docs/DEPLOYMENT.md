# VM Deployment Guide (sim.innate.bot)

This guide details the steps for deploying the Innate Simulator on a specific Virtual Machine setup, accessible via `sim.innate.bot`.

## Prerequisites

*   Virtual Machine with access configured for the `sim.innate.bot` domain.
*   Docker installed on the VM.
*   `tmux` installed for managing persistent sessions.
*   `nginx` installed for acting as a reverse proxy.
*   Repositories for Genesis Simulation (`genesis-sim`) and Maurice Production (`maurice-prod`) cloned onto the VM.

## Deployment Process

### 1. Start Maurice Production Components

In a first SSH connection/tmux session:

```bash
# Navigate to Maurice production directory
cd <path/to/maurice-prod-directory>

# Start the required Docker containers (e.g., brain components)
# Adjust docker-compose file names as needed
docker-compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.dev.yml exec maurice zsh -l

# Follow steps within the Maurice Production repository 
# to start the ROS bridge and brain logic.
# (Specific commands depend on that repository's setup)
```

### 2. Start Genesis Simulation Backend

In a second SSH connection/tmux session:

```bash
# Create a new tmux session (optional but recommended)
tmux new -s sim_backend # Example session name

# Navigate to Genesis simulation directory
cd <path/to/genesis-sim-directory>

# Ensure Python environment is set up (e.g., activate venv)
source venv/bin/activate

# Start the simulation backend, connecting to the local agent bridge
# Run without visualization (-v) typically for servers
python main_web.py --local 
```

### 3. Configure Nginx Reverse Proxy

1.  **Edit Nginx Configuration:**
    Open the default site configuration file (or create a new one for `sim.innate.bot`):
    ```bash
    sudo nano /etc/nginx/sites-available/default 
    # Or: sudo nano /etc/nginx/sites-available/sim.innate.bot
    ```

2.  **Add Proxy Configuration:**
    Configure `nginx` to proxy requests to the FastAPI backend (running on port 8000 by default). A minimal example:
    ```nginx
    server {
        listen 80;
        server_name sim.innate.bot;

        location / {
            proxy_pass http://127.0.0.1:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            
            # WebSocket support
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }
    }
    ```
    *(Adjust based on your specific `nginx` setup and security requirements, e.g., adding SSL/HTTPS)*

3.  **Enable Site and Test/Restart Nginx:**
    ```bash
    # If you created a new file, link it:
    # sudo ln -s /etc/nginx/sites-available/sim.innate.bot /etc/nginx/sites-enabled/
    
    # Test configuration syntax
    sudo nginx -t
    
    # Restart nginx to apply changes
    sudo systemctl restart nginx
    ```

### 4. Access the Simulation

The simulation frontend should now be accessible in a web browser via `http://sim.innate.bot` (or `https://` if you configured SSL).

## Managing the Deployment

*   **Tmux Sessions:**
    *   Detach: Press `Ctrl+B` then `D`.
    *   List sessions: `tmux ls`
    *   Reattach: `tmux attach -t <session-name>` (e.g., `tmux attach -t sim_backend`)
*   **Stopping Services:** Reattach to the relevant `tmux` sessions and press `Ctrl+C` to stop the running processes (like `main_web.py`). Use `docker-compose down` for the Docker services. 