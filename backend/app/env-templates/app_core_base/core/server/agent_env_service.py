"""
Agent Environment Service - Business logic for agent environment operations.
"""
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import zipfile
import uuid
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime

from .models import FileNode, FolderSummary, WorkspaceTreeResponse

logger = logging.getLogger(__name__)

# Orphan-cleanup guard: only files whose stem is a UUID are considered
# agent-managed. Without this, a user's `~/.ssh/id_rsa` / `id_ed25519` / any
# other `id_*` file would be deleted on every sync.
_SSH_KEY_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


# Pre-seeded ~/.ssh/known_hosts entries for common Git providers.
# Collected once via `ssh-keyscan` and hardcoded here so the container never
# needs network access to ssh-keyscan hosts before the first git clone.
# Keeping these strict protects against MITM on the most popular endpoints.
# Other hosts are handled via `StrictHostKeyChecking accept-new` in ~/.ssh/config
# (trust-on-first-use).
_KNOWN_HOSTS_SEED = """# Managed by cinna — do not edit
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXrPQ2LL4XFC1jNgUoMbjMhMj3jvKuZJQUHvxFKRhBYxdOmDXo=
gitlab.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAfuCHKVTjquxvt6CM6tdG4SLp1Btn/nOeHHE5UOzRdf
gitlab.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBFSMqzJeV9rUzU4kWitGjeR4PWSa29SPqJ1fVkhtj3Hw9xjLVXVYrU9QlYWrOLXBpQ6KWjbjTDTdDkoohFzgbEY=
gitlab.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCsj2bNKTBSpIYDEGk9KxsGh3mySTRgMtXL583qmBpzeQ+jqCMRgBqB98u3z++J1sKlXHWfM9dyhSevkMwSbhoR8XIq/U0tCNyokEi/ueaBMCvbcTHhO7FcwzY92WK4Yt0aGROY5qX2UKSeOvuP4D6TPqKF1onrSzH9bx9XUf2lEdWT/ia1NEKjunUqu1xOB/StKDHMoX4/OKyIzuS0q/T1zOATthvasJFoPrAjkohTyaDUz2LN5JoH839hViyEG82yB+MjcFV5MU3N1l1QL3cVUCh93xSaua1N85qivl+siMkPGbO5xR/En4iEY6K2XhABlBsEjBjpQssmMJsGGnRKvwHJNIULHIUzUZVu4LaM5eAbMlvj4oHMtAoq9DVExcnA2sllpFBlmZ2RLGHHzEQKE4qoU8RwBsrU4PLR9y0bLrgpR36/U4/XkKyIAKQEhpCtJWVhkaHC5PIVbVcAvKGBb8RNKvRgI+k5lXBgh1tzqSV0JGmB2Y1yUSjMAakBUoQ=
bitbucket.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAzWUknS7QXyUz5x/nj1oqIRjQWPx7KQKjVqRnFUC5Yb
bitbucket.org ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBPIQmuzMBuKdWeF4+a2sjSSpBK0iqitSQ+5BM9KhpexuGt20JpTVM7u5BDZngncgrqDMbWdxMWWOGtZ9UgbqgZE=
bitbucket.org ssh-rsa AAAAB3NzaC1yc2EAAAABIwAAAQEAubiN81eDcafrgMeLzaFPsw2kNvEcqTKl/VqLat/MaB33pZy0y3rJZtnqwR2qOOvbwKZYKiEO1O6VqNEBxKvJJelCq0dTXWT5pbO2gDXC6h6QDXCaHo6pOHGPUy+YBaGQRGuSusMEASYiWunYN0vCAI8QaXnWMXNMdFP3jHAJH0eDsoiGnLPBlBp4TNm6rYI74nMzgz3B9IikW4WVK+dc8KZJZWYjAuORU3jc1c/NPskD2ASinf8v3xnfXeukU0sJ5N6m5E8VLjObPEO+mN2t/FZTMZLiFqPWc/ALSqnMnnhwrNi2rbfg/rd/IpL8Le3pSBne8+seeFVBoGqzHM9yXw==
"""


class AgentEnvService:
    """
    Handles business logic for agent environment operations.

    Responsibilities:
    - Read/write agent prompt files (WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md)
    - Manage workspace configuration
    - Validate file operations
    - Manage plugins
    """

    def __init__(self, workspace_dir: str):
        """
        Initialize AgentEnvService.

        Args:
            workspace_dir: Path to workspace directory
        """
        self.workspace_dir = Path(workspace_dir)
        self.docs_dir = self.workspace_dir / "docs"
        self.credentials_dir = self.workspace_dir / "credentials"
        self.plugins_dir = self.workspace_dir / "plugins"
        # Per-mode MCP-provider manifest (user_mcp.json) baseline directory.
        # Holds the credential-derived remote MCP servers the SDK adapters merge
        # into the runtime config at session start (RD-5). Lives outside the
        # agent-readable credentials dir; the manifest carries bearer tokens so
        # the file is written 0o600.
        self.mcp_dir = self.workspace_dir / "mcp"

    def get_agent_prompts(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Get current agent prompts from docs files.

        Returns:
            Tuple of (workflow_prompt, entrypoint_prompt, refiner_prompt)
            Any value can be None if file doesn't exist or is empty
        """
        workflow_prompt = self._read_prompt_file("WORKFLOW_PROMPT.md")
        entrypoint_prompt = self._read_prompt_file("ENTRYPOINT_PROMPT.md")
        refiner_prompt = self._read_prompt_file("REFINER_PROMPT.md")

        return workflow_prompt, entrypoint_prompt, refiner_prompt

    def get_agent_prompt_mtimes(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Get the POSIX mtimes of the prompt docs files.

        Used by the backend prompt-sync reconcile as the env-side logical clock
        for the LWW conflict tiebreak. Missing files report ``None``.

        Returns:
            Tuple of (workflow_mtime, entrypoint_mtime, refiner_mtime)
        """
        return (
            self._read_prompt_mtime("WORKFLOW_PROMPT.md"),
            self._read_prompt_mtime("ENTRYPOINT_PROMPT.md"),
            self._read_prompt_mtime("REFINER_PROMPT.md"),
        )

    def _read_prompt_mtime(self, filename: str) -> Optional[float]:
        """Return the POSIX mtime of a prompt file, or None if it doesn't exist."""
        file_path = self.docs_dir / filename
        try:
            if not file_path.exists():
                return None
            return file_path.stat().st_mtime
        except Exception as e:
            logger.debug(f"Failed to stat {filename}: {e}")
            return None

    def update_agent_prompts(
        self,
        workflow_prompt: Optional[str] = None,
        entrypoint_prompt: Optional[str] = None,
        refiner_prompt: Optional[str] = None
    ) -> list[str]:
        """
        Update agent prompts in docs files.

        Args:
            workflow_prompt: New content for WORKFLOW_PROMPT.md (None to skip)
            entrypoint_prompt: New content for ENTRYPOINT_PROMPT.md (None to skip)
            refiner_prompt: New content for REFINER_PROMPT.md (None to skip)

        Returns:
            List of updated filenames

        Raises:
            IOError: If file write fails
        """
        # Ensure docs directory exists
        self.docs_dir.mkdir(parents=True, exist_ok=True)

        updated_files = []

        if workflow_prompt is not None:
            self._write_prompt_file("WORKFLOW_PROMPT.md", workflow_prompt)
            updated_files.append("WORKFLOW_PROMPT.md")
            logger.info(f"Updated WORKFLOW_PROMPT.md ({len(workflow_prompt)} chars)")

        if entrypoint_prompt is not None:
            self._write_prompt_file("ENTRYPOINT_PROMPT.md", entrypoint_prompt)
            updated_files.append("ENTRYPOINT_PROMPT.md")
            logger.info(f"Updated ENTRYPOINT_PROMPT.md ({len(entrypoint_prompt)} chars)")

        if refiner_prompt is not None:
            self._write_prompt_file("REFINER_PROMPT.md", refiner_prompt)
            updated_files.append("REFINER_PROMPT.md")
            logger.info(f"Updated REFINER_PROMPT.md ({len(refiner_prompt)} chars)")

        return updated_files

    def _read_prompt_file(self, filename: str) -> Optional[str]:
        """
        Read a prompt file from docs directory.

        Args:
            filename: Name of the file to read

        Returns:
            File content if exists and not empty, None otherwise
        """
        file_path = self.docs_dir / filename

        if not file_path.exists():
            logger.debug(f"{filename} not found at {file_path}")
            return None

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                if content.strip():
                    logger.info(f"Read {filename} ({len(content)} chars)")
                    return content
                else:
                    logger.debug(f"{filename} is empty")
                    return None
        except Exception as e:
            logger.error(f"Failed to read {filename}: {e}")
            return None

    def _write_prompt_file(self, filename: str, content: str):
        """
        Write content to a prompt file in docs directory.

        Args:
            filename: Name of the file to write
            content: Content to write

        Raises:
            IOError: If write operation fails
        """
        file_path = self.docs_dir / filename

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f"Wrote {filename} ({len(content)} chars)")
        except Exception as e:
            logger.error(f"Failed to write {filename}: {e}")
            raise IOError(f"Failed to write {filename}: {str(e)}")

    def validate_workspace(self) -> bool:
        """
        Validate that workspace directory exists and is accessible.

        Returns:
            True if workspace is valid, False otherwise
        """
        if not self.workspace_dir.exists():
            logger.error(f"Workspace directory does not exist: {self.workspace_dir}")
            return False

        if not self.workspace_dir.is_dir():
            logger.error(f"Workspace path is not a directory: {self.workspace_dir}")
            return False

        # Check if we can write to workspace
        try:
            test_file = self.workspace_dir / ".workspace_test"
            test_file.touch()
            test_file.unlink()
            return True
        except Exception as e:
            logger.error(f"Workspace is not writable: {e}")
            return False

    def ensure_docs_directory(self):
        """
        Ensure docs directory exists in workspace.

        Creates the directory if it doesn't exist.
        """
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Ensured docs directory exists: {self.docs_dir}")

    def get_workspace_info(self) -> dict:
        """
        Get information about the workspace.

        Returns:
            Dictionary with workspace metadata
        """
        scripts_dir = self.workspace_dir / "scripts"
        files_dir = self.workspace_dir / "files"
        uploads_dir = self.workspace_dir / "uploads"

        return {
            "workspace_dir": str(self.workspace_dir),
            "docs_dir": str(self.docs_dir),
            "has_scripts_dir": scripts_dir.exists(),
            "has_files_dir": files_dir.exists(),
            "has_uploads_dir": uploads_dir.exists(),
            "has_docs_dir": self.docs_dir.exists(),
            "has_workflow_prompt": (self.docs_dir / "WORKFLOW_PROMPT.md").exists(),
            "has_entrypoint_prompt": (self.docs_dir / "ENTRYPOINT_PROMPT.md").exists(),
            "has_refiner_prompt": (self.docs_dir / "REFINER_PROMPT.md").exists(),
        }

    def update_credentials(
        self,
        credentials_json: list[dict],
        credentials_readme: str,
        service_account_files: list[dict] | None = None,
        ssh_keys: list[dict] | None = None,
    ) -> list[str]:
        """
        Update credentials in workspace credentials directory.

        Creates two files:
        - credentials/credentials.json: Full credentials data with actual values
        - credentials/README.md: Redacted documentation for agent prompt

        SSH keys are written to ~/.ssh/ (NOT credentials/) and reconciled on
        every sync via `update_ssh_keys()`.

        Args:
            credentials_json: List of credentials with full data
            credentials_readme: Markdown content with redacted credentials
            service_account_files: List of standalone SA JSON key files
            ssh_keys: List of SSH key bundles to materialize under ~/.ssh/

        Returns:
            List of updated filenames

        Raises:
            IOError: If file write fails
        """
        import json

        # Ensure credentials directory exists
        self.credentials_dir.mkdir(parents=True, exist_ok=True)

        updated_files = []

        try:
            # Write credentials.json with full data
            credentials_file = self.credentials_dir / "credentials.json"
            with open(credentials_file, 'w', encoding='utf-8') as f:
                json.dump(credentials_json, f, indent=2)
            updated_files.append("credentials.json")
            logger.info(f"Updated credentials.json ({len(credentials_json)} credentials)")

            # Write README.md with redacted data
            readme_file = self.credentials_dir / "README.md"
            with open(readme_file, 'w', encoding='utf-8') as f:
                f.write(credentials_readme)
            updated_files.append("README.md")
            logger.info(f"Updated credentials/README.md ({len(credentials_readme)} chars)")

            # Write standalone service account JSON files
            if service_account_files:
                for sa_file in service_account_files:
                    cred_id = sa_file["credential_id"]
                    sa_filepath = self.credentials_dir / f"{cred_id}.json"
                    with open(sa_filepath, 'w', encoding='utf-8') as f:
                        json.dump(sa_file["json_content"], f, indent=2)
                    updated_files.append(f"{cred_id}.json")
                    logger.info(f"Wrote service account file: {cred_id}.json")

            # Clean up orphaned SA JSON files (not credentials.json or README.md)
            sa_ids = {sf["credential_id"] for sf in (service_account_files or [])}
            for existing_file in self.credentials_dir.glob("*.json"):
                if existing_file.name == "credentials.json":
                    continue
                file_id = existing_file.stem
                if file_id not in sa_ids:
                    existing_file.unlink()
                    logger.info(f"Removed orphaned SA file: {existing_file.name}")

            # SSH keys go into ~/.ssh/ (never under credentials/ — workspace is a
            # user-visible surface and SSH keys should remain invisible to the
            # workspace file browser). Reconciles orphans on every call so a
            # deleted/unlinked credential removes its key files on next sync.
            try:
                ssh_updates = self.update_ssh_keys(ssh_keys or [])
                updated_files.extend(ssh_updates)
            except Exception as ssh_err:
                # SSH key materialization failure must not block other
                # credentials from syncing — matches existing per-credential
                # resiliency behavior.
                logger.error(f"Failed to update SSH keys (continuing): {ssh_err}")

            # Update CredentialGuard with new values for output redaction (Phase 2).
            # Note: for ssh_key credentials, `credentials_json` only contains the
            # whitelisted metadata (public_key, fingerprint, key_type, host_aliases)
            # — the private key body is intentionally NOT fed to the guard because
            # it never appears in credentials.json or in any agent-readable file.
            # Any private-key material leaking into agent output would be a much
            # larger incident than a single-value mis-redaction would catch.
            try:
                from security.credential_guard import credential_guard
                credential_guard.update_values(credentials_json)
            except Exception as guard_err:
                logger.warning(f"Failed to update CredentialGuard (output redaction disabled): {guard_err}")

            return updated_files

        except Exception as e:
            logger.error(f"Failed to update credentials: {e}")
            raise IOError(f"Failed to update credentials: {str(e)}")

    # ------------------------------------------------------------------ #
    # SSH key materialization (~/.ssh/)                                   #
    # ------------------------------------------------------------------ #

    # Filename prefix for agent-managed SSH key files. Anything matching
    # `id_<uuid>` (or `.pub`) under ~/.ssh/ is owned by the sync loop and
    # subject to orphan cleanup — users should not hand-author files with this
    # prefix.
    _SSH_KEY_PREFIX = "id_"

    def update_ssh_keys(self, ssh_keys: list[dict]) -> list[str]:
        """
        Materialize SSH key credentials into ~/.ssh/ for use by standard SSH
        tooling (ssh, scp, git clone git@host:repo).

        Steps (run on every sync, idempotent):
          1. mkdir ~/.ssh (0700).
          2. Seed known_hosts with GitHub/GitLab/Bitbucket entries if missing.
          3. Write each key pair as id_<credential_id> (0600) + .pub (0644).
          4. Regenerate ~/.ssh/config (0600) from scratch with IdentityFile
             entries for each key.
          5. Reconcile orphans: delete any id_<uuid> / id_<uuid>.pub files whose
             UUID is not in the current sync list. Only UUID-shaped stems are
             ever touched — user-authored files like `id_rsa` are preserved.

        Args:
            ssh_keys: List of {credential_id, private_key, public_key,
                               passphrase, host_aliases} dicts.

        Returns:
            List of ssh/ relative paths that were written or removed.
        """
        # Resolve ~ against the process's HOME. Under current env templates the
        # agent runs as root, so this resolves to /root/.ssh — the same
        # directory `git` and `ssh` consult by default. If a future template
        # runs as non-root, HOME must point at that user's home for this to
        # keep working; OpenSSH tooling will follow the same HOME automatically.
        ssh_dir = Path(os.path.expanduser("~/.ssh"))

        # Step 1 — ensure ~/.ssh exists with 0700.
        ssh_dir.mkdir(parents=True, exist_ok=True)
        # mkdir may ignore the mode arg depending on the umask; force it.
        try:
            os.chmod(ssh_dir, 0o700)
        except OSError as e:
            logger.warning(f"Could not chmod {ssh_dir} to 0700: {e}")

        updated: list[str] = []

        # Step 2 — seed known_hosts. If the file already contains the expected
        # github.com entry, leave it alone; otherwise overwrite with the seed.
        # This preserves any custom entries the user has added by letting
        # trust-on-first-use (StrictHostKeyChecking accept-new) handle them.
        known_hosts = ssh_dir / "known_hosts"
        seed_needed = True
        if known_hosts.exists():
            try:
                existing_content = known_hosts.read_text(encoding="utf-8", errors="replace")
                if "github.com" in existing_content:
                    seed_needed = False
            except OSError:
                seed_needed = True
        if seed_needed:
            try:
                known_hosts.write_text(_KNOWN_HOSTS_SEED, encoding="utf-8")
                os.chmod(known_hosts, 0o644)
                updated.append(".ssh/known_hosts")
                logger.info("Seeded ~/.ssh/known_hosts with GitHub/GitLab/Bitbucket entries")
            except OSError as e:
                logger.warning(f"Could not seed known_hosts: {e}")

        # Step 3 — write each key pair
        synced_ids: set[str] = set()
        for entry in ssh_keys:
            cred_id = entry.get("credential_id")
            private_key = entry.get("private_key")
            public_key = entry.get("public_key")
            if not (cred_id and private_key and public_key):
                logger.warning(
                    "Skipping ssh_keys entry with missing fields "
                    "(credential_id=%s, has_private=%s, has_public=%s)",
                    cred_id, bool(private_key), bool(public_key),
                )
                continue

            synced_ids.add(cred_id)

            priv_path = ssh_dir / f"{self._SSH_KEY_PREFIX}{cred_id}"
            pub_path = ssh_dir / f"{self._SSH_KEY_PREFIX}{cred_id}.pub"

            try:
                # Ensure private keys always end with a newline — OpenSSH PEM
                # parsers are strict about this (trailing-newline-less keys
                # cause `Load key: invalid format` errors).
                priv_body = private_key if private_key.endswith("\n") else private_key + "\n"
                priv_path.write_text(priv_body, encoding="utf-8")
                os.chmod(priv_path, 0o600)
                updated.append(f".ssh/{priv_path.name}")

                pub_body = public_key if public_key.endswith("\n") else public_key + "\n"
                pub_path.write_text(pub_body, encoding="utf-8")
                os.chmod(pub_path, 0o644)
                updated.append(f".ssh/{pub_path.name}")

                logger.info("Wrote SSH key files for credential_id=%s", cred_id)
            except OSError as e:
                logger.error(f"Failed to write SSH key files for {cred_id}: {e}")
                # Continue to the next credential; the backend will retry on
                # the next sync event.

        # Step 4 — regenerate ~/.ssh/config deterministically
        try:
            config_lines = self._build_ssh_config(ssh_keys, synced_ids, ssh_dir)
            config_path = ssh_dir / "config"
            config_path.write_text(config_lines, encoding="utf-8")
            os.chmod(config_path, 0o600)
            updated.append(".ssh/config")
        except OSError as e:
            logger.error(f"Failed to write ~/.ssh/config: {e}")

        # Step 5 — orphan reconciliation. List files with our prefix whose stem
        # is a UUID, and delete any not in synced_ids. Files with non-UUID stems
        # (id_rsa, id_ed25519, user-placed keys) are left untouched.
        try:
            for existing in ssh_dir.iterdir():
                if not existing.is_file():
                    continue
                name = existing.name
                if not name.startswith(self._SSH_KEY_PREFIX):
                    continue
                # Strip prefix and any trailing `.pub`
                stem = name[len(self._SSH_KEY_PREFIX):]
                if stem.endswith(".pub"):
                    stem = stem[: -len(".pub")]
                # Defence: only delete UUID-shaped stems (our files). This
                # protects id_rsa/id_ed25519/id_ecdsa and any other user-placed
                # file sharing the `id_` prefix.
                if not _SSH_KEY_UUID_RE.match(stem):
                    continue
                if stem in synced_ids:
                    continue
                try:
                    existing.unlink()
                    updated.append(f".ssh/{name} (removed)")
                    logger.info(f"Removed orphaned SSH key file: {name}")
                except OSError as e:
                    logger.warning(f"Could not remove orphan {name}: {e}")
        except OSError as e:
            logger.warning(f"Could not enumerate {ssh_dir} for orphan cleanup: {e}")

        return updated

    def _build_ssh_config(
        self,
        ssh_keys: list[dict],
        synced_ids: set[str],
        ssh_dir: Path,
    ) -> str:
        """
        Build the ~/.ssh/config content from the current sync list.

        Rules:
          - Keys with `host_aliases=['*']` (or empty) become global identities
            under `Host *` with `IdentitiesOnly no` (SSH will try each one).
          - Keys with specific host aliases get their own `Host <aliases>` block
            with `IdentitiesOnly yes` so only that key is offered to those hosts.
        """
        global_keys: list[str] = []
        scoped_entries: list[tuple[list[str], str]] = []

        for entry in ssh_keys:
            cred_id = entry.get("credential_id")
            if not cred_id or cred_id not in synced_ids:
                continue
            identity_path = f"~/.ssh/{self._SSH_KEY_PREFIX}{cred_id}"
            aliases = entry.get("host_aliases") or ["*"]
            # Filter out empty strings and dedupe while preserving order
            aliases = [a.strip() for a in aliases if isinstance(a, str) and a.strip()]
            if not aliases or aliases == ["*"]:
                global_keys.append(identity_path)
            else:
                scoped_entries.append((aliases, identity_path))

        lines: list[str] = []
        lines.append("# Managed by cinna — do not edit")
        lines.append("# Regenerated on every credential sync.")
        lines.append("")

        # Scoped (host-specific) entries FIRST. OpenSSH applies the first
        # matching Host stanza's IdentitiesOnly directive; having scoped blocks
        # before the catch-all ensures that specific aliases present only their
        # dedicated key.
        for aliases, identity in scoped_entries:
            host_pattern = " ".join(aliases)
            lines.append(f"Host {host_pattern}")
            lines.append(f"    IdentityFile {identity}")
            lines.append("    IdentitiesOnly yes")
            lines.append("")

        # Catch-all block: global defaults + every global-scope IdentityFile.
        # Multiple IdentityFile lines are supported and tried in order. We set
        # IdentitiesOnly=no so keys from ssh-agent are also considered.
        lines.append("Host *")
        lines.append("    StrictHostKeyChecking accept-new")
        lines.append("    UserKnownHostsFile ~/.ssh/known_hosts")
        lines.append("    ServerAliveInterval 60")
        lines.append("    IdentitiesOnly no")
        for identity in global_keys:
            lines.append(f"    IdentityFile {identity}")
        lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def get_credentials_readme(self) -> Optional[str]:
        """
        Get credentials README content.

        Returns:
            Content of credentials/README.md if exists and not empty, None otherwise
        """
        readme_file = self.credentials_dir / "README.md"

        if not readme_file.exists():
            logger.debug(f"credentials/README.md not found at {readme_file}")
            return None

        try:
            with open(readme_file, 'r', encoding='utf-8') as f:
                content = f.read()
                if content.strip():
                    logger.info(f"Read credentials/README.md ({len(content)} chars)")
                    return content
                else:
                    logger.debug("credentials/README.md is empty")
                    return None
        except Exception as e:
            logger.error(f"Failed to read credentials/README.md: {e}")
            return None

    def get_agent_handover_config(self) -> dict:
        """
        Get agent handover configuration from JSON file.

        Returns:
            Dictionary with handovers list and handover_prompt, or empty structure if file doesn't exist
        """
        config_file = self.docs_dir / "agent_handover_config.json"

        if not config_file.exists():
            logger.debug(f"agent_handover_config.json not found at {config_file}")
            return {"handovers": [], "handover_prompt": ""}

        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                logger.info(f"Read agent_handover_config.json ({len(config.get('handovers', []))} handovers)")
                return config
        except Exception as e:
            logger.error(f"Failed to read agent_handover_config.json: {e}")
            return {"handovers": [], "handover_prompt": ""}

    def update_agent_handover_config(
        self,
        handovers: list[dict],
        handover_prompt: str
    ) -> bool:
        """
        Update agent handover configuration in JSON file.

        Creates/updates docs/agent_handover_config.json with:
        - handovers: Array of {id, name, prompt} objects
        - handover_prompt: Prompt text to append to conversation mode system prompt

        Args:
            handovers: List of handover configs with id, name, prompt fields
            handover_prompt: Instructions for handover tool usage

        Returns:
            True if successful, False otherwise

        Raises:
            IOError: If file write fails
        """
        # Ensure docs directory exists
        self.docs_dir.mkdir(parents=True, exist_ok=True)

        config_file = self.docs_dir / "agent_handover_config.json"
        config = {
            "handovers": handovers,
            "handover_prompt": handover_prompt
        }

        try:
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
            logger.info(f"Updated agent_handover_config.json ({len(handovers)} handovers)")
            return True
        except Exception as e:
            logger.error(f"Failed to write agent_handover_config.json: {e}")
            raise IOError(f"Failed to write agent_handover_config.json: {str(e)}")

    def validate_workspace_path(self, relative_path: str) -> Path:
        """
        Validate a relative path is safe and within workspace.

        Args:
            relative_path: User-provided path (e.g., "files/data.csv")

        Returns:
            Resolved absolute Path if valid

        Security Checks:
        1. Reject absolute paths (starts with /)
        2. Reject paths with .. components
        3. Resolve to absolute path
        4. Verify resolved path is under workspace_dir
        5. Check for symlinks pointing outside workspace

        Raises:
            ValueError: If path is invalid or unsafe
        """
        # 1. Reject absolute paths
        if relative_path.startswith('/'):
            raise ValueError("Absolute paths not allowed")

        # 2. Reject .. components
        if '..' in Path(relative_path).parts:
            raise ValueError("Parent directory references (..) not allowed")

        # 3. Resolve to absolute path
        full_path = (self.workspace_dir / relative_path).resolve()

        # 4. Verify within workspace boundary
        try:
            full_path.relative_to(self.workspace_dir.resolve())
        except ValueError:
            raise ValueError("Path outside workspace boundary")

        # 5. Check symlinks don't escape workspace
        if full_path.is_symlink():
            link_target = full_path.readlink()
            if link_target.is_absolute():
                resolved_target = link_target.resolve()
            else:
                resolved_target = (full_path.parent / link_target).resolve()

            try:
                resolved_target.relative_to(self.workspace_dir.resolve())
            except ValueError:
                raise ValueError("Symlink points outside workspace")

        return full_path

    def get_workspace_tree(self) -> WorkspaceTreeResponse:
        """
        Build complete workspace tree for files, logs, scripts, docs, uploads folders.

        Returns:
            WorkspaceTreeResponse with full tree structure and summaries

        Raises:
            IOError: If workspace directory doesn't exist or isn't accessible
        """
        if not self.workspace_dir.exists():
            raise IOError(f"Workspace directory does not exist: {self.workspace_dir}")

        if not self.workspace_dir.is_dir():
            raise IOError(f"Workspace path is not a directory: {self.workspace_dir}")

        # Define the main folders to scan. The bundle-owned `uploads/` folder
        # was removed: user file uploads now land in `app-data/uploads/`, which
        # survives bundle updates and uninstall/reinstall.
        folders = ["files", "logs", "scripts", "docs", "app-data"]
        tree_nodes: dict[str, FileNode] = {}
        summaries: dict[str, FolderSummary] = {}

        for folder_name in folders:
            folder_path = self.workspace_dir / folder_name
            summary_key = folder_name.replace("-", "_")

            if not folder_path.exists():
                # Create empty folder node if directory doesn't exist
                logger.warning(f"Folder {folder_name} does not exist, creating empty node")
                tree_nodes[folder_name] = FileNode(
                    name=folder_name,
                    type="folder",
                    path=folder_name,
                    size=0,
                    modified=None,
                    children=[]
                )
                summaries[summary_key] = FolderSummary(fileCount=0, totalSize=0)
            else:
                # Build tree for existing folder
                logger.debug(f"Building tree for {folder_name}")
                node = self._build_tree_recursive(folder_path, self.workspace_dir)
                tree_nodes[folder_name] = node

                # Calculate summary
                summary = self._calculate_folder_summary(node)
                summaries[summary_key] = summary
                logger.info(f"{folder_name}: {summary.fileCount} files, {summary.totalSize} bytes")

        # Include webapp folder if it exists
        webapp_node = None
        webapp_path = self.workspace_dir / "webapp"
        if webapp_path.exists() and webapp_path.is_dir():
            webapp_node = self._build_tree_recursive(webapp_path, self.workspace_dir)
            summary = self._calculate_folder_summary(webapp_node)
            summaries["webapp"] = summary
            logger.info(f"webapp: {summary.fileCount} files, {summary.totalSize} bytes")

        # Include agent_api folder if it exists (cinna_api producer source)
        agent_api_node = None
        agent_api_path = self.workspace_dir / "agent_api"
        if agent_api_path.exists() and agent_api_path.is_dir():
            agent_api_node = self._build_tree_recursive(agent_api_path, self.workspace_dir)
            summary = self._calculate_folder_summary(agent_api_node)
            summaries["agent_api"] = summary
            logger.info(f"agent_api: {summary.fileCount} files, {summary.totalSize} bytes")

        return WorkspaceTreeResponse(
            files=tree_nodes["files"],
            logs=tree_nodes["logs"],
            scripts=tree_nodes["scripts"],
            docs=tree_nodes["docs"],
            app_data=tree_nodes["app-data"],
            webapp=webapp_node,
            agent_api=agent_api_node,
            summaries=summaries
        )

    def _build_tree_recursive(self, dir_path: Path, relative_to: Path) -> FileNode:
        """
        Recursively build tree structure for a directory.

        Args:
            dir_path: Absolute path to directory
            relative_to: Base path for calculating relative paths

        Returns:
            FileNode with children populated recursively
        """
        # Get relative path
        try:
            rel_path = dir_path.relative_to(relative_to)
            path_str = str(rel_path)
        except ValueError:
            # Shouldn't happen if we're called correctly, but handle gracefully
            path_str = dir_path.name

        # Get directory metadata
        try:
            stat = dir_path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime)
        except Exception as e:
            logger.warning(f"Failed to get metadata for {dir_path}: {e}")
            modified = None

        # Create folder node
        node = FileNode(
            name=dir_path.name,
            type="folder",
            path=path_str,
            size=None,  # Will be calculated later if needed
            modified=modified,
            children=[]
        )

        # List directory contents
        try:
            items = list(dir_path.iterdir())
        except PermissionError as e:
            logger.warning(f"Permission denied reading {dir_path}: {e}")
            return node
        except Exception as e:
            logger.error(f"Error reading directory {dir_path}: {e}")
            return node

        # Separate files and folders
        files = []
        folders = []

        for item in items:
            # Skip hidden files and __pycache__
            if item.name.startswith('.') or item.name == '__pycache__':
                continue

            if item.is_file():
                try:
                    stat = item.stat()
                    file_node = FileNode(
                        name=item.name,
                        type="file",
                        path=str(item.relative_to(relative_to)),
                        size=stat.st_size,
                        modified=datetime.fromtimestamp(stat.st_mtime),
                        children=None
                    )
                    files.append(file_node)
                except Exception as e:
                    logger.warning(f"Failed to process file {item}: {e}")

            elif item.is_dir():
                # Recursively process subdirectory
                try:
                    folder_node = self._build_tree_recursive(item, relative_to)
                    folders.append(folder_node)
                except Exception as e:
                    logger.warning(f"Failed to process directory {item}: {e}")

        # Sort: folders alphabetically first, then files alphabetically
        folders.sort(key=lambda x: x.name.lower())
        files.sort(key=lambda x: x.name.lower())

        # Combine into children list
        node.children = folders + files

        return node

    def _calculate_folder_summary(self, node: FileNode) -> FolderSummary:
        """
        Calculate fileCount and totalSize for a folder tree.

        Args:
            node: Root FileNode (must be type="folder")

        Returns:
            FolderSummary with counts and sizes
        """
        if node.type != "folder":
            # If it's a file, count it
            return FolderSummary(fileCount=1, totalSize=node.size or 0)

        file_count = 0
        total_size = 0

        # Recursively traverse children
        if node.children:
            for child in node.children:
                if child.type == "file":
                    file_count += 1
                    total_size += child.size or 0
                else:
                    # Recursively process subfolder
                    sub_summary = self._calculate_folder_summary(child)
                    file_count += sub_summary.fileCount
                    total_size += sub_summary.totalSize

        return FolderSummary(fileCount=file_count, totalSize=total_size)

    def create_workspace_zip(self, relative_path: str) -> Path:
        """
        Create a zip archive of a workspace folder or file.

        Args:
            relative_path: Path relative to workspace root (e.g., "files/project1")

        Returns:
            Path to created zip file in /tmp

        Security:
        1. Validate relative_path doesn't escape workspace (no .., absolute paths)
        2. Resolve to absolute path and verify it's under workspace_dir
        3. Check path exists

        Implementation:
        1. Validate and resolve path
        2. Create temporary zip file: /tmp/workspace_{uuid}.zip
        3. If path is file: add single file to zip
        4. If path is folder: recursively add all contents
        5. Return zip file path

        Raises:
            IOError: If path invalid, doesn't exist, or zip creation fails
        """
        # Validate path
        try:
            absolute_path = self.validate_workspace_path(relative_path)
        except ValueError as e:
            raise IOError(f"Invalid path: {e}")

        if not absolute_path.exists():
            raise IOError(f"Path does not exist: {relative_path}")

        # Create temp zip file
        zip_id = str(uuid.uuid4())[:8]
        zip_path = Path(f"/tmp/workspace_{zip_id}.zip")

        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                if absolute_path.is_file():
                    # Add single file
                    arcname = absolute_path.name
                    zipf.write(absolute_path, arcname=arcname)
                    logger.debug(f"Added file {arcname} to zip")
                else:
                    # Add folder recursively
                    for item in absolute_path.rglob('*'):
                        if item.is_file():
                            # Skip hidden files and __pycache__
                            if any(part.startswith('.') or part == '__pycache__' for part in item.parts):
                                continue

                            # Calculate relative path within the zip
                            arcname = item.relative_to(absolute_path)
                            zipf.write(item, arcname=str(arcname))

                    logger.debug(f"Added folder {absolute_path.name} to zip")

            logger.info(f"Created zip archive: {zip_path} ({zip_path.stat().st_size} bytes)")
            return zip_path

        except Exception as e:
            # Clean up zip file if creation failed
            if zip_path.exists():
                zip_path.unlink()
            logger.error(f"Failed to create zip archive: {e}")
            raise IOError(f"Failed to create zip archive: {str(e)}")

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """
        Sanitize filename for agent-env storage.

        Rules:
        - Remove/replace dangerous characters
        - Preserve extension
        - Limit length to 255 characters
        - Replace spaces with underscores
        """
        import re
        import unicodedata
        import os

        # Normalize unicode
        filename = unicodedata.normalize('NFKD', filename)
        filename = filename.encode('ascii', 'ignore').decode('ascii')

        # Remove path separators and dangerous chars
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', filename)

        # Replace spaces with underscores
        filename = filename.replace(' ', '_')

        # Remove multiple underscores
        filename = re.sub(r'_+', '_', filename)

        # Truncate to 255 chars while preserving extension
        if len(filename) > 255:
            name, ext = os.path.splitext(filename)
            max_name_len = 255 - len(ext)
            filename = name[:max_name_len] + ext

        # Ensure filename is not empty
        if not filename:
            filename = "file"

        return filename

    @staticmethod
    def resolve_filename_conflict(
        filename: str,
        directory: Path,
        max_attempts: int = 100
    ) -> str:
        """
        Generate unique filename if conflict exists.

        If document.pdf exists, tries:
        - document_1.pdf
        - document_2.pdf
        - ...
        - document_100.pdf

        Raises HTTPException if max_attempts exceeded.
        """
        from fastapi import HTTPException
        import os

        base_path = directory / filename
        if not base_path.exists():
            return filename

        name, ext = os.path.splitext(filename)

        for i in range(1, max_attempts + 1):
            new_filename = f"{name}_{i}{ext}"
            new_path = directory / new_filename
            if not new_path.exists():
                return new_filename

        raise HTTPException(
            status_code=500,
            detail=f"Could not resolve filename conflict for {filename} after {max_attempts} attempts"
        )

    # SQLite Database Methods

    SQLITE_EXTENSIONS = [".db", ".sqlite", ".sqlite3"]

    @staticmethod
    def is_sqlite_file(filename: str) -> bool:
        """Check if filename has SQLite extension."""
        lower = filename.lower()
        return any(lower.endswith(ext) for ext in AgentEnvService.SQLITE_EXTENSIONS)

    def get_database_tables(self, relative_path: str) -> list[dict]:
        """
        Get list of tables and views from SQLite database.

        Args:
            relative_path: Path to SQLite file relative to workspace

        Returns:
            List of dicts with 'name' and 'type' keys (type is 'table' or 'view')

        Raises:
            ValueError: If path is invalid
            IOError: If file doesn't exist or can't be read
        """
        import sqlite3

        absolute_path = self.validate_workspace_path(relative_path)

        if not absolute_path.exists():
            raise IOError(f"Database file not found: {relative_path}")

        if not absolute_path.is_file():
            raise IOError(f"Path is not a file: {relative_path}")

        try:
            conn = sqlite3.connect(str(absolute_path), timeout=5.0)
            cursor = conn.cursor()

            # Get tables
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            tables = [{"name": row[0], "type": "table"} for row in cursor.fetchall()]

            # Get views
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
            )
            views = [{"name": row[0], "type": "view"} for row in cursor.fetchall()]

            conn.close()

            return tables + views

        except sqlite3.Error as e:
            logger.error(f"SQLite error reading {relative_path}: {e}")
            raise IOError(f"Failed to read database: {str(e)}")

    def get_database_schema(self, relative_path: str) -> dict:
        """
        Get complete schema for SQLite database including tables, views, and columns.

        Args:
            relative_path: Path to SQLite file relative to workspace

        Returns:
            Dict with path, tables, and views (each with columns)

        Raises:
            ValueError: If path is invalid
            IOError: If file doesn't exist or can't be read
        """
        import sqlite3

        absolute_path = self.validate_workspace_path(relative_path)

        if not absolute_path.exists():
            raise IOError(f"Database file not found: {relative_path}")

        if not absolute_path.is_file():
            raise IOError(f"Path is not a file: {relative_path}")

        try:
            conn = sqlite3.connect(str(absolute_path), timeout=5.0)
            cursor = conn.cursor()

            def get_columns(table_name: str) -> list[dict]:
                """Get column info for a table/view."""
                cursor.execute(f"PRAGMA table_info('{table_name}')")
                columns = []
                for row in cursor.fetchall():
                    columns.append({
                        "name": row[1],
                        "type": row[2] or "TEXT",
                        "nullable": row[3] == 0,  # notnull = 0 means nullable
                        "primary_key": row[5] > 0
                    })
                return columns

            # Get tables with columns
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            tables = []
            for row in cursor.fetchall():
                table_name = row[0]
                tables.append({
                    "name": table_name,
                    "type": "table",
                    "columns": get_columns(table_name)
                })

            # Get views with columns
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
            )
            views = []
            for row in cursor.fetchall():
                view_name = row[0]
                views.append({
                    "name": view_name,
                    "type": "view",
                    "columns": get_columns(view_name)
                })

            conn.close()

            return {
                "path": relative_path,
                "tables": tables,
                "views": views
            }

        except sqlite3.Error as e:
            logger.error(f"SQLite error reading schema from {relative_path}: {e}")
            raise IOError(f"Failed to read database schema: {str(e)}")

    def execute_query(
        self,
        relative_path: str,
        query: str,
        page: int | None = None,
        page_size: int | None = None,
        timeout_seconds: int = 30
    ) -> dict:
        """
        Execute SQL query on SQLite database.

        Args:
            relative_path: Path to SQLite file relative to workspace
            query: SQL query to execute
            page: Page number (1-based) for SELECT queries, None = no pagination
            page_size: Number of rows per page, None = no pagination
            timeout_seconds: Query timeout in seconds

        Returns:
            Dict with columns, rows, pagination info, and execution stats

        For SELECT queries: returns paginated results (if page/page_size provided)
        For DML queries: returns rows_affected count
        """
        import sqlite3
        import time

        absolute_path = self.validate_workspace_path(relative_path)

        if not absolute_path.exists():
            return {
                "columns": [],
                "rows": [],
                "total_rows": 0,
                "page": page,
                "page_size": page_size,
                "has_more": False,
                "execution_time_ms": 0,
                "query_type": "OTHER",
                "rows_affected": None,
                "error": f"Database file not found: {relative_path}",
                "error_type": "file_error"
            }

        # Detect query type
        query_stripped = query.strip().upper()
        if query_stripped.startswith("SELECT"):
            query_type = "SELECT"
        elif query_stripped.startswith("INSERT"):
            query_type = "INSERT"
        elif query_stripped.startswith("UPDATE"):
            query_type = "UPDATE"
        elif query_stripped.startswith("DELETE"):
            query_type = "DELETE"
        else:
            query_type = "OTHER"

        try:
            conn = sqlite3.connect(str(absolute_path), timeout=float(timeout_seconds))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            start_time = time.time()

            if query_type == "SELECT":
                # Check if pagination is requested
                use_pagination = page is not None and page_size is not None

                if use_pagination:
                    # For paginated SELECT queries, get total count first
                    # Wrap in subquery to handle complex queries
                    try:
                        count_query = f"SELECT COUNT(*) FROM ({query})"
                        cursor.execute(count_query)
                        total_rows = cursor.fetchone()[0]
                    except sqlite3.Error:
                        # If count fails (e.g., UNION queries), execute without count
                        total_rows = -1

                    # Execute with pagination
                    offset = (page - 1) * page_size
                    paginated_query = f"{query} LIMIT {page_size} OFFSET {offset}"
                    cursor.execute(paginated_query)
                else:
                    # No pagination - execute query as-is
                    cursor.execute(query)
                    total_rows = -1

                # Get column names
                columns = [description[0] for description in cursor.description] if cursor.description else []

                # Fetch rows
                rows = []
                for row in cursor.fetchall():
                    rows.append(list(row))

                execution_time_ms = (time.time() - start_time) * 1000

                # Calculate has_more
                if use_pagination:
                    if total_rows >= 0:
                        has_more = (offset + len(rows)) < total_rows
                    else:
                        # If we couldn't get count, check if we got a full page
                        has_more = len(rows) == page_size
                        total_rows = offset + len(rows)
                        if has_more:
                            total_rows = -1  # Unknown total
                else:
                    has_more = False
                    total_rows = len(rows)

                conn.close()

                return {
                    "columns": columns,
                    "rows": rows,
                    "total_rows": total_rows,
                    "page": page,
                    "page_size": page_size,
                    "has_more": has_more,
                    "execution_time_ms": round(execution_time_ms, 2),
                    "query_type": query_type,
                    "rows_affected": None,
                    "error": None,
                    "error_type": None
                }

            else:
                # DML or other queries
                cursor.execute(query)
                rows_affected = cursor.rowcount
                conn.commit()

                execution_time_ms = (time.time() - start_time) * 1000
                conn.close()

                return {
                    "columns": [],
                    "rows": [],
                    "total_rows": 0,
                    "page": 1,
                    "page_size": page_size,
                    "has_more": False,
                    "execution_time_ms": round(execution_time_ms, 2),
                    "query_type": query_type,
                    "rows_affected": rows_affected,
                    "error": None,
                    "error_type": None
                }

        except sqlite3.OperationalError as e:
            error_str = str(e).lower()
            if "timeout" in error_str or "locked" in error_str:
                error_type = "timeout"
            else:
                error_type = "execution_error"

            logger.error(f"SQLite OperationalError on {relative_path}: {e}, original_query={query!r}, page={page}, page_size={page_size}")
            return {
                "columns": [],
                "rows": [],
                "total_rows": 0,
                "page": page,
                "page_size": page_size,
                "has_more": False,
                "execution_time_ms": 0,
                "query_type": query_type,
                "rows_affected": None,
                "error": str(e),
                "error_type": error_type
            }

        except sqlite3.Error as e:
            logger.error(f"SQLite error executing query on {relative_path}: {e}")
            return {
                "columns": [],
                "rows": [],
                "total_rows": 0,
                "page": page,
                "page_size": page_size,
                "has_more": False,
                "execution_time_ms": 0,
                "query_type": query_type,
                "rows_affected": None,
                "error": str(e),
                "error_type": "syntax_error" if "syntax" in str(e).lower() else "execution_error"
            }

    # =========================================================================
    # Plugin Management Methods
    # =========================================================================

    # A valid plugin/marketplace dir segment: no path separators, no traversal,
    # no leading dot. Mirrors the "simple path segment" guard required by the
    # plan (§4) so neither manifest apply nor pruning can ever escape
    # /app/workspace/plugins/.
    _PLUGIN_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    # Marker file written into each materialized plugin dir recording the git
    # ref the files were checked out at. Lets the install routine skip a
    # re-clone when the on-disk commit already matches (idempotency, §14.6).
    _PLUGIN_REF_MARKER = ".cinna_plugin_ref"

    # A full git commit hash (7-40 hex). Only immutable refs are eligible for the
    # idempotency skip; branch/tag names always re-fetch.
    _COMMIT_HASH_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

    @classmethod
    def _is_safe_plugin_segment(cls, segment: str) -> bool:
        """Validate a marketplace/plugin name is a single safe path segment."""
        if not segment or segment in (".", ".."):
            return False
        if "/" in segment or "\\" in segment or "\x00" in segment:
            return False
        return bool(cls._PLUGIN_SEGMENT_RE.match(segment))

    def _safe_plugin_dir(self, marketplace_name: str, plugin_name: str) -> Optional[Path]:
        """Resolve a plugin dir under plugins_dir, returning None if unsafe.

        Confirms both names are safe segments AND that the resolved path is
        strictly contained within plugins_dir (defence in depth).
        """
        if not (self._is_safe_plugin_segment(marketplace_name)
                and self._is_safe_plugin_segment(plugin_name)):
            return None
        candidate = (self.plugins_dir / marketplace_name / plugin_name).resolve()
        plugins_root = self.plugins_dir.resolve()
        try:
            candidate.relative_to(plugins_root)
        except ValueError:
            return None
        return candidate

    def install_plugins(self, manifest: dict) -> list[dict]:
        """Materialize plugins declaratively from a manifest (container-side).

        This is the container-local, self-healing install routine — the plugin
        analogue of ``uv pip install -r workspace_requirements.txt``. It runs at
        container setup (new + post-rebuild) and on every plugin change.

        Steps:
          1. Write ``plugins/manifest.json`` (the persisted SSOT).
          2. For each entry ensure files are present at the pinned ref:
             - ``marketplace``: git clone @ ref into /tmp, copy subdir into
               ``plugins/<mkt>/<plugin>/``; skip when the ``.cinna_plugin_ref``
               marker already matches the ref.
             - ``bundle``: verify snapshot-seeded files exist (no git fetch).
          3. Prune plugin dirs not in the manifest (uninstall).
          4. Regenerate ``settings.json`` from the manifest, including ONLY
             plugins whose files are present on disk (failed/missing excluded so
             the SDK never receives a missing path).
          5. Return a per-plugin result list (errors are results, not raises).

        All filesystem writes are confined to ``plugins_dir``; unsafe names are
        rejected as ``failed`` results.

        Args:
            manifest: ``{"plugins": [...], "allowed_tools": [...]}``.

        Returns:
            List of PluginInstallResult dicts.
        """
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

        entries = manifest.get("plugins") or []
        allowed_tools = manifest.get("allowed_tools")

        # Step 1 — persist the manifest as the SSOT.
        try:
            manifest_file = self.plugins_dir / "manifest.json"
            with open(manifest_file, "w", encoding="utf-8") as f:
                json.dump({"plugins": entries, "allowed_tools": allowed_tools}, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write plugins/manifest.json: {e}")

        results: list[dict] = []
        # Track the (mkt, plugin) pairs that belong on disk, for pruning.
        wanted: set[tuple[str, str]] = set()

        for entry in entries:
            marketplace_name = entry.get("marketplace_name") or ""
            plugin_name = entry.get("plugin_name") or ""
            source = entry.get("source") or "marketplace"

            result = {
                "marketplace_name": marketplace_name,
                "plugin_name": plugin_name,
                "source": source,
                "status": "failed",
                "error_message": None,
            }

            plugin_dir = self._safe_plugin_dir(marketplace_name, plugin_name)
            if plugin_dir is None:
                result["error_message"] = "Unsafe marketplace/plugin name (rejected)"
                logger.warning(
                    "Rejected unsafe plugin path segment(s): %r/%r",
                    marketplace_name, plugin_name,
                )
                results.append(result)
                continue

            wanted.add((marketplace_name, plugin_name))

            try:
                if source == "bundle":
                    status, error = self._ensure_bundle_plugin(plugin_dir)
                else:
                    status, error = self._ensure_marketplace_plugin(
                        plugin_dir, entry.get("git") or {}
                    )
                result["status"] = status
                result["error_message"] = error
            except Exception as e:
                logger.error(
                    f"Unexpected error installing plugin {marketplace_name}/{plugin_name}: {e}"
                )
                result["status"] = "failed"
                result["error_message"] = str(e)

            results.append(result)

        # Step 3 — prune plugin dirs no longer in the manifest.
        self._prune_plugin_dirs(wanted)

        # Step 4 — regenerate settings.json (files-present only).
        self._regenerate_plugin_settings(entries, allowed_tools)

        installed = sum(1 for r in results if r["status"] == "installed")
        failed = sum(1 for r in results if r["status"] == "failed")
        skipped = sum(1 for r in results if r["status"] == "skipped")
        logger.info(
            f"Plugin install complete: {installed} installed, "
            f"{skipped} skipped, {failed} failed"
        )
        return results

    # Well-known PUBLIC git hosts whose SSH URLs clone keyless over HTTPS. Kept
    # in sync with the backend `LLMPluginService._normalize_public_git_url`
    # (env-core can't import backend code — the established pattern is to
    # duplicate this tiny helper). Only these hosts are rewritten.
    _PUBLIC_GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")
    _SCP_GIT_RE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")
    _SSH_GIT_RE = re.compile(r"^ssh://(?:[^@/]+@)?([^:/]+)(?::\d+)?/(.+)$")

    @classmethod
    def _normalize_public_git_url(cls, url: Optional[str]) -> Optional[str]:
        """Rewrite a well-known PUBLIC host's SSH git URL to its HTTPS form.

        The container has no GitHub/GitLab/Bitbucket SSH key, so an SSH-form URL
        (``git@github.com:org/repo.git`` or ``ssh://git@github.com/org/repo.git``)
        fails ``Permission denied (publickey)`` even for a PUBLIC repo. Only the
        recognized public hosts are rewritten; any other SSH URL (a genuinely
        private host) is returned unchanged. Mirrors the backend helper.
        """
        if not url:
            return url
        candidate = url.strip()
        if candidate.startswith(("https://", "http://", "git://")):
            return url

        host = path = None
        m = cls._SSH_GIT_RE.match(candidate)
        if m:
            host, path = m.group(1), m.group(2)
        else:
            m = cls._SCP_GIT_RE.match(candidate)
            if m:
                host, path = m.group(1), m.group(2)

        if not host or host.lower() not in cls._PUBLIC_GIT_HOSTS:
            return url
        path = path.lstrip("/")
        if not path.endswith(".git"):
            path = f"{path}.git"
        return f"https://{host.lower()}/{path}"

    def _ensure_marketplace_plugin(
        self, plugin_dir: Path, git: dict
    ) -> tuple[str, Optional[str]]:
        """Ensure a marketplace plugin's files exist on disk at the pinned ref.

        Returns (status, error_message). status is "installed" | "skipped" |
        "failed". "skipped" means files already present at the requested ref.
        """
        import shutil as _shutil
        import subprocess
        import tempfile

        url = git.get("url")
        ref = git.get("ref")
        subdir = (git.get("subdir") or "").strip().lstrip("./")

        if not url:
            return "failed", "Missing git url for marketplace plugin"

        # Defensive: the backend manifest builder already normalizes well-known
        # public-host SSH URLs to HTTPS, but a stale manifest or a direct caller
        # could still carry git@github.com:… — the container has no SSH key, so
        # rewrite recognized PUBLIC hosts here too so the keyless clone succeeds.
        url = self._normalize_public_git_url(url)

        # Reject traversal in the declared subdir before any filesystem use.
        if ".." in Path(subdir).parts or subdir.startswith("/"):
            return "failed", f"Unsafe plugin subdir: {subdir!r}"

        marker = plugin_dir / self._PLUGIN_REF_MARKER

        # Idempotency: skip when the on-disk marker already records this ref —
        # but ONLY for immutable (commit-hash) refs. A branch/tag name is
        # mutable, so we always re-fetch those to pick up moved tips. Marketplace
        # plugins are normally pinned to a commit (reproducibility); branch refs
        # are only a fallback.
        if (
            ref
            and self._COMMIT_HASH_RE.match(ref)
            and plugin_dir.exists()
            and marker.exists()
        ):
            try:
                if marker.read_text(encoding="utf-8").strip() == ref:
                    return "skipped", None
            except OSError:
                pass  # fall through to re-clone

        tmp_clone = Path(tempfile.mkdtemp(prefix="cinna_plugin_"))
        try:
            clone = subprocess.run(
                ["git", "clone", "--no-checkout", "--depth", "1", url, str(tmp_clone)],
                capture_output=True, text=True, timeout=120,
            )
            if clone.returncode != 0:
                # Shallow clone can't always reach an arbitrary ref; retry full.
                clone = subprocess.run(
                    ["git", "clone", "--no-checkout", url, str(tmp_clone)],
                    capture_output=True, text=True, timeout=300,
                )
                if clone.returncode != 0:
                    return "failed", f"git clone failed: {clone.stderr.strip()[:300]}"

            if ref:
                # Ensure the ref is fetched, then check it out.
                fetch = subprocess.run(
                    ["git", "-C", str(tmp_clone), "fetch", "--depth", "1", "origin", ref],
                    capture_output=True, text=True, timeout=120,
                )
                checkout = subprocess.run(
                    ["git", "-C", str(tmp_clone), "checkout", ref],
                    capture_output=True, text=True, timeout=60,
                )
                if checkout.returncode != 0:
                    # Retry after unshallowing if the pinned commit isn't present.
                    subprocess.run(
                        ["git", "-C", str(tmp_clone), "fetch", "--unshallow"],
                        capture_output=True, text=True, timeout=300,
                    )
                    checkout = subprocess.run(
                        ["git", "-C", str(tmp_clone), "checkout", ref],
                        capture_output=True, text=True, timeout=60,
                    )
                    if checkout.returncode != 0:
                        return "failed", f"git checkout {ref} failed: {checkout.stderr.strip()[:200]}"
            else:
                checkout = subprocess.run(
                    ["git", "-C", str(tmp_clone), "checkout", "HEAD"],
                    capture_output=True, text=True, timeout=60,
                )
                if checkout.returncode != 0:
                    return "failed", f"git checkout failed: {checkout.stderr.strip()[:200]}"

            # Locate the plugin files within the clone.
            src = (tmp_clone / subdir).resolve() if subdir else tmp_clone.resolve()
            try:
                src.relative_to(tmp_clone.resolve())
            except ValueError:
                return "failed", f"Resolved subdir escaped clone: {subdir!r}"
            if not src.exists() or not src.is_dir():
                return "failed", f"Plugin subdir not found in repo: {subdir or '.'}"

            # Replace the destination atomically-ish: remove old, copy new.
            if plugin_dir.exists():
                _shutil.rmtree(plugin_dir, ignore_errors=True)
            plugin_dir.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copytree(
                src, plugin_dir,
                ignore=_shutil.ignore_patterns(".git"),
            )

            # Write the idempotency marker.
            try:
                (plugin_dir / self._PLUGIN_REF_MARKER).write_text(
                    (ref or "HEAD"), encoding="utf-8"
                )
            except OSError as e:
                logger.warning(f"Could not write plugin ref marker: {e}")

            return "installed", None

        except subprocess.TimeoutExpired:
            return "failed", "git operation timed out"
        except Exception as e:
            return "failed", str(e)
        finally:
            _shutil.rmtree(tmp_clone, ignore_errors=True)

    def _ensure_bundle_plugin(self, plugin_dir: Path) -> tuple[str, Optional[str]]:
        """Verify a bundle-sourced plugin's files were seeded into the workspace.

        Bundle plugins have no git source — files arrive via the bundle
        workspace snapshot. If present → installed; if missing → failed (cannot
        fetch).
        """
        if plugin_dir.exists() and plugin_dir.is_dir() and any(plugin_dir.iterdir()):
            return "installed", None
        return "failed", "Bundle plugin files missing from workspace snapshot"

    def _prune_plugin_dirs(self, wanted: set[tuple[str, str]]) -> None:
        """Remove plugin directories under plugins_dir not present in the manifest.

        Confined to plugins_dir and to two-level <mkt>/<plugin> dirs whose names
        are safe segments. ``manifest.json`` / ``settings.json`` (files) are
        skipped since only directories are considered.
        """
        if not self.plugins_dir.exists():
            return
        import shutil as _shutil
        try:
            for mkt_dir in self.plugins_dir.iterdir():
                if not mkt_dir.is_dir():
                    continue
                if not self._is_safe_plugin_segment(mkt_dir.name):
                    continue
                for plugin_dir in mkt_dir.iterdir():
                    if not plugin_dir.is_dir():
                        continue
                    if not self._is_safe_plugin_segment(plugin_dir.name):
                        continue
                    if (mkt_dir.name, plugin_dir.name) in wanted:
                        continue
                    _shutil.rmtree(plugin_dir, ignore_errors=True)
                    logger.info(f"Pruned removed plugin: {mkt_dir.name}/{plugin_dir.name}")
                # Drop now-empty marketplace dir.
                try:
                    if not any(mkt_dir.iterdir()):
                        mkt_dir.rmdir()
                except OSError:
                    pass
        except OSError as e:
            logger.warning(f"Plugin prune enumeration failed: {e}")

    def _regenerate_plugin_settings(
        self, entries: list[dict], allowed_tools: Optional[list[str]]
    ) -> None:
        """Derive settings.json from the manifest, files-present only.

        A plugin is included in ``active_plugins`` only when it is not disabled
        AND its directory exists on disk — so a failed/missing plugin can never
        reach the SDK as a dangling path.
        """
        active_plugins: list[dict] = []
        for entry in entries:
            marketplace_name = entry.get("marketplace_name") or ""
            plugin_name = entry.get("plugin_name") or ""
            if entry.get("disabled"):
                continue
            plugin_dir = self._safe_plugin_dir(marketplace_name, plugin_name)
            if plugin_dir is None or not plugin_dir.exists():
                continue
            active_plugins.append({
                "marketplace_name": marketplace_name,
                "plugin_name": plugin_name,
                "path": str(plugin_dir),
                "conversation_mode": entry.get("conversation_mode", False),
                "building_mode": entry.get("building_mode", False),
                "version": entry.get("version"),
                "commit_hash": entry.get("commit_hash"),
            })

        settings_json: dict = {"active_plugins": active_plugins}
        if allowed_tools is not None:
            settings_json["allowed_tools"] = allowed_tools

        try:
            settings_file = self.plugins_dir / "settings.json"
            with open(settings_file, "w", encoding="utf-8") as f:
                json.dump(settings_json, f, indent=2)
            logger.info(
                f"Regenerated plugins/settings.json with {len(active_plugins)} active plugins"
            )
        except Exception as e:
            logger.error(f"Failed to write plugins/settings.json: {e}")

    def get_plugins_settings(self) -> dict:
        """
        Get current plugins settings from settings.json.

        Returns:
            Dictionary with active_plugins list, or empty structure if not found
        """
        settings_file = self.plugins_dir / "settings.json"

        if not settings_file.exists():
            logger.debug(f"plugins/settings.json not found at {settings_file}")
            return {"active_plugins": []}

        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                logger.info(f"Read plugins/settings.json ({len(settings.get('active_plugins', []))} plugins)")
                return settings
        except Exception as e:
            logger.error(f"Failed to read plugins/settings.json: {e}")
            return {"active_plugins": []}

    def get_active_plugins_for_mode(self, mode: str) -> list[dict]:
        """
        Get active plugins filtered by mode.

        Args:
            mode: "conversation" or "building"

        Returns:
            List of plugin dicts that are active for the specified mode
        """
        settings = self.get_plugins_settings()
        active_plugins = settings.get("active_plugins", [])

        # Self-protect filter: never hand the SDK a plugin whose path is missing
        # on disk, even if settings.json is stale. Belt-and-suspenders on top of
        # the install routine already excluding failed plugins.
        def _path_present(p: dict) -> bool:
            path = p.get("path")
            if not path:
                return False
            try:
                if not Path(path).exists():
                    logger.warning(
                        "Excluding plugin %s/%s — path missing on disk: %s",
                        p.get("marketplace_name"), p.get("plugin_name"), path,
                    )
                    return False
            except OSError:
                return False
            return True

        active_plugins = [p for p in active_plugins if _path_present(p)]

        if mode == "conversation":
            return [p for p in active_plugins if p.get("conversation_mode", False)]
        elif mode == "building":
            return [p for p in active_plugins if p.get("building_mode", False)]
        else:
            # If mode is not specified, return all active plugins
            logger.warning(f"Unknown mode '{mode}', returning all active plugins")
            return active_plugins

    def get_allowed_tools(self) -> list[str]:
        """
        Get user-approved allowed tools from settings.json.

        These tools are pre-authorized by the user and should be merged with
        the pre-allowed tools list when initializing SDK sessions.

        Returns:
            List of tool names approved by the user
        """
        settings = self.get_plugins_settings()
        return settings.get("allowed_tools", [])

    # =========================================================================
    # OpenCode plugin artifacts
    # =========================================================================

    # Plugin capabilities OpenCode has no equivalent for (yet). Detected and
    # reported as "unsupported under OpenCode" rather than silently dropped.
    # Tracked as a documented fast-follow (skills/agents/hooks parity).
    _OPENCODE_UNSUPPORTED_DIRS = ("skills", "agents", "hooks")

    def get_opencode_plugin_artifacts(self, mode: str) -> dict:
        """Collect OpenCode-consumable artifacts from active plugins for a mode.

        For each active plugin (files-present, enabled for ``mode``) this:
          - registers MCP servers actually declared in the plugin's
            ``.mcp.json`` (root) or ``.claude-plugin/plugin.json`` (``mcpServers``)
            — NOT a python3 wrapper of the plugin dir;
          - lists the plugin's ``commands/*.md`` files (to copy into OpenCode's
            per-mode command dir);
          - detects capabilities OpenCode can't map (skills / agents / hooks) and
            reports them as ``unsupported`` (non-blocking) instead of dropping.

        Returns:
            ``{"mcp_servers": {name: cfg}, "command_files": [Path...],
               "unsupported": [{plugin_name, marketplace_name, capability,
                                message}...]}``.
        """
        mcp_servers: dict = {}
        command_files: list[Path] = []
        unsupported: list[dict] = []

        try:
            active_plugins = self.get_active_plugins_for_mode(mode)
        except Exception as e:
            logger.warning(f"Could not enumerate active plugins for {mode}: {e}")
            return {"mcp_servers": {}, "command_files": [], "unsupported": []}

        for plugin in active_plugins:
            path = plugin.get("path") or ""
            if not path:
                continue
            plugin_dir = Path(path)
            if not plugin_dir.exists() or not plugin_dir.is_dir():
                continue

            marketplace_name = plugin.get("marketplace_name") or ""
            plugin_name = plugin.get("plugin_name") or plugin_dir.name
            # Namespaced, filesystem-safe label for collision-free server keys.
            safe_label = re.sub(r"[^A-Za-z0-9_]", "_", f"{marketplace_name}_{plugin_name}").strip("_")

            # 1) MCP servers declared by the plugin.
            try:
                declared = self._read_plugin_mcp_servers(plugin_dir)
                for server_name, cfg in declared.items():
                    safe_server = re.sub(r"[^A-Za-z0-9_]", "_", server_name).strip("_") or "server"
                    key = f"plugin_{safe_label}_{safe_server}"
                    mcp_servers[key] = cfg
            except Exception as e:
                logger.warning(
                    f"Failed to read MCP servers for plugin {plugin_name}: {e}"
                )

            # 2) Command markdown files.
            commands_dir = plugin_dir / "commands"
            if commands_dir.exists() and commands_dir.is_dir():
                for md in sorted(commands_dir.glob("*.md")):
                    if md.is_file():
                        command_files.append(md)

            # 3) Unsupported capabilities → report, don't drop.
            for cap in self._OPENCODE_UNSUPPORTED_DIRS:
                cap_dir = plugin_dir / cap
                if cap_dir.exists() and cap_dir.is_dir() and any(cap_dir.iterdir()):
                    unsupported.append({
                        "plugin_name": plugin_name,
                        "marketplace_name": marketplace_name,
                        "capability": cap,
                        "message": (
                            f"Plugin '{marketplace_name}/{plugin_name}' provides "
                            f"'{cap}', which is not yet supported under OpenCode "
                            f"(supported on Claude Code). It was skipped."
                        ),
                    })

        return {
            "mcp_servers": mcp_servers,
            "command_files": command_files,
            "unsupported": unsupported,
        }

    def _read_plugin_mcp_servers(self, plugin_dir: Path) -> dict:
        """Parse a plugin's declared MCP servers into OpenCode mcp-config entries.

        Sources (first match wins per server name):
          - ``<plugin>/.mcp.json`` → ``{"mcpServers": {name: {command, args, env, url}}}``
          - ``<plugin>/.claude-plugin/plugin.json`` → ``mcpServers`` key

        Translates the Claude/standard MCP shape into OpenCode's:
          - stdio → ``{"type": "local", "command": [cmd, *args], "environment": env, "enabled": True}``
          - remote (``url``) → ``{"type": "remote", "url": url, "enabled": True}``

        Only entries with a usable ``command`` or ``url`` are emitted; malformed
        entries are skipped (logged by the caller).
        """
        raw_servers: dict = {}

        mcp_json = plugin_dir / ".mcp.json"
        if mcp_json.exists() and mcp_json.is_file():
            try:
                data = json.loads(mcp_json.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    servers = data.get("mcpServers")
                    if isinstance(servers, dict):
                        raw_servers.update(servers)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"Invalid .mcp.json in {plugin_dir.name}: {e}")

        plugin_json = plugin_dir / ".claude-plugin" / "plugin.json"
        if plugin_json.exists() and plugin_json.is_file():
            try:
                data = json.loads(plugin_json.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    servers = data.get("mcpServers")
                    if isinstance(servers, dict):
                        for name, cfg in servers.items():
                            raw_servers.setdefault(name, cfg)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"Invalid plugin.json in {plugin_dir.name}: {e}")

        result: dict = {}
        for name, cfg in raw_servers.items():
            if not isinstance(cfg, dict):
                continue
            translated = self._translate_mcp_server(cfg)
            if translated is not None:
                result[str(name)] = translated
        return result

    @staticmethod
    def _translate_mcp_server(cfg: dict) -> Optional[dict]:
        """Translate one standard MCP server entry into OpenCode's mcp shape.

        Returns None for entries with neither a runnable command nor a url.
        """
        url = cfg.get("url")
        if isinstance(url, str) and url:
            entry = {"type": "remote", "url": url, "enabled": True}
            headers = cfg.get("headers")
            if isinstance(headers, dict) and headers:
                entry["headers"] = headers
            return entry

        command = cfg.get("command")
        if isinstance(command, str) and command:
            args = cfg.get("args")
            cmd_list = [command]
            if isinstance(args, list):
                cmd_list.extend(str(a) for a in args)
            entry = {"type": "local", "command": cmd_list, "enabled": True}
            env = cfg.get("env") or cfg.get("environment")
            if isinstance(env, dict) and env:
                entry["environment"] = {str(k): str(v) for k, v in env.items()}
            return entry

        return None

    # =========================================================================
    # MCP-provider servers (user_mcp.json) — credential-derived remote MCP
    # servers injected into the SDK runtime config per mode (RD-5).
    # =========================================================================

    # Filename of the persisted per-mode MCP-provider manifest baseline.
    _MCP_MANIFEST_FILENAME = "user_mcp.json"

    def set_mcp_servers(self, manifest: dict) -> dict:
        """Persist the per-mode MCP-provider manifest as the baseline.

        The backend pushes ``{"conversation": [entry...], "building": [...]}``
        where each entry is ``{key, url, transport, headers}``. We write it to
        ``mcp/user_mcp.json`` (0o600 — entries may carry a bearer token) so the
        SDK adapters can read the matching mode's servers at session start
        without a full config regeneration.

        The whole manifest is overwritten on every push (declarative SSOT,
        mirroring the plugin manifest): a disconnected provider simply drops out
        of the next push and disappears. Returns per-mode counts.
        """
        self.mcp_dir.mkdir(parents=True, exist_ok=True)
        conversation = manifest.get("conversation") or []
        building = manifest.get("building") or []

        normalised = {
            "conversation": [self._normalise_mcp_entry(e) for e in conversation],
            "building": [self._normalise_mcp_entry(e) for e in building],
        }
        # Drop entries that failed normalisation (no url / no key).
        normalised["conversation"] = [e for e in normalised["conversation"] if e]
        normalised["building"] = [e for e in normalised["building"] if e]

        manifest_file = self.mcp_dir / self._MCP_MANIFEST_FILENAME
        try:
            with open(manifest_file, "w", encoding="utf-8") as f:
                json.dump(normalised, f, indent=2)
            try:
                os.chmod(manifest_file, 0o600)
            except OSError:
                pass
            logger.info(
                "Wrote %s (%d conversation, %d building MCP provider server(s))",
                self._MCP_MANIFEST_FILENAME,
                len(normalised["conversation"]),
                len(normalised["building"]),
            )
        except OSError as e:
            logger.error(f"Failed to write {self._MCP_MANIFEST_FILENAME}: {e}")
            raise

        return {
            "conversation_count": len(normalised["conversation"]),
            "building_count": len(normalised["building"]),
        }

    @staticmethod
    def _normalise_mcp_entry(entry: dict) -> Optional[dict]:
        """Validate + normalise one manifest entry; None if unusable."""
        if not isinstance(entry, dict):
            return None
        key = entry.get("key")
        url = entry.get("url")
        if not isinstance(key, str) or not key:
            return None
        if not isinstance(url, str) or not url:
            return None
        transport = entry.get("transport") or "streamable-http"
        headers = entry.get("headers")
        if not isinstance(headers, dict):
            headers = {}
        return {
            "key": key,
            "url": url,
            "transport": str(transport),
            "headers": {str(k): str(v) for k, v in headers.items()},
        }

    def _read_user_mcp_manifest(self) -> dict:
        """Read the persisted per-mode MCP-provider manifest (empty if absent)."""
        manifest_file = self.mcp_dir / self._MCP_MANIFEST_FILENAME
        if not manifest_file.exists():
            return {"conversation": [], "building": []}
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to read {self._MCP_MANIFEST_FILENAME}: {e}")
            return {"conversation": [], "building": []}
        if not isinstance(data, dict):
            return {"conversation": [], "building": []}
        return {
            "conversation": data.get("conversation") or [],
            "building": data.get("building") or [],
        }

    def get_user_mcp_servers_for_mode(self, mode: str, engine: str) -> dict:
        """Build the engine-specific MCP server config for ``mode``.

        Reads the persisted ``user_mcp.json`` baseline and translates each entry
        for the requested ``mode`` into the target engine's MCP server shape:

          - ``engine="opencode"`` →
            ``{"type": "remote", "url", "headers", "enabled": True}``
          - ``engine="claude_code"`` →
            ``{"type": "http"|"sse", "url", "headers"}`` (Claude SDK
            ``McpHttpServerConfig`` / ``McpSSEServerConfig``)

        Keyed by the credential-namespaced ``cinna_mcp_<id>`` so it never
        collides with the knowledge / agent_task bridges or plugin servers.
        """
        if mode not in ("conversation", "building"):
            return {}
        manifest = self._read_user_mcp_manifest()
        entries = manifest.get(mode) or []

        servers: dict = {}
        for raw in entries:
            entry = self._normalise_mcp_entry(raw)
            if entry is None:
                continue
            translated = self._translate_user_mcp_for_engine(entry, engine)
            if translated is not None:
                servers[entry["key"]] = translated
        return servers

    @staticmethod
    def _translate_user_mcp_for_engine(entry: dict, engine: str) -> Optional[dict]:
        """Translate one normalised manifest entry into an engine MCP config."""
        url = entry["url"]
        transport = entry.get("transport") or "streamable-http"
        headers = entry.get("headers") or {}

        if engine == "opencode":
            cfg: dict = {"type": "remote", "url": url, "enabled": True}
            if headers:
                cfg["headers"] = dict(headers)
            return cfg

        if engine == "claude_code":
            # Claude SDK distinguishes "sse" from "http" (streamable-http).
            cfg_type = "sse" if transport == "sse" else "http"
            cfg = {"type": cfg_type, "url": url}
            if headers:
                cfg["headers"] = dict(headers)
            return cfg

        return None

    # =========================================================================
    # Workspace Tarball Upload & Manifest
    # =========================================================================

    def extract_workspace_tarball(self, tarball_bytes: bytes) -> int:
        """
        Extract a gzipped tar archive into the workspace directory.

        Args:
            tarball_bytes: Raw bytes of a .tar.gz archive

        Returns:
            Number of regular files extracted

        Raises:
            ValueError: If archive contains path traversal entries
            IOError: If extraction fails
        """
        workspace_resolved = str(self.workspace_dir.resolve())

        try:
            with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tf:
                members = tf.getmembers()

                # Security: validate all member paths before extracting anything
                for member in members:
                    name = member.name
                    # Reject absolute paths
                    if name.startswith("/"):
                        raise ValueError(f"Archive contains absolute path: {name}")
                    # Reject .. components
                    if ".." in Path(name).parts:
                        raise ValueError(f"Archive contains path traversal entry: {name}")
                    # Resolve and verify within workspace
                    resolved = str((self.workspace_dir / name).resolve())
                    if not resolved.startswith(workspace_resolved):
                        raise ValueError(f"Path traversal detected in archive: {name}")

                tf.extractall(self.workspace_dir)
                file_count = sum(1 for m in members if m.isfile())
                logger.info(f"Extracted workspace tarball: {file_count} files to {self.workspace_dir}")
                return file_count

        except ValueError:
            raise
        except tarfile.TarError as e:
            logger.error(f"Failed to extract tarball: {e}")
            raise IOError(f"Failed to extract tarball: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error extracting tarball: {e}")
            raise IOError(f"Failed to extract tarball: {str(e)}")

    def get_workspace_manifest(self) -> dict:
        """
        Walk the workspace directory and compute SHA-256 for each file.

        Returns:
            Dict mapping relative path strings to {"sha256": str, "size": int, "mtime": float}

        Raises:
            IOError: If workspace directory is not accessible
        """
        try:
            manifest = {}

            for file_path in self.workspace_dir.rglob("*"):
                # Skip directories and symlinks
                if not file_path.is_file() or file_path.is_symlink():
                    continue

                # Skip hidden files and directories (any path component starting with ".")
                relative = file_path.relative_to(self.workspace_dir)
                if any(part.startswith(".") for part in relative.parts):
                    continue

                # Compute SHA-256
                sha256 = hashlib.sha256()
                try:
                    with open(file_path, "rb") as f:
                        while chunk := f.read(65536):
                            sha256.update(chunk)
                except OSError as e:
                    logger.warning(f"Skipping unreadable file {file_path}: {e}")
                    continue

                stat = file_path.stat()
                manifest[str(relative)] = {
                    "sha256": sha256.hexdigest(),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }

            logger.info(f"Generated workspace manifest: {len(manifest)} files")
            return manifest

        except Exception as e:
            logger.error(f"Failed to generate workspace manifest: {e}")
            raise IOError(f"Failed to generate workspace manifest: {str(e)}")
