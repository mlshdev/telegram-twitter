# Video Download API

A FastAPI-based API server that downloads videos from various platforms using [yt-dlp](https://github.com/yt-dlp/yt-dlp) and returns them in Apple QuickTime friendly format.

Designed for integration with iOS apps via the "Share with App" feature.

## Features

- **Multi-platform support**: Download videos from YouTube, Twitter/X, TikTok, Instagram, and [1000+ other sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)
- **Apple QuickTime optimized**: Automatically converts videos to H.264/AAC in MP4 container
- **Minimal processing**: Uses ffmpeg only when necessary (codec conversion, aspect ratio fixes)
- **Secure**: Token-based authentication with support for multiple tokens
- **Container-ready**: OCI-compliant Docker image works with Docker, Podman, and Kubernetes
- **Traefik integration**: Ready-to-use reverse proxy configuration with optional HTTPS

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/mlshdev/telegram-twitter.git
cd telegram-twitter

# Create data directory for cookies (optional)
mkdir -p data

# Create .env file with your configuration
cat > .env << EOF
AUTH_TOKENS=your-secret-token-here
API_DOMAIN=api.yourdomain.com
EOF
```

### 2. Start with Docker Compose

```bash
docker compose up -d
```

The API will be available at `http://localhost:80` (via Traefik) or directly at `http://localhost:8000` if running without Traefik.

### 3. Test the API

```bash
# Health check
curl http://localhost/health

# Download a video
curl -X POST http://localhost/download \
  -H "Authorization: Bearer your-secret-token-here" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}' \
  --output video.mp4
```

## API Reference

### `GET /`

Root endpoint - returns health status for reverse proxy compatibility.

**Response:**
```json
{
  "status": "healthy",
  "version": "1.0.0"
}
```

### `GET /health`

Health check endpoint for container orchestration.

**Response:**
```json
{
  "status": "healthy",
  "version": "1.0.0"
}
```

### `POST /download`

Download a video from the provided URL.

**Headers:**
- `Authorization: Bearer <token>` (required)
- `Content-Type: application/json` (required)

**Request Body:**
```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID"
}
```

**Response:**
- `200 OK`: Video file as attachment with `Content-Type: video/mp4`
- `400 Bad Request`: Invalid URL or download error
- `401 Unauthorized`: Missing or invalid token
- `403 Forbidden`: Video is private
- `404 Not Found`: Video not available
- `500 Internal Server Error`: Processing failed

**Example with curl:**
```bash
curl -X POST http://localhost/download \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://twitter.com/user/status/123456"}' \
  --output tweet_video.mp4
```

**Example with Python:**
```python
import requests

response = requests.post(
    "http://localhost/download",
    headers={"Authorization": "Bearer your-token"},
    json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
)

if response.status_code == 200:
    with open("video.mp4", "wb") as f:
        f.write(response.content)
```

**Example with Swift (iOS):**
```swift
import Foundation

struct DownloadRequest: Codable {
    let url: String
}

func downloadVideo(from videoURL: String) async throws -> Data {
    let endpoint = URL(string: "https://api.yourdomain.com/download")!
    var request = URLRequest(url: endpoint)
    request.httpMethod = "POST"
    request.setValue("Bearer your-token", forHTTPHeaderField: "Authorization")
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    
    let body = DownloadRequest(url: videoURL)
    request.httpBody = try JSONEncoder().encode(body)
    
    let (data, response) = try await URLSession.shared.data(for: request)
    guard let httpResponse = response as? HTTPURLResponse,
          httpResponse.statusCode == 200 else {
        throw URLError(.badServerResponse)
    }
    
    return data
}
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AUTH_TOKENS` | Comma-separated list of valid auth tokens | (required) |
| `API_HOST` | Host to bind the server | `0.0.0.0` |
| `API_PORT` | Port to bind the server | `8000` |
| `API_WORKERS` | Number of uvicorn workers | `1` |
| `API_LOG_LEVEL` | Log level (debug, info, warning, error) | `info` |
| `API_DOMAIN` | Domain for Traefik routing | `api.localhost` |
| `YTDLP_FORMAT` | yt-dlp format selection | QuickTime-optimized |
| `YTDLP_COOKIES_FILE` | Path to cookies.txt file | `/data/cookies.txt` |
| `YTDLP_TWITTER_API` | Twitter API to use | `graphql` |
| `YTDLP_TWITTER_API_ORDER` | Twitter API fallback order | `graphql,legacy,syndication` |
| `YTDLP_USER_AGENT` | Custom User-Agent header | (none) |

### Multiple Auth Tokens

You can specify multiple auth tokens separated by commas:

```bash
AUTH_TOKENS=token1,token2,token3
```

### Cookies for Authentication

Some sites require authentication. Export cookies from your browser and mount them:

1. Install a browser extension like "Get cookies.txt LOCALLY"
2. Export cookies for the target site
3. Save as `data/cookies.txt`
4. The container will automatically use them

## Deployment

### Docker Compose (recommended)

```bash
# Production with HTTPS (edit docker-compose.yml to enable Let's Encrypt)
docker compose up -d
```

### Podman

```bash
# Using podman-compose
podman-compose -f docker-compose.yml up -d

# Generate Kubernetes YAML
podman generate kube video-download-api > pod.yaml
```

### Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: video-download-api
spec:
  replicas: 1
  selector:
    matchLabels:
      app: video-download-api
  template:
    metadata:
      labels:
        app: video-download-api
    spec:
      containers:
      - name: api
        image: ghcr.io/mlshdev/telegram-twitter:latest
        ports:
        - containerPort: 8000
        env:
        - name: AUTH_TOKENS
          valueFrom:
            secretKeyRef:
              name: video-api-secrets
              key: auth-tokens
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 30
        resources:
          limits:
            memory: "2Gi"
            cpu: "2"
          requests:
            memory: "512Mi"
            cpu: "500m"
```

### HTTPS with Let's Encrypt

Edit `docker-compose.yml` and uncomment the Let's Encrypt configuration:

```yaml
command:
  # ... existing commands ...
  - "--certificatesresolvers.letsencrypt.acme.httpchallenge=true"
  - "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web"
  - "--certificatesresolvers.letsencrypt.acme.email=${ACME_EMAIL}"
  - "--certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json"
```

## iOS Integration

This API is designed to work with iOS apps using the Share Extension feature. When a user shares a link from Safari or other apps, your iOS app can:

1. Receive the shared URL in the Share Extension
2. Send a POST request to the `/download` endpoint
3. Save the returned video to the Photos library or Files app

### Swift Share Extension Example

```swift
import UniformTypeIdentifiers

class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        handleSharedURL()
    }
    
    func handleSharedURL() {
        guard let extensionItem = extensionContext?.inputItems.first as? NSExtensionItem,
              let itemProvider = extensionItem.attachments?.first else {
            return
        }
        
        if itemProvider.hasItemConformingToTypeIdentifier(UTType.url.identifier) {
            itemProvider.loadItem(forTypeIdentifier: UTType.url.identifier) { [weak self] item, error in
                guard let url = item as? URL else { return }
                Task {
                    await self?.downloadVideo(from: url.absoluteString)
                }
            }
        }
    }
    
    func downloadVideo(from url: String) async {
        // Use the download function from the API Reference section
    }
}
```

## Development

### Local Development

```bash
# Install dependencies
pip install -e ".[dev]"

# Run locally
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Run tests
pytest
```

### Building Docker Image

```bash
# Build locally
docker build -t video-download-api .

# Build for multiple platforms
docker buildx build --platform linux/amd64,linux/arm64 -t video-download-api .
```

## License

MIT License - see LICENSE file for details.

## Troubleshooting

### Traefik Reverse Proxy 404 Errors

If you get a 404 error when accessing the API through Traefik:

1. **Verify the service is explicitly linked to the router**:
   ```yaml
   labels:
     - "traefik.enable=true"
     - "traefik.http.routers.ytdlp.rule=Host(`your-domain.com`)"
     - "traefik.http.routers.ytdlp.entrypoints=websecure"
     - "traefik.http.routers.ytdlp.service=ytdlp"  # Explicitly link router to service
     - "traefik.http.routers.ytdlp.tls=true"
     - "traefik.http.services.ytdlp.loadbalancer.server.port=8000"
     - "traefik.docker.network=your-network"  # Must match Traefik's network
   ```

2. **Ensure network configuration is correct**:
   - The container must be on the same Docker network as Traefik
   - Use `traefik.docker.network=<network-name>` label to specify which network Traefik should use

3. **Check entrypoints match your request**:
   - If using HTTPS (port 443), the router must use `entrypoints=websecure`
   - If using HTTP (port 80), the router must use `entrypoints=web`

4. **Enable Traefik debug logging** to see why requests don't match:
   ```yaml
   command:
     - "--log.level=DEBUG"
   ```

5. **Verify the API is accessible directly**:
   ```bash
   # Test directly (bypassing Traefik)
   curl http://container-ip:8000/health
   ```

### Common Traefik Configuration for External Setup

If running Traefik separately from the provided docker-compose.yml:

```yaml
video-download-api:
  image: ghcr.io/mlshdev/telegram-twitter:latest
  container_name: video-download-api
  restart: unless-stopped
  networks:
    - proxy  # Same network as Traefik
  environment:
    - AUTH_TOKENS=your-secret-token
    - API_PORT=8000
  labels:
    - "traefik.enable=true"
    # Router configuration
    - "traefik.http.routers.ytdlp.rule=Host(`ytdlp-api.yourdomain.com`)"
    - "traefik.http.routers.ytdlp.entrypoints=websecure"
    - "traefik.http.routers.ytdlp.service=ytdlp"
    - "traefik.http.routers.ytdlp.tls=true"
    - "traefik.http.routers.ytdlp.tls.certresolver=your-certresolver"
    # Service configuration
    - "traefik.http.services.ytdlp.loadbalancer.server.port=8000"
    # Network specification (required when container is on multiple networks)
    - "traefik.docker.network=proxy"
```

## Acknowledgments

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - The video downloading engine
- [FastAPI](https://fastapi.tiangolo.com/) - The web framework
- [Traefik](https://traefik.io/) - The reverse proxy
