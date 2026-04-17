"""
TemplateImageService — shared per-template Docker image management.

Instead of building a new Docker image for every environment instance, this
service maintains ONE image per template, tagged by the content hash of the
build inputs (Dockerfile + pyproject.toml + uv.lock).

Tag format:  cinna-agent-<env_name>:<sha256[:12]>
Build context: backend/app/env-templates/<env_name>/

Concurrency: one asyncio.Lock per template name prevents duplicate parallel builds.
"""

import asyncio
import hashlib
import logging
from pathlib import Path

import docker
import docker.errors

from app.core.config import settings

logger = logging.getLogger(__name__)

# Files (in fixed order) whose contents define the image build inputs.
# Changing any of these produces a new hash → new tag → rebuild triggered.
_HASH_FILES = ("Dockerfile", "pyproject.toml", "uv.lock")


class TemplateImageService:
    """Manages shared Docker images for environment templates."""

    def __init__(self, templates_dir: Path) -> None:
        self.templates_dir = templates_dir
        self._locks: dict[str, asyncio.Lock] = {}
        self._docker_client: docker.DockerClient | None = None

    def _get_docker_client(self) -> docker.DockerClient:
        """Return (lazily initialised) Docker client."""
        if self._docker_client is None:
            self._docker_client = docker.from_env()
        return self._docker_client

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def _get_lock(self, env_name: str) -> asyncio.Lock:
        """Return (creating if needed) the per-template asyncio lock."""
        if env_name not in self._locks:
            self._locks[env_name] = asyncio.Lock()
        return self._locks[env_name]

    def compute_template_hash(self, env_name: str) -> str:
        """
        Compute a 12-character SHA-256 hex digest over the build-input files.

        Files hashed (in fixed order): Dockerfile, pyproject.toml, uv.lock.
        Missing files contribute an empty-bytes slot so the hash remains stable
        across all templates even if uv.lock is absent.

        Args:
            env_name: Template name (e.g. "python-env-advanced").

        Returns:
            First 12 hex characters of the SHA-256 digest.
        """
        template_dir = self.templates_dir / env_name
        h = hashlib.sha256()
        for filename in _HASH_FILES:
            filepath = template_dir / filename
            try:
                h.update(filepath.read_bytes())
            except FileNotFoundError:
                h.update(b"")  # absent file → empty sentinel, keeps hash stable
        return h.hexdigest()[:12]

    def get_image_tag(self, env_name: str) -> str:
        """
        Return the image tag for the current state of the template.

        Does NOT build the image — call ensure_template_image() for that.

        Args:
            env_name: Template name.

        Returns:
            Tag string, e.g. "cinna-agent-python-env-advanced:a1b2c3d4e5f6".
        """
        hash12 = self.compute_template_hash(env_name)
        return f"cinna-agent-{env_name}:{hash12}"

    async def ensure_template_image(self, env_name: str) -> str:
        """
        Return the Docker image tag for the given template, building it if necessary.

        Flow:
        1. Verify the template directory exists.
        2. Acquire the per-template lock (serialises concurrent callers for the same template).
        3. Compute the content-hash tag.
        4. If the image already exists locally → return immediately (cache hit).
        5. Otherwise run ``docker build`` from the template directory.

        Args:
            env_name: Template name (e.g. "python-env-advanced").

        Returns:
            Image tag string.

        Raises:
            FileNotFoundError: Template directory not found.
            RuntimeError: docker build exited with a nonzero status.
        """
        template_dir = self.templates_dir / env_name
        if not template_dir.exists():
            raise FileNotFoundError(
                f"Template directory not found for env_name={env_name!r}: {template_dir}"
            )

        async with self._get_lock(env_name):
            tag = self.get_image_tag(env_name)

            # --- cache check (blocking Docker SDK call offloaded to a thread) ---
            try:
                client = await asyncio.to_thread(self._get_docker_client)
                await asyncio.to_thread(client.images.get, tag)
                logger.info(f"Template image cache hit — reusing {tag}")
                return tag
            except docker.errors.ImageNotFound:
                pass  # fall through to build
            except Exception as exc:
                # Docker daemon unreachable or other SDK error — surface it.
                raise RuntimeError(f"Docker image inspection failed for {tag}: {exc}") from exc

            # --- build ---
            logger.info(f"Building template image {tag} from {template_dir}")
            await self._build_image(env_name, tag)
            logger.info(f"Template image built successfully: {tag}")
            return tag

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _build_image(self, env_name: str, tag: str) -> None:
        """
        Run ``docker build --tag <tag> <template_dir>`` as a subprocess.

        Uses asyncio.create_subprocess_exec (same pattern as
        DockerEnvironmentAdapter._run_compose_command) so it does not block
        the event loop during the potentially lengthy build.

        Args:
            env_name: Template name (used to resolve the build context path).
            tag: Fully-qualified image tag to apply.

        Raises:
            RuntimeError: Build process exited with nonzero status.
        """
        template_dir = self.templates_dir / env_name
        cmd = ["docker", "build", "--tag", tag, str(template_dir)]

        logger.debug(f"docker build command: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()

        if stdout:
            logger.info(f"docker build stdout for {env_name}:\n{stdout.decode()}")
        if stderr:
            # stderr carries Docker's build progress output even on success
            logger.info(f"docker build stderr for {env_name}:\n{stderr.decode()}")

        if process.returncode != 0:
            stderr_text = stderr.decode()
            logger.error(
                f"docker build FAILED for {env_name} "
                f"(exit code {process.returncode}): {stderr_text}"
            )
            raise RuntimeError(
                f"docker build failed for template {env_name!r} "
                f"(exit {process.returncode}): {stderr_text}"
            )


# ---------------------------------------------------------------------------
# Module-level singleton — import this in lifecycle manager.
# ---------------------------------------------------------------------------
template_image_service = TemplateImageService(Path(settings.ENV_TEMPLATES_DIR))
