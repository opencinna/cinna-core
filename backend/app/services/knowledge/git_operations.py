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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple
from contextlib import contextmanager

import git
from git import Repo, GitCommandError

from app.core.config import settings
from app.services.common.egress_guard import (
    EgressBlockedError,
    assert_host_allowed,
    assert_url_allowed,
)

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


class GitNonFastForwardError(GitOperationError):
    """Raised when a push is rejected because the remote advanced (non-ff)."""
    pass


# Host extractor for schemeless SSH git URLs (git@host:owner/repo.git).
_SSH_HOST_PATTERN = re.compile(r'git@([^:]+):')


def assert_git_url_allowed(git_url: str) -> None:
    """
    SSRF / egress chokepoint for every outbound git network call.

    Runs the egress guard on the *resolved host* of ``git_url`` (HTTPS or SSH).
    ``assert_url_allowed`` only accepts ``http(s)`` URLs, so SSH URLs (which
    carry no scheme) are handled by extracting the host and calling
    ``assert_host_allowed`` directly. Honors ``GIT_SOURCE_ALLOW_PRIVATE_HOSTS``.

    Raises :class:`EgressBlockedError` when the target is a private / loopback /
    link-local address. Must be called before any clone / pull / push /
    ls-remote network operation.
    """
    allow_private = settings.GIT_SOURCE_ALLOW_PRIVATE_HOSTS
    if git_url.startswith('git@'):
        match = _SSH_HOST_PATTERN.match(git_url)
        if not match:
            raise EgressBlockedError(f"Could not parse host from SSH URL: {git_url}")
        assert_host_allowed(match.group(1), allow_private_hosts=allow_private)
    else:
        assert_url_allowed(git_url, allow_private_hosts=allow_private)


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


def _split_host_path(git_url: str) -> Tuple[Optional[str], Optional[str]]:
    """Split a git URL into ``(host, owner/repo)``; ``(None, None)`` if unparseable.

    Handles schemeless SSH (``git@host:owner/repo.git``), ``ssh://`` SSH
    (``ssh://[user@]host[:port]/owner/repo.git``) and HTTP(S)
    (``https://host/owner/repo.git``) shapes and strips a trailing ``.git``. The
    path keeps every segment after the host (so GitLab subgroups survive), it is
    just the ``owner/repo`` portion of a typical GitHub URL.
    """
    git_url = git_url.strip()
    ssh_match = re.match(r'^git@([^:]+):(.+?)(?:\.git)?/?$', git_url)
    if ssh_match:
        return ssh_match.group(1), ssh_match.group(2)
    # ssh://[user@]host[:port]/owner/repo(.git) — strip the optional user@ and
    # :port so the host matches the provider registry the same as git@ / https.
    ssh_scheme_match = re.match(
        r'^ssh://(?:[^@/]+@)?([^:/]+)(?::\d+)?/(.+?)(?:\.git)?/?$', git_url
    )
    if ssh_scheme_match:
        return ssh_scheme_match.group(1), ssh_scheme_match.group(2)
    https_match = re.match(r'^https?://([^/]+)/(.+?)(?:\.git)?/?$', git_url)
    if https_match:
        return https_match.group(1), https_match.group(2)
    return None, None


# ── Git hosting providers (web-URL layouts) ──────────────────────────────────
#
# Each provider knows the host(s) it serves and how it lays out its browser
# pages relative to the repo root ``https://<host>/<owner>/<repo>``. The shared
# machinery (SSH/HTTPS parsing, host matching, graceful ``None``) lives in
# ``_resolve_web_provider`` so adding a provider is a single tuple entry — no
# changes to the public ``build_web_*`` functions or their callers.
#
# To add a provider (e.g. Bitbucket / GitLab) append a ``GitWebProvider`` below
# with its host(s) and path builders; the commented examples show their layouts.


@dataclass(frozen=True)
class GitWebProvider:
    """Browser-URL layout for one git hosting provider.

    Each ``*_path`` callable returns the path that follows the repo root
    ``https://<host>/<owner>/<repo>``, e.g. ``/commits/main/sub``,
    ``/commit/<sha>``, ``/tree/main/sub``.
    """

    name: str
    hosts: frozenset[str]
    history_path: Callable[[str, Optional[str]], str]
    commit_path: Callable[[str], str]
    tree_path: Callable[[str, Optional[str]], str]


def _ref_subdir_path(prefix: str, ref: str, subdir: Optional[str]) -> str:
    """Build ``<prefix>/<ref>[/<subdir>]`` (the GitHub/GitLab tree+history shape)."""
    path = f"{prefix}/{ref or 'main'}"
    if subdir:
        path = f"{path}/{subdir.strip('/')}"
    return path


_WEB_PROVIDERS: Tuple[GitWebProvider, ...] = (
    GitWebProvider(
        name="github",
        hosts=frozenset({"github.com"}),
        # https://github.com/<owner>/<repo>/commits/<ref>[/<subdir>]
        history_path=lambda ref, subdir: _ref_subdir_path("/commits", ref, subdir),
        # https://github.com/<owner>/<repo>/commit/<sha>  (singular)
        commit_path=lambda sha: f"/commit/{sha}",
        # https://github.com/<owner>/<repo>/tree/<ref>[/<subdir>]
        tree_path=lambda ref, subdir: _ref_subdir_path("/tree", ref, subdir),
    ),
    # Future providers — add an entry, nothing else changes. For example:
    #
    # GitWebProvider(
    #     name="bitbucket",
    #     hosts=frozenset({"bitbucket.org"}),
    #     # Bitbucket scopes history by branch, not path:
    #     history_path=lambda ref, subdir: f"/commits/branch/{ref or 'main'}",
    #     commit_path=lambda sha: f"/commits/{sha}",
    #     # Bitbucket browses source under /src/<ref>[/<subdir>]:
    #     tree_path=lambda ref, subdir: _ref_subdir_path("/src", ref, subdir),
    # ),
    # GitWebProvider(
    #     name="gitlab",
    #     hosts=frozenset({"gitlab.com"}),
    #     history_path=lambda ref, subdir: _ref_subdir_path("/-/commits", ref, subdir),
    #     commit_path=lambda sha: f"/-/commit/{sha}",
    #     tree_path=lambda ref, subdir: _ref_subdir_path("/-/tree", ref, subdir),
    # ),
)


def _resolve_web_provider(
    repo_url: str,
) -> Tuple[Optional[GitWebProvider], Optional[str]]:
    """Resolve ``(provider, repo_web_base)`` for ``repo_url``; ``(None, None)``.

    The single place that decides whether web-URL generation is available: it
    parses the URL (SSH or HTTPS), matches the host against the registry, and
    returns the provider plus the ``https://<host>/<owner>/<repo>`` root. Every
    ``build_web_*`` function goes through here, so they always agree on support.
    """
    host, path = _split_host_path(repo_url)
    if host is None or path is None:
        return None, None
    host_l = host.lower()
    for provider in _WEB_PROVIDERS:
        if host_l in provider.hosts:
            return provider, f"https://{host_l}/{path}"
    return None, None


def build_web_history_url(
    repo_url: str,
    ref: str = "main",
    subdir: Optional[str] = None,
) -> Optional[str]:
    """Build a browser URL for a repo's commit history, scoped to ``subdir``.

    Provider-aware: returns a URL only for hosts in the provider registry
    (**GitHub** today). Returns ``None`` for any other host (self-hosted git,
    GitLab, Bitbucket, …) so callers can hide a "View history" link when no URL
    can be generated. Works for both SSH and HTTPS ``repo_url`` shapes.

    GitHub layout: ``https://github.com/<owner>/<repo>/commits/<ref>[/<subdir>]``
    e.g. ``…/commits/main/agents/localhost/hello-testing``.
    """
    provider, base = _resolve_web_provider(repo_url)
    if provider is None or base is None:
        return None
    return base + provider.history_path(ref, subdir)


def build_web_commit_url(repo_url: str, sha: str) -> Optional[str]:
    """Build a browser URL for a single commit, or ``None`` if unsupported.

    Provider-aware sibling of :func:`build_web_history_url`. GitHub layout:
    ``https://github.com/<owner>/<repo>/commit/<sha>`` (note: singular
    ``/commit/``, the full SHA). Returns ``None`` for hosts outside the provider
    registry or when ``sha`` is empty.
    """
    if not sha:
        return None
    provider, base = _resolve_web_provider(repo_url)
    if provider is None or base is None:
        return None
    return base + provider.commit_path(sha)


def build_web_tree_url(
    repo_url: str,
    ref: str = "main",
    subdir: Optional[str] = None,
) -> Optional[str]:
    """Build a browser URL for browsing the repo tree at ``ref`` / ``subdir``.

    Provider-aware sibling of :func:`build_web_history_url`. GitHub layout:
    ``https://github.com/<owner>/<repo>/tree/<ref>[/<subdir>]`` e.g.
    ``…/tree/main/agents/localhost/hello-testing``. Returns ``None`` for hosts
    outside the provider registry.
    """
    provider, base = _resolve_web_provider(repo_url)
    if provider is None or base is None:
        return None
    return base + provider.tree_path(ref, subdir)


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

        # SSRF / egress guard on the resolved host before any network call.
        assert_git_url_allowed(git_url)

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

    except EgressBlockedError as e:
        logger.warning(f"Egress guard blocked repository access: {e}")
        return False, str(e)

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
    depth: Optional[int] = 1
) -> Repo:
    """
    Clone a Git repository to the specified destination.

    Args:
        git_url: Git repository URL
        destination: Local path to clone to
        branch: Branch to checkout
        ssh_key_path: Optional path to SSH key for private repositories
        depth: Clone depth (1 = shallow clone). Pass ``None`` for a full-history
            clone — required by ``fast_forward_push`` whose merge-base ancestry
            check needs the ref history (a shallow clone has no merge bases).

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

        # SSRF / egress guard on the resolved host before any network call.
        assert_git_url_allowed(git_url)

        # Set up SSH command for SSH URLs (to disable strict host key checking)
        env = os.environ.copy()
        if git_url.startswith('git@'):
            env['GIT_SSH_COMMAND'] = create_git_ssh_command(ssh_key_path)

        logger.info(f"Cloning repository {git_url} to {destination}")

        # Clone with specified depth (shallow clone by default). ``depth=None``
        # omits the flag entirely for a full-history clone.
        clone_kwargs = {"branch": branch, "env": env}
        if depth is not None:
            clone_kwargs["depth"] = depth
        repo = Repo.clone_from(
            git_url,
            destination,
            **clone_kwargs,
        )

        logger.info(f"Successfully cloned repository to {destination}")
        return repo

    except EgressBlockedError:
        # Egress guard runs before any clone, so nothing to clean up. Let the
        # typed error propagate to the service layer (maps to 400).
        raise

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


def init_repo_with_remote(
    *,
    workdir: str,
    repo_url: str,
    ref: str = "main",
    ssh_key_path: Optional[str] = None,
) -> Repo:
    """
    ``git init`` a fresh working tree, point ``HEAD`` at branch ``ref``, and add
    ``origin``.

    For the empty-remote / absent-ref bootstrap case where there is nothing to
    clone (the first export push to a brand-new repo or branch). The caller
    writes the tree, :func:`commit_all`, then :func:`fast_forward_push` — which
    creates ``ref`` on the remote (its first-push branch treats an absent remote
    ref as ancestor-OK).

    The unborn ``HEAD`` is pointed at ``refs/heads/<ref>`` via ``symbolic-ref``
    (version-safe, unlike ``--initial-branch``) so the first commit lands on the
    branch ``fast_forward_push`` will push.

    Args:
        workdir: Directory to initialize as a git working tree (must exist).
        repo_url: Git repository URL (HTTPS or SSH).
        ref: Branch name to create / push.
        ssh_key_path: Optional path to an SSH private key for private repos.

    Returns:
        A GitPython Repo with no commits yet and ``origin`` configured.

    Raises:
        EgressBlockedError: If the resolved host is blocked.
        GitOperationError: On any git failure.
    """
    # URL conversion mirrors clone_repository (SSH if key present, else HTTPS).
    if ssh_key_path:
        target_url = convert_https_to_ssh_url(repo_url)
    else:
        target_url = convert_ssh_to_https_url(repo_url)

    # SSRF / egress guard on the resolved host before the remote is added.
    assert_git_url_allowed(target_url)

    try:
        repo = Repo.init(workdir)
        # Point the unborn HEAD at the desired branch so the first commit lands
        # on ``ref`` (works regardless of the host git's default branch name).
        repo.git.symbolic_ref("HEAD", f"refs/heads/{ref}")
        repo.create_remote("origin", target_url)
        logger.info("init_repo_with_remote: initialized %s (ref %s)", workdir, ref)
        return repo
    except GitCommandError as e:
        raise GitOperationError(
            f"Failed to initialize repository: {str(e)}"
        ) from e
    except Exception as e:
        raise GitOperationError(
            f"Unexpected error initializing repository: {str(e)}"
        ) from e


def git_log_subdir(
    *,
    repo_url: str,
    ref: str = "main",
    subdir: Optional[str] = None,
    ssh_key_path: Optional[str] = None,
    max_count: int = 50,
) -> list[dict]:
    """
    Return up to ``max_count`` commits touching ``subdir``, newest first.

    Each dict carries ``sha``, ``short_sha``, ``author_name``, ``author_email``,
    ``date`` (ISO-8601), and ``message``. Uses a bounded shallow clone
    (``depth=max_count``), so history older than ``max_count`` commits is not
    visible — acceptable for a UI list. Egress-guarded (the clone runs the
    guard); the temp clone is removed in ``finally``.

    Args:
        repo_url: Git repository URL (HTTPS or SSH).
        ref: Branch or tag to read.
        subdir: Path within the repo; ``None`` = repo root (all commits).
        ssh_key_path: Optional path to an SSH private key for private repos.
        max_count: Maximum number of commits to return / clone depth.

    Returns:
        A list of commit dicts (possibly empty), newest first.

    Raises:
        EgressBlockedError: If the resolved host is blocked.
        GitAuthenticationError / GitConnectionError / GitOperationError: On
        git transport / parse failures.
    """
    # Unit (field) / record separators that cannot appear in commit metadata,
    # so author names / messages with tabs or newlines split cleanly.
    unit = "\x1f"
    record = "\x1e"
    pretty = unit.join(["%H", "%h", "%an", "%ae", "%aI", "%s"]) + record

    with clone_repository_context(
        repo_url, branch=ref, ssh_key_path=ssh_key_path, depth=max_count
    ) as (repo_path, repo):
        try:
            log_args = ["--max-count", str(max_count), f"--pretty=format:{pretty}"]
            if subdir:
                out = repo.git.log(*log_args, "--", f"{subdir}/")
            else:
                out = repo.git.log(*log_args)
        except GitCommandError as e:
            raise GitOperationError(f"Git log failed: {str(e)}") from e

    commits: list[dict] = []
    for raw in out.split(record):
        raw = raw.strip("\n")
        if not raw:
            continue
        fields = raw.split(unit)
        if len(fields) < 6:
            continue
        sha, short_sha, author_name, author_email, date, message = fields[:6]
        commits.append(
            {
                "sha": sha,
                "short_sha": short_sha,
                "author_name": author_name,
                "author_email": author_email,
                "date": date,
                "message": message,
            }
        )
    return commits


def subdir_changed_between(
    *,
    repo_url: str,
    ref: str = "main",
    subdir: str,
    base_commit: str,
    ssh_key_path: Optional[str] = None,
) -> bool:
    """
    Return whether the tree at ``subdir`` differs between ``base_commit`` and the
    current tip of ``ref`` — i.e. whether any commit newer than ``base_commit``
    actually touched ``subdir``.

    This powers the subdir-scoped "update available" check: when several agents
    share one repo under different subdirs, a commit to *another* folder advances
    the remote HEAD but must not mark this agent as having an update. Comparing
    the *subdir tree object hash* at both commits answers exactly that — identical
    tree hashes mean the subdir is byte-for-byte unchanged regardless of how many
    unrelated commits landed in between (and is robust to rebases / force-pushes,
    unlike a ``base..HEAD`` commit walk).

    A bounded shallow clone of ``ref`` (``depth=1``) provides the tip; the
    ``base_commit`` object is fetched on its own (``depth=1``) so the comparison
    works no matter how far back ``base_commit`` sits in history. If
    ``base_commit`` cannot be fetched or either side's subdir tree cannot be
    resolved (server disallows fetch-by-SHA, history was rewritten, the subdir was
    added/removed), the result is indeterminate and we conservatively return
    ``True`` — never hide a real update (matches the legacy ``remote != synced``
    verdict).

    Egress-guarded (the clone and the follow-up fetch both run the guard); the
    temp clone is removed by the context manager.

    Args:
        repo_url: Git repository URL (HTTPS or SSH).
        ref: Branch or tag whose tip is compared against ``base_commit``.
        subdir: Path within the repo (leading/trailing slashes are tolerated).
        base_commit: The commit SHA to compare the subdir tree against
            (typically ``AgentGitSource.last_synced_commit``).
        ssh_key_path: Optional path to an SSH private key for private repos.

    Returns:
        ``True`` if the subdir tree differs (or the comparison is indeterminate),
        ``False`` if the subdir tree is identical at both commits.

    Raises:
        EgressBlockedError: If the resolved host is blocked.
        GitAuthenticationError / GitConnectionError / GitOperationError: On clone
        transport / parse failures.
    """
    subdir = subdir.strip("/")

    with clone_repository_context(
        repo_url, branch=ref, ssh_key_path=ssh_key_path, depth=1
    ) as (_repo_path, repo):
        # Rebuild the SSH env for the follow-up fetch. The clone already ran the
        # egress guard and rewrote origin to the converted (SSH/HTTPS) URL, so
        # read it back and re-assert the guard before the next network call.
        remote_url = repo.remotes.origin.url if repo.remotes else repo_url
        assert_git_url_allowed(remote_url)
        env = os.environ.copy()
        if remote_url.startswith("git@"):
            env["GIT_SSH_COMMAND"] = create_git_ssh_command(ssh_key_path)

        try:
            # Fetch just the base commit's snapshot (depth=1) so its subdir tree
            # is resolvable no matter how distant it is from the tip.
            repo.remotes.origin.fetch(base_commit, depth=1, env=env)
            tip_tree = repo.git.rev_parse(f"HEAD:{subdir}")
            base_tree = repo.git.rev_parse(f"{base_commit}:{subdir}")
        except GitCommandError as exc:
            logger.info(
                "subdir_changed_between: indeterminate subdir diff for %s@%s "
                "(subdir=%s, base=%s): %s — assuming changed",
                repo_url, ref, subdir, base_commit, exc,
            )
            return True

        return tip_tree != base_tree


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
    base_dir: Optional[str] = None,
    depth: Optional[int] = 1
):
    """
    Context manager for cloning a repository with automatic cleanup.

    Args:
        git_url: Git repository URL
        branch: Branch to checkout
        ssh_key_path: Optional path to SSH key
        base_dir: Base directory for temporary clone (default: system temp)
        depth: Clone depth (1 = shallow). Pass ``None`` for a full-history clone
            (required before ``fast_forward_push``).

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
            ssh_key_path=ssh_key_path,
            depth=depth,
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

        # SSRF / egress guard on the resolved host before any network call.
        assert_git_url_allowed(target_url)

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

    except EgressBlockedError:
        raise

    except Exception as e:
        logger.error(f"Error pulling repository: {e}")
        raise GitOperationError(f"Failed to pull repository: {str(e)}") from e


def ls_remote_head(
    git_url: str,
    ref: str = "main",
    ssh_key_path: Optional[str] = None,
) -> str:
    """
    Resolve the SHA a remote ``ref`` points at, without cloning.

    Used by the "update available" check (compare against
    ``AgentGitSource.last_synced_commit``) and as the fast-forward precheck on
    push. Tries the branch (``refs/heads/<ref>``) first, then the tag
    (``refs/tags/<ref>``), then the raw ref string.

    Args:
        git_url: Git repository URL (HTTPS or SSH).
        ref: Branch or tag name to resolve.
        ssh_key_path: Optional path to an SSH private key for private repos.

    Returns:
        The 40-char commit SHA the ref resolves to.

    Raises:
        EgressBlockedError: If the resolved host is blocked.
        GitOperationError: If the ref cannot be found or the remote errors.
    """
    # Convert URL based on authentication method (mirrors clone/verify).
    if ssh_key_path:
        git_url = convert_https_to_ssh_url(git_url)
    else:
        git_url = convert_ssh_to_https_url(git_url)

    # SSRF / egress guard on the resolved host before any network call.
    assert_git_url_allowed(git_url)

    env = os.environ.copy()
    if git_url.startswith('git@'):
        env['GIT_SSH_COMMAND'] = create_git_ssh_command(ssh_key_path)

    try:
        from git.cmd import Git
        g = Git()

        refs = ""
        for candidate in (f'refs/heads/{ref}', f'refs/tags/{ref}', ref):
            refs = g.ls_remote(git_url, candidate, env=env)
            if refs:
                break

        if not refs:
            raise GitOperationError(f"Ref '{ref}' not found in repository")

        # Output format: "<sha>\t<refname>\n...". For an annotated tag, git
        # emits two lines — the tag object and the dereferenced commit with a
        # "^{}" suffix:
        #     <tag-obj-sha>\trefs/tags/v1
        #     <commit-sha>\trefs/tags/v1^{}
        # last_synced_commit is always a commit SHA (via get_current_commit_hash),
        # so prefer the "^{}" dereferenced line when present to avoid spurious
        # "update available" / non-ff mismatches on tag refs.
        lines = refs.splitlines()
        deref = next(
            (ln for ln in lines if ln.split("\t", 1)[-1].endswith("^{}")), None
        )
        chosen = deref if deref is not None else lines[0]
        sha = chosen.split()[0]
        return sha

    except GitCommandError as e:
        error_msg = str(e)
        logger.error(f"Git ls-remote error: {error_msg}")

        if "could not read Username" in error_msg or "could not read Password" in error_msg:
            raise GitAuthenticationError(
                "Cannot authenticate with HTTPS URL. Use SSH URL format "
                "(git@host:owner/repo.git) with SSH keys."
            ) from e
        elif "Permission denied" in error_msg or "publickey" in error_msg:
            raise GitAuthenticationError(
                "Authentication failed. Check SSH key configuration."
            ) from e
        elif "Could not resolve host" in error_msg or "Could not read from remote" in error_msg:
            raise GitConnectionError(
                "Connection failed. Check the repository URL."
            ) from e
        else:
            raise GitOperationError(f"Git ls-remote failed: {error_msg}") from e


def commit_all(
    repo: Repo,
    message: str,
    author_name: str,
    author_email: str,
) -> str:
    """
    Stage every change in the working tree and commit it.

    No-op safe: if the staged tree is identical to ``HEAD`` (nothing changed),
    returns the current HEAD SHA without creating an empty commit.

    Args:
        repo: GitPython Repo object (a cloned working copy).
        message: Commit message.
        author_name: Commit author + committer name.
        author_email: Commit author + committer email.

    Returns:
        The SHA of the new commit, or the existing HEAD SHA if nothing changed.

    Raises:
        GitOperationError: On any git failure.
    """
    try:
        # Stage all changes including deletions and untracked files.
        repo.git.add(A=True)

        # Short-circuit when the index matches HEAD (nothing to commit). An
        # unborn HEAD (a freshly ``Repo.init``'d tree with no commits yet — the
        # empty-remote bootstrap path) has no commit to diff against, and
        # ``repo.head.commit`` would raise ``ValueError``; ``head.is_valid()`` is
        # False there, so we fall through and create the first commit instead.
        if repo.head.is_valid() and not repo.index.diff(repo.head.commit):
            logger.info("commit_all: working tree unchanged; skipping empty commit")
            return repo.head.commit.hexsha

        actor = git.Actor(author_name, author_email)
        commit = repo.index.commit(message, author=actor, committer=actor)
        logger.info(f"commit_all: created commit {commit.hexsha}")
        return commit.hexsha

    except GitCommandError as e:
        logger.error(f"Git commit error: {e}")
        raise GitOperationError(f"Failed to commit working tree: {str(e)}") from e


def fast_forward_push(
    repo: Repo,
    ref: str = "main",
    ssh_key_path: Optional[str] = None,
) -> None:
    """
    Push the local ``ref`` to origin, fast-forward only.

    Fetches the remote ``ref`` and asserts the local branch is ahead-of-or-equal
    (the remote commit must be an ancestor of, or equal to, the local commit).
    If the remote advanced past the local branch, raises
    :class:`GitNonFastForwardError` and does **not** push. The push itself is
    issued without ``--force``; a remote-side rejection is also surfaced as a
    non-ff error.

    Args:
        repo: GitPython Repo object (a non-shallow cloned working copy — ff
            requires the ref history).
        ref: Branch to push.
        ssh_key_path: Optional path to an SSH private key for private repos.

    Raises:
        EgressBlockedError: If the resolved host is blocked.
        GitNonFastForwardError: If the remote advanced (non-fast-forward).
        GitOperationError: On any other git failure.
    """
    try:
        remote_url = repo.remotes.origin.url if repo.remotes else ""

        # Convert URL based on authentication method (mirrors pull).
        if ssh_key_path:
            target_url = convert_https_to_ssh_url(remote_url)
        else:
            target_url = convert_ssh_to_https_url(remote_url)

        if target_url != remote_url and repo.remotes:
            repo.remotes.origin.set_url(target_url)

        # SSRF / egress guard on the resolved host before any network call.
        assert_git_url_allowed(target_url)

        env = os.environ.copy()
        if target_url.startswith('git@'):
            env['GIT_SSH_COMMAND'] = create_git_ssh_command(ssh_key_path)

        origin = repo.remotes.origin

        # Fetch the remote ref so we can verify fast-forward safety locally.
        # An absent remote ref (empty remote / brand-new branch) is the
        # first-push case: there is nothing to fast-forward against, so treat it
        # as ancestor-OK and let the push below create the branch.
        try:
            origin.fetch(ref, env=env)
        except GitCommandError as fetch_exc:
            if "couldn't find remote ref" not in str(fetch_exc).lower():
                raise
            logger.info(
                "fast_forward_push: remote ref %s absent — first push (creates branch)",
                ref,
            )

        local_commit = repo.head.commit
        try:
            remote_commit = origin.refs[ref].commit
        except (IndexError, KeyError):
            remote_commit = None

        if remote_commit is not None and remote_commit.hexsha != local_commit.hexsha:
            # ff-only: the remote commit must be an ancestor of the local commit.
            merge_bases = repo.merge_base(local_commit, remote_commit)
            if not merge_bases or merge_bases[0].hexsha != remote_commit.hexsha:
                raise GitNonFastForwardError(
                    "Remote has advanced; the local branch is not a "
                    "fast-forward. Pull first."
                )

        # Push ff-only (no --force). Inspect the result for rejections.
        push_infos = origin.push(refspec=f"{ref}:{ref}", env=env)
        for info in push_infos:
            if info.flags & (info.REJECTED | info.REMOTE_REJECTED | info.ERROR):
                raise GitNonFastForwardError(
                    f"Push rejected by remote: {info.summary.strip()}"
                )

        logger.info(f"fast_forward_push: pushed {ref} to {target_url}")

    except GitNonFastForwardError:
        raise

    except EgressBlockedError:
        raise

    except GitCommandError as e:
        error_msg = str(e)
        logger.error(f"Git push error: {error_msg}")

        if "non-fast-forward" in error_msg or "fetch first" in error_msg:
            raise GitNonFastForwardError(
                "Remote has advanced; push is not a fast-forward. Pull first."
            ) from e
        elif "could not read Username" in error_msg or "could not read Password" in error_msg:
            raise GitAuthenticationError(
                "Cannot authenticate with HTTPS URL. Use SSH URL format "
                "(git@host:owner/repo.git) with SSH keys."
            ) from e
        elif "Permission denied" in error_msg or "publickey" in error_msg:
            raise GitAuthenticationError(
                "Authentication failed. Check SSH key configuration."
            ) from e
        elif "Could not resolve host" in error_msg or "Could not read from remote" in error_msg:
            raise GitConnectionError(
                "Connection failed. Check the repository URL."
            ) from e
        else:
            raise GitOperationError(f"Git push failed: {error_msg}") from e
