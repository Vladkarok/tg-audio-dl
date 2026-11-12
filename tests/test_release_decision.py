"""scripts/release_decision.sh: when the yt-dlp updater may tag and deploy."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release_decision.sh"
BOT = ("github-actions[bot]", "github-actions[bot]@users.noreply.github.com")
HUMAN = ("Dev", "dev@example.com")

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="needs git and bash",
)


def _env(author: tuple[str, str] = HUMAN) -> dict[str, str]:
    # Keep the developer's git config out of both the setup and the script.
    name, email = author
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q", "-b", "master")

    def git(self, *args: str, author: tuple[str, str] = HUMAN) -> str:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=self.path,
            env=_env(author),
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def commit(
        self, name: str, content: str, message: str, author: tuple[str, str] = HUMAN
    ) -> None:
        (self.path / name).write_text(content)
        self.git("add", name)
        self.git("commit", "-q", "-m", message, author=author)

    def bot_bump(self, n: int) -> None:
        self.commit("uv.lock", f"lock {n}\n", f"chore: bump yt-dlp a → b{n}", BOT)

    def decide(self) -> dict[str, str]:
        out = subprocess.run(  # noqa: S603
            ["bash", str(SCRIPT)],  # noqa: S607
            cwd=self.path,
            env=_env(),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return dict(line.split("=", 1) for line in out.splitlines())


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path)
    r.commit("uv.lock", "lock 0\n", "initial")
    return r


def test_nothing_since_tag_releases(repo: Repo) -> None:
    repo.git("tag", "v1.2.3")
    d = repo.decide()
    assert d["decision"] == "release"
    assert d["next_tag"] == "v1.2.4"


def test_only_earlier_bot_bumps_since_tag_release(repo: Repo) -> None:
    repo.git("tag", "v1.2.3")
    repo.bot_bump(1)
    d = repo.decide()
    assert d["decision"] == "release"
    assert d["next_tag"] == "v1.2.4"


def test_human_commit_since_tag_holds(repo: Repo) -> None:
    repo.git("tag", "v1.2.3")
    repo.commit("app.py", "x\n", "fix: something")
    repo.bot_bump(1)
    d = repo.decide()
    assert d["decision"] == "hold"
    assert d["last_tag"] == "v1.2.3"
    # Suggested for the person who tags by hand.
    assert d["next_tag"] == "v1.2.4"
    assert d["reason"].startswith("1 commit(s)")


def test_merge_commit_holds(repo: Repo) -> None:
    repo.git("tag", "v1.2.3")
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit("app.py", "x\n", "feat: thing")
    repo.git("checkout", "-q", "master")
    repo.git("merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    assert repo.decide()["decision"] == "hold"


@pytest.mark.parametrize(
    ("files", "message", "author"),
    [
        # Bot-authored, bump subject, but touches more than uv.lock.
        ({"uv.lock": "lock 9\n", "pyproject.toml": "x\n"}, "chore: bump yt-dlp", BOT),
        # Only uv.lock with the bump subject, but written by a person.
        ({"uv.lock": "lock 9\n"}, "chore: bump yt-dlp a → b", HUMAN),
        # Bot-authored uv.lock change that is not a yt-dlp bump.
        ({"uv.lock": "lock 9\n"}, "chore: relock", BOT),
    ],
)
def test_lookalike_bumps_hold(
    repo: Repo, files: dict[str, str], message: str, author: tuple[str, str]
) -> None:
    repo.git("tag", "v1.2.3")
    for name, content in files.items():
        (repo.path / name).write_text(content)
        repo.git("add", name)
    repo.git("commit", "-q", "-m", f"{message} ", author=author)
    assert repo.decide()["decision"] == "hold"


def test_no_tag_holds(repo: Repo) -> None:
    d = repo.decide()
    assert d["decision"] == "hold"
    assert d["last_tag"] == ""
    assert d["next_tag"] == ""


def test_non_semver_tags_are_ignored(repo: Repo) -> None:
    repo.git("tag", "v1.2.3-rc1")
    assert repo.decide()["last_tag"] == ""
    repo.git("tag", "v1.2.2")
    d = repo.decide()
    assert d["decision"] == "release"
    assert d["next_tag"] == "v1.2.3"


def test_newer_tag_off_master_holds(repo: Repo) -> None:
    repo.git("tag", "v1.2.3")
    repo.git("checkout", "-q", "-b", "other")
    repo.commit("app.py", "x\n", "fix: on another branch")
    repo.git("tag", "v1.3.0")
    repo.git("checkout", "-q", "master")
    d = repo.decide()
    assert d["decision"] == "hold"
    assert "v1.3.0" in d["reason"]
    # last_tag+1 would be v1.2.4; suggest past the newest tag instead.
    assert d["next_tag"] == "v1.3.1"


def test_off_master_tag_taking_next_patch_is_skipped(repo: Repo) -> None:
    repo.git("tag", "v1.2.3")
    repo.git("checkout", "-q", "-b", "other")
    repo.commit("app.py", "x\n", "fix: on another branch")
    repo.git("tag", "v1.2.4")
    repo.git("checkout", "-q", "master")
    d = repo.decide()
    assert d["decision"] == "hold"
    assert d["next_tag"] == "v1.2.5"


def test_bot_author_with_human_committer_holds(repo: Repo) -> None:
    # A rebase merge or `git commit --author` keeps the bot as author but
    # records a person (or GitHub) as committer.
    repo.git("tag", "v1.2.3")
    (repo.path / "uv.lock").write_text("lock 9\n")
    repo.git("add", "uv.lock")
    repo.git(
        "commit",
        "-q",
        "-m",
        "chore: bump yt-dlp a → b",
        f"--author={BOT[0]} <{BOT[1]}>",
    )
    assert repo.git("log", "-1", "--format=%ae %ce").split() == [BOT[1], HUMAN[1]]
    assert repo.decide()["decision"] == "hold"


def test_git_failure_never_releases(
    repo: Repo, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # Only bot bumps since the tag, so a silent zero-iteration loop would
    # release. A failing rev-list must abort the script instead.
    repo.git("tag", "v1.2.3")
    repo.bot_bump(1)
    shim = tmp_path_factory.mktemp("shim")
    real_git = shutil.which("git")
    (shim / "git").write_text(
        f'#!/bin/sh\n[ "$1" = rev-list ] && exit 1\nexec "{real_git}" "$@"\n'
    )
    (shim / "git").chmod(0o755)
    env = {**_env(), "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}
    res = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT)],  # noqa: S607
        cwd=repo.path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert "decision=release" not in res.stdout


def test_two_tags_on_one_commit_uses_highest(repo: Repo) -> None:
    repo.git("tag", "v1.9.0")
    repo.git("tag", "v1.9.1")
    d = repo.decide()
    assert d["decision"] == "release"
    assert d["next_tag"] == "v1.9.2"


def test_bump_lookalike_merge_commit_holds(repo: Repo) -> None:
    # A merge whose subject and author look like a bot bump still holds.
    repo.git("tag", "v1.2.3")
    repo.git("checkout", "-q", "-b", "side")
    repo.bot_bump(1)
    repo.git("checkout", "-q", "master")
    repo.git(
        "merge", "-q", "--no-ff", "-m", "chore: bump yt-dlp merged", "side", author=BOT
    )
    assert repo.decide()["decision"] == "hold"


def test_nearest_tag_wins(repo: Repo) -> None:
    repo.git("tag", "v1.2.3")
    repo.commit("app.py", "x\n", "fix: released below")
    repo.git("tag", "v1.3.0")
    repo.bot_bump(1)
    d = repo.decide()
    assert d["decision"] == "release"
    assert d["next_tag"] == "v1.3.1"
