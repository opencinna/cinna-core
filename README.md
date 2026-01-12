# Workflow Runner Core

A **conversational AI agent platform** for creating custom AI agents with specific capabilities, running them in isolated environments (Docker containers), and interacting through persistent chat sessions.

## Core Concepts

- **Agent** - User-defined AI assistant with custom prompts, credentials, and configuration
- **Environment** - Docker container or remote server where the agent runs (supports multiple per agent for testing/production)
- **Session** - Chat thread with independent message history (multiple sessions share the agent's workspace)
- **Message** - Individual communication unit within a session

## Architecture

```
User → Session → Environment → Docker Container → Google ADK Agent / Claude SDK
```

## Key Features

- **Environment Abstraction**: Supports Docker, SSH, HTTP (extensible to Kubernetes, cloud functions)
- **Blue-Green Deployments**: Switch between environments instantly for rollback
- **Session Modes**: Building mode (setup/config) and Conversation mode (task execution)
- **Shared File System**: Files persist across sessions within an environment
- **Credential Security**: Encrypted at rest, mounted during environment initialization only

## Tech Stack

- **Backend**: FastAPI (Python) with PostgreSQL
- **Frontend**: React + TypeScript
- **Agent Runtime**: Google Agent Development Kit (ADK) / Claude SDK
- **Isolation**: Docker containers with mounted volumes
