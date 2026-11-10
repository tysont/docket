"""The privacy scan's commit-metadata rule: an author or committer email must be a
reserved or no-reply address, or one the maintainer listed outside the repository."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "privacy_scan.py"
spec = importlib.util.spec_from_file_location("privacy_scan", SCRIPT)
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

# Built at run time: a literal address outside the reserved domains would fail the scan of this file.
PERSONAL = "dev" + "@" + "mailbox" + ".test"


def repo_with_commit(tmp_path: Path, email: str) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": email}
    for argv in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(argv, cwd=root, env=env, check=True, capture_output=True)
    return root


def test_a_personal_commit_email_is_a_finding_unless_listed(tmp_path):
    root = repo_with_commit(tmp_path, PERSONAL)
    findings: list[str] = []
    scan.scan_commits(root, "HEAD", [], findings, allowed=set())
    assert len(findings) == 2 and all("email" in f and PERSONAL not in f for f in findings)  # masked
    listed: list[str] = []
    scan.scan_commits(root, "HEAD", [], listed, allowed={PERSONAL})
    assert listed == []


def test_a_no_reply_commit_email_needs_no_listing(tmp_path):
    root = repo_with_commit(tmp_path, "1+dev@users.noreply.github.com")
    findings: list[str] = []
    scan.scan_commits(root, "HEAD", [], findings, allowed=set())
    assert findings == []


def test_the_list_comes_from_the_environment_or_a_file_outside_the_repository(tmp_path):
    assert scan.commit_emails({"DOCKET_COMMIT_EMAILS": f" {PERSONAL.upper()} , other@example.org"}, tmp_path) == {
        PERSONAL, "other@example.org"}
    assert scan.commit_emails({}, tmp_path) == set()
    cfg = tmp_path / ".config" / "docket"
    cfg.mkdir(parents=True)
    (cfg / "commit-emails.txt").write_text(f"# published address\n{PERSONAL}  # main\n\n", encoding="utf-8")
    assert scan.commit_emails({}, tmp_path) == {PERSONAL}


def test_the_list_never_excuses_an_address_in_a_file():
    findings: list[str] = []
    scan.scan_text("notes.md", f"write to {PERSONAL}", [], findings)
    assert len(findings) == 1 and "email" in findings[0]
