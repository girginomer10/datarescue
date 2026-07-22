from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

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
                    message=f"Draft PR creation failed; local bundle retained: {error}",
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
        worktree = self.runtime_dir / "worktrees" / _slug(case_id)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if worktree.exists():
            raise ValueError(f"Worktree path already exists: {worktree}")

        def run(args: list[str], cwd: Path = self.repo_root) -> str:
            result = subprocess.run(
                args,
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            return result.stdout.strip()

        run(["gh", "auth", "status"])
        remote_name = run(
            [
                "gh",
                "repo",
                "view",
                self.repository,
                "--json",
                "nameWithOwner",
                "--jq",
                ".nameWithOwner",
            ]
        )
        if remote_name.casefold() != self.repository.casefold():
            raise ValueError(f"GitHub allowlist mismatch: {remote_name!r}")
        created = False
        try:
            run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree),
                    self.base_branch,
                ]
            )
            created = True
            target = (worktree / target_relative).resolve()
            if worktree.resolve() not in target.parents:
                raise ValueError("Resolved patch path escaped the worktree")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(patch.content, encoding="utf-8")
            run(["git", "add", "--", str(target_relative)], cwd=worktree)
            run(
                [
                    "git",
                    "commit",
                    "-m",
                    f"fix(data): recover schema drift for {case_id}",
                ],
                cwd=worktree,
            )
            run(["git", "push", "--set-upstream", "origin", branch], cwd=worktree)
            return run(
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
                ],
                cwd=worktree,
            )
        finally:
            if created:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=self.repo_root,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.casefold()).strip("-")[:64]


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
