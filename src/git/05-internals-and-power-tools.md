# Part 5 — Git Internals and Power Tools

Advanced Git troubleshooting is much easier once you understand what Git actually stores. This Part opens up the object database, then covers the power tools that rely on it: bisect, blame, worktrees, submodules and subtrees, sparse checkout and partial clone for monorepos, Git LFS, and performance tuning for large repositories.

## 5.1 The Object Database

Git is, at its core, a **content-addressable key-value store**. You give it content; it returns a key (the hash); the content can later be retrieved by that key. Everything else (branches, commits, history) is built on top.

There are four object types:

| Type | Represents | Contains |
|---|---|---|
| `blob` | File contents | Raw bytes only. No filename, no permissions |
| `tree` | A directory | List of entries: mode, type, hash, name |
| `commit` | A snapshot plus metadata | Tree hash, parent hash(es), author, committer, message |
| `tag` | An annotated tag | Target object hash, type, tag name, tagger, message, optional signature |

```text
   commit 9f2c1ab
   +--------------------------+
   | tree    0d8a474          |
   | parent  70aa3d1          |
   | author  Dana ...         |
   | message "feat: ..."      |
   +-----------|--------------+
               v
   tree 0d8a474                       tree 5be1c02 (charts/)
   +---------------------------+      +--------------------------+
   | 100644 blob 45b983 README |      | 100644 blob 8ac1d0 a.yaml|
   | 040000 tree 5be1c0 charts | ---> | 100644 blob e69de2 b.yaml|
   +---------------------------+      +--------------------------+
```

### 5.1.1 Hashing content yourself

```bash
echo "hello" | git hash-object --stdin
```

```text
ce013625030ba8dba906f756967f9e9ca394464a
```

The hash is SHA-1 over a header (`blob 6\0`) plus the content. The same content produces the same hash in every repository in the world, which is why identical files are stored once and why Git can tell instantly whether two trees are equal.

### 5.1.2 Exploring with git cat-file

`git cat-file` is the X-ray tool. `-t` prints the type, `-p` pretty-prints the content, `-s` the size.

```bash
git cat-file -p HEAD
```

```text
tree 0d8a474fc67971fb3dd7616e26323d3066442555
parent 70aa3d1f0c5e2b4a9d8e7f6a5b4c3d2e1f0a9b8c
author Dana Ops <dana@example.com> 1728720000 +0000
committer Dana Ops <dana@example.com> 1728720000 +0000

feat(helm): add PDB to ingress chart
```

```bash
git cat-file -p HEAD^{tree}
```

```text
100644 blob 45b983be36b73c0788dc9cbcb76cbb80fc7bb057	README.md
040000 tree 5be1c02a7d3e9f8b6c4a2d1e0f9a8b7c6d5e4f3a	charts
```

```bash
git cat-file -p 45b983be
git cat-file -t v1.0.0
git cat-file -p v1.0.0
```

```text
tag
object 53c9f9cf26fed7ba52c02ac15b9cd4d3037e566a
type commit
tag v1.0.0
tagger Dana Ops <dana@example.com> 1728720000 +0000

Release 1.0.0
```

Note that the annotated tag is its own object pointing at a commit, whereas a lightweight tag is just a ref.

### 5.1.3 Why this matters

- **A commit hash covers everything.** Commit -> tree -> blobs, plus parent hashes. That is a Merkle tree, so one commit hash cryptographically pins the whole history. It is also why rebasing or amending changes hashes.
- **Renames are not stored.** A tree entry is just a name pointing at a blob; Git infers renames by comparing blobs.
- **Empty directories cannot be committed.** A tree only exists if it contains entries, which is why people commit `.gitkeep` files.
- **File mode matters.** `100755` vs `100644` is part of the tree; `chmod +x` on a script is a real change.

| Mode | Meaning |
|---|---|
| `100644` | Regular file |
| `100755` | Executable file |
| `120000` | Symbolic link |
| `040000` | Tree (directory) |
| `160000` | Gitlink (submodule commit) |

### 5.1.4 Plumbing vs porcelain

Commands like `commit`, `log`, `switch` are **porcelain**: friendly, output may change between versions. Commands like `cat-file`, `hash-object`, `rev-parse`, `update-ref`, `for-each-ref`, `rev-list` are **plumbing**: stable, designed for scripts. In CI scripts, prefer plumbing or porcelain with stable flags (`--porcelain`, `--format`).

```bash
git rev-parse HEAD                    # full hash of HEAD
git rev-parse --abbrev-ref HEAD       # current branch name
git rev-parse --show-toplevel         # repo root
git status --porcelain=v2             # stable machine-readable status
git for-each-ref --format='%(refname:short) %(committerdate:relative)' refs/heads/
git rev-list --count main             # number of commits
```

> **Interview:** "SHA-1 is broken; is Git insecure?" Git uses a hardened SHA-1 implementation that detects known collision attacks, and Git supports SHA-256 repositories (`git init --object-format=sha256`), though hosting support is still limited. Commit and tag signing (Part 6) adds identity on top of integrity.

## 5.2 Refs, Packfiles and Garbage Collection

### 5.2.1 Refs

A ref is a name for a hash.

| Ref | Location |
|---|---|
| Branches | `refs/heads/<name>` |
| Remote-tracking | `refs/remotes/<remote>/<name>` |
| Tags | `refs/tags/<name>` |
| Stash | `refs/stash` |
| Notes | `refs/notes/commits` |
| PR heads (GitHub) | `refs/pull/<n>/head` on the server |
| MR heads (GitLab) | `refs/merge-requests/<n>/head` on the server |
| Symbolic | `HEAD` -> `ref: refs/heads/main` |

Loose refs are individual files; `git pack-refs` consolidates them into `.git/packed-refs`. Newer Git (2.45+) also offers the **reftable** backend (`git init --ref-format=reftable`), which scales far better for repositories with hundreds of thousands of refs.

```bash
git update-ref refs/heads/rescue 3c1d2e9    # plumbing: create a branch
git symbolic-ref HEAD                        # refs/heads/main
```

Fetching a pull request locally uses those server-side refs:

```bash
git fetch origin pull/123/head:pr-123
git switch pr-123
```

### 5.2.2 Loose objects and packfiles

New objects are first written as individual zlib-compressed **loose objects** in `.git/objects/ab/cdef...`. Periodically, Git packs them into a **packfile** (`.pack`) with an index (`.idx`). Inside a pack, similar objects are stored as deltas against each other, which is how a repo with thousands of versions of a file stays small.

```bash
git count-objects -vH
```

```text
count: 0
size: 0 bytes
in-pack: 48213
packs: 1
size-pack: 61.20 MiB
prune-packable: 0
garbage: 0
size-garbage: 0 bytes
```

```bash
git verify-pack -v .git/objects/pack/pack-*.idx | sort -k3 -n | tail -5   # largest objects
```

### 5.2.3 Garbage collection and maintenance

Objects not reachable from any ref or reflog are eventually pruned by `git gc`. Defaults: reflog entries expire after 90 days, unreachable ones after 30 days, and unreachable loose objects are pruned after 2 weeks.

```bash
git gc                      # pack, prune, pack refs
git gc --aggressive         # slower, tighter deltas; rarely needed
git prune --dry-run
git maintenance start       # schedule background maintenance (prefetch, commit-graph, loose-objects, incremental-repack)
```

`git maintenance start` registers the repository with the OS scheduler (launchd, systemd timers, cron or Windows Task Scheduler) and runs incremental tasks hourly and daily, keeping large repositories fast without long `gc` pauses.

## 5.3 git bisect: Binary Search for Bugs

When you know a version that works and a version that is broken, `git bisect` finds the first bad commit in O(log n) steps. 1,000 commits take about 10 tests.

```bash
git bisect start
git bisect bad                    # current commit is broken
git bisect good v1.3.0            # this tag was fine
```

```text
Bisecting: 37 revisions left to test after this (roughly 5 steps)
[a1b2c3d] refactor(api): move retry config
```

Test, then mark:

```bash
git bisect good       # or: git bisect bad, or: git bisect skip (cannot test this one)
```

Eventually:

```text
c2d0a88e1f is the first bad commit
commit c2d0a88e1f
Author: Lee Chen <lee@example.com>
    perf(api): reduce connection pool size
```

```bash
git bisect log > bisect.log      # save the session
git bisect reset                 # return to where you started
```

### 5.3.1 Automated bisect

If a script can detect the bug, let Git run it. Exit code 0 means good, 1 to 127 (except 125) means bad, 125 means skip.

```bash
git bisect start HEAD v1.3.0
git bisect run ./scripts/check-healthz.sh
```

```bash
#!/usr/bin/env bash
# scripts/check-healthz.sh
make build >/dev/null 2>&1 || exit 125     # cannot build: skip
./bin/api --selftest | grep -q "pool ok"
```

You can rename the terms for non-bug searches, for example finding when performance changed:

```bash
git bisect start --term-old=fast --term-new=slow
```

> **Tip:** Bisect works best on a history where every commit builds and passes tests. That is a concrete, practical reason to keep commits atomic and to use `git rebase -i --exec "make test"` before merging.

## 5.4 git blame: Who Changed This Line and Why

```bash
git blame -L 40,60 deploy/values.yaml
```

```text
3a1f2c0e (Dana Ops 2025-03-02 10:14:22 +0000 40) resources:
c2d0a88e (Lee Chen 2025-05-19 16:02:51 +0000 41)   limits:
c2d0a88e (Lee Chen 2025-05-19 16:02:51 +0000 42)     memory: 256Mi
```

| Option | Purpose |
|---|---|
| `-L 40,60` / `-L :funcname` | Limit to lines or a function |
| `-w` | Ignore whitespace changes |
| `-M` / `-C` / `-C -C -C` | Detect lines moved within a file / copied from other files |
| `--ignore-rev <sha>` | Skip a commit (e.g. mass reformatting) |
| `--ignore-revs-file .git-blame-ignore-revs` | Skip a list of commits |
| `-e` | Show emails |

After a large automatic reformat (prettier, black, terraform fmt), add its hash to `.git-blame-ignore-revs` and configure:

```bash
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

GitHub and GitLab honour this file in their web blame views too.

Blame tells you which commit last touched a line, not who is "at fault". The real value is the commit message and the linked pull request: `git show <sha>` usually answers "why is this memory limit 256Mi?".

## 5.5 Worktrees: Multiple Branches Checked Out at Once

A worktree is an additional working directory attached to the same repository. Each worktree has its own HEAD, index and files, but they share the object database and refs.

```bash
git worktree add ../infra-hotfix -b hotfix/cve origin/main
git worktree add ../infra-review origin/feature/pdb --detach
git worktree list
```

```text
/home/dana/infra          9f2c1ab [main]
/home/dana/infra-hotfix   9f2c1ab [hotfix/cve]
/home/dana/infra-review   3c1d2e9 (detached HEAD)
```

```bash
git worktree remove ../infra-review
git worktree prune             # clean up records of manually deleted worktrees
```

Why use worktrees instead of stash or a second clone?

- Fix a production issue without disturbing a half-finished feature or a running local dev server.
- Review a colleague's PR side by side with your own work.
- Run a long test suite on one branch while coding on another.
- Cheaper than a second clone: no duplicated objects, no second fetch.

> **Note:** A branch can be checked out in only one worktree at a time. Git refuses a second checkout to prevent two working directories from fighting over one branch.

## 5.6 Submodules vs Subtrees

Both embed one repository inside another. They make opposite trade-offs.

### 5.6.1 Submodules

A submodule records a **pointer** (a gitlink, mode `160000`) to a specific commit of another repository, plus its URL in `.gitmodules`.

```bash
git submodule add git@github.com:acme/terraform-modules.git modules/shared
git commit -m "build: add shared terraform modules as submodule"
```

```ini
# .gitmodules
[submodule "modules/shared"]
	path = modules/shared
	url = git@github.com:acme/terraform-modules.git
	branch = main
```

```bash
git clone --recurse-submodules git@github.com:acme/platform-infra.git
git submodule update --init --recursive     # after a normal clone
git submodule update --remote modules/shared # move pointer to latest of tracked branch
git add modules/shared && git commit -m "build: bump shared modules"
git config --global submodule.recurse true   # make switch/pull update submodules
```

Pain points: forgetting `--recurse-submodules` leaves empty directories; a submodule checkout is in detached HEAD by default, so commits made there are easy to lose; CI needs credentials for every submodule repository.

### 5.6.2 Subtrees

`git subtree` copies another repository's content (optionally with its history) into a subdirectory of yours. Consumers see normal files; no special clone steps.

```bash
git subtree add --prefix=modules/shared git@github.com:acme/terraform-modules.git main --squash
git subtree pull --prefix=modules/shared git@github.com:acme/terraform-modules.git main --squash
git subtree push --prefix=modules/shared git@github.com:acme/terraform-modules.git feature/fix
```

### 5.6.3 Comparison

| Aspect | Submodule | Subtree |
|---|---|---|
| What is stored | Pointer to external commit | Actual files (and optionally history) |
| Clone experience | Needs `--recurse-submodules` | Just works |
| Pinning exact version | Explicit, precise | Implicit in merged content |
| Contributing back upstream | Natural (it is a real repo) | Possible with `subtree push`, more awkward |
| Repo size | Small | Grows with embedded content |
| CI complexity | Extra auth and checkout steps | None |
| Best for | Large, independently versioned dependencies | Vendoring small shared code |

> **Tip:** For many DevOps use cases neither is ideal. Terraform modules, Helm charts and Ansible roles are often better consumed through their own registries or versioned references (`source = "git::https://...?ref=v1.4.0"`) than embedded in Git.

## 5.7 Monorepos: Sparse Checkout and Partial Clone

Monorepos hold many services in one repository. They simplify atomic cross-service changes and shared tooling but stress Git with large histories and file counts. Git has several features designed for this.

### 5.7.1 Shallow clone

```bash
git clone --depth 1 https://github.com/acme/monorepo.git
git fetch --deepen 50          # get more history later
git fetch --unshallow          # convert to a full clone
```

Shallow clones truncate **history**. They are ideal for one-off CI builds but break commands that need history (`describe`, `log` across ranges, merge-base calculations).

### 5.7.2 Partial clone

Partial clone keeps full history but omits objects until needed.

| Filter | Downloads up front | Fetches on demand |
|---|---|---|
| `--filter=blob:none` | All commits and trees | File contents (blobs) when checked out |
| `--filter=tree:0` | Commits only | Trees and blobs |
| `--filter=blob:limit=1m` | Everything except blobs over 1 MB | Large blobs |

```bash
git clone --filter=blob:none git@github.com:acme/monorepo.git
```

`blob:none` (a "blobless clone") is usually the best default for developers on large repos: `git log` and `git merge-base` work fully offline, and only the files you actually check out are downloaded. `tree:0` ("treeless") suits throwaway CI builds that need history but few files.

### 5.7.3 Sparse checkout

Sparse checkout limits which files appear in your working directory. Cone mode (the default) works on directories and is fast.

```bash
git clone --filter=blob:none --sparse git@github.com:acme/monorepo.git
cd monorepo
git sparse-checkout set services/payments libs/common deploy/payments
git sparse-checkout add services/auth
git sparse-checkout list
git sparse-checkout disable
```

Combined, partial clone plus sparse checkout means you download only the history metadata plus the files of the directories you work on. On a multi-gigabyte monorepo this can turn a 20-minute clone into under a minute.

### 5.7.4 Scalar

`scalar` ships with Git and configures a repository with the recommended large-repo settings (partial clone, sparse checkout, commit-graph, multi-pack index, background maintenance, file system monitor):

```bash
scalar clone https://github.com/acme/monorepo.git
scalar register            # apply to an existing clone
```

## 5.8 Large Files: Git LFS

Git stores every version of every file forever, and each clone downloads all of it. Committing a 500 MB model or ISO a few times makes the repository unusable. Git LFS (Large File Storage) replaces large files in Git with small pointer files and stores the real content on an LFS server.

```bash
git lfs install                        # once per machine: sets up filters and hooks
git lfs track "*.tar.gz" "*.onnx" "assets/**"
git add .gitattributes
git add model.onnx
git commit -m "feat: add inference model"
git push
```

`.gitattributes` gets:

```text
*.tar.gz filter=lfs diff=lfs merge=lfs -text
*.onnx filter=lfs diff=lfs merge=lfs -text
```

What Git actually stores is a pointer:

```text
version https://git-lfs.github.com/spec/v1
oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393
size 524288000
```

| Command | Purpose |
|---|---|
| `git lfs ls-files` | List LFS-managed files |
| `git lfs pull` | Download LFS content for current checkout |
| `git lfs fetch --all` | Download all LFS objects (for mirroring) |
| `git lfs migrate import --include="*.zip" --everything` | Rewrite history to move existing files into LFS |
| `git lfs prune` | Delete old local LFS copies |
| `GIT_LFS_SKIP_SMUDGE=1 git clone ...` | Clone without downloading LFS content |

> **Warning:** LFS is not a substitute for an artifact repository. Build outputs, container images and packages belong in Artifactory, Nexus, a container registry or S3, not in Git. Also, hosting plans meter LFS storage and bandwidth; CI pipelines that pull LFS on every run can burn through quotas.

## 5.9 Performance for Big Repositories

| Symptom | Remedy |
|---|---|
| `git status` slow on many files | `git config core.fsmonitor true` (built-in FS monitor on macOS/Windows) and `core.untrackedCache true` |
| `git log --graph` / merge-base slow | `git commit-graph write --reachable` or `fetch.writeCommitGraph true` |
| Many packfiles | `git maintenance start`, or `git repack -d --write-midx` |
| Clone is huge | Partial clone, sparse checkout, shallow in CI |
| Huge objects in history | Find with `git rev-list --objects --all` plus `cat-file --batch-check`; move to LFS or remove with filter-repo |
| Thousands of refs | `git pack-refs --all`, reftable backend, prune stale branches |
| Slow fetch in CI | Reuse caches, `--depth`, `--filter`, fetch only needed refs |

Finding the biggest blobs in history:

```bash
git rev-list --objects --all \
  | git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' \
  | awk '$1 == "blob"' | sort -k3 -n -r | head -10
```

```text
blob 8f3e1a... 214958080 vendor/old-installer.iso
blob 1c22b7... 52428800 data/dump.sql
blob 77ad0e... 10485760 assets/video.mp4
```

Useful diagnostics:

```bash
GIT_TRACE=1 git status                 # what Git is executing
GIT_TRACE_PERFORMANCE=1 git status     # timings
GIT_TRACE_PACKET=1 git fetch           # protocol-level debugging
GIT_CURL_VERBOSE=1 git fetch           # HTTPS debugging
GIT_SSH_COMMAND="ssh -vvv" git fetch   # SSH debugging
```

## 5.10 Lab: Inside the Machine

**Goal:** inspect objects by hand, find a regression with automated bisect, and use worktrees and sparse checkout.

1. Build a repository with history:

   ```bash
   cd /tmp && rm -rf lab5 && mkdir lab5 && cd lab5 && git init
   for i in $(seq 1 30); do echo "pool_size=$((i < 18 ? 10 : 2))" > config.ini; echo "$i" > build.txt; git add .; git commit -qm "chore: build $i"; done
   git log --oneline | head -3
   ```

2. Walk the object graph:

   ```bash
   git cat-file -t HEAD
   git cat-file -p HEAD
   git cat-file -p HEAD^{tree}
   git cat-file -p HEAD:config.ini
   echo "pool_size=2" | git hash-object --stdin    # compare with the blob hash in the tree
   ```

3. Bisect automatically. A good build has `pool_size=10`:

   ```bash
   git bisect start HEAD HEAD~29
   git bisect run sh -c 'grep -q "pool_size=10" config.ini'
   git bisect reset
   ```

   Confirm the reported first bad commit is "build 18".

4. Pack and inspect:

   ```bash
   git count-objects -vH
   git gc
   git count-objects -vH
   ls .git/objects/pack
   ```

5. Worktrees:

   ```bash
   git worktree add ../lab5-hotfix -b hotfix/pool HEAD~20
   cat ../lab5-hotfix/config.ini
   git worktree list
   git worktree remove ../lab5-hotfix
   ```

6. Sparse checkout:

   ```bash
   mkdir -p services/a services/b && echo a > services/a/x && echo b > services/b/y
   git add . && git commit -qm "feat: services"
   git sparse-checkout set services/a
   ls services                       # only a
   git sparse-checkout disable
   ```

7. Optional: install Git LFS, track `*.bin`, commit a 5 MB random file (`head -c 5000000 /dev/urandom > big.bin`) and run `git cat-file -p HEAD:big.bin` to see the pointer.

## 5.11 Key Takeaways

- Git stores four object types: blobs (content), trees (directories), commits (snapshot + metadata) and annotated tags, each named by the hash of its content.
- A commit hash pins the entire history (a Merkle tree); this is why rewriting changes hashes.
- Refs are names for hashes; packfiles delta-compress objects; `git maintenance` keeps large repos healthy.
- `git bisect run` finds regressions automatically in logarithmic time, if commits are atomic and buildable.
- `git blame` with `-w`, `-C` and `.git-blame-ignore-revs` leads you to the commit and PR that explain a line.
- Worktrees give you several branches checked out at once from one repository.
- Submodules pin external commits precisely but complicate clones; subtrees vendor files simply.
- For monorepos, combine blobless partial clone with cone-mode sparse checkout, or use `scalar`.
- Use Git LFS for large binaries that must be versioned, and artifact repositories for build outputs.
