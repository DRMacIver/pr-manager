"""Container lifecycle management for PR/branch work environments.

Each PR or local branch gets its own Docker container with:
- A persistent home directory (Docker volume)
- The repo cloned into ~/repo (non-shared)
- Host ~/.claude mounted directly
- SSH keys and gh auth copied from host on first start
- Auto-shutdown 10 minutes after last usage
"""

from __future__ import annotations

import json
from pathlib import Path

from .git import run_cmd

CONTAINER_PREFIX = "pr-manager"
IMAGE_NAME = "pr-manager-dev"
IDLE_TIMEOUT_MINUTES = 10
GC_AFTER_DAYS = 1

HOME = Path.home()


def _container_name(repo: str, identifier: str) -> str:
    safe_repo = repo.replace("/", "-")
    safe_id = identifier.replace("/", "-")
    return f"{CONTAINER_PREFIX}-{safe_repo}-{safe_id}"


def _volume_name(repo: str, identifier: str) -> str:
    return f"{CONTAINER_PREFIX}-home-{repo.replace('/', '-')}-{identifier.replace('/', '-')}"


async def ensure_image_built(project_root: Path) -> None:
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
    rc, _, _ = await run_cmd(["docker", "inspect", name], check=False)
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

    # Mount ~/.claude directly from the host.
    claude_dir = HOME / ".claude"
    if claude_dir.exists():
        cmd += ["-v", f"{claude_dir}:/home/dev/.claude"]

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
        _startup_script(ssh_url, branch, create_branch),
    ]

    await run_cmd(cmd)
    return name


def _startup_script(ssh_url: str, branch: str, create_branch: bool) -> str:
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
    return await run_cmd([
        "docker", "exec", "-w", workdir, name,
    ] + cmd, check=False)


async def get_tmux_command_for_container(
    name: str,
    cmd: str = "bash",
    workdir: str = "/home/dev/repo",
) -> list[str]:
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
        rc, out, _ = await run_cmd([
            "docker", "top", cname, "-o", "pid,comm",
        ], check=False)
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
