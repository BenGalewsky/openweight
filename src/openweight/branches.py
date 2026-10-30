"""Evaluation branches and their workspaces.

Each evaluation works on an orphan branch ``eval/<harness>/<model-slug>`` in a standalone git
repository at ``workspaces/<name>/``. The branch itself records progress: every step commit carries
``Eval-Step`` (and ``Eval-Run-Id``) trailers, and each step is tagged
``eval/<harness>/<model-slug>/step-NN``. After each step the branch and its tags are fetched back
into the main repository so they can be reviewed from there.
"""

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HARNESSES = ("claude", "opencode")

ROOT_COMMIT_SUBJECT = "chore(eval): benchmark context"
ACCESS_DBS = ("IDOT.accdb", "Wetlands_IDOT_BE.accdb")
SCHEMA = "access_db_schema.sql"

# Commits the runner makes itself. Agent commits use the workspace's own identity.
RUNNER_NAME = "openweight"
RUNNER_EMAIL = "openweight@localhost"
AGENT_NAME = "openweight-agent"
AGENT_EMAIL = "openweight-agent@localhost"

STEP_TRAILER = "Eval-Step"
RUN_ID_TRAILER = "Eval-Run-Id"
ASSISTED_BY_TRAILER = "Assisted-by"


class GitError(RuntimeError):
    """A git command failed."""


class WorkspaceError(RuntimeError):
    """A workspace or its inputs are not in the expected state."""


@dataclass(frozen=True)
class Evaluation:
    """One (harness, model, effort) pair and the names derived from it."""

    harness: str
    model: str
    effort: str | None = None

    def __post_init__(self) -> None:
        if self.harness not in HARNESSES:
            raise ValueError(f"unknown harness {self.harness!r} (expected one of {HARNESSES})")
        if not self.model:
            raise ValueError("model must not be empty")

    @property
    def slug(self) -> str:
        """Model part of the branch name, e.g. ``lumen--glm-5.3`` or ``claude-opus-5-5-high``."""
        slug = re.sub(r"[/:]", "--", self.model.lower())
        if self.effort:
            slug += f"-{self.effort.lower()}"
        return slug

    @property
    def branch(self) -> str:
        return f"eval/{self.harness}/{self.slug}"

    @property
    def name(self) -> str:
        """Flat name for the workspace directory and sandbox containers."""
        return f"{self.harness}--{self.slug}"

    @property
    def assisted_by(self) -> str:
        return f"{self.harness}:{self.model}"

    def tag(self, step: int) -> str:
        return f"{self.branch}/step-{step:02d}"


@dataclass(frozen=True)
class StepCommit:
    sha: str
    step: int
    run_id: str | None


@dataclass(frozen=True)
class StepRecord:
    """What `Workspace.record_step` did with the agent's work."""

    sha: str
    tag: str
    agent_commits: int
    agent_committed: bool


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd`` without checking the exit status.

    Repository discovery is limited to ``cwd`` itself, so a workspace whose ``.git`` has gone
    missing can never fall through to the main repository that contains it.
    """
    env = os.environ | {"GIT_CEILING_DIRECTORIES": str(cwd.resolve().parent)}
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env, check=False
    )


def git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` and return stdout, raising `GitError` on failure."""
    result = run_git(cwd, *args)
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd}:\n{result.stderr.strip()}")
    return result.stdout


def repo_root(start: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise WorkspaceError(f"{start} is not inside a git repository")
    return Path(result.stdout.strip())


def parse_trailers(text: str) -> dict[str, list[str]]:
    """Parse ``Key: value`` lines as printed by ``%(trailers:only,unfold)``."""
    trailers: dict[str, list[str]] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            trailers.setdefault(key.strip(), []).append(value.strip())
    return trailers


def has_ref(repo: Path, ref: str) -> bool:
    return run_git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def step_commits(repo: Path, branch: str) -> list[StepCommit]:
    """Commits on ``branch`` with an ``Eval-Step`` trailer, oldest first."""
    if not has_ref(repo, f"refs/heads/{branch}"):
        return []
    out = git(
        repo,
        "log",
        "--reverse",
        "--format=%H%x00%(trailers:only,unfold)%x1e",
        f"refs/heads/{branch}",
    )
    commits = []
    for record in out.split("\x1e"):
        sha, sep, trailers_text = record.strip().partition("\x00")
        if not sep:
            continue
        trailers = parse_trailers(trailers_text)
        for value in trailers.get(STEP_TRAILER, []):
            if not value.isdigit():
                raise WorkspaceError(
                    f"commit {sha[:12]} has a bad {STEP_TRAILER} trailer: {value!r}"
                )
            run_ids = trailers.get(RUN_ID_TRAILER)
            commits.append(StepCommit(sha, int(value), run_ids[-1] if run_ids else None))
    return commits


def next_step(repo: Path, branch: str) -> int:
    """Highest ``Eval-Step`` on the branch + 1 (1 for a branch with no steps yet)."""
    return max((c.step for c in step_commits(repo, branch)), default=0) + 1


def eval_branches(repo: Path) -> list[str]:
    out = git(repo, "for-each-ref", "--format=%(refname:lstrip=2)", "refs/heads/eval/")
    return out.split()


def copy_cow(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` as a copy-on-write clone where the filesystem supports it."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        cmd = ["cp", "-c", str(src), str(dst)]
    elif sys.platform.startswith("linux"):
        cmd = ["cp", "--reflink=auto", str(src), str(dst)]
    else:
        cmd = None
    if cmd is None or subprocess.run(cmd, capture_output=True, check=False).returncode != 0:
        shutil.copyfile(src, dst)


@dataclass(frozen=True)
class Inputs:
    """Benchmark inputs taken from the main repository.

    ``context`` files are committed into the branch's root commit; ``untracked`` files are placed
    in the workspace and excluded from git. Both map workspace-relative paths to source paths.
    """

    context: dict[str, Path]
    untracked: dict[str, Path]

    @classmethod
    def from_repo(cls, repo: Path) -> "Inputs":
        context = {path: repo / path for path in _tracked(repo, "plans")}
        context[f"plans/{SCHEMA}"] = repo / "db" / SCHEMA
        untracked = {name: repo / "db" / name for name in ACCESS_DBS}
        untracked |= {path: repo / path for path in _tracked(repo, "data")}

        missing = [str(src) for src in (context | untracked).values() if not src.is_file()]
        if missing:
            raise WorkspaceError("missing benchmark inputs:\n  " + "\n  ".join(missing))
        if not any(path.startswith("data/") for path in untracked):
            raise WorkspaceError(f"no tracked logger fixtures in {repo / 'data'}")
        return cls(context, untracked)


def _tracked(repo: Path, directory: str) -> list[str]:
    return git(repo, "ls-files", "--", directory).split("\n")[:-1]


class Workspace:
    """The standalone git repository an evaluation works in."""

    def __init__(self, path: Path, evaluation: Evaluation) -> None:
        if not (path / ".git").is_dir():
            raise WorkspaceError(f"{path} is not a workspace (no .git directory)")
        self.path = path
        self.evaluation = evaluation

    def git(self, *args: str) -> str:
        return git(self.path, *args)

    def _runner_git(self, *args: str) -> str:
        return self.git("-c", f"user.name={RUNNER_NAME}", "-c", f"user.email={RUNNER_EMAIL}", *args)

    @property
    def branch(self) -> str:
        return self.evaluation.branch

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def current_branch(self) -> str | None:
        result = run_git(self.path, "symbolic-ref", "--quiet", "--short", "HEAD")
        return result.stdout.strip() if result.returncode == 0 else None

    def is_clean(self) -> bool:
        return self.git("status", "--porcelain", "--untracked-files=all") == ""

    def step_commits(self) -> list[StepCommit]:
        return step_commits(self.path, self.branch)

    def next_step(self) -> int:
        return next_step(self.path, self.branch)

    def tag_sha(self, step: int) -> str | None:
        tag = f"refs/tags/{self.evaluation.tag(step)}"
        if not has_ref(self.path, tag):
            return None
        return self.git("rev-parse", f"{tag}^{{commit}}").strip()

    def place_inputs(self, inputs: Inputs) -> None:
        """Copy the untracked inputs into the workspace (if absent) and exclude them from git."""
        for rel, src in inputs.untracked.items():
            dst = self.path / rel
            if not dst.exists():
                copy_cow(src, dst)
                # Readable by the sandbox's agent user whatever the source's mode was.
                dst.chmod(0o644)
        exclude = self.path / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        existing = exclude.read_text().splitlines() if exclude.exists() else []
        missing = [f"/{rel}" for rel in inputs.untracked if f"/{rel}" not in existing]
        if missing:
            with exclude.open("a") as f:
                f.write("".join(f"{line}\n" for line in ["# openweight inputs", *missing]))

    def ensure_ready(self, step: int) -> None:
        """Check the workspace is clean, on the eval branch, and at the tag of ``step - 1``."""
        if self.current_branch() != self.branch:
            raise WorkspaceError(f"{self.path} is not on {self.branch}")
        if not self.is_clean():
            raise WorkspaceError(f"{self.path} has uncommitted changes")
        if (actual := self.next_step()) != step:
            raise WorkspaceError(f"the next step on {self.branch} is {actual}, not {step}")
        expected = self.tag_sha(step - 1)
        if expected is None:
            raise WorkspaceError(f"no tag {self.evaluation.tag(step - 1)} in {self.path}")
        if self.head() != expected:
            raise WorkspaceError(f"{self.path} is not at {self.evaluation.tag(step - 1)}")

    def reset_to(self, step: int) -> None:
        """Discard everything after ``step``'s tag: commits, changes, and later step tags.

        Ignored files (``node_modules``, the inputs, agent-produced extracts) are kept.
        """
        tag = self.evaluation.tag(step)
        if self.tag_sha(step) is None:
            raise WorkspaceError(f"no tag {tag} in {self.path}")
        if self.current_branch() != self.branch:
            self.git("checkout", "--force", self.branch)
        self.git("reset", "--hard", f"refs/tags/{tag}")
        self.git("clean", "-fd")
        later = [
            name
            for name in self.git("tag", "--list", f"{self.branch}/step-*").split()
            if int(name.rsplit("-", 1)[1]) > step
        ]
        if later:
            self.git("tag", "--delete", *later)

    def record_step(self, step: int, run_id: str | None = None) -> StepRecord:
        """Make ``step``'s work a tagged step commit carrying the eval trailers.

        Uncommitted changes are committed by the runner. Otherwise the agent's last commit is
        amended to add the trailers, or an empty commit is made if the agent did nothing.
        """
        if self.current_branch() != self.branch:
            raise WorkspaceError(
                f"{self.path} is on {self.current_branch() or 'a detached HEAD'}, not {self.branch}"
            )
        base = self.tag_sha(step - 1)
        if base is None:
            raise WorkspaceError(f"no tag {self.evaluation.tag(step - 1)} in {self.path}")
        if run_git(self.path, "merge-base", "--is-ancestor", base, "HEAD").returncode != 0:
            raise WorkspaceError(
                f"{self.branch} no longer contains {self.evaluation.tag(step - 1)}"
            )
        agent_commits = int(self.git("rev-list", "--count", f"{base}..HEAD").strip())

        eval_trailers = [f"{STEP_TRAILER}: {step}"]
        if run_id:
            eval_trailers.append(f"{RUN_ID_TRAILER}: {run_id}")

        if not self.is_clean():
            self.git("add", "--all")
            self._runner_commit(f"chore(eval): uncommitted changes from step {step}", eval_trailers)
        elif agent_commits:
            # Only the eval trailers: whether the agent wrote its own Assisted-by is part of what
            # the evaluation measures.
            self._runner_git(
                "commit",
                "--amend",
                "--no-edit",
                "--no-verify",
                *(f"--trailer={trailer}" for trailer in eval_trailers),
            )
        else:
            self._runner_commit(
                f"chore(eval): no changes from step {step}", eval_trailers, allow_empty=True
            )

        tag = self.evaluation.tag(step)
        self.git("tag", "--force", tag, "HEAD")
        return StepRecord(
            sha=self.head(), tag=tag, agent_commits=agent_commits, agent_committed=agent_commits > 0
        )

    def _runner_commit(self, subject: str, trailers: list[str], allow_empty: bool = False) -> None:
        """Commit the index as the runner, attributing the content to the agent under test."""
        message = "\n".join(
            [subject, "", f"{ASSISTED_BY_TRAILER}: {self.evaluation.assisted_by}", *trailers]
        )
        args = ["--allow-empty"] if allow_empty else []
        self._runner_git("commit", "--quiet", "--no-verify", *args, "-m", message)


def workspace_name(branch: str) -> str:
    """The flat name (`Evaluation.name`) of an ``eval/<harness>/<model-slug>`` branch."""
    prefix, _, rest = branch.partition("/")
    harness, _, slug = rest.partition("/")
    if prefix != "eval" or harness not in HARNESSES or not slug or "/" in slug:
        raise ValueError(f"not an eval branch: {branch!r} (expected eval/<harness>/<model-slug>)")
    return f"{harness}--{slug}"


def workspace_dir(repo: Path, name: str) -> Path:
    return repo / "workspaces" / name


def workspace_path(repo: Path, evaluation: Evaluation) -> Path:
    return workspace_dir(repo, evaluation.name)


def _refspecs(evaluation: Evaluation) -> list[str]:
    return [
        f"+refs/heads/{evaluation.branch}:refs/heads/{evaluation.branch}",
        f"+refs/tags/{evaluation.branch}/*:refs/tags/{evaluation.branch}/*",
    ]


def open_workspace(repo: Path, evaluation: Evaluation) -> Workspace:
    """Open the evaluation's workspace, creating or restoring it as needed.

    - An existing workspace is opened as is (its inputs are re-placed if missing).
    - If the branch exists in the main repository but the workspace does not, the workspace is
      restored from the main repository's branch and step tags.
    - Otherwise a new workspace is created with the orphan branch's root commit, tagged step 0.
    """
    inputs = Inputs.from_repo(repo)
    path = workspace_path(repo, evaluation)
    if path.exists():
        workspace = Workspace(path, evaluation)
        if not has_ref(path, f"refs/heads/{evaluation.branch}"):
            raise WorkspaceError(f"{path} has no branch {evaluation.branch}")
    elif has_ref(repo, f"refs/heads/{evaluation.branch}"):
        workspace = _init(path, evaluation)
        # The fresh repo's unborn HEAD already names the branch, hence --update-head-ok.
        workspace.git(
            "fetch", "--quiet", "--no-tags", "--update-head-ok", str(repo.resolve()),
            *_refspecs(evaluation),
        )  # fmt: skip
        workspace.git("reset", "--quiet", "--hard", f"refs/heads/{evaluation.branch}")
    else:
        workspace = _init(path, evaluation)
        _commit_context(workspace, inputs)
    workspace.place_inputs(inputs)
    return workspace


def _init(path: Path, evaluation: Evaluation) -> Workspace:
    path.mkdir(parents=True)
    git(path, "init", "--quiet", f"--initial-branch={evaluation.branch}")
    for key, value in [
        ("user.name", AGENT_NAME),
        ("user.email", AGENT_EMAIL),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ]:
        git(path, "config", key, value)
    return Workspace(path, evaluation)


def _commit_context(workspace: Workspace, inputs: Inputs) -> None:
    for rel, src in inputs.context.items():
        dst = workspace.path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    workspace.git("add", "--", *inputs.context)
    workspace._runner_git("commit", "--no-verify", "--quiet", "-m", ROOT_COMMIT_SUBJECT)
    workspace.git("tag", workspace.evaluation.tag(0), "HEAD")


def sync_to_main(repo: Path, workspace: Workspace) -> None:
    """Fetch the workspace's eval branch and step tags into the main repository.

    Step tags that no longer exist in the workspace (after a reset) are pruned from the main
    repository too.
    """
    git(
        repo,
        "fetch",
        "--quiet",
        "--no-tags",
        "--prune",
        str(workspace.path.resolve()),
        *_refspecs(workspace.evaluation),
    )
