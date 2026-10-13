# Part 8 — Troubleshooting Common Disasters

Sooner or later you will be the person someone messages with "I think I broke Git". This Part is a field guide. Each scenario lists the symptom, why it happened, and the fix. The general method is always the same:

1. **Stop.** Do not run more commands hoping something works; each one can overwrite evidence.
2. **Look.** `git status`, `git log --oneline --graph --all -20`, `git reflog -20`.
3. **Back up.** Create a rescue branch (`git branch rescue-$(date +%s)`) or copy the whole directory, including `.git`, before risky repairs.
4. **Fix** with the smallest reversible step.

> **Tip:** Committed work is almost never truly lost while the reflog lasts (90 days by default). Uncommitted work is the fragile part. That is the strongest argument for committing early and often on local branches.

## 8.1 Detached HEAD

**Symptom:**

```text
You are in 'detached HEAD' state. You can look around, make experimental
changes and commit them, and you can discard any commits you make in this
state without impacting any branches by switching back to a branch.
```

**Why:** HEAD points directly at a commit instead of at a branch. It happens when you check out a tag, a commit hash, a remote-tracking branch (`git switch --detach origin/main`), or during a rebase or bisect. Commits you make are not on any branch.

**Fix:** if you made commits you want to keep, give them a branch before switching away:

```bash
git switch -c fix/from-detached
```

If you already switched away, Git printed the hash:

```text
Warning: you are leaving 1 commit behind, not connected to
any of your branches:

  955b38d three
```

```bash
git branch fix/recovered 955b38d
# or find it later:
git reflog | grep -m1 "three"
```

## 8.2 Committed to the Wrong Branch

**Symptom:** you committed to `main` locally but meant to use a feature branch. Not pushed yet.

```bash
git branch feature/oops          # new branch at the current commit (keeps the work)
git reset --hard origin/main     # move main back to the remote state
git switch feature/oops
```

If the commits belong on an existing branch:

```bash
git switch feature/existing
git cherry-pick main~2..main     # copy the last two commits
git switch main
git reset --hard HEAD~2
```

If the commits were already pushed to `main` (and main is protected or shared), use `git revert` on main instead of reset.

## 8.3 Pushed a Secret

**Symptom:** a password, token or private key is in a pushed commit.

**Why this is urgent:** removing it in a new commit leaves it in history; anyone with a clone or fork has it, and automated scanners harvest public repos within minutes.

**Fix, in order:**

1. Revoke and rotate the credential now. This is the fix; everything else is cleanup.
2. Check audit logs for misuse since the push time.
3. Remove the secret from the code and load it from a secret manager.
4. If policy requires, rewrite history with `git filter-repo --replace-text` or `--invert-paths`, force push all refs, and require every collaborator to re-clone (Part 7).
5. Ask the platform to purge cached views and pull request refs if needed.
6. Add a pre-commit secret scanner and enable push protection so it does not recur.

> **Warning:** Making the repository private after a public leak does not help; the secret was already exposed. Rotation is non-negotiable.

## 8.4 Someone Force-Pushed Over Work

**Symptom:** `main` or a shared branch suddenly lost commits after a teammate's `git push --force`.

```text
 + 7c3e1a0...a1b2c3d main -> main (forced update)
```

**Why:** a plain force push replaces the remote branch with the pusher's version, discarding whatever was there.

**Fix:** the lost commits exist in at least one place: the clone of whoever pushed them, the reflog of anyone who fetched before the force push, or the platform's event log.

```bash
# On any clone that fetched the old state:
git reflog show origin/main
```

```text
a1b2c3d refs/remotes/origin/main@{0}: fetch: forced-update
7c3e1a0 refs/remotes/origin/main@{1}: fetch: fast-forward
```

```bash
git switch -c restore-main 7c3e1a0
git log --oneline a1b2c3d..7c3e1a0      # the commits that were overwritten
# Decide: restore the old tip entirely, or merge both histories
git switch main
git merge restore-main                  # keep both
git push                                # normal push, no force needed if it fast-forwards
```

If nobody has a local copy, GitHub's activity view and events API record the previous SHA for pushes, and the objects usually remain on the server for a while; GitLab keeps similar push audit events. Contact support quickly, before server-side garbage collection.

**Prevent:** block force pushes on protected branches, and use `--force-with-lease --force-if-includes` on personal branches.

## 8.5 Merge Gone Wrong

### 8.5.1 Merge still in progress, conflicts look bad

```bash
git merge --abort          # back to pre-merge state
git rebase --abort         # same for a rebase
git cherry-pick --abort
```

### 8.5.2 Merge committed locally, not pushed

```bash
git reset --hard ORIG_HEAD   # ORIG_HEAD is set before the merge
# or
git reset --hard HEAD~1      # first parent of the merge commit is your previous tip
```

### 8.5.3 Merge already pushed to a shared branch

Revert the merge, keeping the mainline (first parent):

```bash
git log --oneline --graph -5
git revert -m 1 4e7d0c2
git push
```

```text
       4e7d0c2 (bad merge)
  ---o---M---R  main          R = revert of M, relative to parent 1
        /
   o---o  feature
```

Remember: to merge `feature` again later, first revert the revert (`git revert R`), otherwise Git considers its commits already integrated.

### 8.5.4 Conflict resolved incorrectly in a merge

Find the merge, inspect what the resolution did compared to each parent:

```bash
git show --remerge-diff 4e7d0c2   # Git 2.36+: shows how the resolution differs from an automatic merge
git diff 4e7d0c2^1 4e7d0c2 -- path/file
```

Fix with a normal follow-up commit on the branch; rewriting a pushed merge is rarely worth it.

## 8.6 Lost Commits After Reset or Rebase

```bash
git reflog -20
git reset --hard HEAD@{5}        # or the hash from the reflog
```

If the branch itself was deleted:

```bash
git reflog --all | grep feature/pdb
git branch feature/pdb <sha>
```

Last resort, when no reflog references remain:

```bash
git fsck --lost-found --no-reflogs
git show <sha-of-dangling-commit>
```

## 8.7 Lost a Stash

A dropped or cleared stash is an unreachable commit. Find it:

```bash
git fsck --no-reflogs --unreachable | awk '/commit/ {print $3}' \
  | xargs -n1 git log -1 --format="%h %ci %s" | grep -i "WIP on\|On "
git stash apply <sha>
```

## 8.8 Accidentally Committed a Huge File

Not pushed yet:

```bash
git rm --cached big.iso
git commit --amend --no-edit      # if it was the last commit
# or for older commits: git rebase -i and edit, or filter-repo on a local clone
```

Pushed: the server will usually reject files over the platform limit (100 MB on GitHub). If it was accepted, remove with `git filter-repo --strip-blobs-bigger-than 50M` or migrate to LFS with `git lfs migrate import --include="*.iso" --everything`, then force push and coordinate.

## 8.9 "Diverged" Branches and Rejected Pushes

```text
hint: You have divergent branches and need to specify how to reconcile them.
fatal: Need to specify how to reconcile divergent branches.
```

**Why:** both your local branch and the remote have new commits, and `pull` does not know whether you want merge, rebase or fast-forward only.

```bash
git config --global pull.rebase true    # one-time preference
git pull --rebase                       # or explicitly for this pull
```

## 8.10 Other Frequent Errors

| Error | Cause | Fix |
|---|---|---|
| `fatal: refusing to merge unrelated histories` | Two repos with no common commit (e.g. created a README on the platform and also `git init` locally) | `git pull --allow-unrelated-histories` once, if intended |
| `fatal: detected dubious ownership in repository` | Repo owned by another user (common in containers and CI) | `git config --global --add safe.directory /path/to/repo` |
| `error: cannot lock ref 'refs/remotes/origin/x'` | Stale or conflicting ref (e.g. `x` and `x/y`) | `git remote prune origin` or `git fetch --prune` |
| `fatal: index.lock exists` | Crashed or concurrent Git process | Ensure no Git process runs, then `rm .git/index.lock` |
| `Permission denied (publickey)` | SSH key not loaded or not registered | `ssh-add`, `ssh -T git@github.com`, check `GIT_SSH_COMMAND="ssh -v"` |
| `remote: Support for password authentication was removed` | Using a password over HTTPS | Use a personal access token, credential manager or SSH |
| `error: RPC failed; curl 56` / `early EOF` | Large push or flaky network/proxy | Push in smaller batches, check proxy, use SSH, `http.postBuffer` only as last resort |
| `Your branch is ahead of 'origin/main' by N commits` but push shows nothing | Upstream misconfigured | `git branch -vv`, `git push -u origin HEAD` |
| `fatal: You are not currently on a branch` | Detached HEAD | `git switch -c new-branch` |
| Line ending churn, whole file shows changed | CRLF/LF conversion | Add `.gitattributes` with `* text=auto eol=lf`, run `git add --renormalize .` |
| File mode churn (`old mode 100644 new mode 100755`) | Filesystem without exec bits | `git config core.fileMode false` on that clone |
| Submodule directory is empty | Not initialized | `git submodule update --init --recursive` |

## 8.11 Lab: Disaster Drills

**Goal:** practise each recovery until it is routine. Use a bare repo as the remote.

1. Setup:

   ```bash
   cd /tmp && rm -rf lab8 && mkdir lab8 && cd lab8
   git init --bare origin.git
   git clone origin.git dev && cd dev
   for i in 1 2 3; do echo $i > f$i; git add .; git commit -qm "feat: f$i"; done
   git push -u origin main
   ```

2. **Detached HEAD drill:** `git switch --detach HEAD~1`, commit something, `git switch main`, then recover the commit using the hash Git printed.

3. **Wrong branch drill:** commit to main, move it to `feature/x` with `git branch` plus `git reset --hard origin/main`.

4. **Force-push drill:** in a second clone (`git clone origin.git other`), add a commit and push. In `dev`, without fetching, make a different commit and `git push --force`. Then in `other`, find the lost commit with `git reflog show origin/main` after a fetch (or from `main`'s own reflog) and restore it with a merge.

5. **Bad merge drill:** create `feature/bad`, merge it with `--no-ff`, push, then `git revert -m 1 HEAD` and push. Re-land the feature by reverting the revert.

6. **Lost stash drill:** create a stash, run `git stash clear`, then recover it using `git fsck`.

7. **Rescue reflex:** before each drill, run `git branch rescue-$(date +%s)` and notice how it makes every mistake trivially reversible.

## 8.12 Key Takeaways

- Stop, look (status, graph, reflog), back up, then fix with the smallest reversible step.
- Detached HEAD is harmless until you leave commits behind; `git switch -c` saves them.
- Pushed secrets require rotation first; history cleanup is secondary and disruptive.
- Force-pushed work survives in other clones' reflogs and platform event logs; restore it with a merge and block force pushes on shared branches.
- Undo unpushed merges with `reset --hard ORIG_HEAD`; undo pushed merges with `revert -m 1`, and remember to revert the revert before re-merging.
- `git reflog` and `git fsck --lost-found` recover almost any committed or stashed work.
- Most cryptic errors (dubious ownership, unrelated histories, index.lock, divergent branches) have short, well-known fixes.
