# Part 4 — Rewriting History, Undoing and Recovery

Git lets you rewrite local history freely and makes it hard to lose committed work permanently. This Part covers rebase in depth, cherry-pick, the three undo tools (restore, reset, revert), the reflog safety net, stash, and tags with semantic versioning.

The golden rule to keep in mind throughout:

> **Warning:** Do not rewrite history that other people have based work on. Rebasing, amending or resetting a branch that is already pushed and shared forces everyone else to repair their clones. Rewriting your own unshared feature branch is fine and encouraged.

## 4.1 Rebase

`git rebase <base>` takes the commits on your current branch that are not in `<base>`, and replays them one by one on top of `<base>`.

```text
Before (on feature):                 After git rebase main:

  A---B---E---F  main                  A---B---E---F  main
       \                                            \
        C---D  feature                               C'---D'  feature
```

`C'` and `D'` contain the same changes as `C` and `D` but are **new commits** with new hashes, because their parent changed. The originals still exist (reachable through the reflog) until garbage collection.

```bash
git switch feature
git rebase main
```

```text
Successfully rebased and updated refs/heads/feature.
```

If a replayed commit conflicts, Git stops:

```bash
# fix files
git add <files>
git rebase --continue
# or skip this commit entirely
git rebase --skip
# or give up and restore the original branch
git rebase --abort
```

### 4.1.1 Why rebase?

- **Linear history**: no merge bubbles from "sync with main" merges. `git log` reads like a story.
- **Clean pull requests**: your branch sits on top of current main, so reviewers see only your changes and CI tests what will actually land.
- **Easier bisect and revert**: one linear sequence of meaningful commits.

### 4.1.2 Interactive rebase

Interactive rebase is your editing suite for local history: reorder, squash, reword, split or drop commits before you share them.

```bash
git rebase -i HEAD~4        # edit the last four commits
git rebase -i main          # edit everything since branching from main
```

The editor opens with a todo list (oldest first):

```text
pick 3a1f2c0 feat(helm): add PDB template
pick 7b9e114 wip
pick c2d0a88 fix typo
pick e41f6d3 feat(helm): expose PDB minAvailable in values

# Commands:
# p, pick <commit> = use commit
# r, reword <commit> = use commit, but edit the commit message
# e, edit <commit> = use commit, but stop for amending
# s, squash <commit> = use commit, but meld into previous commit
# f, fixup [-C | -c] <commit> = like "squash" but keep only the previous
#                    commit's log message
# x, exec <command> = run command (the rest of the line) using shell
# b, break = stop here (continue rebase later with 'git rebase --continue')
# d, drop <commit> = remove commit
```

Edit it to:

```text
pick 3a1f2c0 feat(helm): add PDB template
fixup 7b9e114 wip
fixup c2d0a88 fix typo
pick e41f6d3 feat(helm): expose PDB minAvailable in values
```

Save and close. The result is two meaningful commits instead of four.

| Action | Use |
|---|---|
| `reword` | Fix a commit message |
| `squash` | Combine with previous, edit combined message |
| `fixup` | Combine with previous, discard this message |
| `edit` | Stop to amend or split the commit |
| `drop` | Delete the commit |
| `exec` | Run a command after a commit, e.g. tests |
| reorder lines | Reorder commits |

**Splitting a commit**: mark it `edit`, then when the rebase stops:

```bash
git reset HEAD~1            # undo the commit, keep changes unstaged
git add -p                  # stage first logical part
git commit -m "refactor: extract helper"
git add -A
git commit -m "feat: use helper in deploy script"
git rebase --continue
```

**Test every commit** so bisect works later:

```bash
git rebase -i --exec "make test" main
```

### 4.1.3 Autosquash

Instead of remembering which commit a fix belongs to, record it at commit time:

```bash
git commit --fixup=3a1f2c0          # creates "fixup! feat(helm): add PDB template"
git commit --squash=3a1f2c0         # same, but lets you edit the message
git commit --fixup=reword:3a1f2c0   # only change the message of 3a1f2c0
git rebase -i --autosquash main
```

With `--autosquash`, Git moves each `fixup!` commit directly under its target and marks it `fixup` in the todo list. Make it the default:

```bash
git config --global rebase.autoSquash true
```

This is the cleanest way to address code review comments: push fixup commits so reviewers see exactly what changed, then autosquash before merging.

### 4.1.4 rebase --onto

`--onto` transplants a range of commits onto a new base. The form is:

```text
git rebase --onto <newbase> <upstream> <branch>
```

"Take the commits in `<branch>` that are not in `<upstream>`, and replay them onto `<newbase>`."

Classic case: you branched `feature-b` off `feature-a`, but `feature-b` should be based on `main`:

```text
Before:                               After git rebase --onto main feature-a feature-b:

  A---B  main                           A---B  main
       \                                     \
        C---D  feature-a                      C---D  feature-a
             \                                 \
              E---F  feature-b                  (E'---F' now on B)

                                        A---B---E'---F'  feature-b
```

Another case: drop commits from the middle of a branch.

```bash
# remove commits C and D (HEAD~4..HEAD~2) from the current branch
git rebase --onto HEAD~4 HEAD~2
```

### 4.1.5 Stacked branches: --update-refs

When you maintain a stack of dependent branches (`part-1` <- `part-2` <- `part-3`), rebasing the top one used to leave the lower branches pointing at the old commits. Since Git 2.38:

```bash
git switch part-3
git rebase --update-refs main
```

Git moves every branch that points into the rebased range. Set `rebase.updateRefs=true` to make it the default.

## 4.2 Merge vs Rebase: The Trade-offs

| Aspect | Merge | Rebase |
|---|---|---|
| History shape | True, non-linear; shows when work was integrated | Linear; looks as if work happened sequentially |
| Commit hashes | Preserved | Rewritten for replayed commits |
| Safe on shared branches | Yes | No (requires force push) |
| Conflict resolution | Once, in the merge commit | Possibly once per replayed commit (mitigated by rerere) |
| Traceability of "which PR brought this" | Merge commit groups it | Lost unless you use merge commits at the end |
| Bisect and log readability | Harder with many merge bubbles | Easier |
| Reverting a whole feature | `git revert -m 1 <merge>` | Revert each commit, or squash on merge |

A pragmatic team policy that gets the best of both:

1. **Rebase** your own feature branch on main while developing (`git pull --rebase`, `git rebase main`).
2. **Clean up** with interactive rebase before review or before merge.
3. **Integrate** into main through the platform using either a merge commit (`--no-ff`) or squash merge, never by rewriting main.

> **Interview:** "When would you not rebase?" When the branch is shared and others have built on it; when you need an exact record of when integration happened (some regulated environments); or when a long-lived branch would require resolving the same conflict across dozens of replayed commits, where a single merge is simpler.

## 4.3 Cherry-pick

`git cherry-pick` applies the change introduced by an existing commit as a new commit on the current branch. It is how you backport a fix to a release branch without merging everything else from main.

```text
  A---B---C---F---G  main           (F is a security fix)
       \
        D---E  release/2.4

  git switch release/2.4
  git cherry-pick F

        D---E---F'  release/2.4
```

```bash
git switch release/2.4
git cherry-pick -x 4e7d0c2          # -x appends "(cherry picked from commit ...)"
git cherry-pick A..B                # range: commits after A through B
git cherry-pick --no-commit 4e7d0c2 # apply changes, let me edit before committing
git cherry-pick -m 1 <merge-sha>    # cherry-pick a merge, relative to its first parent
git cherry-pick --continue | --skip | --abort
```

> **Tip:** Always use `-x` when backporting to release branches. The trailer records where the fix came from, so auditors and future engineers can trace it, and tooling can detect which fixes are already backported.

Downsides: the cherry-picked commit is a duplicate with a different hash. If the branches are later merged, Git usually handles identical changes cleanly, but heavy cherry-picking between long-lived branches is a sign the branching strategy needs attention (Part 6).

## 4.4 Undoing Things: restore, reset, revert

These three are often confused. The key questions are "which tree do I want to change?" and "has this been shared?".

| Command | Changes history? | Safe on shared branches? | Typical use |
|---|---|---|---|
| `git restore` | No | Yes | Discard or unstage file changes |
| `git reset` | Yes, moves the branch pointer | No | Undo local commits, reshape local history |
| `git revert` | No, adds a new commit | Yes | Undo a pushed commit publicly |

### 4.4.1 git reset: soft, mixed, hard

`git reset <commit>` moves the current branch to `<commit>`. The mode decides what happens to the index and working directory.

| Mode | Branch (HEAD) | Index | Working dir | Result |
|---|---|---|---|---|
| `--soft` | Moved | Unchanged | Unchanged | Undone commits' changes are staged |
| `--mixed` (default) | Moved | Reset to commit | Unchanged | Changes are unstaged but on disk |
| `--hard` | Moved | Reset to commit | Reset to commit | Changes are gone (from the working tree) |
| `--keep` | Moved | Reset | Reset, but aborts if local edits would be lost | Safer alternative to hard |

```text
Start:   A---B---C---D  main (HEAD)

git reset --soft B    ->  A---B  main;  index and files still contain C+D changes (staged)
git reset --mixed B   ->  A---B  main;  files contain C+D changes (unstaged)
git reset --hard B    ->  A---B  main;  files match B exactly
```

Common recipes:

```bash
git reset --soft HEAD~3 && git commit -m "feat: one clean commit"   # squash last 3
git reset HEAD~1                    # undo last commit, keep changes to re-stage
git reset --hard origin/main        # make my branch exactly match the remote
git reset -p HEAD file              # unstage selected hunks
```

> **Warning:** `git reset --hard` discards uncommitted changes permanently. Committed work it "removes" is still recoverable through the reflog, but edits that were never committed or stashed are not. Run `git status` and `git stash` first if in doubt.

### 4.4.2 git revert

`git revert` creates a new commit that applies the inverse of an existing commit. History is preserved, so it is the correct way to undo something already on a shared branch, such as a bad deploy to `main`.

```bash
git revert 9f2c1ab
git revert --no-edit HEAD
git revert HEAD~3..HEAD             # revert a range (creates one revert per commit)
git revert --no-commit HEAD~3..HEAD && git commit -m "revert: roll back release 2.4 changes"
```

Reverting a merge commit requires telling Git which parent is the mainline:

```bash
git revert -m 1 4e7d0c2             # undo everything the merged branch brought in
```

> **Warning:** After reverting a merge, re-merging the same branch later will not bring the changes back, because Git sees those commits as already merged. To re-land, revert the revert (`git revert <revert-sha>`) and then merge any new fixes.

### 4.4.3 Choosing the right tool

```text
Is the change committed?
 |-- No  -> git restore (discard) / git restore --staged (unstage) / git stash (park)
 |-- Yes -> Has it been pushed to a shared branch?
            |-- No  -> git reset, git commit --amend, git rebase -i
            |-- Yes -> git revert
```

## 4.5 The Reflog: Your Safety Net

Every time a branch tip or HEAD moves (commit, reset, rebase, switch, merge, pull), Git records the old and new positions in the **reflog**. It is local to your clone and by default keeps entries for 90 days (30 days for unreachable commits).

```bash
git reflog
```

```text
e41f6d3 (HEAD -> feature) HEAD@{0}: rebase (finish): returning to refs/heads/feature
e41f6d3 HEAD@{1}: rebase (pick): feat(helm): expose PDB minAvailable
3a1f2c0 HEAD@{2}: rebase (start): checkout main
b77c9d2 HEAD@{3}: commit: feat(helm): expose PDB minAvailable
5d12aa0 HEAD@{4}: commit: wip
3c1d2e9 HEAD@{5}: checkout: moving from main to feature
```

```bash
git reflog show feature            # reflog for one branch
git log -g --oneline               # reflog as a log
git show HEAD@{3}
git diff HEAD@{1} HEAD
```

### 4.5.1 Recovery recipes

**Undo a bad rebase or reset:**

```bash
git reflog                          # find the entry before the operation, e.g. HEAD@{3}
git reset --hard HEAD@{3}
```

Git also writes `ORIG_HEAD` before risky operations (rebase, reset, merge), so `git reset --hard ORIG_HEAD` often undoes the last one directly.

**Recover a deleted branch:**

```bash
git branch -D old-spike
```

```text
Deleted branch old-spike (was a77e010).
```

```bash
git branch old-spike a77e010        # hash from the message or from git reflog
```

**Find commits not referenced by any reflog** (e.g. a dropped stash):

```bash
git fsck --lost-found --no-reflogs
git fsck --unreachable | grep commit
```

> **Note:** The reflog is per clone and is not pushed. If you rewrote history on a laptop that has since been wiped, the server and teammates' clones (and their reflogs) are your fallback. Hosting platforms also keep push event logs and may restore force-pushed branches via support or API.

## 4.6 Stash

`git stash` saves uncommitted changes (staged and unstaged) on a stack and cleans your working directory, so you can switch context quickly.

```bash
git stash push -m "wip: ingress tuning"   # save tracked changes
git stash -u                              # include untracked files
git stash -a                              # include ignored files too
git stash push -p                         # stash selected hunks
git stash push -- values.yaml             # stash a single path
git stash list
git stash show -p stash@{1}
git stash pop                             # apply latest and drop it
git stash apply stash@{1}                 # apply, keep in list
git stash drop stash@{1}
git stash branch fix/ingress stash@{0}    # new branch from where the stash was made, then apply
git stash clear                           # delete all (careful)
```

```text
stash@{0}: On feature/pdb: wip: ingress tuning
stash@{1}: WIP on main: 9f2c1ab ci: cache Go modules
```

Use stash for short interruptions ("prod is on fire, I need main now"). For anything longer than a day, commit to a WIP branch instead; stashes are easy to forget and are not pushed.

> **Tip:** `git stash branch` is the cleanest fix when `stash pop` conflicts because the code moved on. It recreates the original context.

## 4.7 Tags, Semantic Versioning and Releases

### 4.7.1 Lightweight vs annotated tags

A tag is a fixed name for a specific commit. Unlike branches, tags do not move.

| Type | Stores | Use |
|---|---|---|
| Lightweight | Just a ref pointing at a commit | Private or temporary markers |
| Annotated | A tag object with tagger, date, message, optional signature | Releases (always) |

```bash
git tag v1.4.0                                    # lightweight
git tag -a v1.4.0 -m "Release 1.4.0"              # annotated
git tag -s v1.4.0 -m "Release 1.4.0"              # signed annotated
git tag -a v1.3.1 4e7d0c2 -m "Hotfix release"     # tag an older commit
git tag -l "v1.*"                                 # list
git show v1.4.0
git push origin v1.4.0                            # push one tag
git push --follow-tags                            # push annotated tags reachable from pushed commits
git tag -d v1.4.0 && git push origin --delete v1.4.0   # delete locally and remotely
git describe --tags                               # e.g. v1.4.0-3-g9f2c1ab
```

`git describe` output `v1.4.0-3-g9f2c1ab` means "three commits after v1.4.0, at commit 9f2c1ab", which is ideal for embedding build versions:

```bash
VERSION=$(git describe --tags --always --dirty)
docker build -t registry.acme.io/api:"$VERSION" .
```

> **Warning:** Never move or reuse a published release tag. Consumers, caches, Go module proxies and artifact registries assume tags are immutable. If v1.4.0 is broken, release v1.4.1.

### 4.7.2 Semantic Versioning

SemVer uses `MAJOR.MINOR.PATCH`:

| Part | Increment when | Example |
|---|---|---|
| MAJOR | Incompatible API change | 1.9.3 -> 2.0.0 |
| MINOR | Backward-compatible new functionality | 1.9.3 -> 1.10.0 |
| PATCH | Backward-compatible bug fix | 1.9.3 -> 1.9.4 |
| Pre-release | Unstable preview | 2.0.0-rc.1, 2.0.0-beta.2 |
| Build metadata | Ignored for precedence | 2.0.0+build.417 |

Versions below 1.0.0 signal that anything may change. Combined with Conventional Commits (Part 2), the next version can be computed from commit messages: any `!` or `BREAKING CHANGE` means major, any `feat` means minor, otherwise `fix`/`perf` means patch.

### 4.7.3 Automating releases

Tools such as semantic-release, release-please and git-cliff read commit history since the last tag, compute the next version, generate a changelog, create the tag and publish a platform release. A typical pipeline:

```text
merge to main
   -> CI parses commits since last tag (feat, fix, BREAKING CHANGE)
   -> computes v1.5.0, updates CHANGELOG.md
   -> creates annotated tag v1.5.0 and GitHub/GitLab release
   -> tag push triggers the build-and-publish pipeline (Part 7)
```

## 4.8 Lab: Rewrite, Break and Recover

**Goal:** clean up a messy branch with interactive rebase and autosquash, backport with cherry-pick, then deliberately destroy work and recover it.

1. Set up:

   ```bash
   cd /tmp && rm -rf lab4 && mkdir lab4 && cd lab4 && git init
   echo "base" > app.txt && git add . && git commit -m "chore: init"
   git switch -c feature/pdb
   echo "pdb template" > pdb.yaml && git add . && git commit -m "feat(helm): add PDB template"
   echo "values" > values.yaml && git add . && git commit -m "feat(helm): expose PDB values"
   ```

2. Make a fixup for the first feature commit and autosquash:

   ```bash
   echo "pdb template v2" > pdb.yaml
   git commit -a --fixup=HEAD~1        # targets "add PDB template"
   git log --oneline
   git rebase -i --autosquash main     # inspect the todo list, then save it unchanged
   git log --oneline                   # two clean commits
   ```

3. Backport: create a release branch and cherry-pick only the template commit:

   ```bash
   git switch -c release/1.0 main
   git cherry-pick -x feature/pdb~1
   git log -1
   ```

4. Tag the release and describe it:

   ```bash
   git tag -a v1.0.0 -m "Release 1.0.0"
   echo "fix" >> app.txt && git commit -am "fix: patch"
   git describe --tags
   ```

5. Destroy and recover:

   ```bash
   git switch feature/pdb
   git reset --hard main               # "lose" both feature commits
   git log --oneline                   # gone
   git reflog -n 5
   git reset --hard HEAD@{1}           # back
   git log --oneline
   ```

6. Delete a branch and resurrect it:

   ```bash
   git switch main
   git branch -D feature/pdb           # note the hash in the output
   git branch feature/pdb <hash>
   ```

7. Revert publicly: on main, make a commit, then `git revert --no-edit HEAD` and inspect `git log -p -2`.

8. Practise stash: edit `app.txt`, run `git stash -u -m "wip"`, switch branches, come back, `git stash pop`.

## 4.9 Key Takeaways

- Rebase replays commits onto a new base, producing new hashes; use it on your own branches, never on shared ones.
- Interactive rebase, `--fixup` plus `--autosquash`, `--onto` and `--update-refs` give you precise control over local history.
- Rebase while developing, merge (or squash merge) into main through the platform.
- `cherry-pick -x` is the standard backport tool; heavy cherry-picking signals a branching problem.
- `restore` changes files, `reset` moves branches (soft/mixed/hard), `revert` undoes publicly with a new commit.
- The reflog and `ORIG_HEAD` make almost any committed work recoverable for weeks; uncommitted work has no such safety net.
- Stash is for short interruptions; commit WIP branches for anything longer.
- Release with annotated, immutable tags following SemVer, and automate version bumps from Conventional Commits.
