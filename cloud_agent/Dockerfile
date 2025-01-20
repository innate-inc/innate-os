# Use a Python base image
FROM python:3.10-slim

# Create a working directory
WORKDIR /app

# Copy requirements first to leverage Docker layer caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the actual code
COPY run_server.py .

# Expose port 8765 for the WebSocket
EXPOSE 8765

# Define the command to run the server
CMD ["python", "run_server.py"]
