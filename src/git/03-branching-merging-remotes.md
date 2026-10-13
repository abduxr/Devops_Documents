# Part 3 — Branching, Merging and Remotes

Branches are what make Git a collaboration tool rather than a backup tool. This Part explains what a branch really is, how the different kinds of merge work, how to resolve conflicts with confidence, and how local branches relate to the branches on a remote server.

## 3.1 What a Branch Really Is

A branch is a movable pointer to a commit. Physically, it is a tiny file in `.git/refs/heads/` containing a 40-character hash (or an entry in `.git/packed-refs`).

```bash
cat .git/refs/heads/main
```

```text
9f2c1ab4d1e8c3a7f6b0e2d9c5a4b3f2e1d0c9b8
```

`HEAD` is a pointer to a branch (normally) that tells Git "this is where the next commit goes":

```bash
cat .git/HEAD
```

```text
ref: refs/heads/main
```

When you commit, Git creates the commit with the current tip as its parent, then moves the branch that HEAD points to forward to the new commit.

```text
Before commit:                    After commit:

      HEAD -> main                       HEAD -> main
                |                                  |
  A <- B <- C                        A <- B <- C <- D
```

Because a branch is just a pointer, creating one is instantaneous and costs nothing, no matter how big the repository is. That is the main reason Git workflows lean so heavily on short-lived branches.

## 3.2 Working With Branches

```bash
git branch                       # list local branches
git branch -vv                   # with last commit and upstream tracking info
git branch -a                    # include remote-tracking branches
git branch feature/pdb           # create without switching
git switch feature/pdb           # switch to it
git switch -c feature/pdb        # create and switch in one step
git switch -c hotfix/cve origin/release/2.4   # create from a specific start point
git switch -                     # go back to the previous branch
git branch -m old-name new-name  # rename
git branch -d feature/pdb        # delete (refuses if unmerged)
git branch -D feature/pdb        # force delete
git branch --merged main         # branches already merged into main
git branch --no-merged main      # branches with work not in main
```

```text
$ git branch -vv
  feature/pdb  3c1d2e9 [origin/feature/pdb: ahead 2] feat(helm): add PDB
* main         9f2c1ab [origin/main] ci: cache Go modules
  old-spike    a77e010 wip: try kustomize
```

> **Note:** `git checkout` still works for all of this, but it overloads two unrelated jobs (switching branches and overwriting files). `git switch` and `git restore`, introduced in Git 2.23, split those jobs so a typo cannot silently discard your edits.

### 3.2.1 Switching with uncommitted changes

If your uncommitted changes do not conflict with the target branch, Git carries them over. If they would be overwritten, Git refuses:

```text
error: Your local changes to the following files would be overwritten by checkout:
	values.yaml
Please commit your changes or stash them before you switch branches.
```

Options: commit them, `git stash` them (Part 4), or `git switch --merge` to attempt a three-way merge of your local changes onto the new branch.

### 3.2.2 Branch naming conventions

Pick a convention and enforce it. A common one:

| Prefix | Purpose | Example |
|---|---|---|
| `feature/` or `feat/` | New capability | `feature/OPS-812-pdb` |
| `fix/` | Bug fix | `fix/ingress-timeout` |
| `hotfix/` | Urgent production fix | `hotfix/cve-2024-3094` |
| `release/` | Release stabilization | `release/2.4` |
| `chore/`, `docs/`, `ci/` | Non-functional changes | `ci/cache-go-mod` |

Prefixes let you apply branch protection rules and CI triggers by pattern (`release/*`), and including a ticket ID links work to your tracker automatically.

## 3.3 Merging

Merging combines the work of two branches. Git picks one of two shapes depending on the history.

### 3.3.1 Fast-forward merge

If the target branch has not moved since the feature branched off, there is nothing to combine. Git simply moves the pointer forward.

```text
Before:                          After git merge feature (on main):

  A---B  main                      A---B---C---D  main, feature
       \
        C---D  feature
```

```bash
git switch main
git merge feature
```

```text
Updating 9f2c1ab..3c1d2e9
Fast-forward
 charts/ingress/templates/pdb.yaml | 14 ++++++++++++++
 1 file changed, 14 insertions(+)
```

No merge commit is created; history stays linear.

### 3.3.2 Three-way merge

If both branches have new commits, Git finds the **merge base** (the best common ancestor), compares both tips against it, and creates a new **merge commit** with two parents.

```text
Before:                          After git merge feature (on main):

  A---B---E  main                  A---B---E-------M  main
       \                                \         /
        C---D  feature                   C---D----   feature
```

```bash
git merge-base main feature    # shows B
git merge feature
```

```text
Merge made by the 'ort' strategy.
 charts/ingress/templates/pdb.yaml | 14 ++++++++++++++
 1 file changed, 14 insertions(+)
```

It is called "three-way" because three versions are compared: base (B), ours (E) and theirs (D). For each change, if only one side touched it, that side wins automatically. Only when both sides changed the same region differently is there a conflict.

> **Note:** `ort` ("Ostensibly Recursive's Twin") has been the default merge strategy since Git 2.34. It replaced `recursive` and is significantly faster, especially with renames.

### 3.3.3 Controlling merge shape

| Option | Effect | When to use |
|---|---|---|
| `--ff` (default) | Fast-forward if possible, otherwise merge commit | Default local behaviour |
| `--no-ff` | Always create a merge commit | Preserve the fact that a feature existed as a unit (GitFlow) |
| `--ff-only` | Refuse unless fast-forward is possible | Keeping `main` strictly linear; safe `pull` |
| `--squash` | Stage the combined changes but do not commit or record the merge | Collapse a branch into one commit |

```text
--no-ff result:                       --squash result:

  A---B-----------M  main               A---B---S  main   (S = C+D as one commit,
       \         /                                        feature not recorded as merged)
        C-------D  feature
```

> **Warning:** After `git merge --squash`, Git does not know the feature branch was merged. `git branch -d feature` will complain it is unmerged; that is expected. Delete it with `-D` once you have verified the squash commit.

## 3.4 Merge Conflicts

A conflict means Git found two changes to the same lines (or a change versus a delete) and refuses to guess. Conflicts are normal; they are not errors.

### 3.4.1 Anatomy of a conflict

Suppose `main` set replicas to 3 and `feature` set them to 4, starting from 2:

```bash
git merge feature
```

```text
Auto-merging values.yaml
CONFLICT (content): Merge conflict in values.yaml
Automatic merge failed; fix conflicts and then commit the result.
```

With `merge.conflictStyle=zdiff3` the file looks like this:

```text
<<<<<<< HEAD
replicas: 3
||||||| 10a799a
replicas: 2
=======
replicas: 4
>>>>>>> feature
image: app:1.0
```

| Section | Meaning |
|---|---|
| From `<<<<<<< HEAD` to the base marker (seven vertical bars) | Ours: the branch you are on |
| From the base marker to `=======` | Base: the common ancestor |
| Between `=======` and `>>>>>>>` | Theirs: the branch being merged |

The base section is why `zdiff3` is worth enabling: you can see that the original was 2, that main raised it to 3, and feature raised it to 4. Without the base you only see "3 vs 4" and must guess what each side intended.

### 3.4.2 Resolution workflow

```bash
git status                       # lists "both modified" files
# edit the file, remove markers, keep the correct content
git add values.yaml              # mark as resolved
git merge --continue             # or: git commit
```

Helpful tools during a conflict:

| Command | Purpose |
|---|---|
| `git diff` | Shows a combined diff of the conflict |
| `git log --merge -p` | Commits on either side that touched conflicted files |
| `git checkout --ours file` / `--theirs file` | Take one side wholesale |
| `git restore --source=feature file` | Take a file from a specific branch |
| `git mergetool` | Launch a configured visual tool (vimdiff, meld, VS Code) |
| `git merge --abort` | Give up and return to the pre-merge state |

```bash
git config --global merge.tool vscode
git config --global mergetool.vscode.cmd 'code --wait --merge $REMOTE $LOCAL $BASE $MERGED'
```

> **Warning:** During a **rebase**, "ours" and "theirs" are swapped compared to a merge. "Ours" is the branch you are rebasing onto (e.g. main), and "theirs" is your commit being replayed. This trips up even experienced engineers.

### 3.4.3 Reducing conflicts

- Keep branches short-lived and merge or rebase from main frequently.
- Avoid sweeping reformatting mixed with logic changes; land formatting in its own commit.
- Split large generated files or lock files, or mark them with merge drivers in `.gitattributes`.
- Enable `rerere` so a resolution done once is reused automatically.
- In config repositories (Helm values, Kustomize overlays), favour one file per environment or service so teams rarely edit the same lines.

## 3.5 Remotes

A **remote** is a named URL of another copy of the repository. `origin` is just the default name `git clone` gives the source.

```bash
git remote -v
```

```text
origin	git@github.com:acme/platform-infra.git (fetch)
origin	git@github.com:acme/platform-infra.git (push)
```

```bash
git remote add upstream git@github.com:cncf/project.git
git remote rename origin fork
git remote set-url origin git@gitlab.com:acme/platform-infra.git
git remote remove old
git remote show origin           # detailed tracking info
```

### 3.5.1 Remote-tracking branches

`origin/main` is a **remote-tracking branch**: your local, read-only record of where `main` was on `origin` the last time you talked to it. It is updated only by `fetch` (and `pull`, which fetches). You never commit to it directly.

```text
Local repository

  refs/heads/main            your branch            -> D
  refs/remotes/origin/main   last-known server main -> C

  A---B---C  origin/main
           \
            D  main          (git status: "ahead 1")
```

### 3.5.2 fetch, pull and push

| Command | What it does |
|---|---|
| `git fetch` | Download new objects and update `origin/*`. Never touches your working files or local branches. Always safe. |
| `git fetch --all --prune` | Fetch every remote and delete stale remote-tracking branches |
| `git pull` | `git fetch` then integrate `@{u}` into the current branch (merge, or rebase if `pull.rebase=true`) |
| `git pull --ff-only` | Only update if a fast-forward is possible |
| `git push` | Upload commits and move the branch on the remote |
| `git push -u origin feature` | Push and set upstream tracking |
| `git push origin --delete feature` | Delete a remote branch |
| `git push --tags` / `git push origin v1.2.0` | Push tags |
| `git push --force-with-lease` | Overwrite remote branch only if it is still where you last saw it |

> **Tip:** Many experienced engineers prefer `git fetch` followed by an explicit `git rebase origin/main` or `git merge origin/main` over `git pull`. It separates "see what changed" from "integrate it", giving you a chance to inspect with `git log HEAD..origin/main` first.

### 3.5.3 Upstream tracking

A local branch can have an **upstream**: the remote branch it syncs with by default. It is what makes plain `git pull`, `git push` and the "ahead/behind" message in `git status` work.

```bash
git branch --set-upstream-to=origin/main main
git branch -u origin/feature/pdb
git rev-list --left-right --count main...@{u}
```

```text
1	3
```

That output means one commit ahead and three behind.

### 3.5.4 Why a push is rejected

```text
 ! [rejected]        main -> main (fetch first)
error: failed to push some refs to 'github.com:acme/platform-infra.git'
hint: Updates were rejected because the remote contains work that you do not
hint: have locally.
```

Git only accepts a push that fast-forwards the remote branch, so it can never silently drop someone else's commits. Fetch, integrate (rebase or merge), then push again.

### 3.5.5 Force pushing safely

When you have rewritten your own branch (rebase, amend), you must force push. Never use bare `--force` on shared branches.

```bash
git push --force-with-lease
git push --force-with-lease=feature/pdb:3c1d2e9   # expect remote to be exactly at this commit
git push --force-if-includes --force-with-lease    # also require you've integrated the remote tip
```

`--force-with-lease` checks that the remote branch still points to what your `origin/feature` says. If a teammate pushed in the meantime, the push is refused instead of erasing their work. Be aware that a background `git fetch` (some IDEs do this) updates `origin/*` and can defeat the lease; `--force-if-includes` (Git 2.30+) closes that gap by verifying the remote tip is actually in your local branch's history.

## 3.6 The Fork Workflow

Open source projects and some enterprises do not give contributors write access to the main repository. Contributors **fork** (a server-side clone under their own account), push to their fork and open a pull request against the original.

```text
   upstream (acme/project)  <---- pull request ----+
         |                                         |
         | fork (server-side copy)                 |
         v                                         |
   origin (dana/project)  <---- git push ----  local clone
         ^                                         |
         +---------- git fetch upstream -----------+
```

```bash
git clone git@github.com:dana/project.git
cd project
git remote add upstream git@github.com:acme/project.git
git fetch upstream
git switch -c fix/typo upstream/main
# ...commit...
git push -u origin fix/typo
# open PR: dana:fix/typo -> acme:main

# keep your fork's main current
git switch main
git fetch upstream
git rebase upstream/main     # or: git merge --ff-only upstream/main
git push origin main
```

> **Tip:** Base each contribution branch on `upstream/main`, not on your fork's possibly stale `main`. It avoids dragging old commits into your pull request.

## 3.7 Lab: Branch, Conflict, Collaborate

**Goal:** experience fast-forward and three-way merges, resolve a conflict with zdiff3, and work against a remote using a local bare repository as the "server".

1. Create a bare "server" and two clones simulating two engineers:

   ```bash
   cd /tmp && rm -rf lab3 && mkdir lab3 && cd lab3
   git init --bare server.git
   git clone server.git alice && git clone server.git bob
   ```

2. Alice creates the initial content:

   ```bash
   cd /tmp/lab3/alice
   printf "replicas: 2\nimage: app:1.0\n" > values.yaml
   git add values.yaml && git commit -m "feat: initial values"
   git push -u origin main
   ```

3. Bob pulls, then both edit the same line:

   ```bash
   cd /tmp/lab3/bob && git pull
   git switch -c feature/scale
   sed -i.bak 's/replicas: 2/replicas: 4/' values.yaml && rm values.yaml.bak
   git commit -am "feat: scale to 4 replicas"
   git push -u origin feature/scale

   cd /tmp/lab3/alice
   sed -i.bak 's/replicas: 2/replicas: 3/' values.yaml && rm values.yaml.bak
   git commit -am "feat: scale to 3 replicas"
   git push
   ```

4. Alice merges Bob's branch and hits a conflict:

   ```bash
   git fetch
   git log --oneline --graph --all
   git -c merge.conflictStyle=zdiff3 merge origin/feature/scale
   cat values.yaml
   ```

   Resolve to `replicas: 4`, then:

   ```bash
   git add values.yaml
   git merge --continue
   git log --oneline --graph
   git push
   ```

5. Observe a fast-forward: Bob updates his `main`.

   ```bash
   cd /tmp/lab3/bob && git switch main && git pull --ff-only
   ```

6. Practise a rejected push and a safe force push:

   ```bash
   # Bob commits to main without pulling Alice's next change
   cd /tmp/lab3/alice && echo "# owner: alice" >> values.yaml && git commit -am "docs: owner" && git push
   cd /tmp/lab3/bob && echo "# team: sre" >> values.yaml && git commit -am "docs: team"
   git push                     # rejected
   git pull --rebase && git push
   ```

7. Clean up merged branches: `git branch --merged main` and `git push origin --delete feature/scale`.

## 3.8 Key Takeaways

- A branch is a 41-byte pointer to a commit; HEAD points to the current branch. That is why branching is cheap.
- Fast-forward merges just move a pointer; three-way merges compare base, ours and theirs and create a merge commit.
- `--no-ff`, `--ff-only` and `--squash` let you choose the shape of history deliberately.
- Enable `zdiff3` so conflict markers include the base version; resolve, `git add`, then `--continue`.
- `origin/main` is a local snapshot of the remote, updated only by fetch. `git fetch` is always safe.
- A normal push only fast-forwards; rewritten branches need `--force-with-lease` (plus `--force-if-includes`), never bare `--force` on shared branches.
- In the fork workflow, `origin` is your fork and `upstream` is the canonical repository; base contribution branches on `upstream/main`.
