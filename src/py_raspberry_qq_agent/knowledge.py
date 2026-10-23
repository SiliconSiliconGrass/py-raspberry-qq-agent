from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal


class KnowledgeBase:
    def __init__(
        self,
        knowledge_root: Path,
        notebook_root: Path,
        *,
        max_file_bytes: int = 2_000_000,
        max_results: int = 50,
        max_read_lines: int = 400,
    ) -> None:
        self._roots: dict[str, Path] = {
            "knowledge": knowledge_root.resolve(),
            "notebook": notebook_root.resolve(),
        }
        self._max_file_bytes = max_file_bytes
        self._max_results = max_results
        self._max_read_lines = max_read_lines

    def list_files(
        self,
        scope: Literal["all", "knowledge", "notebook"] = "all",
        prefix: str = "",
    ) -> list[dict[str, object]]:
        roots = self._selected_roots(scope)
        output: list[dict[str, object]] = []
        for scope_name, root in roots:
            root.mkdir(parents=True, exist_ok=True)
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                if prefix and not relative.startswith(prefix):
                    continue
                output.append(
                    {
                        "scope": scope_name,
                        "path": relative,
                        "size": path.stat().st_size,
                    }
                )
        return output

    def read_lines(
        self,
        scope: Literal["knowledge", "notebook"],
        path: str,
        start_line: int = 1,
        end_line: int = 80,
    ) -> dict[str, object]:
        file_path = self._resolve(scope, path)
        if start_line < 1 or end_line < start_line:
            raise ValueError("line range must satisfy 1 <= start_line <= end_line")
        if end_line - start_line > self._max_read_lines:
            raise ValueError(
                f"a single read is limited to {self._max_read_lines} lines"
            )
        self._check_size(file_path)
        lines = file_path.read_text(encoding="utf-8").splitlines()
        selected = lines[start_line - 1 : end_line]
        return {
            "scope": scope,
            "path": path,
            "start_line": start_line,
            "end_line": min(end_line, len(lines)),
            "total_lines": len(lines),
            "lines": selected,
        }

    def search(
        self,
        query: str,
        *,
        scope: Literal["all", "knowledge", "notebook"] = "all",
        path: str = "",
        max_results: int | None = None,
    ) -> list[dict[str, object]]:
        needle = query.strip()
        if not needle:
            raise ValueError("search query cannot be empty")
        limit = min(max_results or 20, self._max_results)
        matches: list[dict[str, object]] = []

        for scope_name, root in self._selected_roots(scope):
            candidates = [self._resolve(scope_name, path)] if path else sorted(root.rglob("*"))
            for file_path in candidates:
                if not file_path.is_file() or file_path.is_symlink():
                    continue
                if not path and file_path.suffix.lower() in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".zip",
                    ".db",
                    ".exe",
                    ".dll",
                }:
                    continue
                self._check_size(file_path)
                try:
                    lines = file_path.read_text(encoding="utf-8").splitlines()
                except UnicodeDecodeError:
                    continue
                relative = file_path.relative_to(root).as_posix()
                for line_number, line in enumerate(lines, 1):
                    if needle.casefold() not in line.casefold():
                        continue
                    matches.append(
                        {
                            "scope": scope_name,
                            "path": relative,
                            "line": line_number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= limit:
                        return matches
        return matches

    def write(
        self,
        path: str,
        content: str,
        *,
        mode: Literal["replace", "append"] = "replace",
    ) -> dict[str, object]:
        destination = self._resolve("notebook", path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._check_destination(destination, content)
        if mode == "append" and destination.exists():
            with destination.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                if content and not content.endswith("\n"):
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        elif mode in {"replace", "append"}:
            fd, temporary = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        else:
            raise ValueError("write mode must be replace or append")
        return {
            "scope": "notebook",
            "path": path,
            "size": destination.stat().st_size,
            "mode": mode,
        }

    def delete(self, path: str) -> dict[str, object]:
        destination = self._resolve("notebook", path)
        if not destination.exists():
            return {"deleted": False, "scope": "notebook", "path": path}
        if not destination.is_file():
            raise ValueError("only notebook files can be deleted")
        destination.unlink()
        return {"deleted": True, "scope": "notebook", "path": path}

    def _selected_roots(
        self, scope: Literal["all", "knowledge", "notebook"]
    ) -> list[tuple[str, Path]]:
        if scope == "all":
            return list(self._roots.items())
        if scope not in self._roots:
            raise ValueError("scope must be all, knowledge, or notebook")
        return [(scope, self._roots[scope])]

    def _resolve(
        self,
        scope: Literal["knowledge", "notebook"],
        raw_path: str,
    ) -> Path:
        if scope not in self._roots:
            raise ValueError("scope must be knowledge or notebook")
        if not raw_path.strip() or Path(raw_path).is_absolute():
            raise ValueError("path must be a non-empty relative path")
        root = self._roots[scope]
        candidate = (root / raw_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("path escapes the configured root") from exc
        return candidate

    def _check_size(self, path: Path) -> None:
        if path.stat().st_size > self._max_file_bytes:
            raise ValueError("file is too large for this tool")

    def _check_destination(self, path: Path, content: str) -> None:
        if path.exists() and path.is_dir():
            raise ValueError("destination is a directory")
        encoded_size = len(content.encode("utf-8"))
        if encoded_size > self._max_file_bytes:
            raise ValueError("content is too large")
