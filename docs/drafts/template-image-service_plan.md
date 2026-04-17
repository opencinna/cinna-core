# Template Image Service — Implementation Plan

## Overview

Replace per-environment Docker image builds with a shared, content-addressed image per template. A new `TemplateImageService` owns the build lifecycle for each template image, tagging images with a hash of their build inputs (`Dockerfile` + `pyproject.toml` + `uv.lock`). When multiple environments share the same template and the inputs have not changed, they reuse the same image tag, eliminating dangling images and redundant builds.

**Core capabilities:**
- Content-hash-based image tagging: `cinna-agent-<env_name>:<sha256[:12]>`
- Idempotent build: if the tag already exists locally, build is skipped
- Per-template concurrency safety via `asyncio.Lock`
- Build context is the template directory (not the per-env instance dir)
- `app/core` removed from all Dockerfiles — it is bind-mounted read-only at runtime already
- `TEMPLATE_IMAGE_TAG` placeholder substituted into compose at generation time

**High-level flow:**

```
create_environment_instance() / rebuild_environment()
    │
    └── TemplateImageService.ensure_template_image(env_name)
            │
            ├── Compute hash(Dockerfile + pyproject.toml + uv.lock)
            ├── tag = cinna-agent-<env_name>:<hash>
            ├── docker image inspect <tag> → exists? return tag immediately
            └── docker build --tag <tag> <templates_dir>/<env_name>/
                    │
                    └── tag returned to lifecycle → written into compose via ${TEMPLATE_IMAGE_TAG}
```

---

## Architecture Overview

```
backend/app/services/environments/
├── template_image_service.py   ← NEW: hash, inspect, build
├── environment_lifecycle.py    ← MODIFIED: calls ensure_template_image(), skips per-env build
└── adapters/
    └── docker_adapter.py       ← MODIFIED: initialize() validates compose only (no build)
                                             rebuild() drops docker-compose build step

backend/app/env-templates/
├── python-env-advanced/
│   ├── Dockerfile              ← MODIFIED: remove COPY app/core
│   └── docker-compose.template.yml  ← MODIFIED: remove build:, use ${TEMPLATE_IMAGE_TAG}
├── general-env/
│   ├── Dockerfile              ← MODIFIED: remove COPY app/core
│   └── docker-compose.template.yml  ← MODIFIED: remove build:, use ${TEMPLATE_IMAGE_TAG}
└── general-assistant-env/
    ├── Dockerfile              ← MODIFIED: remove COPY app/core
    └── docker-compose.template.yml  ← MODIFIED: remove build:, use ${TEMPLATE_IMAGE_TAG}
```

**Data flow for environment creation (new):**
1. `create_environment_instance()` calls `TemplateImageService.ensure_template_image(env_name)` → receives `image_tag`
2. `_generate_compose_file()` receives `image_tag`, substitutes `${TEMPLATE_IMAGE_TAG}` in compose template
3. `adapter.initialize()` validates compose file structure only (no build)
4. `adapter.start()` runs `docker-compose up -d` — Docker pulls the already-present local image by tag

**Data flow for rebuild (new):**
1. `rebuild_environment()` calls `ensure_template_image(env_name)` — returns quickly if hash unchanged, otherwise builds
2. Compose is regenerated with the (possibly new) tag
3. `adapter.rebuild()` runs `docker-compose down` + `docker-compose up -d` (no `docker-compose build`)

---

## New Service: TemplateImageService

**File:** `backend/app/services/environments/template_image_service.py`

### Responsibilities
- Compute a stable content hash over the files that define an image's build inputs
- Map that hash to a Docker image tag
- Inspect whether that tag already exists locally via the Docker SDK
- If not present, run `docker build` from the template directory
- Enforce one build at a time per template via per-template asyncio locks

### Public API

```python
class TemplateImageService:
    def __init__(self, templates_dir: Path):
        ...

    async def ensure_template_image(self, env_name: str) -> str:
        """
        Return the Docker image tag for the given template, building it if necessary.

        Args:
            env_name: Template name (e.g. "python-env-advanced")

        Returns:
            Image tag string, e.g. "cinna-agent-python-env-advanced:abc123def456"

        Raises:
            FileNotFoundError: If the template directory or required files are missing
            RuntimeError: If docker build fails
        """
        ...

    def compute_template_hash(self, env_name: str) -> str:
        """
        Compute SHA-256 hash over Dockerfile + pyproject.toml + uv.lock contents.

        Returns: First 12 hex characters of the hash (sufficient for uniqueness, compact tag)
        """
        ...

    def get_image_tag(self, env_name: str) -> str:
        """Return the tag that WOULD be used for the current template state, without building."""
        ...
```

### Internal Implementation Details

**Hash computation:**
- Read `Dockerfile`, `pyproject.toml`, `uv.lock` (in that fixed order) from `templates_dir / env_name /`
- Hash each file's raw bytes using `hashlib.sha256`
- If `uv.lock` does not exist (unlikely but defensive), hash an empty bytes sentinel
- Return the first 12 hex characters of the final digest
- This makes the tag stable: identical inputs → identical tag → cache hit → no rebuild

**Image tag format:** `cinna-agent-{env_name}:{hash12}`
- Example: `cinna-agent-python-env-advanced:a1b2c3d4e5f6`
- The `cinna-agent-` prefix avoids collision with compose-generated image names (`agent-*`)

**Existence check:**
- Use the Docker Python SDK (`docker.from_env().images.get(tag)`)
- Catch `docker.errors.ImageNotFound` → image absent, proceed to build
- Do NOT use `docker image inspect` subprocess — use the SDK directly, consistent with `docker_adapter.py`'s pattern

**Build execution:**
- Use `asyncio.create_subprocess_exec` (same pattern as `docker_adapter._run_compose_command`)
- Command: `docker build --tag <tag> <template_dir>`
- Build context is `backend/app/env-templates/<env_name>/` — NOT the per-env instance dir
- The Dockerfile inside the template dir no longer has `COPY app/core`, so core files are not baked in
- Stream stdout/stderr to logger at INFO/DEBUG level; on nonzero exit, raise `RuntimeError` with stderr

**Concurrency safety:**
```python
_locks: dict[str, asyncio.Lock] = {}  # class-level or module-level dict

def _get_lock(self, env_name: str) -> asyncio.Lock:
    if env_name not in self._locks:
        self._locks[env_name] = asyncio.Lock()
    return self._locks[env_name]
```
`ensure_template_image` acquires the per-template lock before the inspect+build sequence. Two concurrent calls for the same template will serialize; the second will find the image already present after the first builds it.

**Module-level singleton:**
```python
# Bottom of template_image_service.py
template_image_service = TemplateImageService(
    templates_dir=Path(settings.ENV_TEMPLATES_DIR)
)
```
Import in lifecycle: `from app.services.environments.template_image_service import template_image_service`

---

## Lifecycle Manager Changes

**File:** `backend/app/services/environments/environment_lifecycle.py`

### 1. `REBUILD_OVERWRITE_FILES` constant (line 27-32)

**Before:**
```python
REBUILD_OVERWRITE_FILES = [
    "uv.lock",
    "pyproject.toml",
    "Dockerfile",
    "docker-compose.template.yml",
]
```

**After:**
```python
REBUILD_OVERWRITE_FILES = [
    "docker-compose.template.yml",
]
```

Rationale: `Dockerfile`, `pyproject.toml`, and `uv.lock` no longer live in the per-env instance dir — they stay exclusively in the template dir and are owned by `TemplateImageService`. Only the compose template is copied and may need refreshing.

### 2. `_copy_template()` method

**Change:** Skip copying `Dockerfile`, `pyproject.toml`, `uv.lock` into the instance directory.

Add an exclusion set inside `_copy_sync()`:
```python
SKIP_FROM_TEMPLATE = {"Dockerfile", "pyproject.toml", "uv.lock"}
```
When iterating `template_dir.iterdir()`, skip any item whose `name` is in this set. Everything else (workspace, `docker-compose.template.yml`, `BUILDING_AGENT.md`, etc.) is copied as before.

### 3. `_generate_compose_file()` method

**Change:** Accept `image_tag: str` parameter and substitute `${TEMPLATE_IMAGE_TAG}` in the compose template content.

```python
def _generate_compose_file(
    self,
    instance_dir: Path,
    environment: AgentEnvironment,
    agent: Agent,
    port: int,
    auth_token: str,
    image_tag: str,           # NEW PARAMETER
):
```

Add to substitutions:
```python
content = content.replace("${TEMPLATE_IMAGE_TAG}", image_tag)
```

### 4. `_update_environment_config()` method

**Change:** Accept `image_tag: str` parameter and pass it through to `_generate_compose_file()`.

```python
def _update_environment_config(
    self,
    ...
    image_tag: str,           # NEW PARAMETER
):
```

### 5. `create_environment_instance()` method

**Change:** Before calling `_update_environment_config()` and before calling `adapter.initialize()`:

```python
# Build (or reuse) shared template image
environment.status_message = "Building template image..."
db_session.add(environment)
db_session.commit()

from app.services.environments.template_image_service import template_image_service
image_tag = await template_image_service.ensure_template_image(environment.env_name)
logger.info(f"Template image ready: {image_tag}")
```

Pass `image_tag` to `_update_environment_config()`. Remove the call to `adapter.initialize()` entirely (or keep it only for compose validation — see adapter section).

### 6. `rebuild_environment()` method

**Change:** Before calling `adapter.rebuild()`, call `ensure_template_image()`:

```python
# Rebuild (or reuse) shared template image
environment.status_message = "Building template image..."
db_session.add(environment)
db_session.commit()

from app.services.environments.template_image_service import template_image_service
image_tag = await template_image_service.ensure_template_image(environment.env_name)
logger.info(f"Template image ready for rebuild: {image_tag}")
```

Pass `image_tag` to `_update_environment_config()`. The adapter's `rebuild()` no longer runs `docker-compose build` (see below).

---

## Docker Adapter Changes

**File:** `backend/app/services/environments/adapters/docker_adapter.py`

### `initialize()` method

**Before:** Runs `docker-compose build`, then returns.

**After:** Validates that `docker-compose.yml` exists and optionally validates compose configuration. No build step.

```python
async def initialize(self, config: EnvInitConfig) -> bool:
    """
    Validate environment directory and compose configuration.

    Image build is handled by TemplateImageService before this is called.
    """
    logger.info(f"Validating Docker environment: env_dir={self.env_dir}, env_id={self.env_id}")

    if not self.env_dir.exists():
        raise FileNotFoundError(f"Environment directory not found: {self.env_dir}")

    compose_file = self.env_dir / "docker-compose.yml"
    if not compose_file.exists():
        raise FileNotFoundError(f"docker-compose.yml not found in {self.env_dir}")

    logger.info(f"Environment {self.env_id} validated successfully")
    return True
```

### `rebuild()` method

**Before:** Runs `docker-compose down`, overwrites infrastructure files, copies core, `docker-compose build`, then optionally `docker-compose up`.

**After:** Runs `docker-compose down`, updates core files and knowledge, then optionally `docker-compose up`. The `docker-compose build` step is removed. The overwrite of infrastructure files from `rebuild_overwrite_files` is kept (handles `docker-compose.template.yml` update). Core replacement logic is unchanged.

Remove lines:
```python
# Rebuild Docker image
logger.info(f"Rebuilding Docker image for environment {self.env_id}")
await self._run_compose_command(["build"])
logger.info(f"Docker image rebuilt successfully")
```

The rest of the method (down, overwrite files, replace core, sync knowledge, optionally start) remains unchanged.

---

## Template File Changes

### `python-env-advanced/Dockerfile`

Remove lines 53-54:
```dockerfile
# Copy core application files (system files baked into image)
COPY app/core /app/core
```

The `CMD` line remains: `CMD fastapi run --host 0.0.0.0 --port ${AGENT_PORT} core/main.py`

Note: `app/core` is bind-mounted read-only at container runtime via the volume mount in compose, so the `core/` directory is available when the CMD runs. No change needed to the CMD line.

### `python-env-advanced/docker-compose.template.yml`

Remove the `build:` block and change `image:`:

**Before (lines 3-7):**
```yaml
    build:
      context: .
      dockerfile: Dockerfile
    container_name: agent-${ENV_ID}
    image: agent-python-env-advanced:${ENV_VERSION}
```

**After:**
```yaml
    image: ${TEMPLATE_IMAGE_TAG}
    container_name: agent-${ENV_ID}
```

### `general-env/Dockerfile`

Same change as `python-env-advanced/Dockerfile`: remove `COPY app/core /app/core` lines.

### `general-env/docker-compose.template.yml`

Same change as `python-env-advanced/docker-compose.template.yml`: remove `build:` block, replace `image:` with `${TEMPLATE_IMAGE_TAG}`.

### `general-assistant-env/Dockerfile`

Same change: remove lines 50-51:
```dockerfile
# Copy core application files (system files baked into image)
COPY app/core /app/core
```

### `general-assistant-env/docker-compose.template.yml`

Same change as above: remove `build:` block, replace `image: agent-general-assistant-env:${ENV_VERSION}` with `image: ${TEMPLATE_IMAGE_TAG}`.

---

## Security Architecture

No new security concerns introduced. The service:
- Only reads files from `backend/app/env-templates/` (controlled, not user-facing)
- Only interacts with Docker daemon (same privilege level as before)
- Does not expose image tags via any API
- Does not store anything in the database

The image tag change (`cinna-agent-*` prefix instead of `agent-*`) does not affect existing container naming (`agent-{env_id}` stays the same).

---

## Backend Implementation

### No new API routes

This is a pure internal service change. No new routes, no new models, no frontend changes, no API client regeneration required.

### No database changes

`env_version` continues to exist on `AgentEnvironment` as metadata. It is no longer used in image tags (the hash-based tag replaces it). No migration needed.

---

## Tests

### Unit tests for `TemplateImageService`

**Location:** `backend/tests/unit/test_template_image_service.py`

These are pure unit tests using `unittest.mock` to patch Docker SDK calls and filesystem reads. They do NOT require Docker to be running.

**Test scenarios:**

1. `test_compute_template_hash_stable`: Given fixed file contents, the hash is deterministic and consistent across calls.

2. `test_compute_template_hash_changes_when_dockerfile_changes`: Changing Dockerfile bytes changes the hash.

3. `test_compute_template_hash_changes_when_pyproject_changes`: Changing `pyproject.toml` changes the hash.

4. `test_compute_template_hash_changes_when_uv_lock_changes`: Changing `uv.lock` changes the hash.

5. `test_ensure_template_image_returns_existing_tag`: When `docker.images.get(tag)` succeeds, `docker build` is NOT called, and the tag is returned immediately.

6. `test_ensure_template_image_builds_when_missing`: When `docker.images.get(tag)` raises `ImageNotFound`, `docker build` subprocess is called with the correct arguments, and the tag is returned after build.

7. `test_ensure_template_image_raises_on_build_failure`: When the build subprocess exits nonzero, a `RuntimeError` is raised.

8. `test_ensure_template_image_concurrent_calls_serialize`: Two concurrent calls for the same template name do not both trigger a build — the second call exits after the lock is released (image already present).

9. `test_different_templates_do_not_share_lock`: Two concurrent calls for different template names are not serialized by each other.

10. `test_template_dir_not_found`: `ensure_template_image` raises `FileNotFoundError` for an unknown template name.

**Mocking approach:**
- Mock `docker.from_env()` to control `images.get()` behavior
- Mock `asyncio.create_subprocess_exec` to control build subprocess
- Use `tmp_path` pytest fixture (or `tempfile.TemporaryDirectory`) to create fake template dirs with controlled file contents

### Existing test compatibility

The `EnvironmentTestAdapter` stub in `backend/tests/stubs/environment_adapter_stub.py` does not need changes — its `initialize()` and `rebuild()` stubs already return `True` without running Docker commands.

The lifecycle integration tests (in `tests/api/agents/`) that use `EnvironmentTestAdapter` mock the adapter, so they never call `ensure_template_image`. These tests are unaffected.

If any existing test patches `docker_adapter.initialize` or `docker_adapter.rebuild` to intercept `docker-compose build` calls, those patches need to be updated to remove that expectation. (Review `tests/api/agents/` for any such assertions before finalizing.)

### Domain regression test scope

After implementation, run the `tests/api/agents/` directory to confirm no regressions in environment creation, rebuild, or lifecycle flows. Do NOT run `make test-backend` (full suite) — that is the user's responsibility.

---

## Error Handling & Edge Cases

| Scenario | Handling |
|----------|----------|
| Template directory missing | `FileNotFoundError` raised early in `ensure_template_image` before any Docker call |
| `uv.lock` absent from template | Defensive: hash over empty bytes for that slot; build proceeds normally |
| Docker build fails (nonzero exit) | `RuntimeError` raised with stderr captured; environment status set to `error` by lifecycle manager's existing exception handler |
| Concurrent create for same template | Serialized by per-template `asyncio.Lock`; second call skips build after first completes |
| Concurrent create for different templates | Parallel builds proceed — different locks |
| Image deleted from Docker manually between calls | Next call to `ensure_template_image` will rebuild — normal operation |
| `docker-compose.yml` missing `${TEMPLATE_IMAGE_TAG}` | `_generate_compose_file` substitutes the variable; if the template file does not contain the placeholder, the generated compose will not have the image reference — Docker will error when starting. This is a template authoring concern, not a runtime guard needed. |
| Existing per-env dirs (legacy) still contain `Dockerfile`/`pyproject.toml` | These files are inert — compose no longer has a `build:` block so Docker never uses them. They can be left in place safely (no cleanup needed). |

---

## Migration Behaviour for Existing Environments

- **Existing running containers**: unaffected. Suspend/activate uses `docker-compose stop`/`up` on the existing container, which already has its image. No rebuild triggered.
- **Next rebuild of an existing environment**: `ensure_template_image` is called, builds the new shared image (first time, since the hash tag won't exist yet). The old per-env `agent-python-env-advanced:1.0.0`-style image remains on disk. Users can run `docker image prune` manually to reclaim space.
- **New environments**: immediately use the shared image path.
- **No automatic cleanup of old images**: out of scope per the agreed plan.

---

## Future Enhancements (Out of Scope)

- Periodic `docker image prune` scheduler to clean up orphaned images
- Warm-up call at backend startup to pre-build all template images
- Pushing template images to a registry for distributed deployments
- Template image versioning visible in the admin UI
- Replacing `suspend`'s `docker-compose stop` with `docker-compose down` (consciously excluded)

---

## Summary Checklist

### Backend tasks

- [ ] **Create** `backend/app/services/environments/template_image_service.py`
  - `TemplateImageService` class with `templates_dir: Path` constructor arg
  - `compute_template_hash(env_name) -> str`: SHA-256 over Dockerfile + pyproject.toml + uv.lock, return first 12 hex chars
  - `get_image_tag(env_name) -> str`: return `cinna-agent-{env_name}:{hash12}`
  - `ensure_template_image(env_name) -> str`: async, per-template lock, inspect → skip or build, return tag
  - Module-level singleton: `template_image_service = TemplateImageService(Path(settings.ENV_TEMPLATES_DIR))`
  - Build subprocess via `asyncio.create_subprocess_exec` following the same pattern as `_run_compose_command` in `docker_adapter.py`
  - Use Docker SDK for image inspection (`docker.from_env().images.get(tag)`)

- [ ] **Modify** `backend/app/services/environments/environment_lifecycle.py`
  - Shrink `REBUILD_OVERWRITE_FILES` to `["docker-compose.template.yml"]`
  - Add `SKIP_FROM_TEMPLATE = {"Dockerfile", "pyproject.toml", "uv.lock"}` constant; apply in `_copy_template()` iteration
  - Add `image_tag: str` parameter to `_generate_compose_file()` and `_update_environment_config()`
  - Add `content.replace("${TEMPLATE_IMAGE_TAG}", image_tag)` substitution in `_generate_compose_file()`
  - In `create_environment_instance()`: call `ensure_template_image()` before config generation; pass tag; remove `adapter.initialize()` call (or convert to validation-only)
  - In `rebuild_environment()`: call `ensure_template_image()` before `adapter.rebuild()`; pass tag to config generation

- [ ] **Modify** `backend/app/services/environments/adapters/docker_adapter.py`
  - `initialize()`: Remove `await self._run_compose_command(["build"])`. Validate compose file exists and return `True`.
  - `rebuild()`: Remove `await self._run_compose_command(["build"])` and the surrounding log lines. Keep all other steps (down, overwrite files, update core, sync knowledge, optionally start).

### Template tasks

- [ ] **Modify** `backend/app/env-templates/python-env-advanced/Dockerfile`: Remove `COPY app/core /app/core` (and its comment line)
- [ ] **Modify** `backend/app/env-templates/python-env-advanced/docker-compose.template.yml`: Remove `build:` block; replace `image: agent-python-env-advanced:${ENV_VERSION}` with `image: ${TEMPLATE_IMAGE_TAG}`
- [ ] **Modify** `backend/app/env-templates/general-env/Dockerfile`: Same Dockerfile change
- [ ] **Modify** `backend/app/env-templates/general-env/docker-compose.template.yml`: Same compose change
- [ ] **Modify** `backend/app/env-templates/general-assistant-env/Dockerfile`: Same Dockerfile change
- [ ] **Modify** `backend/app/env-templates/general-assistant-env/docker-compose.template.yml`: Same compose change

### Testing tasks

- [ ] **Create** `backend/tests/unit/test_template_image_service.py`
  - 10 test scenarios listed above, using mocked Docker SDK and subprocess, with tmp_path for fake template files
  - No Docker daemon required
- [ ] **Verify** existing `tests/api/agents/` stubs (`EnvironmentTestAdapter`) still pass — no changes expected
- [ ] **Run** `docker compose exec backend python -m pytest tests/unit/test_template_image_service.py -v` to confirm new tests pass
- [ ] **Run** `docker compose exec backend python -m pytest tests/api/agents/ -v` to confirm no domain regressions

### Documentation tasks

- [ ] **Update** `docs/agents/agent_environments/agent_environments.md`: Replace "Docker image built per env" language with shared per-template image description; update "Environment Creation" and "Environment Rebuild" flows
- [ ] **Update** `docs/agents/agent_environments/agent_environments_tech.md`: Update "Rebuild Overwrite Files" list; update `DockerEnvironmentAdapter.initialize()` description; update per-env instance dir contents; add `template_image_service.py` to services list
- [ ] **Update** `docs/agents/agent_environments/agent_multi_image_environments.md`: Update template architecture section to reflect that `Dockerfile`/`pyproject.toml`/`uv.lock` live only in `backend/app/env-templates/` and are NOT copied to per-env dirs
