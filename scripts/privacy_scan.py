#!/usr/bin/env python3
"""Scan the repository for data that must not be published.

Checks, in order:
  1. A private denylist kept outside the repository (one literal per line,
     matched case-insensitively on word boundaries). Path: $DOCKET_DENYLIST,
     else ~/.config/docket/denylist.txt. A missing denylist fails the scan
     unless --no-denylist is given.
  2. Generic patterns: email addresses outside reserved example domains,
     credential-shaped tokens, private keys, and absolute home directories.
  3. Commit metadata (author and committer emails) for a revision range.

Exit status 0 when clean, 1 when anything is found. Findings print as
file:line: rule, with the matched text masked so the scan output itself
never republishes a secret.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ALLOWED_EMAIL = re.compile(
    r"@(example\.(com|org|net)|[a-z0-9.-]+\.example|users\.noreply\.github\.com|localhost)$",
    re.I,
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SECRETS = [
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]
HOME_PATH = re.compile(r"/(Users|home)/(?!runner\b|user\b|dana\b|me\b|example\b)[A-Za-z0-9._-]+")
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".lock"}


def mask(s: str) -> str:
    return s[:2] + "*" * max(0, len(s) - 2) if len(s) > 2 else "**"


def load_denylist(path: Path) -> list[re.Pattern]:
    pats = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        term = raw.strip()
        if not term or term.startswith("#"):
            continue
        esc = re.escape(term)
        left = r"(?<![A-Za-z0-9])" if term[0].isalnum() else ""
        right = r"(?![A-Za-z0-9])" if term[-1].isalnum() else ""
        pats.append(re.compile(left + esc + right, re.I))
    return pats


def repo_files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, check=True,
    ).stdout.decode()
    return [root / p for p in out.split("\0") if p and (root / p).is_file()]


def scan_text(label: str, text: str, deny: list[re.Pattern], findings: list[str]) -> None:
    for n, line in enumerate(text.splitlines(), 1):
        for pat in deny:
            for m in pat.finditer(line):
                findings.append(f"{label}:{n}: denylist match '{mask(m.group(0))}'")
        for m in EMAIL.finditer(line):
            if not ALLOWED_EMAIL.search(m.group(0)):
                findings.append(f"{label}:{n}: email '{mask(m.group(0))}'")
        for name, pat in SECRETS:
            for m in pat.finditer(line):
                findings.append(f"{label}:{n}: {name} '{mask(m.group(0))}'")
        for m in HOME_PATH.finditer(line):
            findings.append(f"{label}:{n}: home path '{mask(m.group(0))}'")


def scan_commits(root: Path, rev_range: str, deny: list[re.Pattern], findings: list[str]) -> None:
    fmt = "%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e"
    out = subprocess.run(
        ["git", "log", f"--format={fmt}", rev_range], cwd=root,
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        findings.append(f"commits: cannot read range {rev_range}: {out.stderr.strip()}")
        return
    for rec in out.stdout.split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        sha, an, ae, cn, ce, body = (rec.split("\x1f") + [""] * 6)[:6]
        for who, email in (("author", ae), ("committer", ce)):
            if not ALLOWED_EMAIL.search(email):
                findings.append(f"commit {sha[:8]}: {who} email '{mask(email)}'")
        scan_text(f"commit {sha[:8]} metadata", "\n".join([an, cn, body]), deny, findings)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=".", help="repository root")
    ap.add_argument("--range", dest="rev_range", help="also scan commit metadata in this range, e.g. origin/main..HEAD or HEAD")
    ap.add_argument("--no-denylist", action="store_true", help="skip the private denylist (CI)")
    ap.add_argument("--denylist", help="denylist path override")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    deny: list[re.Pattern] = []
    if not args.no_denylist:
        path = Path(args.denylist or os.environ.get("DOCKET_DENYLIST") or Path.home() / ".config/docket/denylist.txt")
        if not path.is_file():
            print(f"privacy-scan: denylist not found at {path}; pass --no-denylist to skip", file=sys.stderr)
            return 1
        deny = load_denylist(path)

    findings: list[str] = []
    for f in repo_files(root):
        rel = f.relative_to(root).as_posix()
        if rel == "scripts/privacy_scan.py":
            # The scanner's own generic patterns; the denylist still applies.
            scan_text(rel, f.read_text(encoding="utf-8"), deny, findings)
            continue
        scan_text(f"path {rel}", rel, deny, findings)
        if f.suffix.lower() in SKIP_SUFFIX:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scan_text(rel, text, deny, findings)
    if args.rev_range:
        scan_commits(root, args.rev_range, deny, findings)

    for line in findings:
        print(line)
    if findings:
        print(f"privacy-scan: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print("privacy-scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
