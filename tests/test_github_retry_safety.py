from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from apps.api.models import IntegrationStatus, PatchArtifact
from packages.remediation.github import GitHubDraftPRAdapter

REPOSITORY = "acme/datarescue-test"
CASE_ID = "DR-RETRY"
BRANCH = "datarescue/dr-retry"
TARGET = "models/payments.sql"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _repo_with_bare_origin(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare")

    fake_ssh = tmp_path / "fake-github-ssh"
    fake_ssh.write_text(
        f"""#!/usr/bin/env python3
import os
import shlex
import sys

remote = {str(remote)!r}
command = shlex.split(sys.argv[-1])
if len(command) != 2 or command[0] not in ("git-upload-pack", "git-receive-pack"):
    raise SystemExit(2)
os.execvp(command[0], [command[0], remote])
""",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "datarescue@example.test")
    _git(repo, "config", "user.name", "DataRescue Test")
    _git(repo, "config", "core.sshCommand", str(fake_ssh))
    _git(repo, "config", "ssh.variant", "ssh")
    target = repo / TARGET
    target.parent.mkdir(parents=True)
    target.write_text("select amount as revenue from raw.payments\n", encoding="utf-8")
    _git(repo, "add", TARGET)
    _git(repo, "commit", "-m", "initial fixture")
    _git(repo, "remote", "add", "origin", f"git@github.com:{REPOSITORY}.git")
    _git(repo, "push", "--set-upstream", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return repo, remote


def _install_fake_gh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_creates: int = 0,
    prs: list[dict[str, object]] | None = None,
    resolved_repository: str = REPOSITORY,
) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    executable = bin_dir / "gh"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import subprocess
import sys

state_path = os.environ["FAKE_GH_STATE"]
with open(state_path, encoding="utf-8") as handle:
    state = json.load(handle)
args = sys.argv[1:]

if args[:2] == ["auth", "status"]:
    raise SystemExit(0)
if args[:2] == ["repo", "view"]:
    print(state["repository"])
    raise SystemExit(0)
if args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1]
    print(json.dumps([pr for pr in state["prs"] if pr["headRefName"] == head]))
    raise SystemExit(0)
if args[:2] == ["pr", "create"]:
    state["create_calls"] += 1
    if state["fail_creates"]:
        state["fail_creates"] -= 1
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        print("simulated transport failure", file=sys.stderr)
        raise SystemExit(1)
    head = args[args.index("--head") + 1]
    base = args[args.index("--base") + 1]
    owner = state["repository"].split("/", 1)[0]
    head_oid = subprocess.run(
        ["git", "rev-parse", f"refs/remotes/origin/{head}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    url = f"https://github.com/{state['repository']}/pull/{len(state['prs']) + 1}"
    state["prs"].append(
        {
            "url": url,
            "headRefName": head,
            "headRefOid": head_oid,
            "baseRefName": base,
            "headRepositoryOwner": {"login": owner},
            "headRepository": {"nameWithOwner": state["repository"]},
            "isCrossRepository": False,
            "isDraft": True,
        }
    )
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    print(url)
    raise SystemExit(0)

print(f"unexpected fake gh invocation: {args!r}", file=sys.stderr)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    state_path = tmp_path / "fake-gh-state.json"
    state_path.write_text(
        json.dumps(
            {
                "repository": resolved_repository,
                "fail_creates": fail_creates,
                "create_calls": 0,
                "prs": prs or [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_GH_STATE", str(state_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return state_path


def _patch() -> PatchArtifact:
    content = "select net_amount as revenue from raw.payments\n"
    return PatchArtifact(
        path=TARGET,
        content=content,
        sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


def _adapter(repo: Path, runtime: Path) -> GitHubDraftPRAdapter:
    return GitHubDraftPRAdapter(
        enabled=True,
        repository=REPOSITORY,
        repo_root=repo,
        base_branch="main",
        patch_path=TARGET,
        runtime_dir=runtime,
    )


def _state(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_remote_orphan_retry_and_existing_pr_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote = _repo_with_bare_origin(tmp_path)
    state_path = _install_fake_gh(tmp_path, monkeypatch, fail_creates=1)
    adapter = _adapter(repo, tmp_path / "runtime")

    first = adapter.create_draft(case_id=CASE_ID, patch=_patch())
    assert first.integration.status is IntegrationStatus.FAILED
    assert _git(remote, "show-ref", "--verify", f"refs/heads/{BRANCH}").returncode == 0

    # Retry must reuse the exact DataRescue-owned remote commit instead of
    # attempting another non-fast-forward push.
    second = adapter.create_draft(case_id=CASE_ID, patch=_patch())
    assert second.integration.status is IntegrationStatus.SUCCEEDED
    assert second.url == f"https://github.com/{REPOSITORY}/pull/1"

    # A subsequent call reconciles the already-open PR and performs no create.
    third = adapter.create_draft(case_id=CASE_ID, patch=_patch())
    assert third.integration.status is IntegrationStatus.SUCCEEDED
    assert third.url == second.url
    assert _state(state_path)["create_calls"] == 2


def test_legacy_existing_pr_without_trailers_is_safely_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote = _repo_with_bare_origin(tmp_path)
    patch = _patch()
    base = _git(repo, "rev-parse", "main").stdout.strip()
    _git(repo, "switch", "-c", BRANCH, base)
    (repo / TARGET).write_text(patch.content, encoding="utf-8")
    _git(repo, "add", TARGET)
    _git(repo, "commit", "-m", f"fix(data): recover schema drift for {CASE_ID}")
    _git(repo, "push", "origin", BRANCH)
    _git(repo, "switch", "main")
    _git(repo, "branch", "-D", BRANCH)
    head = _git(remote, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
    state_path = _install_fake_gh(
        tmp_path,
        monkeypatch,
        prs=[
            {
                "url": f"https://github.com/{REPOSITORY}/pull/1",
                "headRefName": BRANCH,
                "headRefOid": head,
                "baseRefName": "main",
                "headRepositoryOwner": {"login": "acme"},
                "headRepository": {"nameWithOwner": REPOSITORY},
                "isCrossRepository": False,
                "isDraft": True,
            }
        ],
    )

    result = _adapter(repo, tmp_path / "runtime").create_draft(
        case_id=CASE_ID, patch=patch
    )

    assert result.integration.status is IntegrationStatus.SUCCEEDED
    assert result.url == f"https://github.com/{REPOSITORY}/pull/1"
    assert _state(state_path)["create_calls"] == 0


def test_unknown_local_branch_and_worktree_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = _repo_with_bare_origin(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    user_worktree = tmp_path / "user-owned-worktree"
    _git(repo, "worktree", "add", "-b", BRANCH, str(user_worktree), "main")
    original_sha = _git(repo, "rev-parse", BRANCH).stdout.strip()

    result = _adapter(repo, tmp_path / "runtime").create_draft(
        case_id=CASE_ID, patch=_patch()
    )

    assert result.integration.status is IntegrationStatus.FAILED
    assert user_worktree.exists()
    assert _git(repo, "rev-parse", BRANCH).stdout.strip() == original_sha


def test_cleanup_timeout_cannot_mask_successful_pr_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = _repo_with_bare_origin(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    real_run = subprocess.run

    def timeout_on_worktree_cleanup(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        command = args[0]
        if (
            isinstance(command, list)
            and command[:4] == ["git", "worktree", "remove", "--force"]
        ):
            raise subprocess.TimeoutExpired(command, 30)
        return real_run(*args, **kwargs)  # type: ignore[return-value]

    monkeypatch.setattr("packages.remediation.github.subprocess.run", timeout_on_worktree_cleanup)

    result = _adapter(repo, tmp_path / "runtime").create_draft(
        case_id=CASE_ID, patch=_patch()
    )

    assert result.integration.status is IntegrationStatus.SUCCEEDED
    assert result.url == f"https://github.com/{REPOSITORY}/pull/1"


def test_worktree_add_timeout_cleans_only_the_proven_incomplete_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = _repo_with_bare_origin(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime"
    real_run = subprocess.run

    def timeout_after_worktree_was_added(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        command = args[0]
        if (
            isinstance(command, list)
            and command[:3] == ["git", "worktree", "add"]
        ):
            real_run(*args, **kwargs)
            raise subprocess.TimeoutExpired(command, 120)
        return real_run(*args, **kwargs)  # type: ignore[return-value]

    monkeypatch.setattr(
        "packages.remediation.github.subprocess.run", timeout_after_worktree_was_added
    )
    adapter = _adapter(repo, runtime)

    first = adapter.create_draft(case_id=CASE_ID, patch=_patch())

    assert first.integration.status is IntegrationStatus.FAILED
    assert _git(repo, "branch", "--list", BRANCH).stdout.strip() == ""
    assert not (runtime / "worktrees" / "dr-retry").exists()

    monkeypatch.setattr("packages.remediation.github.subprocess.run", real_run)
    second = adapter.create_draft(case_id=CASE_ID, patch=_patch())
    assert second.integration.status is IntegrationStatus.SUCCEEDED


def test_pr_create_response_timeout_reconciles_the_created_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = _repo_with_bare_origin(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    real_run = subprocess.run

    def timeout_after_gh_accepted(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        command = args[0]
        if isinstance(command, list) and command[:3] == ["gh", "pr", "create"]:
            real_run(*args, **kwargs)
            raise subprocess.TimeoutExpired(command, 120)
        return real_run(*args, **kwargs)  # type: ignore[return-value]

    monkeypatch.setattr("packages.remediation.github.subprocess.run", timeout_after_gh_accepted)

    result = _adapter(repo, tmp_path / "runtime").create_draft(
        case_id=CASE_ID, patch=_patch()
    )

    assert result.integration.status is IntegrationStatus.SUCCEEDED
    assert result.url == f"https://github.com/{REPOSITORY}/pull/1"


def test_successful_pr_create_rejects_a_raced_origin_branch_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote = _repo_with_bare_origin(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    remote_main = _git(remote, "rev-parse", "refs/heads/main").stdout.strip()
    real_run = subprocess.run

    def replace_branch_after_gh_accepted(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        command = args[0]
        result = real_run(*args, **kwargs)  # type: ignore[call-overload]
        if isinstance(command, list) and command[:3] == ["gh", "pr", "create"]:
            real_run(
                ["git", "update-ref", f"refs/heads/{BRANCH}", remote_main],
                cwd=remote,
                check=True,
                capture_output=True,
                text=True,
            )
        return result  # type: ignore[return-value]

    monkeypatch.setattr(
        "packages.remediation.github.subprocess.run", replace_branch_after_gh_accepted
    )

    result = _adapter(repo, tmp_path / "runtime").create_draft(
        case_id=CASE_ID, patch=_patch()
    )

    assert result.integration.status is IntegrationStatus.FAILED
    assert result.url is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("isDraft", False),
        ("isCrossRepository", True),
        ("headRepository", {"nameWithOwner": "acme/datarescue-fork"}),
    ],
)
def test_existing_pr_must_remain_a_same_repository_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    repo, _ = _repo_with_bare_origin(tmp_path)
    state_path = _install_fake_gh(tmp_path, monkeypatch)
    adapter = _adapter(repo, tmp_path / "runtime")
    created = adapter.create_draft(case_id=CASE_ID, patch=_patch())
    assert created.integration.status is IntegrationStatus.SUCCEEDED

    state = _state(state_path)
    prs = state["prs"]
    assert isinstance(prs, list) and isinstance(prs[0], dict)
    prs[0][field] = value
    state_path.write_text(json.dumps(state), encoding="utf-8")

    retried = adapter.create_draft(case_id=CASE_ID, patch=_patch())

    assert retried.integration.status is IntegrationStatus.FAILED
    assert retried.url is None


def test_existing_pr_must_match_allowed_repository_owner_and_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = _repo_with_bare_origin(tmp_path)
    wrong_pr: dict[str, object] = {
        "url": f"https://github.com/{REPOSITORY}/pull/9",
        "headRefName": BRANCH,
        "headRefOid": "0" * 40,
        "baseRefName": "main",
        "headRepositoryOwner": {"login": "someone-else"},
        "headRepository": {"nameWithOwner": REPOSITORY},
        "isCrossRepository": False,
        "isDraft": True,
    }
    _install_fake_gh(tmp_path, monkeypatch, prs=[wrong_pr])

    result = _adapter(repo, tmp_path / "runtime").create_draft(
        case_id=CASE_ID, patch=_patch()
    )

    assert result.integration.status is IntegrationStatus.FAILED
    assert _git(repo, "branch", "--list", BRANCH).stdout.strip() == ""


def test_existing_pr_with_altered_head_commit_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = _repo_with_bare_origin(tmp_path)
    main_sha = _git(repo, "rev-parse", "main").stdout.strip()
    altered_pr: dict[str, object] = {
        "url": f"https://github.com/{REPOSITORY}/pull/9",
        "headRefName": BRANCH,
        "headRefOid": main_sha,
        "baseRefName": "main",
        "headRepositoryOwner": {"login": "acme"},
        "headRepository": {"nameWithOwner": REPOSITORY},
        "isCrossRepository": False,
        "isDraft": True,
    }
    _install_fake_gh(tmp_path, monkeypatch, prs=[altered_pr])

    result = _adapter(repo, tmp_path / "runtime").create_draft(
        case_id=CASE_ID, patch=_patch()
    )

    assert result.integration.status is IntegrationStatus.FAILED
    assert _git(repo, "branch", "--list", BRANCH).stdout.strip() == ""


def test_gh_repo_override_cannot_hide_mismatched_origin_fetch_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote = _repo_with_bare_origin(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("GH_REPO", REPOSITORY)
    _git(repo, "remote", "set-url", "origin", "git@github.com:acme/different-repo.git")

    result = _adapter(repo, tmp_path / "runtime").create_draft(
        case_id=CASE_ID, patch=_patch()
    )

    assert result.integration.status is IntegrationStatus.FAILED
    assert _git(repo, "branch", "--list", BRANCH).stdout.strip() == ""
    assert (
        _git(
            remote,
            "show-ref",
            "--verify",
            f"refs/heads/{BRANCH}",
            check=False,
        ).returncode
        != 0
    )


def test_mismatched_origin_push_url_is_rejected_before_branch_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote = _repo_with_bare_origin(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    _git(
        repo,
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        f"git@github.com:{REPOSITORY}.git",
    )
    _git(
        repo,
        "remote",
        "set-url",
        "--add",
        "--push",
        "origin",
        "ssh://git@github.com/acme/different-repo.git",
    )

    result = _adapter(repo, tmp_path / "runtime").create_draft(
        case_id=CASE_ID, patch=_patch()
    )

    assert result.integration.status is IntegrationStatus.FAILED
    assert _git(repo, "branch", "--list", BRANCH).stdout.strip() == ""
    assert (
        _git(
            remote,
            "show-ref",
            "--verify",
            f"refs/heads/{BRANCH}",
            check=False,
        ).returncode
        != 0
    )


def test_unpushed_and_dirty_local_main_cannot_enter_pr_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote = _repo_with_bare_origin(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    remote_base = _git(remote, "rev-parse", "refs/heads/main").stdout.strip()

    local_only = repo / "local-only.txt"
    local_only.write_text("must not enter the PR\n", encoding="utf-8")
    _git(repo, "add", "local-only.txt")
    _git(repo, "commit", "-m", "local unpushed work")
    (repo / TARGET).write_text("dirty local main\n", encoding="utf-8")

    result = _adapter(repo, tmp_path / "runtime").create_draft(
        case_id=CASE_ID, patch=_patch()
    )

    assert result.integration.status is IntegrationStatus.SUCCEEDED
    head = _git(remote, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
    assert _git(remote, "rev-parse", f"{head}^").stdout.strip() == remote_base
    assert (
        _git(remote, "cat-file", "-e", f"{head}:local-only.txt", check=False).returncode
        != 0
    )
    assert _git(remote, "show", f"{head}:{TARGET}").stdout == _patch().content


def test_stale_local_main_uses_freshly_fetched_origin_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote = _repo_with_bare_origin(tmp_path)
    _install_fake_gh(tmp_path, monkeypatch)
    stale_local_main = _git(repo, "rev-parse", "main").stdout.strip()

    upstream = tmp_path / "upstream-clone"
    _git(tmp_path, "clone", str(remote), str(upstream))
    _git(upstream, "config", "user.email", "upstream@example.test")
    _git(upstream, "config", "user.name", "Upstream Test")
    (upstream / "remote-only.txt").write_text("new origin base\n", encoding="utf-8")
    _git(upstream, "add", "remote-only.txt")
    _git(upstream, "commit", "-m", "advance origin main")
    _git(upstream, "push", "origin", "main")
    fresh_remote_base = _git(remote, "rev-parse", "refs/heads/main").stdout.strip()
    assert fresh_remote_base != stale_local_main

    result = _adapter(repo, tmp_path / "runtime").create_draft(
        case_id=CASE_ID, patch=_patch()
    )

    assert result.integration.status is IntegrationStatus.SUCCEEDED
    head = _git(remote, "rev-parse", f"refs/heads/{BRANCH}").stdout.strip()
    assert _git(remote, "rev-parse", f"{head}^").stdout.strip() == fresh_remote_base
    assert _git(remote, "show", f"{head}:remote-only.txt").stdout == "new origin base\n"


def test_remote_retry_rejects_an_extra_commit_in_the_base_to_head_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, remote = _repo_with_bare_origin(tmp_path)
    state_path = _install_fake_gh(tmp_path, monkeypatch)
    base = _git(repo, "rev-parse", "main").stdout.strip()
    patch = _patch()

    _git(repo, "switch", "-c", BRANCH, base)
    (repo / "unexpected.txt").write_text("must reject\n", encoding="utf-8")
    _git(repo, "add", "unexpected.txt")
    _git(repo, "commit", "-m", "unexpected intermediate commit")
    (repo / TARGET).write_text(patch.content, encoding="utf-8")
    _git(repo, "add", TARGET)
    _git(
        repo,
        "commit",
        "-m",
        (
            f"fix(data): recover schema drift for {CASE_ID}\n\n"
            f"DataRescue-Case: {CASE_ID}\n"
            f"DataRescue-Patch-SHA256: {patch.sha256}\n"
            f"DataRescue-Base-Commit: {base}"
        ),
    )
    _git(repo, "push", "origin", BRANCH)
    _git(repo, "switch", "main")
    _git(repo, "branch", "-D", BRANCH)

    result = _adapter(repo, tmp_path / "runtime").create_draft(
        case_id=CASE_ID, patch=patch
    )

    assert result.integration.status is IntegrationStatus.FAILED
    assert _state(state_path)["create_calls"] == 0
    assert _git(remote, "show-ref", "--verify", f"refs/heads/{BRANCH}").returncode == 0
