"""
Git operations helper for knowledge source management.

This module provides utilities for:
- Cloning Git repositories with SSH key support
- Verifying repository access
- Managing temporary directories
- Configuring SSH for Git operations
"""

import os
import tempfile
import shutil
import logging
import re
from pathlib import Path
from typing import Optional, Tuple
from contextlib import contextmanager

import git
from git import Repo, GitCommandError

logger = logging.getLogger(__name__)


class GitOperationError(Exception):
    """Base exception for Git operation errors."""
    pass


class GitAuthenticationError(GitOperationError):
    """Exception for authentication failures."""
    pass


class GitConnectionError(GitOperationError):
    """Exception for connection failures."""
    pass


@contextmanager
def create_ssh_key_file(private_key: str, passphrase: Optional[str] = None):
    """
    Create a temporary SSH key file for Git operations.

    Args:
        private_key: SSH private key content
        passphrase: Optional passphrase for the key

    Yields:
        Path to the temporary SSH key file

    Note:
        The file is automatically cleaned up after use.
    """
    # Create a temporary file for the SSH key
    fd, key_path = tempfile.mkstemp(prefix='ssh_key_', suffix='.pem')

    try:
        # Write the private key with restrictive permissions (600)
        os.write(fd, private_key.encode())
        os.close(fd)
        os.chmod(key_path, 0o600)

        logger.debug(f"Created temporary SSH key file: {key_path}")

        yield key_path

    finally:
        # Clean up the temporary key file
        try:
            if os.path.exists(key_path):
                os.unlink(key_path)
                logger.debug(f"Removed temporary SSH key file: {key_path}")
        except Exception as e:
            logger.warning(f"Failed to remove temporary SSH key file {key_path}: {e}")


def create_git_ssh_command(ssh_key_path: Optional[str] = None) -> str:
    """
    Create a Git SSH command with optional SSH key.

    Args:
        ssh_key_path: Optional path to the SSH private key file

    Returns:
        SSH command string for Git
    """
    # Disable strict host key checking for ease of use
    # In production, you might want to configure known_hosts properly
    base_options = (
        '-o StrictHostKeyChecking=no '
        '-o UserKnownHostsFile=/dev/null '
        '-o LogLevel=ERROR'
    )
    if ssh_key_path:
        return f'ssh -i "{ssh_key_path}" {base_options}'
    return f'ssh {base_options}'


def convert_https_to_ssh_url(git_url: str) -> str:
    """
    Convert HTTPS Git URL to SSH format.

    This is necessary because SSH keys only work with SSH protocol URLs,
    not HTTPS URLs. When a user provides an HTTPS URL with SSH authentication,
    we need to convert it.

    Args:
        git_url: Git repository URL (HTTPS or SSH format)

    Returns:
        SSH format URL (git@host:owner/repo.git)

    Examples:
        https://github.com/owner/repo.git -> git@github.com:owner/repo.git
        https://github.com/owner/repo -> git@github.com:owner/repo.git
        git@github.com:owner/repo.git -> git@github.com:owner/repo.git (unchanged)
    """
    # If already SSH format, return as-is
    if git_url.startswith('git@'):
        return git_url

    # Parse HTTPS URL
    # Match patterns like: https://github.com/owner/repo or https://github.com/owner/repo.git
    https_pattern = r'https?://([^/]+)/(.+?)(?:\.git)?$'
    match = re.match(https_pattern, git_url)

    if match:
        host = match.group(1)
        path = match.group(2)
        # Convert to SSH format: git@host:path.git
        ssh_url = f"git@{host}:{path}.git"
        logger.info(f"Converted HTTPS URL to SSH: {git_url} -> {ssh_url}")
        return ssh_url

    # If no match, return original URL
    logger.warning(f"Could not convert URL to SSH format: {git_url}")
    return git_url


def convert_ssh_to_https_url(git_url: str) -> str:
    """
    Convert SSH Git URL to HTTPS format.

    This is useful for public repositories when no SSH key is provided,
    as HTTPS allows anonymous access to public repos.

    Args:
        git_url: Git repository URL (SSH or HTTPS format)

    Returns:
        HTTPS format URL (https://host/owner/repo.git)

    Examples:
        git@github.com:owner/repo.git -> https://github.com/owner/repo.git
        git@github.com:owner/repo -> https://github.com/owner/repo.git
        https://github.com/owner/repo.git -> https://github.com/owner/repo.git (unchanged)
    """
    # If already HTTPS format, return as-is
    if git_url.startswith('https://') or git_url.startswith('http://'):
        return git_url

    # Parse SSH URL
    # Match patterns like: git@github.com:owner/repo.git or git@github.com:owner/repo
    ssh_pattern = r'git@([^:]+):(.+?)(?:\.git)?$'
    match = re.match(ssh_pattern, git_url)

    if match:
        host = match.group(1)
        path = match.group(2)
        # Convert to HTTPS format: https://host/path.git
        https_url = f"https://{host}/{path}.git"
        logger.info(f"Converted SSH URL to HTTPS: {git_url} -> {https_url}")
        return https_url

    # If no match, return original URL
    logger.warning(f"Could not convert URL to HTTPS format: {git_url}")
    return git_url


def verify_repository_access(
    git_url: str,
    branch: str = "main",
    ssh_key_path: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Verify that a Git repository is accessible.

    Args:
        git_url: Git repository URL
        branch: Branch name to verify
        ssh_key_path: Optional path to SSH key for private repositories

    Returns:
        Tuple of (accessible: bool, message: str)
    """
    try:
        # Convert URL based on authentication method:
        # - If SSH key is provided: convert to SSH URL (for private repos)
        # - If no SSH key: convert to HTTPS URL (for public repos)
        if ssh_key_path:
            git_url = convert_https_to_ssh_url(git_url)
        else:
            git_url = convert_ssh_to_https_url(git_url)

        # Set up SSH command for SSH URLs (to disable strict host key checking)
        env = os.environ.copy()
        if git_url.startswith('git@'):
            env['GIT_SSH_COMMAND'] = create_git_ssh_command(ssh_key_path)

        # Use git ls-remote to check access without cloning
        logger.info(f"Verifying access to repository: {git_url}")

        # Run ls-remote to list remote references
        from git.cmd import Git
        g = Git()

        # Try to list remote refs for the specified branch
        refs = g.ls_remote(git_url, f'refs/heads/{branch}', env=env)

        if not refs:
            return False, f"Branch '{branch}' not found in repository"

        logger.info(f"Successfully verified access to {git_url}")
        return True, f"Repository accessible. Branch '{branch}' exists."

    except GitCommandError as e:
        error_msg = str(e)
        logger.error(f"Git command error: {error_msg}")

        # Provide user-friendly error messages
        if "could not read Username" in error_msg or "could not read Password" in error_msg:
            return False, "Cannot authenticate with HTTPS URL. Use SSH URL format (git@host:owner/repo.git) with SSH keys."
        elif "Could not resolve host" in error_msg or "Could not read from remote" in error_msg:
            return False, "Connection failed. Check the repository URL."
        elif "Permission denied" in error_msg or "publickey" in error_msg:
            return False, "Authentication failed. Check SSH key configuration."
        elif "Repository not found" in error_msg:
            return False, "Repository not found or access denied."
        else:
            return False, f"Git error: {error_msg}"

    except Exception as e:
        logger.error(f"Unexpected error verifying repository access: {e}")
        return False, f"Unexpected error: {str(e)}"


def clone_repository(
    git_url: str,
    destination: str,
    branch: str = "main",
    ssh_key_path: Optional[str] = None,
    depth: int = 1
) -> Repo:
    """
    Clone a Git repository to the specified destination.

    Args:
        git_url: Git repository URL
        destination: Local path to clone to
        branch: Branch to checkout
        ssh_key_path: Optional path to SSH key for private repositories
        depth: Clone depth (1 = shallow clone)

    Returns:
        GitPython Repo object

    Raises:
        GitAuthenticationError: If authentication fails
        GitConnectionError: If connection fails
        GitOperationError: For other Git errors
    """
    try:
        # Convert URL based on authentication method:
        # - If SSH key is provided: convert to SSH URL (for private repos)
        # - If no SSH key: convert to HTTPS URL (for public repos)
        if ssh_key_path:
            git_url = convert_https_to_ssh_url(git_url)
        else:
            git_url = convert_ssh_to_https_url(git_url)

        # Set up SSH command for SSH URLs (to disable strict host key checking)
        env = os.environ.copy()
        if git_url.startswith('git@'):
            env['GIT_SSH_COMMAND'] = create_git_ssh_command(ssh_key_path)

        logger.info(f"Cloning repository {git_url} to {destination}")

        # Clone with specified depth (shallow clone by default)
        repo = Repo.clone_from(
            git_url,
            destination,
            branch=branch,
            depth=depth,
            env=env
        )

        logger.info(f"Successfully cloned repository to {destination}")
        return repo

    except GitCommandError as e:
        error_msg = str(e)
        logger.error(f"Git clone error: {error_msg}")

        # Clean up partial clone
        if os.path.exists(destination):
            shutil.rmtree(destination, ignore_errors=True)

        # Categorize errors
        if "could not read Username" in error_msg or "could not read Password" in error_msg:
            raise GitAuthenticationError(
                "Cannot authenticate with HTTPS URL. Use SSH URL format (git@host:owner/repo.git) with SSH keys, "
                "or use HTTPS URL without SSH key authentication."
            ) from e
        elif "Permission denied" in error_msg or "publickey" in error_msg:
            raise GitAuthenticationError(
                "Authentication failed. Check SSH key configuration."
            ) from e
        elif "Could not resolve host" in error_msg or "Could not read from remote" in error_msg:
            raise GitConnectionError(
                "Connection failed. Check the repository URL."
            ) from e
        elif "Repository not found" in error_msg:
            raise GitOperationError(
                "Repository not found or access denied."
            ) from e
        else:
            raise GitOperationError(f"Git clone failed: {error_msg}") from e

    except Exception as e:
        logger.error(f"Unexpected error cloning repository: {e}")

        # Clean up partial clone
        if os.path.exists(destination):
            shutil.rmtree(destination, ignore_errors=True)

        raise GitOperationError(f"Unexpected error: {str(e)}") from e


def get_current_commit_hash(repo: Repo) -> str:
    """
    Get the current commit hash of the repository.

    Args:
        repo: GitPython Repo object

    Returns:
        Commit hash (SHA)
    """
    return repo.head.commit.hexsha


@contextmanager
def clone_repository_context(
    git_url: str,
    branch: str = "main",
    ssh_key_path: Optional[str] = None,
    base_dir: Optional[str] = None
):
    """
    Context manager for cloning a repository with automatic cleanup.

    Args:
        git_url: Git repository URL
        branch: Branch to checkout
        ssh_key_path: Optional path to SSH key
        base_dir: Base directory for temporary clone (default: system temp)

    Yields:
        Tuple of (repo_path: str, repo: Repo)

    Example:
        with clone_repository_context(url, "main", ssh_key) as (path, repo):
            # Work with the repository
            commit_hash = get_current_commit_hash(repo)
            # Repository is automatically cleaned up after
    """
    # Create temporary directory for clone
    temp_dir = tempfile.mkdtemp(prefix='git_clone_', dir=base_dir)
    repo_path = os.path.join(temp_dir, 'repo')

    try:
        # Clone the repository
        repo = clone_repository(
            git_url=git_url,
            destination=repo_path,
            branch=branch,
            ssh_key_path=ssh_key_path
        )

        yield repo_path, repo

    finally:
        # Clean up the temporary directory
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
                logger.debug(f"Removed temporary clone directory: {temp_dir}")
        except Exception as e:
            logger.warning(f"Failed to remove temporary clone directory {temp_dir}: {e}")


def pull_repository(
    repo_path: str,
    branch: str = "main",
    ssh_key_path: Optional[str] = None
) -> Repo:
    """
    Pull latest changes from a Git repository.

    Args:
        repo_path: Path to existing repository
        branch: Branch to pull
        ssh_key_path: Optional path to SSH key

    Returns:
        Updated GitPython Repo object

    Raises:
        GitOperationError: If pull fails
    """
    try:
        repo = Repo(repo_path)

        # Get remote URL
        remote_url = repo.remotes.origin.url if repo.remotes else ""

        # Convert URL based on authentication method:
        # - If SSH key is provided: use SSH URL (for private repos)
        # - If no SSH key: use HTTPS URL (for public repos)
        if ssh_key_path:
            target_url = convert_https_to_ssh_url(remote_url)
        else:
            target_url = convert_ssh_to_https_url(remote_url)

        # Update remote URL if it changed
        if target_url != remote_url and repo.remotes:
            repo.remotes.origin.set_url(target_url)
            logger.info(f"Updated remote URL from {remote_url} to {target_url}")

        # Set up SSH command for SSH URLs (to disable strict host key checking)
        env = os.environ.copy()
        if target_url.startswith('git@'):
            env['GIT_SSH_COMMAND'] = create_git_ssh_command(ssh_key_path)

        logger.info(f"Pulling latest changes for repository at {repo_path}")

        # Ensure we're on the correct branch
        if repo.active_branch.name != branch:
            repo.git.checkout(branch)

        # Pull latest changes
        origin = repo.remotes.origin
        origin.pull(env=env)

        logger.info(f"Successfully pulled latest changes")
        return repo

    except Exception as e:
        logger.error(f"Error pulling repository: {e}")
        raise GitOperationError(f"Failed to pull repository: {str(e)}") from e
