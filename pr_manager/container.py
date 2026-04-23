"""Container lifecycle management for PR/branch work environments."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .git import get_pristine_path, run_cmd

CONTAINER_PREFIX = "pr-manager"
IMAGE_NAME = "pr-manager-dev"
IDLE_TIMEOUT_MINUTES = 10
GC_AFTER_DAYS = 1

HOME = Path.home()
CREDENTIALS_DIR = HOME / ".cache" / "pr-manager" / "credentials"


def _container_name(repo: str, identifier: str) -> str:
    safe_repo = repo.replace("/", "-")
    safe_id = identifier.replace("/", "-")
    return f"{CONTAINER_PREFIX}-{safe_repo}-{safe_id}"


def _volume_name(repo: str, identifier: str) -> str:
    return f"{CONTAINER_PREFIX}-home-{repo.replace('/', '-')}-{identifier.replace('/', '-')}"


def container_name_for(repo: str, identifier: str) -> str:
    """Public access to the deterministic container name."""
    return _container_name(repo, identifier)


def _extract_claude_credentials() -> Path:
    """Extract Claude OAuth tokens and config for container use.

    Extracts:
    - claude-keychain.json: OAuth tokens from macOS Keychain
    - claude-config.json: ~/.claude/.claude.json (contains oauthAccount)

    Returns the credentials directory.
    """
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)

    # OAuth tokens from macOS Keychain.
    keychain_file = CREDENTIALS_DIR / "claude-keychain.json"
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, check=True,
        )
        keychain_file.write_text(result.stdout.strip())
        os.chmod(keychain_file, 0o600)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Config file with oauthAccount (critical for auth to work).
    config_file = CREDENTIALS_DIR / "claude-config.json"
    for source in [HOME / ".claude" / ".claude.json", HOME / ".claude.json"]:
        if source.exists():
            config_file.write_bytes(source.read_bytes())
            os.chmod(config_file, 0o600)
            break

    return CREDENTIALS_DIR


async def ensure_image_built(project_root: Path) -> None:
    rc, _, _ = await run_cmd(
        ["docker", "image", "inspect", IMAGE_NAME],
        check=False,
    )
    if rc != 0:
        # Build with check=True so failures are raised, not swallowed.
        await run_cmd(["docker", "build", "-t", IMAGE_NAME, str(project_root)])


async def is_container_running(repo: str, identifier: str) -> bool:
    name = _container_name(repo, identifier)
    rc, out, _ = await run_cmd(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        check=False,
    )
    return rc == 0 and out.strip() == "true"


def _ssh_url(repo: str) -> str:
    return f"git@github.com:{repo}.git"


async def start_container(
    repo: str,
    identifier: str,
    branch: str,
    create_branch: bool = False,
) -> str:
    """Start (or create) a container for a PR or branch. Returns the container name."""
    name = _container_name(repo, identifier)
    volume = _volume_name(repo, identifier)

    if await is_container_running(repo, identifier):
        return name

    # Check if container exists but is stopped.
    rc, _, _ = await run_cmd(["docker", "inspect", name], check=False)
    if rc == 0:
        start_rc, _, _ = await run_cmd(["docker", "start", name], check=False)
        if start_rc == 0:
            return name
        # Container exists but won't start (e.g. created with missing image).
        await run_cmd(["docker", "rm", "-f", name], check=False)

    pristine = get_pristine_path(repo)
    ssh_url = _ssh_url(repo)

    # Extract Claude OAuth tokens from macOS Keychain before container creation.
    creds_dir = _extract_claude_credentials()

    # Create and start a new container.
    cmd = [
        "docker", "run", "-d",
        "--name", name,
        "-v", f"{volume}:/home/dev",
        "--hostname", identifier.replace("/", "-"),
    ]

    # Mount pristine clone read-only for fast initial clone.
    if pristine.exists():
        cmd += ["-v", f"{pristine}:/mnt/pristine:ro"]

    # Mount extracted credentials (OAuth tokens + config from host).
    if creds_dir.exists():
        cmd += ["-v", f"{creds_dir}:/mnt/claude-credentials:ro"]

    # Mount host SSH keys read-only (entrypoint copies with correct perms).
    ssh_dir = HOME / ".ssh"
    if ssh_dir.exists():
        cmd += ["-v", f"{ssh_dir}:/mnt/host-ssh:ro"]

    # Mount gh CLI config read-only (entrypoint copies).
    gh_dir = HOME / ".config" / "gh"
    if gh_dir.exists():
        cmd += ["-v", f"{gh_dir}:/mnt/host-gh-config:ro"]

    # Mount gitconfig read-only.
    gitconfig = HOME / ".gitconfig"
    if gitconfig.exists():
        cmd += ["-v", f"{gitconfig}:/mnt/host-gitconfig:ro"]

    cmd += [
        IMAGE_NAME,
        "bash", "-c",
        _startup_script(ssh_url, branch, create_branch, pristine.exists()),
    ]

    await run_cmd(cmd)
    return name


def _startup_script(ssh_url: str, branch: str, create_branch: bool, has_pristine: bool) -> str:
    if has_pristine:
        clone_cmd = f"git clone /mnt/pristine ~/repo && cd ~/repo && git remote set-url origin {ssh_url} && git fetch origin --prune"
    else:
        clone_cmd = f"git clone {ssh_url} ~/repo && cd ~/repo"

    if create_branch:
        checkout = (
            f"cd ~/repo && "
            f"if git rev-parse --verify origin/main >/dev/null 2>&1; then "
            f"git checkout -b {branch} origin/main; else "
            f"git checkout -b {branch} origin/master; fi"
        )
    else:
        checkout = f"cd ~/repo && git checkout {branch}"

    # Always force origin back to SSH on every start so containers whose
    # ~/repo volume predates this guarantee get auto-healed.
    ensure_ssh = f"cd ~/repo && git remote set-url origin {ssh_url}"

    return (
        f"if [ ! -d ~/repo/.git ]; then {clone_cmd} && {checkout}; fi; "
        f"{ensure_ssh}; "
        "touch /tmp/.ready; "
        "exec sleep infinity"
    )


async def wait_for_ready(repo: str, identifier: str, timeout: float = 60) -> bool:
    """Wait until the container's startup script has finished."""
    import asyncio
    name = _container_name(repo, identifier)
    for _ in range(int(timeout * 2)):
        rc, _, _ = await run_cmd(
            ["docker", "exec", name, "test", "-f", "/tmp/.ready"],
            check=False,
        )
        if rc == 0:
            return True
        await asyncio.sleep(0.5)
    return False


# ── Exec helpers ─────────────────────────────────────────────────────────────

async def exec_in_container(name: str, cmd: list[str], workdir: str = "/home/dev/repo") -> tuple[int, str, str]:
    return await run_cmd([
        "docker", "exec", "-w", workdir, name,
    ] + cmd, check=False)


# ── Container git operations ────────────────────────────────────────────────

async def container_git_fetch(name: str) -> None:
    await exec_in_container(name, ["git", "fetch", "origin", "--prune"])


async def container_git_commits_behind_main(name: str, branch: str) -> int:
    await container_git_fetch(name)
    rc, out, _ = await exec_in_container(name, [
        "git", "rev-list", "--count", f"origin/{branch}..origin/main",
    ])
    if rc != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


async def container_git_get_current_sha(name: str) -> str:
    _, out, _ = await exec_in_container(name, ["git", "rev-parse", "HEAD"])
    return out.strip()


async def container_git_get_new_commits_since(name: str, old_sha: str) -> list[str]:
    _, out, _ = await exec_in_container(name, [
        "git", "log", "--format=%H", f"{old_sha}..HEAD",
    ])
    return [s.strip() for s in out.splitlines() if s.strip()]


async def container_git_push_force_with_lease(name: str, branch: str) -> bool:
    rc, _, _ = await exec_in_container(name, [
        "git", "push", "origin", branch, "--force-with-lease",
    ])
    return rc == 0


async def container_git_reattribute_and_push(name: str, branch: str) -> bool:
    rc, _, _ = await exec_in_container(name, [
        "git", "reset", "--hard", f"origin/{branch}",
    ])
    if rc != 0:
        return False
    rc, _, _ = await exec_in_container(name, [
        "git", "commit", "--amend", "--no-edit", "--reset-author",
    ])
    if rc != 0:
        return False
    return await container_git_push_force_with_lease(name, branch)


# ── Lifecycle ────────────────────────────────────────────────────────────────

async def stop_container(repo: str, identifier: str) -> None:
    name = _container_name(repo, identifier)
    await run_cmd(["docker", "stop", "-t", "5", name], check=False)


async def remove_container(repo: str, identifier: str, remove_volume: bool = False) -> None:
    name = _container_name(repo, identifier)
    volume = _volume_name(repo, identifier)
    await run_cmd(["docker", "rm", "-f", name], check=False)
    if remove_volume:
        await run_cmd(["docker", "volume", "rm", "-f", volume], check=False)


async def list_containers() -> list[dict]:
    rc, out, _ = await run_cmd([
        "docker", "ps", "-a",
        "--filter", f"name={CONTAINER_PREFIX}-",
        "--format", "{{json .}}",
    ], check=False)
    if rc != 0 or not out:
        return []
    return [json.loads(line) for line in out.splitlines() if line.strip()]


async def idle_shutdown_sweep() -> list[str]:
    """Stop containers that have no active processes beyond sleep."""
    stopped = []
    containers = await list_containers()
    for c in containers:
        if c.get("State") != "running":
            continue
        cname = c.get("Names", "")
        if not cname.startswith(CONTAINER_PREFIX + "-"):
            continue
        rc, out, _ = await run_cmd(
            ["docker", "top", cname, "-o", "pid,comm"],
            check=False,
        )
        if rc != 0:
            continue
        lines = out.strip().splitlines()[1:]
        active = [
            line for line in lines
            if not any(x in line.lower() for x in ("sleep", "entrypoint", "bash -c"))
        ]
        if not active:
            await run_cmd(["docker", "stop", "-t", "5", cname], check=False)
            stopped.append(cname)
    return stopped


async def gc_old_volumes(state_manager) -> list[str]:
    """Remove volumes for PRs/branches no longer tracked in state."""
    rc, out, _ = await run_cmd([
        "docker", "volume", "ls",
        "--filter", f"name={CONTAINER_PREFIX}-home-",
        "--format", "{{.Name}}",
    ], check=False)
    if rc != 0 or not out:
        return []

    removed = []
    all_repos = await state_manager.get_repos()

    for vol_name in out.splitlines():
        vol_name = vol_name.strip()
        if not vol_name:
            continue

        in_use = False
        for repo in all_repos:
            for pr_num in await state_manager.get_all_pr_states(repo):
                if _volume_name(repo, pr_num) == vol_name:
                    in_use = True
                    break
            if in_use:
                break
            for branch in await state_manager.get_local_branches(repo):
                if _volume_name(repo, branch) == vol_name:
                    in_use = True
                    break
            if in_use:
                break

        if not in_use:
            await run_cmd(["docker", "rm", "-f", vol_name], check=False)
            await run_cmd(["docker", "volume", "rm", "-f", vol_name], check=False)
            removed.append(vol_name)

    return removed
