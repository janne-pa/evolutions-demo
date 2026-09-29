import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_git_excludes_private_local_files_but_keeps_public_templates(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required to verify publication exclusions")
    command = [git, f"--git-dir={tmp_path / 'git'}", f"--work-tree={ROOT}"]
    subprocess.run([*command, "init", "--quiet"], check=True, capture_output=True)
    private = {
        ".env",
        ".env.local",
        ".env.production",
        ".venv/pyvenv.cfg",
        "venv/pyvenv.cfg",
        "infra/demo.local.bicepparam",
        "settings.local.json",
        "scripts/deploy.local.ps1",
        "artifacts/run/input.jsonl",
        "artifacts/run/results.jsonl",
        "artifacts/run/01_summary.md",
        "artifacts/run/02_input.jsonl",
        "artifacts/run/02_chat_turns.jsonl",
        "artifacts/run/03_results.jsonl",
        "artifacts/run/03_judge_results.jsonl",
        "artifacts/run/04_summary.json",
        "artifacts/run/05_run-manifest.json",
        "artifacts/run/06_raw-log-records.json",
        "artifacts/run/07_foundry-output.jsonl",
        "artifacts/run/technical/04_summary.json",
        "artifacts/run/technical/05_run-manifest.json",
        "artifacts/run/technical/06_raw-log-records.json",
        "artifacts/run/technical/07_foundry-output.jsonl",
        "reviews/review.md",
        "IMPLEMENTATION-PLAN.md",
        ".azure/config.json",
        "demo/__pycache__/settings.pyc",
        ".mypy_cache/data.json",
        "evolutions_demo.egg-info/PKG-INFO",
        "dist/demo.whl",
        "build/output.txt",
        "debug.log",
        "identity.pem",
        "identity.key",
        "identity.pfx",
        "identity.p12",
    }
    public = {
        ".env.example",
        ".gitignore",
        ".gitattributes",
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "requirements.lock.txt",
        "infra/demo.bicepparam",
        "infra/main.bicep",
        "demo/settings.py",
        "demo/prompts/chat-en-v2.txt",
        "scripts/deploy.ps1",
        "tests/test_publication.py",
    }
    result = subprocess.run(
        [*command, "check-ignore", "--no-index", "--stdin", "-z"],
        input=("\0".join(sorted(private | public)) + "\0").encode(),
        capture_output=True,
        check=True,
        cwd=ROOT,
    )
    assert set(result.stdout.decode().rstrip("\0").split("\0")) == private
