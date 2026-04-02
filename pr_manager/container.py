"""Container lifecycle management for PR/branch work environments.

Each PR or local branch gets its own Docker container with:
- A persistent home directory (Docker volume)
- The repo cloned into ~/repo (non-shared)
- Credentials bind-mounted from the host
- Auto-shutdown 10 minutes after last usage
"""

from __future__ import annotations

import json
from pathlib import Path

from .git import run_cmd

CONTAINER_PREFIX = "pr-manager"
IMAGE_NAME = "pr-manager-dev"
CREDENTIALS_DIR = Path("~/.config/pr-manager/credentials").expanduser()
IDLE_TIMEOUT_MINUTES = 10
GC_AFTER_DAYS = 1


def _container_name(repo: str, identifier: str) -> str:
    """Deterministic container name from repo + PR number or branch name."""
    safe_repo = repo.replace("/", "-")
    safe_id = identifier.replace("/", "-")
    return f"{CONTAINER_PREFIX}-{safe_repo}-{safe_id}"


def _volume_name(repo: str, identifier: str) -> str:
    return f"{CONTAINER_PREFIX}-home-{repo.replace('/', '-')}-{identifier.replace('/', '-')}"


async def ensure_image_built(project_root: Path) -> None:
    """Build the Docker image if it doesn't exist or is outdated."""
    rc, _, _ = await run_cmd(
        ["docker", "image", "inspect", IMAGE_NAME],
        check=False,
    )
    if rc != 0:
        await run_cmd([
            "docker", "build", "-t", IMAGE_NAME, str(project_root),
        ])


async def is_container_running(repo: str, identifier: str) -> bool:
    name = _container_name(repo, identifier)
    rc, out, _ = await run_cmd(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        check=False,
    )
    return rc == 0 and out.strip() == "true"


async def start_container(
    repo: str,
    identifier: str,
    ssh_url: str,
    branch: str,
    create_branch: bool = False,
) -> str:
    """Start (or create) a container for a PR or branch. Returns the container name."""
    name = _container_name(repo, identifier)
    volume = _volume_name(repo, identifier)

    if await is_container_running(repo, identifier):
        return name

    # Check if container exists but is stopped.
    rc, _, _ = await run_cmd(
        ["docker", "inspect", name],
        check=False,
    )
    if rc == 0:
        await run_cmd(["docker", "start", name])
        return name

    # Create and start a new container.
    cmd = [
        "docker", "run", "-d",
        "--name", name,
        "-v", f"{volume}:/home/dev",
        "--hostname", identifier.replace("/", "-"),
    ]

    # Mount credentials if they exist.
    if CREDENTIALS_DIR.exists():
        cmd += ["-v", f"{CREDENTIALS_DIR}:/mnt/credentials:ro"]

    # Mount SSH keys if they exist.
    ssh_dir = Path("~/.config/pr-manager/ssh-keys").expanduser()
    if ssh_dir.exists():
        cmd += ["-v", f"{ssh_dir}:/mnt/ssh-keys:ro"]

    cmd += [
        IMAGE_NAME,
        "bash", "-c",
        _startup_script(ssh_url, branch, create_branch),
    ]

    await run_cmd(cmd)
    return name


def _startup_script(ssh_url: str, branch: str, create_branch: bool) -> str:
    """Shell script that runs inside the container on first start."""
    clone_cmd = f"git clone {ssh_url} ~/repo"
    if create_branch:
        checkout = f"cd ~/repo && git checkout -b {branch} origin/main"
    else:
        checkout = f"cd ~/repo && git checkout {branch}"

    return (
        f"if [ ! -d ~/repo/.git ]; then {clone_cmd} && {checkout}; fi; "
        "exec sleep infinity"
    )


async def exec_in_container(name: str, cmd: list[str], workdir: str = "/home/dev/repo") -> tuple[int, str, str]:
    """Run a command inside a running container."""
    return await run_cmd([
        "docker", "exec", "-w", workdir, name,
    ] + cmd, check=False)


async def get_tmux_command_for_container(
    name: str,
    cmd: str = "bash",
    workdir: str = "/home/dev/repo",
) -> list[str]:
    """Return a tmux new-window command that docker-execs into the container."""
    return [
        "tmux", "new-window", "-n", name,
        "docker", "exec", "-it", "-w", workdir, name, "bash", "-c", cmd,
    ]


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
    """List all pr-manager containers with their status."""
    rc, out, _ = await run_cmd([
        "docker", "ps", "-a",
        "--filter", f"name={CONTAINER_PREFIX}-",
        "--format", "{{json .}}",
    ], check=False)
    if rc != 0 or not out:
        return []
    results = []
    for line in out.splitlines():
        if line.strip():
            results.append(json.loads(line))
    return results


# ── Idle shutdown ────────────────────────────────────────────────────────────

async def check_container_idle(repo: str, identifier: str) -> bool:
    """Check if a container has been idle (no exec sessions) for IDLE_TIMEOUT_MINUTES."""
    name = _container_name(repo, identifier)

    # Check for active exec sessions (tmux windows into the container).
    rc, out, _ = await run_cmd([
        "docker", "top", name, "-o", "pid,comm",
    ], check=False)
    if rc != 0:
        return True  # Container not running = idle.

    # If there's anything besides sleep/entrypoint, it's active.
    lines = out.strip().splitlines()[1:]  # Skip header
    active_procs = [
        line for line in lines
        if not any(idle in line.lower() for idle in ("sleep", "entrypoint"))
    ]
    return len(active_procs) == 0


async def idle_shutdown_sweep() -> list[str]:
    """Stop containers that have been idle for IDLE_TIMEOUT_MINUTES.

    Returns list of container names that were stopped.
    """
    # This is a simplified version — a full implementation would track
    # last-activity timestamps. For now, just check if there are any
    # active processes beyond sleep.
    stopped = []
    containers = await list_containers()
    for c in containers:
        if c.get("State") != "running":
            continue
        cname = c.get("Names", "")
        if not cname.startswith(CONTAINER_PREFIX + "-"):
            continue
        # Check if idle.
        rc, out, _ = await run_cmd([
            "docker", "top", cname, "-o", "pid,comm",
        ], check=False)
        if rc != 0:
            continue
        lines = out.strip().splitlines()[1:]
        active = [l for l in lines if not any(x in l.lower() for x in ("sleep", "entrypoint", "bash -c"))]
        if not active:
            await run_cmd(["docker", "stop", "-t", "5", cname], check=False)
            stopped.append(cname)
    return stopped


async def gc_old_volumes(state_manager, max_age_days: int = GC_AFTER_DAYS) -> list[str]:
    """Remove volumes for PRs that have been closed for more than max_age_days.

    Returns list of volume names that were removed.
    """
    # List all pr-manager volumes.
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

        # Check if any active PR/branch still uses this volume.
        in_use = False
        for repo in all_repos:
            states = await state_manager.get_all_pr_states(repo)
            for pr_num in states:
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
            # Container is gone and PR/branch removed from state — GC it.
            await run_cmd(["docker", "rm", "-f", vol_name], check=False)
            await run_cmd(["docker", "volume", "rm", "-f", vol_name], check=False)
            removed.append(vol_name)

    return removed
