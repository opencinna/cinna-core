# Production Deployment - WebSocket Setup

## Overview

This guide explains how to deploy the application with WebSocket support in production without Traefik.

## Production Architecture

```
┌─────────────────────────────────────────────────┐
│                                                 │
│  Frontend Container (project.com)              │
│  - Nginx serving static files                  │
│  - Port: 80 (mapped to host)                   │
│  - Connects to backend API via VITE_API_URL    │
│                                                 │
└─────────────────────────────────────────────────┘
                       │
                       │ HTTP/WebSocket
                       ↓
┌─────────────────────────────────────────────────┐
│                                                 │
│  Backend Container (api.project.com)           │
│  - FastAPI + Socket.IO                         │
│  - Port: 8000 (mapped to host)                 │
│  - HTTP API: http://api.project.com:8000       │
│  - WebSocket: ws://api.project.com:8000/ws     │
│                                                 │
└─────────────────────────────────────────────────┘
                       │
                       │
                       ↓
┌─────────────────────────────────────────────────┐
│  Database Container                             │
│  - PostgreSQL                                   │
│  - Port: 5432 (internal only)                   │
└─────────────────────────────────────────────────┘
```

## Domain Configuration

In production, you'll have:

- **Frontend**: `project.com` (served on port 80)
- **Backend API**: `api.project.com:8000` (or port 80/443 with reverse proxy)
- **WebSocket**: `wss://api.project.com/ws` (same port as backend)

## Important: WebSocket Uses Same Port as HTTP

Socket.IO is designed to work on the same port as your HTTP API. It starts as an HTTP request and then "upgrades" to WebSocket. This means:

- ✅ **You don't need a separate port for WebSocket**
- ✅ **WebSocket will work on port 8000 (same as your API)**
- ✅ **In production with HTTPS, it becomes WSS (WebSocket Secure)**

## Docker Compose Configuration

The `docker-compose.yml` file has been updated to expose the necessary ports:

```yaml
services:
  backend:
    image: '${DOCKER_IMAGE_BACKEND}:${TAG-latest}'
    restart: always
    ports:
      # Expose backend API and WebSocket port
      # WebSocket uses the same port as the HTTP API (Socket.IO upgrades the connection)
      - "8000:8000"
    # ... rest of configuration

  frontend:
    image: '${DOCKER_IMAGE_FRONTEND}:${TAG-latest}'
    restart: always
    ports:
      # Expose frontend nginx server
      - "80:80"
    # ... rest of configuration
```

## Environment Variables

### Backend (.env)

```bash
# Domain configuration
DOMAIN=project.com
ENVIRONMENT=production

# Frontend URL (for CORS and links in emails)
FRONTEND_HOST=https://project.com

# CORS origins - include your production domains
BACKEND_CORS_ORIGINS="https://project.com,https://api.project.com"

# ... other variables
```

### Frontend (Build Args)

In `docker-compose.yml`, the frontend is built with:

```yaml
build:
  context: ./frontend
  args:
    - VITE_API_URL=https://api.${DOMAIN}  # e.g., https://api.project.com
    - NODE_ENV=production
```

This means the frontend will:
- Make API calls to `https://api.project.com`
- Connect WebSocket to `wss://api.project.com/ws`

## Reverse Proxy / Load Balancer Setup

If you use a reverse proxy (like nginx or HAProxy) in front of your containers, you need to configure it to support WebSocket connections.

### Nginx Example

```nginx
# Backend API and WebSocket
server {
    listen 80;
    server_name api.project.com;

    location / {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket timeout (keep connection alive)
        proxy_read_timeout 86400;
    }

    # Specific WebSocket path (optional, same config as above)
    location /ws {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}

# Frontend
server {
    listen 80;
    server_name project.com;

    location / {
        proxy_pass http://frontend:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### HTTPS/SSL Configuration

For production, you should use HTTPS. Add SSL certificates and update nginx:

```nginx
# Backend API with SSL
server {
    listen 443 ssl http2;
    server_name api.project.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name api.project.com;
    return 301 https://$server_name$request_uri;
}
```

With HTTPS, the WebSocket connection will automatically use WSS (WebSocket Secure):
- `wss://api.project.com/ws`

## Deployment Steps

### 1. Set Environment Variables

On your production server, set the environment variables:

```bash
export DOMAIN=project.com
export ENVIRONMENT=production
export FRONTEND_HOST=https://project.com
export BACKEND_CORS_ORIGINS="https://project.com,https://api.project.com"
# ... set other required variables
```

### 2. Build and Deploy

```bash
# Build images
docker-compose -f docker-compose.yml build

# Start services
docker-compose -f docker-compose.yml up -d
```

Note: We explicitly use `docker-compose.yml` (not `docker-compose.override.yml`) for production.

### 3. Verify WebSocket Connection

After deployment, check that WebSocket is working:

```bash
# Test WebSocket connection
curl -i -N \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: SGVsbG8sIHdvcmxkIQ==" \
  http://api.project.com:8000/ws/

# Should return HTTP 101 Switching Protocols
```

### 4. Test from Frontend

Open your browser's developer console and check for WebSocket connection:

```javascript
// Should see in console:
// [EventService] Connecting to: https://api.project.com with path: /ws
// [EventService] Connected, socket ID: xyz...
```

## Firewall Configuration

Make sure your firewall allows the necessary ports:

```bash
# Allow HTTP/HTTPS for frontend
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Allow backend port (if not behind reverse proxy)
sudo ufw allow 8000/tcp

# Or if using custom WebSocket port
# sudo ufw allow 8009/tcp
```

## Troubleshooting

### WebSocket Connection Fails

1. **Check CORS settings**: Ensure `BACKEND_CORS_ORIGINS` includes your frontend domain
2. **Check proxy configuration**: If using nginx/HAProxy, ensure WebSocket upgrade headers are set
3. **Check browser console**: Look for connection errors or CORS errors
4. **Check backend logs**: `docker-compose logs -f backend | grep EventService`

### Connection Drops Frequently

1. **Increase proxy timeout**: Set `proxy_read_timeout` to a higher value (e.g., 86400 for 24 hours)
2. **Check load balancer**: Some load balancers have short timeouts for WebSocket connections
3. **Enable keepalive**: Socket.IO has built-in ping/pong for keepalive

### CORS Errors

Update `BACKEND_CORS_ORIGINS` to include all your domains:

```bash
BACKEND_CORS_ORIGINS="https://project.com,https://api.project.com,https://www.project.com"
```

## Security Considerations

1. **Use HTTPS/WSS**: Always use secure connections in production
2. **Authentication**: WebSocket connections require user authentication (user_id in auth data)
3. **Rate Limiting**: Consider adding rate limiting for event broadcasting
4. **CORS**: Restrict CORS origins to your known domains only

## Monitoring

### Check Active Connections

```bash
# Via API
curl https://api.project.com/api/v1/events/stats \
  -H "Authorization: Bearer YOUR_TOKEN"

# Returns:
# {
#   "connection_count": 5,
#   "connected_users": ["user-id-1", "user-id-2"],
#   "is_current_user_connected": true
# }
```

### Backend Logs

```bash
# View WebSocket connections
docker-compose logs -f backend | grep EventService

# Should see:
# [EventService] Client connecting: xyz...
# [EventService] Client xyz connected for user abc..., joined room: user_abc...
```

## Scaling Considerations

If you need to scale to multiple backend instances:

1. **Use Redis for Socket.IO**: Configure Socket.IO to use Redis adapter for multi-instance support
2. **Sticky Sessions**: Configure your load balancer to use sticky sessions for WebSocket connections
3. **Shared State**: Store connection state in Redis instead of in-memory

Example Redis adapter setup (future enhancement):

```python
# In backend/app/services/event_service.py
import socketio
from socketio import AsyncRedisManager

redis_manager = AsyncRedisManager('redis://redis:6379')

self.sio = socketio.AsyncServer(
    async_mode="asgi",
    client_manager=redis_manager,  # Enable multi-instance support
    # ... other options
)
```

## Summary

✅ **WebSocket uses the same port as your HTTP API (8000)**
✅ **No separate port needed for WebSocket**
✅ **Production setup**: `wss://api.project.com/ws`
✅ **Frontend connects automatically** via `eventService.connect(userId)`
✅ **Reverse proxy**: Use nginx with WebSocket upgrade headers
✅ **HTTPS/WSS**: Required for production security

## Next Steps

1. Set up SSL certificates (Let's Encrypt recommended)
2. Configure reverse proxy with WebSocket support
3. Test WebSocket connection in production
4. Monitor active connections via `/api/v1/events/stats`
5. Set up alerting for connection failures

---

For development setup, see the main [Event Bus documentation](./event_bus_system.md).
