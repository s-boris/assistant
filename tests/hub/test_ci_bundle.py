"""hubkit.ci bundles no history: the trees of base and HEAD on the seed the runner keeps in its mirror; the first
bundle (no seed) carries the whole tree, later ones only what is new since the seed (ciworker fetches each into
<work>/mirror.git and checks out through alternates; a cold mirror says "seed missing")."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hubkit import ci


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    git("add", "-A", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", name, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def fetch_checkout(mirror: Path, data: bytes, commit_id: str, work: Path) -> subprocess.CompletedProcess:
    b = work / "x.bundle"
    b.write_bytes(data)
    r = git("-C", str(mirror), "fetch", "-q", str(b), "+refs/*:refs/*", cwd=work)
    if r.returncode:
        return r
    repo = work / "repo"
    git("init", "-q", str(repo), cwd=work)
    (repo / ".git/objects/info/alternates").write_text(str(mirror / "objects") + "\n")
    return git("-C", str(repo), "checkout", "-q", "--detach", commit_id, cwd=work)


def test_seed_chain_is_incremental_and_a_cold_mirror_says_so(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    base = commit(repo, "big.txt", "".join(f"line {i} {i * 7919 % 10007}\n" for i in range(20000)))
    head = commit(repo, "a.txt", "a\n")
    full, h1, b1 = ci.flat(repo, head, base)
    mirror = tmp_path / "mirror.git"
    git("init", "-q", "--bare", str(mirror), cwd=tmp_path)
    (tmp_path / "w1").mkdir()
    assert fetch_checkout(mirror, full, h1, tmp_path / "w1").returncode == 0
    assert (
        git("rev-parse", "HEAD^{tree}", cwd=tmp_path / "w1/repo").stdout
        == git("rev-parse", "HEAD^{tree}", cwd=repo).stdout
    )
    git("update-ref", ci.SEED_REF, b1, cwd=repo)  # what submit does once the hub took the job
    head2 = commit(repo, "b.txt", "b\n")
    inc, h2, b2 = ci.flat(repo, head2, head)
    assert len(inc) * 10 < len(full) and ci.flat(repo, head2, head)[1] == h2  # small; deterministic ids
    (tmp_path / "w2").mkdir()
    assert fetch_checkout(mirror, inc, h2, tmp_path / "w2").returncode == 0
    assert git("diff", "--name-only", f"{b2}..{h2}", cwd=tmp_path / "w2/repo").stdout.split() == ["b.txt"]
    cold = tmp_path / "cold.git"
    git("init", "-q", "--bare", str(cold), cwd=tmp_path)
    (tmp_path / "w3").mkdir()
    r = fetch_checkout(cold, inc, h2, tmp_path / "w3")
    assert r.returncode != 0 and ("prerequisite" in r.stderr or "missing" in r.stderr.lower())
    ci.reset_seed(repo)
    assert git("rev-parse", "--verify", "-q", ci.SEED_REF, cwd=repo).returncode != 0
    assert not git("for-each-ref", "refs/ci/", cwd=repo).stdout  # the job refs are gone
