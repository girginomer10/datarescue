from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from apps.api.models import (
    IntegrationResult,
    IntegrationStatus,
    PatchArtifact,
    PullRequestArtifact,
)


class GitHubDraftPRAdapter:
    def __init__(
        self,
        *,
        enabled: bool,
        repository: str,
        repo_root: Path,
        base_branch: str,
        patch_path: str,
        runtime_dir: Path,
    ) -> None:
        self.enabled = enabled
        self.repository = repository
        self.repo_root = repo_root.resolve()
        self.base_branch = base_branch
        self.patch_path = patch_path
        self.runtime_dir = runtime_dir

    def create_draft(self, *, case_id: str, patch: PatchArtifact) -> PullRequestArtifact:
        branch = f"datarescue/{_slug(case_id)}"
        if not self.enabled:
            bundle = self._write_bundle(case_id=case_id, branch=branch, patch=patch)
            return PullRequestArtifact(
                branch=branch,
                bundle_path=str(bundle),
                integration=IntegrationResult(
                    status=IntegrationStatus.NOT_RUN,
                    operation="github_create_draft_pr",
                    message=(
                        "GitHub write access is disabled; a reviewable local PR bundle was written"
                    ),
                    evidence_refs=[str(bundle)],
                    details={"repository": self.repository, "requires_human_merge": True},
                ),
            )
        try:
            url = self._create_real_draft(case_id=case_id, branch=branch, patch=patch)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            bundle = self._write_bundle(case_id=case_id, branch=branch, patch=patch)
            return PullRequestArtifact(
                branch=branch,
                bundle_path=str(bundle),
                integration=IntegrationResult(
                    status=IntegrationStatus.FAILED,
                    operation="github_create_draft_pr",
                    message=(
                        "Draft PR creation failed; local bundle retained "
                        f"({type(error).__name__})"
                    ),
                    evidence_refs=[str(bundle)],
                    details={"repository": self.repository, "requires_human_merge": True},
                ),
            )
        return PullRequestArtifact(
            branch=branch,
            url=url,
            integration=IntegrationResult(
                status=IntegrationStatus.SUCCEEDED,
                operation="github_create_draft_pr",
                message="Real draft PR created; incident remains active pending human merge",
                resource_id=url,
                evidence_refs=[url],
                details={"repository": self.repository, "requires_human_merge": True},
            ),
        )

    def _write_bundle(self, *, case_id: str, branch: str, patch: PatchArtifact) -> Path:
        bundle_dir = self.runtime_dir / "pr-bundles" / _slug(case_id)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        patch_file = bundle_dir / "payments_fct.sql"
        patch_file.write_text(patch.content, encoding="utf-8")
        manifest = bundle_dir / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "branch": branch,
                    "repository": self.repository,
                    "base": self.base_branch,
                    "target_path": self.patch_path,
                    "patch_sha256": patch.sha256,
                    "status": "NOT_RUN",
                    "reason": "external GitHub writes are disabled or unavailable",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return manifest

    def _create_real_draft(self, *, case_id: str, branch: str, patch: PatchArtifact) -> str:
        if not (self.repo_root / ".git").exists():
            raise ValueError(f"Not a git repository: {self.repo_root}")
        if shutil.which("gh") is None or shutil.which("git") is None:
            raise ValueError("git and gh are required for real draft PR creation")
        target_relative = Path(self.patch_path)
        if target_relative.is_absolute() or ".." in target_relative.parts:
            raise ValueError("GitHub patch path must be a safe repository-relative path")
        if not _slug(case_id):
            raise ValueError("Case id must contain at least one safe branch character")
        if hashlib.sha256(patch.content.encode()).hexdigest() != patch.sha256:
            raise ValueError("Patch SHA-256 does not match its content")

        worktree = self.runtime_dir / "worktrees" / _slug(case_id)
        worktree.parent.mkdir(parents=True, exist_ok=True)

        # Authorization and the explicit repository allowlist are always checked
        # before inspecting, reusing, or removing any local branch/worktree.
        self._run(["gh", "auth", "status"])
        remote_name = self._run(
            [
                "gh",
                "repo",
                "view",
                "--json",
                "nameWithOwner",
                "--jq",
                ".nameWithOwner",
            ]
        ).strip()
        if remote_name.casefold() != self.repository.casefold():
            raise ValueError(f"GitHub allowlist mismatch: {remote_name!r}")
        self._validate_origin_urls()
        base_commit = self._fetch_remote_base()

        # ``gh pr create`` is not an idempotent API. Reconcile the remote state
        # before doing any git mutation, including after a previous timeout.
        existing = self._find_existing_pr(
            case_id=case_id,
            branch=branch,
            patch=patch,
            target_relative=target_relative,
            current_base_commit=base_commit,
        )
        if existing is not None:
            return existing

        remote_sha = self._remote_branch_sha(branch)
        if remote_sha is not None:
            if not self._commit_is_owned(
                commit=remote_sha,
                case_id=case_id,
                patch=patch,
                target_relative=target_relative,
                current_base_commit=base_commit,
            ):
                raise ValueError(
                    f"Remote branch {branch!r} exists but is not owned by this DataRescue case"
                )
            return self._create_or_reconcile_pr(
                case_id=case_id,
                branch=branch,
                patch=patch,
                current_base_commit=base_commit,
            )

        # A same-named local ref or directory is not proof that DataRescue owns
        # it. Preserve unknown state and fail closed rather than deleting it.
        self._assert_local_slot_available(branch=branch, worktree=worktree)

        created = False
        worktree_add_started = False
        try:
            worktree_add_started = True
            self._run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree),
                    base_commit,
                ]
            )
            created = True
            target = (worktree / target_relative).resolve()
            if worktree.resolve() not in target.parents:
                raise ValueError("Resolved patch path escaped the worktree")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(patch.content, encoding="utf-8")
            self._run(["git", "add", "--", str(target_relative)], cwd=worktree)
            self._run(
                [
                    "git",
                    "commit",
                    "-m",
                    _commit_message(case_id, patch.sha256, base_commit),
                ],
                cwd=worktree,
            )
            # A normal push is deliberately used for a newly-created branch.
            # Existing remote branches are reconciled above and never blindly
            # overwritten with force.
            self._run(
                ["git", "push", "--set-upstream", "origin", branch],
                cwd=worktree,
            )
            return self._create_or_reconcile_pr(
                case_id=case_id,
                branch=branch,
                patch=patch,
                current_base_commit=base_commit,
            )
        finally:
            if created:
                # Cleanup is hygiene, not part of the externally-observed PR
                # transaction. It must never turn a successful PR into FAILED.
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    self._cleanup_worktree(worktree, branch)
            elif worktree_add_started:
                # `git worktree add` can finish its mutation and then time out
                # before subprocess.run returns. Remove only a registration that
                # still proves the exact path, branch and starting commit from
                # this attempt; preserve anything ambiguous.
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    self._cleanup_incomplete_worktree(
                        worktree=worktree,
                        branch=branch,
                        base_commit=base_commit,
                    )

    def _create_or_reconcile_pr(
        self,
        *,
        case_id: str,
        branch: str,
        patch: PatchArtifact,
        current_base_commit: str,
    ) -> str:
        try:
            output = self._run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    self.repository,
                    "--draft",
                    "--base",
                    self.base_branch,
                    "--head",
                    branch,
                    "--title",
                    f"DataRescue: evidence-gated recovery for {case_id}",
                    "--body",
                    _pr_body(case_id, patch.sha256),
                ]
            ).strip()
        except (OSError, subprocess.SubprocessError):
            # The command can time out or lose its response after GitHub has
            # accepted the PR. A second read decides whether it really failed.
            existing = self._find_existing_pr(
                case_id=case_id,
                branch=branch,
                patch=patch,
                target_relative=Path(self.patch_path),
                current_base_commit=current_base_commit,
            )
            if existing is not None:
                return existing
            raise
        self._validate_pr_url(output)
        # A successful command response proves only that GitHub returned a URL.
        # Re-read the server-side PR and branch state before reporting success:
        # the remote head can change between push and `gh pr create`.
        existing = self._find_existing_pr(
            case_id=case_id,
            branch=branch,
            patch=patch,
            target_relative=Path(self.patch_path),
            current_base_commit=current_base_commit,
        )
        if existing is None or existing != output:
            raise ValueError("Created GitHub PR could not be safely reconciled")
        return existing

    def _find_existing_pr(
        self,
        *,
        case_id: str,
        branch: str,
        patch: PatchArtifact,
        target_relative: Path,
        current_base_commit: str,
    ) -> str | None:
        raw = self._run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self.repository,
                "--state",
                "open",
                "--head",
                branch,
                "--json",
                (
                    "url,headRefName,headRefOid,baseRefName,headRepositoryOwner,"
                    "headRepository,isCrossRepository,isDraft"
                ),
                "--limit",
                "100",
            ]
        )
        try:
            candidates = json.loads(raw or "[]")
        except json.JSONDecodeError as error:
            raise ValueError("GitHub PR lookup returned invalid JSON") from error
        if not isinstance(candidates, list):
            raise ValueError("GitHub PR lookup returned an invalid payload")

        expected_owner = self.repository.split("/", 1)[0]
        remote_head = self._remote_branch_sha(branch)
        if remote_head is None:
            if candidates:
                raise ValueError("GitHub PR head branch is missing from the allowed repository")
            return None
        urls: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError("GitHub PR lookup returned an invalid entry")
            owner_payload = candidate.get("headRepositoryOwner")
            owner = owner_payload.get("login") if isinstance(owner_payload, dict) else None
            repository_payload = candidate.get("headRepository")
            head_repository = (
                repository_payload.get("nameWithOwner")
                if isinstance(repository_payload, dict)
                else None
            )
            if (
                candidate.get("headRefName") != branch
                or candidate.get("baseRefName") != self.base_branch
                or not isinstance(owner, str)
                or owner.casefold() != expected_owner.casefold()
                or not isinstance(head_repository, str)
                or head_repository.casefold() != self.repository.casefold()
                or candidate.get("isCrossRepository") is not False
                or candidate.get("isDraft") is not True
            ):
                raise ValueError("GitHub PR lookup returned an unexpected repository/head")
            url = candidate.get("url")
            head_oid = candidate.get("headRefOid")
            if not isinstance(url, str) or not isinstance(head_oid, str):
                raise ValueError("GitHub PR lookup omitted its URL or head commit")
            if head_oid.casefold() != remote_head.casefold():
                raise ValueError(
                    "GitHub PR head does not match the allowed origin branch"
                )
            self._validate_pr_url(url)
            self._fetch_remote_commit(head_oid)
            if not self._commit_is_owned(
                commit=head_oid,
                case_id=case_id,
                patch=patch,
                target_relative=target_relative,
                current_base_commit=current_base_commit,
            ):
                raise ValueError("Existing GitHub PR head is not owned by this DataRescue case")
            urls.append(url)
        if len(urls) > 1:
            raise ValueError("Multiple open PRs exist for the same DataRescue branch")
        return urls[0] if urls else None

    def _validate_pr_url(self, url: str) -> None:
        if not url:
            raise ValueError("GitHub PR creation returned no URL")
        parsed = urlsplit(url)
        parts = [part for part in parsed.path.split("/") if part]
        expected = self.repository.split("/", 1)
        if (
            parsed.scheme != "https"
            or parsed.netloc.casefold() != "github.com"
            or len(parts) != 4
            or len(expected) != 2
            or parts[0].casefold() != expected[0].casefold()
            or parts[1].casefold() != expected[1].casefold()
            or parts[2] != "pull"
            or not parts[3].isdigit()
        ):
            raise ValueError("GitHub PR URL does not match the allowed repository")

    def _validate_origin_urls(self) -> None:
        expected = self.repository.casefold()
        url_groups = {
            "fetch": self._run(
                ["git", "remote", "get-url", "--all", "origin"]
            ).splitlines(),
            "push": self._run(
                ["git", "remote", "get-url", "--push", "--all", "origin"]
            ).splitlines(),
        }
        for operation, urls in url_groups.items():
            if not urls:
                raise ValueError(f"Git origin has no {operation} URL")
            for url in urls:
                resolved = _github_repository_from_remote_url(url.strip())
                if resolved is None or resolved.casefold() != expected:
                    raise ValueError(
                        f"Git origin {operation} URL does not match the allowed repository"
                    )

    def _fetch_remote_base(self) -> str:
        self._run(["git", "check-ref-format", "--branch", self.base_branch])
        self._run(
            [
                "git",
                "fetch",
                "--no-tags",
                "origin",
                f"refs/heads/{self.base_branch}",
            ]
        )
        commit = self._run(
            ["git", "rev-parse", "--verify", "FETCH_HEAD^{commit}"]
        ).strip()
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", commit) is None:
            raise ValueError("Origin base branch returned an invalid commit id")
        return commit

    def _remote_branch_sha(self, branch: str) -> str | None:
        ref = f"refs/heads/{branch}"
        output = self._run(["git", "ls-remote", "--heads", "origin", ref])
        rows = [row.split() for row in output.splitlines() if row.strip()]
        if not rows:
            return None
        if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref:
            raise ValueError(f"Unexpected remote branch lookup result for {branch!r}")
        sha = rows[0][0]
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", sha) is None:
            raise ValueError("Remote branch lookup returned an invalid commit id")
        self._fetch_remote_commit(sha)
        return sha

    def _fetch_remote_commit(self, commit: str) -> None:
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", commit) is None:
            raise ValueError("GitHub returned an invalid commit id")
        self._run(["git", "fetch", "--no-tags", "origin", commit])

    def _commit_is_owned(
        self,
        *,
        commit: str,
        case_id: str,
        patch: PatchArtifact,
        target_relative: Path,
        current_base_commit: str,
    ) -> bool:
        message_lines = self._run(["git", "show", "-s", "--format=%B", commit]).splitlines()
        case_trailers = [
            line.removeprefix("DataRescue-Case: ")
            for line in message_lines
            if line.startswith("DataRescue-Case: ")
        ]
        patch_trailers = [
            line.removeprefix("DataRescue-Patch-SHA256: ")
            for line in message_lines
            if line.startswith("DataRescue-Patch-SHA256: ")
        ]
        base_trailers = [
            line.removeprefix("DataRescue-Base-Commit: ")
            for line in message_lines
            if line.startswith("DataRescue-Base-Commit: ")
        ]
        if not message_lines or message_lines[0] != (
            f"fix(data): recover schema drift for {case_id}"
        ):
            return False
        parents = self._run(["git", "rev-list", "--parents", "-n", "1", commit]).split()
        if len(parents) != 2:
            return False
        parent = parents[1]
        has_datarescue_trailer = any(
            line.startswith("DataRescue-") for line in message_lines[1:]
        )
        if has_datarescue_trailer:
            if (
                case_trailers != [case_id]
                or patch_trailers != [patch.sha256]
                or base_trailers != [parent]
            ):
                return False
            recorded_base = base_trailers[0]
        else:
            # Backward compatibility for branches created by the original
            # adapter, which wrote only the exact DataRescue subject. The sole
            # parent plus full range/tree validation below is equivalent proof.
            recorded_base = parent
        try:
            # The immutable recorded base must still belong to the verified
            # origin base history. Local main is intentionally irrelevant.
            self._run(
                [
                    "git",
                    "merge-base",
                    "--is-ancestor",
                    recorded_base,
                    current_base_commit,
                ]
            )
        except subprocess.CalledProcessError:
            return False
        commit_range = self._run(
            ["git", "rev-list", "--reverse", f"{recorded_base}..{commit}"]
        ).splitlines()
        if commit_range != [commit]:
            return False
        changed = self._run(
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--no-renames",
                "--name-only",
                recorded_base,
                commit,
            ]
        ).splitlines()
        if changed != [target_relative.as_posix()]:
            return False
        content = self._run(["git", "show", f"{commit}:{target_relative.as_posix()}"])
        return content == patch.content

    def _assert_local_slot_available(self, *, branch: str, worktree: Path) -> None:
        if self._local_branch_sha(branch) is not None or worktree.exists():
            raise ValueError(
                f"Local branch/worktree {branch!r} already exists; refusing to delete unknown state"
            )

    def _local_branch_sha(self, branch: str) -> str | None:
        result = self._run_unchecked(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"]
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", sha) is None:
            raise ValueError("Local branch lookup returned an invalid commit id")
        return sha

    def _cleanup_worktree(self, worktree: Path, branch: str) -> None:
        """Best-effort removal for a branch/worktree already proven to be ours."""

        for args in (
            ["git", "worktree", "remove", "--force", str(worktree)],
            ["git", "branch", "-D", branch],
        ):
            self._best_effort_run(args)
        try:
            if worktree.exists():
                shutil.rmtree(worktree)
        except OSError:
            pass

    def _cleanup_incomplete_worktree(
        self, *, worktree: Path, branch: str, base_commit: str
    ) -> None:
        expected_path = worktree.resolve()
        expected_branch = f"refs/heads/{branch}"
        records = _worktree_records(
            self._run(["git", "worktree", "list", "--porcelain", "-z"])
        )
        owned = any(
            record.get("worktree") is not None
            and Path(record["worktree"]).resolve() == expected_path
            and record.get("branch") == expected_branch
            and record.get("HEAD", "").casefold() == base_commit.casefold()
            for record in records
        )
        if not owned or self._local_branch_sha(branch) != base_commit:
            return
        self._cleanup_worktree(worktree, branch)

    def _run(self, args: list[str], *, cwd: Path | None = None) -> str:
        result = subprocess.run(
            args,
            cwd=cwd or self.repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.stdout

    def _run_unchecked(
        self, args: list[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            cwd=cwd or self.repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _best_effort_run(self, args: list[str], *, cwd: Path | None = None) -> None:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            self._run_unchecked(args, cwd=cwd)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.casefold()).strip("-")[:64]


def _github_repository_from_remote_url(url: str) -> str | None:
    scp_match = re.fullmatch(r"git@github\.com:([^?#]+)", url, flags=re.IGNORECASE)
    if scp_match is not None:
        return _repository_from_path(scp_match.group(1))

    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.query or parsed.fragment or parsed.hostname is None:
        return None
    if parsed.hostname.casefold() != "github.com":
        return None
    if parsed.scheme == "https":
        if parsed.username is not None or parsed.password is not None or port not in (None, 443):
            return None
    elif parsed.scheme == "ssh":
        if parsed.username != "git" or parsed.password is not None or port not in (None, 22):
            return None
    else:
        return None
    return _repository_from_path(parsed.path)


def _repository_from_path(path: str) -> str | None:
    normalized = path.strip("/")
    if normalized.casefold().endswith(".git"):
        normalized = normalized[:-4]
    parts = normalized.split("/")
    if len(parts) != 2 or any(
        re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None for part in parts
    ):
        return None
    return "/".join(parts)


def _worktree_records(payload: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for field in payload.split("\0"):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        key, separator, value = field.partition(" ")
        current[key] = value if separator else ""
    if current:
        records.append(current)
    return records


def _commit_message(case_id: str, patch_sha256: str, base_commit: str) -> str:
    return (
        f"fix(data): recover schema drift for {case_id}\n\n"
        f"DataRescue-Case: {case_id}\n"
        f"DataRescue-Patch-SHA256: {patch_sha256}\n"
        f"DataRescue-Base-Commit: {base_commit}"
    )


def _pr_body(case_id: str, patch_sha256: str) -> str:
    return (
        "## DataRescue evidence-gated repair\n\n"
        f"- Case: `{case_id}`\n"
        f"- Patch SHA-256: `{patch_sha256}`\n"
        "- Deterministic policy: passed\n"
        "- Human merge: required\n"
        "- Incident: remains active until post-deploy verification\n\n"
        "This PR was opened only after isolated execution, reconciliation, and dbt checks."
    )
