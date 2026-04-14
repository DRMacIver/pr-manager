"""Regression tests for container _startup_script.

The bug: `git remote set-url origin <ssh>` only ran inside the "if no .git
yet" first-clone branch, so a container whose persistent ~/repo volume was
created by an older version of the code could keep a stale non-SSH origin
forever. Make sure the script always runs set-url on start.
"""
from __future__ import annotations

from pr_manager.container import _startup_script


def test_startup_script_sets_ssh_origin_on_every_start_with_pristine():
    script = _startup_script(
        ssh_url="git@github.com:owner/repo.git",
        branch="main",
        create_branch=False,
        has_pristine=True,
    )
    # The set-url must happen unconditionally (not only inside the
    # first-clone branch), so we expect it to appear outside the
    # `if [ ! -d ~/repo/.git ]` guard.
    before_guard, _, after_guard = script.partition("if [ ! -d ~/repo/.git ]")
    assert "git remote set-url origin git@github.com:owner/repo.git" in after_guard
    # And it must also run on existing checkouts — i.e. outside the guarded block.
    remainder = after_guard.split("fi", 1)[1]
    assert "git remote set-url origin git@github.com:owner/repo.git" in (before_guard + remainder)


def test_startup_script_sets_ssh_origin_on_every_start_without_pristine():
    script = _startup_script(
        ssh_url="git@github.com:owner/repo.git",
        branch="main",
        create_branch=False,
        has_pristine=False,
    )
    before_guard, _, after_guard = script.partition("if [ ! -d ~/repo/.git ]")
    remainder = after_guard.split("fi", 1)[1]
    assert "git remote set-url origin git@github.com:owner/repo.git" in (before_guard + remainder)
