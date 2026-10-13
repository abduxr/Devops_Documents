# Part 9 — Capstone, Interview Q&A and Cheat Sheet

This final Part ties the course together. The capstone has you design and run a complete team workflow end to end. The interview section collects the questions DevOps candidates are actually asked, with concise model answers. The cheat sheet is a dense reference to keep next to your terminal.

## 9.1 Capstone Lab: A Production-Grade Team Workflow

**Scenario:** you are the platform engineer for a team of six building `orders-api`, a containerized service deployed to Kubernetes. The team deploys to production several times a week and must also support the previous minor release for one enterprise customer. You will build the Git workflow, guardrails, pipelines and GitOps promotion, then run a full release cycle including a hotfix and an incident.

Use a GitHub or GitLab sandbox organization if you have one. Where a step needs the platform, the local equivalent is noted.

### 9.1.1 Design decisions (write them down first)

Record these in `docs/git-workflow.md` in the repository. A written, agreed workflow is the deliverable as much as the code.

| Decision | Choice for this scenario | Reason |
|---|---|---|
| Branching | Trunk-based with short-lived branches, plus `release/X.Y` branches for supported versions | Frequent deploys, one older version supported |
| Merge method | Squash merge only | One commit per PR, clean revert |
| Commit format | Conventional Commits, enforced on PR titles | Automated SemVer and changelog |
| Versioning | SemVer tags `vX.Y.Z` created by release automation | Immutable releases |
| Protection | PR required, 1 approval + code owners, CI green, linear history, signed commits, no force push | Compliance and safety |
| Deployment | GitOps: separate `orders-config` repo, environments as overlays | Audit trail and drift correction |
| Secrets | External Secrets Operator; gitleaks in pre-commit and CI; push protection on | Nothing secret in Git |

### 9.1.2 Step 1: Create the repositories

```bash
mkdir -p ~/capstone && cd ~/capstone
git init -b main orders-api && git init -b main orders-config
cd orders-api
mkdir -p src docs .github/workflows
echo "print('orders-api')" > src/app.py
printf ".env\n.env.*\n!.env.example\n__pycache__/\n*.pem\n" > .gitignore
printf "* text=auto eol=lf\n" > .gitattributes
git add . && git commit -m "chore: initial project skeleton"
```

Create both repositories on your platform and push (`git remote add origin ...`, `git push -u origin main`). Locally, `git init --bare` repositories work as remotes.

### 9.1.3 Step 2: Guardrails

1. Add `.pre-commit-config.yaml` with whitespace, YAML, large-file, private-key and gitleaks hooks (Part 6). Run `pre-commit install` and `pre-commit run --all-files`.
2. Add `CODEOWNERS`:

   ```text
   *                      @acme/orders-team
   /.github/              @acme/platform
   /CODEOWNERS            @acme/platform
   ```

3. Configure SSH commit signing for every team member.
4. Protect `main` and `release/*` with the rules in 9.1.1. Protect tags matching `v*` so only the release bot can create them.
5. Add a PR template with What / Why / How to test / Risk and rollback.

### 9.1.4 Step 3: Pipelines

Create `.github/workflows/ci.yml` (or `.gitlab-ci.yml`) that:

- On pull requests: runs `pre-commit run --all-files`, unit tests, gitleaks on `origin/main..HEAD`, and validates the PR title against Conventional Commits.
- On push to `main`: builds the image tagged with the commit SHA, pushes it, and opens a PR to `orders-config` bumping the **staging** overlay to that SHA.
- On push of a tag `v*.*.*`: builds the image tagged with the version, signs it, and opens a PR to `orders-config` bumping the **prod** overlay.
- Uses `fetch-depth: 1` everywhere except the release job, which uses `fetch-depth: 0`.

Add a release automation job (release-please or semantic-release) that maintains a release PR with the next version and changelog derived from commits.

### 9.1.5 Step 4: GitOps configuration

In `orders-config`:

```text
apps/orders-api/base/{deployment.yaml,service.yaml,kustomization.yaml}
apps/orders-api/overlays/staging/kustomization.yaml
apps/orders-api/overlays/prod/kustomization.yaml
clusters/staging/orders-api.yaml     # Argo CD Application or Flux Kustomization
clusters/prod/orders-api.yaml
CODEOWNERS                            # /apps/*/overlays/prod/ @acme/sre-leads
```

Each overlay sets the image with `images: [{name: orders-api, newTag: <sha-or-version>}]`. Protect `main` here as well; this repository is production access.

### 9.1.6 Step 5: Run a feature through the workflow

```bash
cd ~/capstone/orders-api
git switch -c feat/ORD-101-idempotency-keys
# implement in two commits
git commit -am "feat(api): accept Idempotency-Key header"
git commit -am "test(api): cover duplicate request handling"
git push
gh pr create --fill --title "feat(api): support idempotency keys"
```

Address review feedback with `git commit --fixup=<sha>`, push, then before merge run `git rebase -i --autosquash origin/main` and `git push --force-with-lease`. Squash merge. Verify that the pipeline built the image and opened the staging promotion PR; merge it and watch the GitOps controller sync.

### 9.1.7 Step 6: Cut a release and a release branch

Merge the release PR so automation tags `v1.3.0`. Then create the support branch:

```bash
git fetch --tags
git switch -c release/1.3 v1.3.0
git push -u origin release/1.3
```

Promote `v1.3.0` to prod by merging the config PR. Confirm `git describe --tags` on main shows `v1.3.0-N-g<sha>` as new work lands.

### 9.1.8 Step 7: Hotfix and backport

A bug is found in production. Fix on main first:

```bash
git switch -c fix/ORD-140-null-total origin/main
git commit -am "fix(api): handle null order total"
# PR, review, squash merge -> commit abc1234 on main
git switch release/1.3 && git pull
git switch -c backport/ORD-140-to-1.3
git cherry-pick -x abc1234
git push -u origin backport/ORD-140-to-1.3
# PR into release/1.3, merge, tag v1.3.1 from release/1.3
```

### 9.1.9 Step 8: Incident simulation

Pick two of the following and execute the recovery, writing a short postmortem for each:

1. A developer commits a fake API key and pushes a branch. Show that pre-commit and CI block it; then bypass the hook with `--no-verify` and show CI and push protection still catch it. Run the rotation and history-cleanup procedure on the branch.
2. A bad config change reaches prod. Roll back with `git revert` in `orders-config` and confirm the controller converges.
3. A force push on a feature branch overwrites a teammate's commits. Recover from the teammate's reflog.
4. A regression appeared somewhere in the last 40 commits. Find it with `git bisect run` and a test script.

### 9.1.10 Capstone deliverables checklist

- `docs/git-workflow.md` with all decisions and their reasons
- Working pre-commit, CODEOWNERS, PR template, branch and tag protection
- CI that tests PRs, builds on main, releases on tags, with correct clone depths
- GitOps repo with environment overlays and a controller definition
- One feature, one release, one backported hotfix, all traceable from commit to running image
- Two postmortems from the incident simulation

## 9.2 Interview Questions and Answers

### 9.2.1 Beginner

**1. What is Git and how does it differ from GitHub?**
Git is a distributed version control system that runs locally. GitHub (like GitLab and Bitbucket) is a hosting platform for Git repositories that adds pull requests, CI, permissions and issue tracking.

**2. What does "distributed" mean for Git?**
Every clone contains the full history and can commit, branch and view logs offline. The central server is a convention, and any clone can restore it.

**3. Explain the working directory, staging area and HEAD.**
The working directory holds your editable files. The staging area (index) holds the exact snapshot proposed for the next commit. HEAD points to the current branch and thus the last commit. `add` moves changes into the index; `commit` turns the index into a commit.

**4. What is the difference between `git fetch` and `git pull`?**
`fetch` downloads new commits and updates remote-tracking branches without changing your work. `pull` is `fetch` followed by merging or rebasing the upstream into your current branch.

**5. What is the difference between `git diff` and `git diff --staged`?**
`git diff` compares working directory to index (unstaged changes). `--staged` compares index to HEAD (what will be committed).

**6. How do you undo changes to a file you have not staged?**
`git restore <file>`, which copies the index version back over the working file. The edits are lost permanently.

**7. How do you unstage a file?**
`git restore --staged <file>`. The file content on disk is not changed.

**8. What does `.gitignore` do, and why is a file I added to it still tracked?**
It stops untracked files from being added. It does not affect files already tracked; untrack with `git rm --cached <file>` and commit.

**9. What is a branch?**
A movable pointer to a commit, stored as a small ref file. Committing moves the current branch forward. Creating branches is nearly free.

**10. What is the difference between `git switch` and `git checkout`?**
`checkout` both switches branches and restores files. Git 2.23 split these into `switch` (branches) and `restore` (files) to make commands safer and clearer.

**11. What is a fast-forward merge?**
When the target branch has no new commits since the source branched off, Git simply moves the target pointer forward. No merge commit is created.

**12. What makes a good commit message?**
A short imperative subject (around 50 characters), a blank line, then a body explaining why the change was made. One logical change per commit. Conventional Commits adds a type prefix like `feat:` or `fix:`.

**13. What is `origin`?**
The default name Git gives to the remote you cloned from. It is just a name and can be renamed.

**14. What is a tag, and annotated vs lightweight?**
A tag names a specific commit permanently. Lightweight tags are just refs; annotated tags are objects with tagger, date, message and optional signature. Use annotated tags for releases.

### 9.2.2 Intermediate

**15. Explain a three-way merge.**
Git finds the merge base (common ancestor) and compares it with both branch tips. Changes made on only one side are applied automatically; conflicting changes to the same region are marked for manual resolution. The result is a merge commit with two parents.

**16. How do you resolve a merge conflict?**
Open each conflicted file, edit to the correct content removing the markers, `git add` the file, then `git merge --continue`. Use `zdiff3` conflict style to see the base, `git log --merge` for context, or `git merge --abort` to back out.

**17. Merge vs rebase: when do you use each?**
Rebase to keep your own unshared branch current and linear, and to clean it up before review. Merge (or squash merge via the platform) to integrate into shared branches. Never rebase shared history.

**18. What does `git rebase -i` let you do?**
Edit local history: reorder, reword, squash, fixup, split (via `edit`), drop commits, or run a command after each commit with `exec`.

**19. What are `--fixup` and `--autosquash`?**
`git commit --fixup=<sha>` creates a commit marked to be folded into `<sha>`. `git rebase -i --autosquash` automatically places and squashes those fixups, which is ideal for addressing review comments.

**20. Explain `git reset --soft`, `--mixed` and `--hard`.**
All move the current branch to a commit. Soft keeps index and working directory (changes become staged). Mixed resets the index but keeps files (changes become unstaged). Hard resets both, discarding uncommitted changes.

**21. Reset vs revert?**
Reset moves the branch pointer and rewrites history; use only on unshared commits. Revert adds a new commit that undoes a previous one; safe on shared branches.

**22. How do you revert a merge commit?**
`git revert -m 1 <merge-sha>`, where `-m 1` says the first parent is the mainline. To re-merge that branch later, revert the revert first.

**23. What is cherry-pick used for?**
Applying a specific commit's change onto another branch, typically backporting a fix from main to a release branch. Use `-x` to record the source commit.

**24. What is the reflog?**
A local log of where HEAD and each branch pointed over time. It lets you recover from bad resets, rebases and deleted branches. Entries expire (90 days by default; 30 for unreachable).

**25. What is `--force-with-lease` and why prefer it?**
It force pushes only if the remote branch is still where your remote-tracking ref says, so you do not overwrite commits someone else pushed meanwhile. Add `--force-if-includes` to guard against background fetches.

**26. What is detached HEAD and how do you get out of it?**
HEAD points directly at a commit rather than a branch (e.g. after checking out a tag). New commits are not on any branch. Run `git switch -c <name>` to keep them, or `git switch <branch>` to leave.

**27. How does `git stash` work, and what are its pitfalls?**
It saves uncommitted changes as special commits on a stash stack and cleans the working tree. `-u` includes untracked files. Pitfalls: stashes are local, easy to forget and can conflict on pop; use `git stash branch` or WIP commits for longer work.

**28. What is `A..B` vs `A...B`?**
In `log`, `A..B` lists commits in B not in A; `A...B` lists commits in either but not both. In `diff`, `A...B` compares the merge base with B, which is what a pull request shows.

**29. How would you find which commit introduced a bug?**
`git bisect start`, mark a bad and a good commit, then test each midpoint or automate with `git bisect run <script>`. It finds the culprit in logarithmic steps.

**30. Compare GitFlow, GitHub Flow and trunk-based development.**
GitFlow uses long-lived `develop` and `main` plus feature, release and hotfix branches; suited to scheduled versioned releases. GitHub Flow uses short-lived branches off an always-deployable main with PRs. Trunk-based integrates into main at least daily with feature flags; best for continuous delivery.

**31. What are CODEOWNERS and branch protection for?**
CODEOWNERS maps paths to responsible reviewers, auto-requests them and can be required for approval. Branch protection enforces rules such as PR reviews, passing checks, signed commits and no force pushes on important branches.

**32. Squash merge, rebase merge or merge commit: which would you choose?**
Squash for clean one-commit-per-PR history and simple reverts; merge commits when individual commits and integration points matter; rebase merge when commits are curated and linear history is required. Pick one per repository for consistency.

### 9.2.3 Advanced

**33. Describe Git's object model.**
Four object types in a content-addressed store: blobs (file contents), trees (directory listings of mode, type, hash, name), commits (tree, parents, author, committer, message) and annotated tags. Each is named by the hash of its content, forming a Merkle DAG.

**34. Why does rebasing change commit hashes?**
A commit's hash includes its parent hash. Replaying a commit onto a new parent produces a different commit object even with identical changes, and every descendant changes too.

**35. What are packfiles and why do they matter?**
Packfiles bundle many objects with delta compression and an index, drastically reducing disk and transfer size. `git gc` and `git maintenance` create and optimize them; fetch and clone transfer packs.

**36. How would you speed up Git on a very large monorepo?**
Blobless partial clone (`--filter=blob:none`), cone-mode sparse checkout, `scalar`, `core.fsmonitor` and untracked cache, commit-graph and multi-pack index, `git maintenance start`, LFS for binaries, and shallow or treeless clones plus sparse checkout in CI.

**37. Shallow clone vs partial clone?**
Shallow truncates history (depth), breaking history-dependent commands. Partial keeps full history but defers downloading blobs or trees until needed, so history commands still work.

**38. Submodules vs subtrees?**
Submodules store a pointer to an exact commit of another repo; precise and small but require extra clone and update steps. Subtrees copy files into a subdirectory; transparent for consumers but heavier and awkward to push upstream.

**39. A secret was pushed to a public repository. Walk me through the response.**
Rotate the credential immediately, check audit logs for misuse, remove it from code and move it into a secret manager, then if required rewrite history with `git filter-repo`, force push all refs, have everyone re-clone, ask the platform to purge caches, and add pre-commit scanning plus push protection.

**40. How do you prevent secrets from being committed?**
Design for no static secrets (OIDC, workload identity, vaults), ignore files, pre-commit hooks such as gitleaks, platform push protection, CI scanning on every PR and periodic full-history scans.

**41. What is GitOps and how do Argo CD and Flux implement it?**
Desired state is declared in Git, versioned, pulled by agents and continuously reconciled. Argo CD uses Application resources pointing at repo paths and revisions with auto-sync and self-heal. Flux uses GitRepository sources and Kustomization or HelmRelease controllers. Rollback is a Git revert.

**42. Why avoid environment branches in GitOps?**
Branches per environment drift apart, cause merge conflicts and hide what is deployed where. Directories or overlays on one main branch make promotion an explicit, reviewable diff and show all environments at once.

**43. Your CI job fails with "No names found, cannot describe anything." Why?**
The checkout is shallow and has no tags. Fetch full history and tags (`fetch-depth: 0`, `GIT_DEPTH: "0"`, or `git fetch --tags --unshallow`).

**44. A teammate force-pushed over main. How do you recover?**
Find the previous tip from any clone's reflog (`git reflog show origin/main`) or platform push events, create a branch there, compare with the current tip, merge or restore, and push. Then block force pushes on main.

**45. How do Git hooks fit into an enforcement strategy?**
Client hooks (pre-commit, commit-msg, pre-push) give fast feedback but can be skipped with `--no-verify`. Enforcement must be repeated in CI and server-side controls: pre-receive hooks, branch protection, push rules and required status checks.

**46. How do commit signatures work, and what do they prove?**
The committer signs the commit object with a GPG, SSH or X.509 key; the platform verifies against registered keys and marks it Verified. It proves the holder of the key created the commit, not that the code is correct or safe.

**47. What is `rerere`?**
"Reuse recorded resolution". Git records how you resolved a conflict and automatically reapplies the same resolution when the identical conflict reappears, which helps with repeated rebases and long-running branches.

**48. How would you design versioning and release automation for a service?**
Conventional Commits enforced on PR titles, squash merges, a release tool (release-please or semantic-release) computing SemVer from commits, annotated signed tags, tag-triggered pipelines building immutable artifacts labelled with the commit SHA, and GitOps promotion of the tagged artifact.

## 9.3 Command Cheat Sheet

### 9.3.1 Setup and configuration

| Command | Purpose |
|---|---|
| `git config --global user.name "Name"` | Set author name |
| `git config --global user.email "a@b.c"` | Set author email |
| `git config --global init.defaultBranch main` | Default branch name |
| `git config --global pull.rebase true` | Rebase on pull |
| `git config --global push.autoSetupRemote true` | Auto upstream on first push |
| `git config --global merge.conflictStyle zdiff3` | Show base in conflicts |
| `git config --global rerere.enabled true` | Reuse conflict resolutions |
| `git config --global core.excludesFile ~/.config/git/ignore` | Global ignore |
| `git config --list --show-origin` | Show all config and sources |

### 9.3.2 Create and inspect

| Command | Purpose |
|---|---|
| `git init -b main` | New repository |
| `git clone <url>` | Clone |
| `git clone --depth 1 <url>` | Shallow clone |
| `git clone --filter=blob:none <url>` | Blobless partial clone |
| `git status -sb` | Short status with branch |
| `git diff` / `--staged` / `HEAD` | Unstaged / staged / all changes |
| `git diff main...feature --stat` | What a branch introduces |
| `git log --oneline --graph --all` | Visual history |
| `git log -S "text"` | Commits adding or removing text |
| `git log --follow -- file` | File history across renames |
| `git show <rev>:<path>` | File at a revision |
| `git blame -w -C -L 10,20 file` | Line authorship |

### 9.3.3 Stage and commit

| Command | Purpose |
|---|---|
| `git add -p` | Stage hunks interactively |
| `git restore <file>` | Discard unstaged changes |
| `git restore --staged <file>` | Unstage |
| `git restore --source=<rev> <file>` | File from another commit |
| `git commit -m "type(scope): msg"` | Commit |
| `git commit --amend --no-edit` | Add to last commit |
| `git commit --fixup=<sha>` | Fixup for autosquash |
| `git rm --cached <file>` | Untrack, keep on disk |
| `git clean -nd` then `-fd` | Preview / delete untracked |

### 9.3.4 Branch and merge

| Command | Purpose |
|---|---|
| `git switch -c <branch> [start]` | Create and switch |
| `git switch -` | Previous branch |
| `git branch -vv` | Branches with upstream |
| `git branch -d` / `-D <branch>` | Delete / force delete |
| `git branch --merged main` | Merged branches |
| `git merge --no-ff <branch>` | Merge with merge commit |
| `git merge --ff-only <branch>` | Only fast-forward |
| `git merge --squash <branch>` | Squash into staged changes |
| `git merge --abort` | Abort merge |

### 9.3.5 Remotes

| Command | Purpose |
|---|---|
| `git remote -v` | List remotes |
| `git remote add upstream <url>` | Add remote |
| `git fetch --all --prune` | Update all remotes |
| `git pull --rebase` | Fetch and rebase |
| `git push -u origin HEAD` | Push current branch, set upstream |
| `git push --force-with-lease --force-if-includes` | Safe force push |
| `git push origin --delete <branch>` | Delete remote branch |
| `git fetch origin pull/123/head:pr-123` | Fetch a GitHub PR |

### 9.3.6 Rewrite and undo

| Command | Purpose |
|---|---|
| `git rebase main` | Replay branch on main |
| `git rebase -i --autosquash main` | Interactive cleanup |
| `git rebase --onto <new> <old> <branch>` | Transplant commits |
| `git rebase --update-refs main` | Rebase a branch stack |
| `git rebase --continue` / `--abort` | Continue / abort |
| `git cherry-pick -x <sha>` | Backport a commit |
| `git reset --soft HEAD~N` | Uncommit, keep staged |
| `git reset HEAD~1` | Uncommit, keep unstaged |
| `git reset --hard <rev>` | Reset everything to rev |
| `git revert <sha>` | Undo with new commit |
| `git revert -m 1 <merge>` | Undo a merge |
| `git reflog` | History of HEAD moves |
| `git reset --hard ORIG_HEAD` | Undo last merge/rebase/reset |
| `git fsck --lost-found` | Find dangling objects |

### 9.3.7 Stash and tags

| Command | Purpose |
|---|---|
| `git stash push -u -m "msg"` | Stash including untracked |
| `git stash list` / `show -p` | Inspect stashes |
| `git stash pop` / `apply stash@{n}` | Restore |
| `git stash branch <name>` | Branch from stash |
| `git tag -a v1.2.0 -m "msg"` | Annotated tag |
| `git tag -s v1.2.0 -m "msg"` | Signed tag |
| `git push origin v1.2.0` / `--follow-tags` | Push tags |
| `git describe --tags --always --dirty` | Build version string |

### 9.3.8 Power tools

| Command | Purpose |
|---|---|
| `git bisect start <bad> <good>` | Start bisect |
| `git bisect run <script>` | Automated bisect |
| `git worktree add ../dir -b <branch>` | Extra working directory |
| `git sparse-checkout set <dirs>` | Limit checkout to dirs |
| `git submodule update --init --recursive` | Init submodules |
| `git subtree add --prefix=dir <url> main --squash` | Vendor a repo |
| `git lfs track "*.bin"` | Track with LFS |
| `git maintenance start` | Background optimization |
| `git cat-file -p <obj>` / `-t` | Inspect object / type |
| `git rev-parse HEAD` / `--abbrev-ref HEAD` | Hash / branch name |
| `git count-objects -vH` | Repo size |

### 9.3.9 Security

| Command | Purpose |
|---|---|
| `git config --global gpg.format ssh` | Use SSH signing |
| `git config --global commit.gpgsign true` | Sign all commits |
| `git log --show-signature -1` | Verify signature |
| `git verify-tag v1.2.0` | Verify tag |
| `gitleaks git -v` | Scan history for secrets |
| `pre-commit run --all-files` | Run all hooks |
| `git filter-repo --invert-paths --path <file>` | Purge file from history |
| `git filter-repo --replace-text repl.txt` | Redact strings in history |
| `git config --global --add safe.directory <path>` | Trust a repo path (CI/containers) |

## 9.4 Key Takeaways

- A good Git workflow is a written set of decisions (branching, merge method, commit format, versioning, protection, deployment, secrets), each justified by how the team releases.
- Guardrails belong at every layer: local hooks for speed, CI and server rules for enforcement.
- Traceability from commit to PR to tag to artifact to running workload is the core value Git brings to DevOps.
- Fix on main first, backport with `cherry-pick -x`, release with immutable signed tags.
- In interviews, explain the why: three trees, object model, merge vs rebase trade-offs, reset vs revert, and incident response for leaks and force pushes.
- Keep the cheat sheet close, but understand what each command does to the working directory, index, HEAD and refs. That understanding is what turns a Git user into the engineer the team calls when things break.
