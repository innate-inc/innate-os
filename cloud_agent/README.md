# Cloud Agent

## Build the Docker image

```bash
docker build \
  --platform=linux/amd64 \
  -t us-central1-docker.pkg.dev/innate-agent/innate-agent-websocket-server/agent-ws-server-image:v0 \
  .
```

## Push the Docker image to Google Cloud Container Registry

```bash
docker push us-central1-docker.pkg.dev/innate-agent/innate-agent-websocket-server/agent-ws-server-image:v0
```

## Deploy the Cloud Run service

```bash
gcloud run deploy agent-ws-server \
  --image us-central1-docker.pkg.dev/innate-agent/innate-agent-websocket-server/agent-ws-server-image:v0 \
  --platform managed \
  --region us-central1 \
  --port 8765
```

## Test the Cloud Run service

Use the test_ws_server.py script to test the Cloud Run service.

```bash
python3 test_ws_server.py
```
