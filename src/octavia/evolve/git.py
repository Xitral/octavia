"""A small synchronous wrapper around the git command line (the manager calls it through ``asyncio.to_thread``).

Each build happens in its own worktree on its own branch, and an install is one ``--no-ff`` merge commit,
so undoing an update is a single ``git revert -m 1``.

Git always runs without a shell, a terminal prompt or an editor: an editor opened in a hidden window would
hang the call until its timeout. Every failure becomes a ``GitError`` that names the command and ends with
the tail of git's output, so the journal records why a step failed.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Author = tuple[str, str]  # (name, email)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_LINK_MODES = frozenset({"120000", "160000"})  # a symbolic link, a submodule (gitlink)
# Inherited from a parent git process (a hook, say), these would point every command at another repository.
_LOCATION_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)
_ERROR_TAIL_CHARS = 1500


class GitError(RuntimeError):
    """A git command failed. The message includes the command and the tail of its output."""

    def __init__(self, message: str, *, command: tuple[str, ...] = (), returncode: int | None = None) -> None:
        super().__init__(message)
        self.command = command
        self.returncode = returncode


class MergeConflictError(GitError):
    """The branch conflicts with changes made since it was built. The merge has been aborted."""


@dataclass
class _Completed:
    returncode: int
    stdout: str
    stderr: str


class Git:
    """Git operations on one repository (``repo`` is the top of the main checkout)."""

    def __init__(self, repo: Path, executable: str = "git") -> None:
        self.repo = Path(repo)
        self.executable = executable

    # ------------------------------------------------------------------ running git

    def run(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        timeout: float = 120.0,
        author: Author | None = None,
    ) -> str:
        """``git <args>`` in ``cwd`` (default: the repo); returns stdout.

        Raises GitError when git exits non-zero (unless ``check`` is off), times out or can't be started.
        ``author`` sets both the author and the committer, so AI commits are never attributed to the user.
        """
        result = self._exec(args, cwd=cwd, timeout=timeout, author=author)
        if check and result.returncode != 0:
            raise self._failure(args, result)
        return result.stdout

    def _exec(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: float = 120.0,
        author: Author | None = None,
    ) -> _Completed:
        try:
            proc = subprocess.run(
                [self.executable, *args],
                cwd=cwd or self.repo,
                env=self._env(author),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {_show(args)} timed out after {timeout:g} s", command=args) from exc
        except OSError as exc:
            raise GitError(f"couldn't run git {_show(args)}: {exc}", command=args) from exc
        return _Completed(proc.returncode, _decode(proc.stdout), _decode(proc.stderr))

    def _env(self, author: Author | None) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if key.upper() not in _LOCATION_VARS}
        env.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",  # `git status` mustn't take index.lock while the user's editor uses the repo
            GIT_EDITOR=":",  # git's "no editor"
            GIT_MERGE_AUTOEDIT="no",
            GIT_PAGER="cat",
        )
        if author is not None:
            name, email = author
            env.update(
                GIT_AUTHOR_NAME=name,
                GIT_AUTHOR_EMAIL=email,
                GIT_COMMITTER_NAME=name,
                GIT_COMMITTER_EMAIL=email,
            )
        return env

    @staticmethod
    def _failure(args: tuple[str, ...], result: _Completed) -> GitError:
        tail = _tail(result.stderr) or _tail(result.stdout)
        message = f"git {_show(args)} failed (exit {result.returncode})"
        return GitError(f"{message}: {tail}" if tail else message, command=args, returncode=result.returncode)

    def _best_effort(self, *args: str, cwd: Path | None = None) -> None:
        try:
            self._exec(args, cwd=cwd)
        except GitError:
            pass

    # ------------------------------------------------------------------ repository state

    def available(self) -> bool:
        """Whether the git executable can be run at all."""
        try:
            return self._exec(("--version",), cwd=self.repo if self.repo.is_dir() else Path.cwd()).returncode == 0
        except GitError:
            return False

    def is_repo(self) -> bool:
        """Whether ``repo`` is the top of a git checkout.

        Merely being somewhere inside one isn't enough: if the project sat in, say, a versioned home
        directory, ``git add -A`` would sweep up the user's unrelated files.
        """
        if not self.repo.is_dir():
            return False
        try:
            result = self._exec(("rev-parse", "--show-toplevel"))
        except GitError:
            return False
        top = result.stdout.strip()
        return result.returncode == 0 and bool(top) and _same_path(Path(top), self.repo)

    def has_commits(self) -> bool:
        return self._exec(("rev-parse", "--verify", "--quiet", "HEAD^{commit}")).returncode == 0

    def head(self, cwd: Path | None = None) -> str:
        """The full sha of HEAD in ``cwd`` (the repo, or one of its worktrees)."""
        return self.run("rev-parse", "--verify", "HEAD^{commit}", cwd=cwd).strip()

    def dirty_files(self, cwd: Path | None = None) -> list[str]:
        """Paths with uncommitted changes, untracked files included and ignored ones not (renames: the new path)."""
        out = self.run("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=cwd)
        tokens = out.split("\0")
        paths: list[str] = []
        i = 0
        while i < len(tokens):
            entry = tokens[i]
            i += 1
            if len(entry) < 4:
                continue
            code, path = entry[:2], entry[3:]
            paths.append(path)
            if "R" in code or "C" in code:
                i += 1  # the original path follows as its own field
        return paths

    # ------------------------------------------------------------------ worktrees and branches

    def add_worktree(self, path: Path, branch: str, base: str) -> None:
        """A fresh worktree at ``path`` on a new ``branch`` starting at ``base``.

        A leftover worktree from an interrupted build is removed first and a leftover branch of the same
        name is reset to ``base`` (``-B``), so a retry always starts clean.
        """
        _check_name(branch)
        _check_name(base)
        path = Path(path)
        self.remove_worktree(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run("worktree", "add", "-B", branch, str(path), base)

    def remove_worktree(self, path: Path) -> None:
        """Delete the worktree at ``path`` and forget it; the branch is kept.

        Tolerates a path that isn't (or is no longer) a worktree. Refuses to delete the main checkout, and a
        link where the folder should be. The folder is deleted by Python rather than ``git worktree remove``:
        Git for Windows follows directory junctions inside the worktree and empties their targets, and code
        run by a build's tests could have left one pointing anywhere.
        """
        path = Path(path)
        if _is_within(self.repo, path):
            raise GitError(f"refusing to remove {path}: it contains the main checkout")
        if os.path.lexists(path):
            try:
                shutil.rmtree(path, onexc=_force_remove)
            except OSError as exc:
                raise GitError(f"couldn't delete the worktree folder {path}: {exc}") from exc
        self.run("worktree", "prune")  # drops the registration of any worktree whose folder is gone

    def branch_exists(self, name: str) -> bool:
        _check_name(name)
        return self._exec(("show-ref", "--verify", "--quiet", f"refs/heads/{name}")).returncode == 0

    def delete_branch(self, name: str) -> None:
        """Delete the branch, merged or not. A missing branch is fine."""
        if self.branch_exists(name):
            self.run("branch", "-D", name)

    def is_ancestor(self, a: str, b: str) -> bool:
        """Whether commit ``a`` is ``b`` or one of its ancestors. Raises GitError for an unknown commit."""
        _check_name(a)
        _check_name(b)
        args = ("merge-base", "--is-ancestor", a, b)
        result = self._exec(args)
        if result.returncode not in (0, 1):
            raise self._failure(args, result)
        return result.returncode == 0

    # ------------------------------------------------------------------ commits

    def commit_all(self, cwd: Path, message: str, author: Author) -> str | None:
        """Stage everything in ``cwd`` (ignored files excepted) and commit it; None if there was nothing to commit."""
        self.run("add", "-A", cwd=cwd)
        try:
            self.run("commit", "-q", "-m", message, cwd=cwd, author=author)
        except GitError:
            if self._exec(("diff", "--cached", "--quiet", "--no-ext-diff"), cwd=cwd).returncode == 0:
                return None  # nothing was staged
            raise
        return self.head(cwd)

    def initial_commit(self, message: str) -> str:
        """The repository's first commit, made as the user (their configured git identity).

        Builds branch off a commit, so a project that was never committed needs this once.
        """
        if not self.is_repo():  # `git add -A` inside some enclosing repository would stage all of it
            raise GitError(f"{self.repo} isn't the top of a git repository; run `git init` there first")
        if self.has_commits():
            raise GitError("the repository already has commits")
        missing = [
            key for key in ("user.name", "user.email") if not self._exec(("config", "--get", key)).stdout.strip()
        ]
        if missing:
            raise GitError(
                f"git doesn't know who you are yet ({' and '.join(missing)} not set). Set it once with: "
                'git config --global user.name "Your Name" and git config --global user.email "you@example.com"'
            )
        self.run("add", "-A")
        self.run("commit", "-q", "--allow-empty", "-m", message)
        return self.head()

    # ------------------------------------------------------------------ what a build changed

    def changed_files(self, cwd: Path, base: str) -> list[tuple[str, str]]:
        """``[(status letter, path)]`` between ``base`` and HEAD in ``cwd``.

        A rename is reported under its new path and, as ``("D", old path)``, under its old one too: moving
        a protected file away changes it just as much as editing it.
        """
        _check_name(base)
        tokens = self.run("diff", "--name-status", "-z", "-M", "--no-ext-diff", base, "HEAD", "--", cwd=cwd).split("\0")
        changes: list[tuple[str, str]] = []
        i = 0
        while i < len(tokens):
            status = tokens[i]
            if not status:
                i += 1
                continue
            letter = status[0]
            if letter in "RC":
                old, new = tokens[i + 1], tokens[i + 2]
                changes.append((letter, new))
                if letter == "R":
                    changes.append(("D", old))
                i += 3
            else:
                changes.append((letter, tokens[i + 1]))
                i += 2
        return changes

    def added_symlinks(self, cwd: Path, base: str) -> list[str]:
        """Paths that are symbolic links or submodules at HEAD and weren't identical at ``base``.

        Links can point outside the worktree, and a submodule pulls in code nobody reviewed.
        """
        _check_name(base)
        tokens = self.run("diff", "--raw", "-z", "--no-ext-diff", base, "HEAD", "--", cwd=cwd).split("\0")
        links: list[str] = []
        i = 0
        while i < len(tokens):
            meta = tokens[i]
            if not meta.startswith(":"):
                i += 1
                continue
            fields = meta[1:].split()
            dst_mode, status = fields[1], fields[4]
            renamed = status[:1] in ("R", "C")
            path = tokens[i + 2] if renamed else tokens[i + 1]
            if dst_mode in _LINK_MODES and not status.startswith("D"):
                links.append(path)
            i += 3 if renamed else 2
        return links

    def numstat(self, cwd: Path, base: str) -> tuple[int, int]:
        """Lines added and removed between ``base`` and HEAD (binary files count as 0)."""
        _check_name(base)
        tokens = self.run("diff", "--numstat", "-z", "-M", "--no-ext-diff", base, "HEAD", "--", cwd=cwd).split("\0")
        added = removed = 0
        i = 0
        while i < len(tokens):
            parts = tokens[i].split("\t", 2)
            i += 1
            if len(parts) < 3:
                continue
            added += int(parts[0]) if parts[0].isdigit() else 0
            removed += int(parts[1]) if parts[1].isdigit() else 0
            if not parts[2]:
                i += 2  # a rename: the old and new paths follow as their own fields
        return added, removed

    def diff(self, cwd: Path, base: str, max_chars: int = 200_000) -> str:
        """The patch between ``base`` and HEAD, for review; cut off after ``max_chars``."""
        _check_name(base)
        text = self.run("diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M", base, "HEAD", "--", cwd=cwd)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n[diff truncated: showing {max_chars:,} of {len(text):,} characters]\n"

    def patch(self, base: str, branch: str, max_chars: int = 400_000) -> str | None:
        """What ``branch`` changed since ``base``, as a patch ``apply_3way`` can apply elsewhere (binary files
        included); None when it's longer than ``max_chars``."""
        _check_name(base)
        _check_name(branch)
        text = self.run(
            "diff", "--binary", "--no-color", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/",
            base, branch, "--",
        )  # fmt: skip
        # Text that isn't UTF-8 came back with replacement characters: applied, it would change those bytes.
        return text if len(text) <= max_chars and "\ufffd" not in text else None

    def apply_3way(self, cwd: Path, patch: str) -> list[str]:
        """Apply ``patch`` (from ``patch``) to the checkout at ``cwd``, merging three ways where the files moved on:
        [] when it went on cleanly, else the files it clashes in, left with conflict markers. GitError when it
        couldn't be applied at all (nothing is changed then)."""
        with tempfile.TemporaryDirectory(prefix="octavia-patch-") as folder:
            path = Path(folder) / "change.patch"
            path.write_text(patch, encoding="utf-8", newline="\n")
            try:
                self.run("apply", "--3way", "--whitespace=nowarn", str(path), cwd=cwd)
            except GitError:
                clashes = self._unmerged_files(cwd)
                if not clashes:
                    raise
                return clashes
        return []

    # ------------------------------------------------------------------ installing and undoing

    def merge_no_ff(self, branch: str, message: str, author: Author, cwd: Path | None = None) -> str:
        """Merge ``branch`` into the checkout at ``cwd`` (default: the main one) as one merge commit; returns its
        sha.

        On any failure the merge is aborted, so the checkout is left as it was. A conflict raises
        MergeConflictError (a GitError).
        """
        _check_name(branch)
        try:
            self.run("merge", "--no-ff", "--no-edit", "-m", message, branch, cwd=cwd, author=author)
        except GitError as exc:
            conflicted = self._unmerged_files(cwd)
            self._best_effort("merge", "--abort", cwd=cwd)
            if conflicted:
                raise MergeConflictError(
                    f"merging {branch} hit a conflict in {', '.join(conflicted)}; the merge was aborted",
                    command=exc.command,
                    returncode=exc.returncode,
                ) from exc
            raise
        return self.head(cwd)

    def _unmerged_files(self, cwd: Path | None = None) -> list[str]:
        try:
            result = self._exec(("diff", "--name-only", "--diff-filter=U", "-z"), cwd=cwd)
        except GitError:
            return []
        return [path for path in result.stdout.split("\0") if path]

    def fast_forward(self, commit: str) -> str:
        """Move the main checkout forward to ``commit`` (a descendant of HEAD); returns the new HEAD.

        Refuses (GitError) when HEAD isn't an ancestor of ``commit`` any more, or when uncommitted changes are
        in files the move would change: nothing is touched then.
        """
        _check_name(commit)
        # --no-autostash: a user's merge.autoStash would otherwise stash their edits and reapply them with
        # conflict markers instead of refusing.
        self.run("merge", "--ff-only", "--no-autostash", "--no-edit", commit)
        return self.head()

    def branch_files(self, branch: str) -> list[str]:
        """The files ``branch`` changes since it split off HEAD's history (what merging it would touch)."""
        _check_name(branch)
        out = self.run("diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", f"HEAD...{branch}")
        return [path for path in out.split("\0") if path]

    def revert_merges(self, commits: list[str], author: Author, message: str, cwd: Path | None = None) -> str:
        """Undo several merges (newest first in ``commits``) with one commit in the checkout at ``cwd``; returns
        the new HEAD sha. Meant for a fresh worktree (``on_the_side``): it commits whatever the index holds.

        All or nothing: when any of them can't be reverted, everything is aborted and GitError raised.
        """
        if len(commits) == 1:
            return self.revert_merge(commits[0], author, message, cwd=cwd)
        for commit in commits:
            _check_name(commit)
            if len(self.run("rev-list", "--parents", "-n", "1", commit, cwd=cwd).split()) < 3:
                raise GitError(f"{commit[:12]} isn't a merge")
        try:
            # One sequence: when any of them conflicts, --abort takes the index back to before the first.
            self.run("revert", "--no-commit", "-m", "1", *commits, cwd=cwd, author=author)
            reverted = "\n".join(f"This reverts commit {c}." for c in commits)  # as a single revert records it
            self.run("commit", "-q", "-m", f"{message}\n\n{reverted}", cwd=cwd, author=author)
        except GitError as exc:
            self._best_effort("revert", "--abort", cwd=cwd)
            raise GitError(f"couldn't revert {len(commits)} merges: {exc}", command=exc.command) from exc
        return self.head(cwd)

    def revert_merge(self, commit: str, author: Author, message: str | None = None, cwd: Path | None = None) -> str:
        """Undo ``commit`` (a merge, relative to its first parent, or a plain commit) with a new commit in the
        checkout at ``cwd`` (default: the main one).

        ``message`` replaces the subject line; git's "This reverts commit ..." body is kept for the record.
        On failure the revert is aborted and GitError raised. Returns the new HEAD sha.
        """
        _check_name(commit)
        parents = self.run("rev-list", "--parents", "-n", "1", commit, cwd=cwd).split()[1:]
        mainline = ("-m", "1") if len(parents) > 1 else ()
        try:
            self.run("revert", "--no-edit", *mainline, commit, cwd=cwd, author=author)
        except GitError as exc:
            self._best_effort("revert", "--abort", cwd=cwd)
            raise GitError(
                f"couldn't revert {commit[:12]}: {exc}", command=exc.command, returncode=exc.returncode
            ) from exc
        if message:
            body = self.run("log", "-1", "--format=%b", cwd=cwd).strip()
            full = f"{message}\n\n{body}" if body else message
            # --only: rewrite just the message, never sweeping in something the user staged meanwhile
            self.run("commit", "--amend", "--only", "-q", "-m", full, cwd=cwd, author=author)
        return self.head(cwd)

    def on_the_side(self, worktree: Path, branch: str, work: Callable[[Path], str]) -> str:
        """Do ``work`` (it commits, and returns the commit) in a fresh worktree at HEAD, then move the main
        checkout forward to that commit; returns it.

        The main checkout only changes once all of it worked, and only as a fast-forward: uncommitted work there
        (staged or not) is left as it is unless the result changes the same files, and then nothing happens
        (GitError). The worktree and its branch are removed either way.
        """
        self.add_worktree(worktree, branch, self.head())
        try:
            return self.fast_forward(work(Path(worktree)))
        finally:
            try:
                self.remove_worktree(worktree)
                self.delete_branch(branch)
            except GitError:
                pass  # a leftover is removed by the next start


# ------------------------------------------------------------------ helpers


def _check_name(name: str) -> None:
    """Refs and shas go on the command line as plain arguments; one starting with '-' would be read as an option."""
    if not name or name.startswith("-"):
        raise GitError(f"not a valid branch or commit name: {name!r}")


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _show(args: tuple[str, ...]) -> str:
    """The command for an error message: long arguments (commit messages) shortened, one line."""
    shown = []
    for arg in args:
        arg = arg.replace("\r", " ").replace("\n", " ")
        shown.append(arg if len(arg) <= 60 else arg[:57] + "...")
    return " ".join(shown)


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _ERROR_TAIL_CHARS else "..." + text[-_ERROR_TAIL_CHARS:]


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def _is_within(child: Path, parent: Path) -> bool:
    """Whether ``child`` is ``parent`` or lies inside it (case-insensitively on Windows)."""
    c = os.path.normcase(str(child.resolve()))
    p = os.path.normcase(str(parent.resolve())).rstrip("\\/")
    return c == p or c.startswith(p + os.sep)


def _force_remove(func: Callable[..., Any], path: str, exc: BaseException) -> None:
    """``shutil.rmtree`` error handler: Windows won't delete read-only files (git makes some), so clear the bit.

    Other failures are re-raised. Links are never touched here: chmod would follow them to their target.
    """
    if func not in (os.unlink, os.rmdir, os.remove) or os.path.islink(path) or os.path.isjunction(path):
        raise exc
    os.chmod(path, stat.S_IWRITE)
    func(path)
