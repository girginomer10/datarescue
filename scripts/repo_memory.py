#!/usr/bin/env python3
"""Repo-scoped semantic memory with a rebuildable local SQLite vector cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import subprocess
from collections import Counter
from datetime import date
from itertools import pairwise
from pathlib import Path

VECTOR_DIMENSIONS = 384
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
REQUIRED_CATEGORIES = (
    "architecture",
    "decisions",
    "bugfixes",
    "gotchas",
    "file-map",
    "api-notes",
)


def repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def repo_namespace(root: Path) -> str:
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    identity = f"{root.name}:{remote or root}"
    readable = re.sub(r"[^a-z0-9]+", "_", identity.casefold()).strip("_")[:42]
    digest = hashlib.sha256(identity.encode()).hexdigest()[:10]
    return f"{readable}_{digest}"


def cache_paths(namespace: str) -> tuple[Path, Path]:
    base = Path.home() / ".codex-memory"
    return base / "repo_memory.sqlite3", base / "manifests" / f"{namespace}.json"


def connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            namespace TEXT NOT NULL,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL,
            tags TEXT NOT NULL,
            related_files TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            excerpt TEXT NOT NULL,
            vector_json TEXT NOT NULL,
            PRIMARY KEY (namespace, source)
        )
        """
    )
    return connection


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[\w.-]+", text.casefold(), flags=re.UNICODE)
    return words + [f"{left}::{right}" for left, right in pairwise(words)]


def embed(text: str) -> list[float]:
    counts = Counter(tokenize(text))
    vector = [0.0] * VECTOR_DIMENSIONS
    for token, count in counts.items():
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % VECTOR_DIMENSIONS
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign * (1.0 + math.log(count))
    magnitude = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / magnitude for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def parse_note(text: str, source: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if text.startswith("---\n"):
        _, frontmatter, _ = text.split("---\n", 2)
        for line in frontmatter.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip().strip('"')
    heading = next(
        (line.removeprefix("# ").strip() for line in text.splitlines() if line.startswith("# ")),
        Path(source).stem.replace("-", " ").title(),
    )
    metadata.setdefault("title", heading)
    metadata.setdefault("status", "active")
    metadata.setdefault("tags", "")
    metadata.setdefault("related_files", "")
    return metadata


def notes(root: Path) -> dict[str, tuple[str, dict[str, str]]]:
    memory_root = root / "memory"
    found: dict[str, tuple[str, dict[str, str]]] = {}
    for path in sorted(memory_root.rglob("*.md")):
        if path.name.startswith("_template"):
            continue
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            raise ValueError(f"Secret-like content refused: {path.relative_to(root)}")
        source = path.relative_to(root).as_posix()
        found[source] = (text, parse_note(text, source))
    return found


def write_manifest(path: Path, namespace: str, indexed: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"namespace": namespace, "sources": indexed},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def sync(root: Path, *, rebuild: bool = False) -> dict[str, int | str]:
    namespace = repo_namespace(root)
    database, manifest = cache_paths(namespace)
    connection = connect(database)
    if rebuild:
        connection.execute("DELETE FROM chunks WHERE namespace = ?", (namespace,))
    existing = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT source, content_hash FROM chunks WHERE namespace = ?", (namespace,)
        )
    }
    current = notes(root)
    inserted = updated = unchanged = 0
    hashes: dict[str, str] = {}
    for source, (text, metadata) in current.items():
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        hashes[source] = content_hash
        if existing.get(source) == content_hash:
            unchanged += 1
            continue
        vector = json.dumps(embed(text), separators=(",", ":"))
        connection.execute(
            """
            INSERT INTO chunks (
                namespace, source, title, status, tags, related_files,
                content_hash, excerpt, vector_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(namespace, source) DO UPDATE SET
                title=excluded.title, status=excluded.status, tags=excluded.tags,
                related_files=excluded.related_files, content_hash=excluded.content_hash,
                excerpt=excluded.excerpt, vector_json=excluded.vector_json
            """,
            (
                namespace,
                source,
                metadata["title"],
                metadata["status"],
                metadata["tags"],
                metadata["related_files"],
                content_hash,
                " ".join(text.split())[:600],
                vector,
            ),
        )
        if source in existing:
            updated += 1
        else:
            inserted += 1
    deleted_sources = set(existing) - set(current)
    for source in deleted_sources:
        connection.execute(
            "DELETE FROM chunks WHERE namespace = ? AND source = ?", (namespace, source)
        )
    connection.commit()
    write_manifest(manifest, namespace, hashes)
    return {
        "namespace": namespace,
        "scanned": len(current),
        "indexed": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "deleted": len(deleted_sources),
    }


def search(root: Path, query: str, top_k: int) -> None:
    sync(root)
    namespace = repo_namespace(root)
    database, _ = cache_paths(namespace)
    connection = connect(database)
    query_vector = embed(query)
    query_terms = set(tokenize(query))
    matches: list[tuple[float, sqlite3.Row | tuple[object, ...]]] = []
    rows = connection.execute(
        """
        SELECT source, title, status, tags, related_files, excerpt, vector_json
        FROM chunks WHERE namespace = ? AND status NOT IN ('deprecated', 'superseded')
        """,
        (namespace,),
    )
    for row in rows:
        vector_score = cosine(query_vector, json.loads(str(row[6])))
        lexical = len(query_terms & set(tokenize(f"{row[1]} {row[3]} {row[5]}")))
        matches.append((vector_score + min(lexical * 0.04, 0.24), row))
    for rank, (score, row) in enumerate(sorted(matches, reverse=True)[:top_k], start=1):
        print(f"{rank}. {row[1]}  score={score:.3f}")
        print(f"   source={row[0]}  status={row[2]}  tags={row[3] or '-'}")
        print(f"   related={row[4] or '-'}")
        print(f"   {row[5]}")


def doctor(root: Path) -> int:
    namespace = repo_namespace(root)
    database, manifest = cache_paths(namespace)
    problems: list[str] = []
    memory_root = root / "memory"
    required_paths = (
        memory_root / "README.md",
        *(memory_root / item for item in REQUIRED_CATEGORIES),
    )
    for required in required_paths:
        if not required.exists():
            problems.append(f"missing {required.relative_to(root)}")
    agents = root / "AGENTS.md"
    if not agents.exists() or "<!-- repo-semantic-memory:start -->" not in agents.read_text():
        problems.append("AGENTS.md memory instructions missing")
    if not database.exists():
        problems.append("local SQLite vector cache missing")
    if not manifest.exists():
        problems.append("index manifest missing")
    else:
        indexed_sources = json.loads(manifest.read_text(encoding="utf-8")).get("sources", {})
        source_hashes = {
            source: hashlib.sha256(text.encode()).hexdigest()
            for source, (text, _) in notes(root).items()
        }
        if indexed_sources != source_hashes:
            problems.append("local index manifest is stale; run memory-sync")
    stale: list[str] = []
    for source, (_, metadata) in notes(root).items():
        related = [item.strip() for item in metadata["related_files"].split(",") if item.strip()]
        missing = [item for item in related if not (root / item).exists()]
        if missing:
            stale.append(f"{source}: missing related files {', '.join(missing)}")
    print(f"repo={root}")
    print(f"namespace={namespace}")
    print(f"database={database}")
    print(f"manifest={manifest}")
    print(f"notes={len(notes(root))}")
    print(f"stale_notes={len(stale)}")
    for item in stale:
        print(f"STALE {item}")
    for item in problems:
        print(f"ERROR {item}")
    print("status=healthy" if not problems else "status=unhealthy")
    return 0 if not problems else 1


def add_note(root: Path, args: argparse.Namespace) -> None:
    category = args.category
    if category not in REQUIRED_CATEGORIES:
        raise ValueError(f"Unknown category: {category}")
    slug = re.sub(r"[^a-z0-9-]+", "-", args.slug.casefold()).strip("-")
    target = root / "memory" / category / f"{slug}.md"
    if target.exists():
        raise ValueError(f"Memory note already exists: {target.relative_to(root)}")
    content = (
        "---\n"
        f'title: "{args.title}"\n'
        f"date: {date.today().isoformat()}\n"
        "status: active\n"
        f"tags: {args.tags}\n"
        f"related_files: {args.related_files}\n"
        "---\n\n"
        f"# {args.title}\n\n"
        "## Summary\n\n"
        f"{args.summary}\n\n"
        "## Details\n\n"
        "Add durable context here. Do not include secrets or raw logs.\n"
    )
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        raise ValueError("Secret-like content refused")
    target.write_text(content, encoding="utf-8")
    print(target.relative_to(root))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("sync")
    commands.add_parser("reindex")
    query = commands.add_parser("search")
    query.add_argument("query")
    query.add_argument("--top-k", type=int, default=5)
    commands.add_parser("doctor")
    add = commands.add_parser("add")
    add.add_argument("--category", required=True)
    add.add_argument("--slug", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--summary", required=True)
    add.add_argument("--tags", default="")
    add.add_argument("--related-files", default="")
    return root


def main() -> int:
    args = parser().parse_args()
    root = repo_root()
    if args.command == "add":
        add_note(root, args)
        return 0
    if args.command in {"init", "sync", "reindex"}:
        result = sync(root, rebuild=args.command == "reindex")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "search":
        search(root, args.query, args.top_k)
        return 0
    if args.command == "doctor":
        return doctor(root)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
