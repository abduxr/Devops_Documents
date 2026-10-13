# Part 6 — Hooks, Signing and Team Workflows

Individual Git skill is only half the job. A DevOps engineer is usually the person who decides how a whole team uses Git: what gets checked before a commit, how identity is verified, which branching model fits the release process, and how the hosting platform enforces it. This Part covers hooks, commit signing, branching strategies, pull requests and code review, branch protection, CODEOWNERS and platform specifics.

## 6.1 Git Hooks

Hooks are scripts Git runs at specific points. Client-side hooks live in `.git/hooks/` (or a directory set by `core.hooksPath`) and run on the developer's machine. Server-side hooks run on the Git server when receiving a push.

### 6.1.1 Client-side hooks

| Hook | Runs | Can abort? | Typical use |
|---|---|---|---|
| `pre-commit` | Before the commit message editor opens | Yes | Lint, format, secret scan on staged files |
| `prepare-commit-msg` | After default message is created | Yes | Insert ticket ID from branch name |
| `commit-msg` | After message is written | Yes | Enforce Conventional Commits |
| `post-commit` | After commit is created | No | Notifications |
| `pre-rebase` | Before rebase starts | Yes | Prevent rebasing protected branches |
| `post-checkout` / `post-merge` | After switch / merge | No | Reinstall dependencies, regenerate files |
| `pre-push` | Before objects are sent | Yes | Run tests, block pushes to main |

A minimal `commit-msg` hook enforcing Conventional Commits:

```bash
#!/usr/bin/env bash
# .githooks/commit-msg
pattern='^(feat|fix|docs|refactor|perf|test|build|ci|chore|revert)(\([a-z0-9-]+\))?!?: .{1,72}$'
first_line=$(head -n1 "$1")
if ! [[ $first_line =~ $pattern ]] && ! [[ $first_line =~ ^(fixup|squash|amend)! ]]; then
  echo "commit-msg: subject must follow Conventional Commits, got:" >&2
  echo "  $first_line" >&2
  exit 1
fi
```

A `prepare-commit-msg` hook that prefixes the ticket ID from the branch name:

```bash
#!/usr/bin/env bash
# .githooks/prepare-commit-msg
branch=$(git rev-parse --abbrev-ref HEAD)
ticket=$(echo "$branch" | grep -oE '[A-Z]+-[0-9]+' | head -1)
if [ -n "$ticket" ] && ! grep -q "$ticket" "$1"; then
  printf '\n\nRefs: %s\n' "$ticket" >> "$1"
fi
```

### 6.1.2 Sharing hooks with the team

Files in `.git/hooks` are not versioned or cloned. Two common solutions:

```bash
# 1. Commit hooks to the repo and point Git at them
chmod +x .githooks/*
git config core.hooksPath .githooks
```

```bash
# 2. Use a hook manager (pre-commit, Husky, Lefthook)
```

> **Warning:** Client-side hooks are a convenience, not a control. Anyone can skip them with `git commit --no-verify` or by not installing them. Anything that must be enforced belongs in CI and server-side rules too.

### 6.1.3 The pre-commit framework

`pre-commit` (pre-commit.com) is a language-agnostic hook manager. It installs pinned versions of each tool into isolated environments, so every developer and CI job runs exactly the same checks.

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
        args: [--allow-multiple-documents]
      - id: check-added-large-files
        args: [--maxkb=1024]
      - id: detect-private-key
      - id: check-merge-conflict
  - repo: https://github.com/gitleaks/gitleaks
    rev: v8.21.2
    hooks:
      - id: gitleaks
  - repo: https://github.com/antonbabenko/pre-commit-terraform
    rev: v1.96.1
    hooks:
      - id: terraform_fmt
      - id: terraform_validate
      - id: terraform_tflint
  - repo: https://github.com/adrienverge/yamllint
    rev: v1.35.1
    hooks:
      - id: yamllint
  - repo: https://github.com/shellcheck-py/shellcheck-py
    rev: v0.10.0.1
    hooks:
      - id: shellcheck
```

```bash
pip install pre-commit            # or: brew install pre-commit / pipx install pre-commit
pre-commit install                # installs .git/hooks/pre-commit
pre-commit install --hook-type commit-msg
pre-commit run --all-files        # run everything once, e.g. in CI
pre-commit autoupdate             # bump rev: pins
```

```text
trim trailing whitespace.................................................Passed
check yaml...............................................................Passed
check for added large files..............................................Passed
detect private key.......................................................Failed
- hook id: detect-private-key
- exit code: 1

Private key found: deploy/keys/id_rsa
Detect hardcoded secrets.................................................Failed
```

Run the same `pre-commit run --all-files` in CI so the checks are mandatory even for people who never installed the hooks.

> **Note:** The `rev:` values above are examples; pin to the current release of each hook repository and let `pre-commit autoupdate` or Dependabot/Renovate keep them fresh.

### 6.1.4 Server-side hooks

| Hook | Runs | Use |
|---|---|---|
| `pre-receive` | Once per push, before any ref is updated | Reject pushes (policy, secrets, size limits) |
| `update` | Once per ref being updated | Per-branch rules |
| `post-receive` | After refs are updated | Trigger CI, notify chat, mirror |

On self-hosted Git servers (GitLab self-managed, Gitea, bare SSH servers) you can install these directly. On SaaS platforms you configure equivalent behaviour through branch protection, push rules, secret scanning push protection and webhooks.

```bash
#!/usr/bin/env bash
# pre-receive: reject any pushed blob larger than 10 MB
zero=0000000000000000000000000000000000000000
while read -r old new ref; do
  [ "$new" = "$zero" ] && continue
  range=$([ "$old" = "$zero" ] && echo "$new" || echo "$old..$new")
  git rev-list --objects "$range" \
    | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' \
    | awk '$1=="blob" && $2 > 10485760 { print "Rejected large file: " $3; bad=1 } END { exit bad }' \
    || exit 1
done
```

## 6.2 Signing Commits and Tags

Anyone can set `user.email` to your address and create commits "authored" by you. Signing proves a commit was created by someone holding a specific private key. Platforms then display a "Verified" badge, and branch protection can require signatures.

### 6.2.1 SSH signing (simplest, Git 2.34+)

You probably already have an SSH key:

```bash
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
git config --global tag.gpgsign true
```

Upload the same public key to GitHub as a **Signing Key** (separate from the authentication key entry, even if it is the same key) or to GitLab with usage type "Signing".

To verify signatures locally, Git needs an allowed signers file:

```bash
echo "dana@example.com $(cat ~/.ssh/id_ed25519.pub)" >> ~/.config/git/allowed_signers
git config --global gpg.ssh.allowedSignersFile ~/.config/git/allowed_signers
git log --show-signature -1
```

```text
commit 9f2c1ab4d1e8c3a7f6b0e2d9c5a4b3f2e1d0c9b8
Good "git" signature for dana@example.com with ED25519 key SHA256:Xq3...
Author: Dana Ops <dana@example.com>
```

### 6.2.2 GPG signing

```bash
gpg --full-generate-key                  # choose ed25519 or RSA 4096
gpg --list-secret-keys --keyid-format=long
```

```text
sec   ed25519/3AA5C34371567BD2 2025-01-10 [SC]
uid                 [ultimate] Dana Ops <dana@example.com>
```

```bash
git config --global user.signingkey 3AA5C34371567BD2
git config --global commit.gpgsign true
gpg --armor --export 3AA5C34371567BD2    # paste into platform settings
export GPG_TTY=$(tty)                    # needed for passphrase prompts in terminals
```

### 6.2.3 Verifying

```bash
git verify-commit HEAD
git verify-tag v1.4.0
git log --pretty="format:%h %G? %GS %s" -5
git merge --verify-signatures feature    # refuse to merge unsigned commits
```

`%G?` prints `G` (good), `B` (bad), `U` (good, unknown validity), `N` (no signature), `E` (cannot check).

| Option | Pros | Cons |
|---|---|---|
| SSH signing | Reuses existing keys, easy setup, hardware keys (FIDO) work | Needs allowed signers file for local verification |
| GPG | Mature, web of trust, expiry and revocation | Complex key management |
| Sigstore gitsign | Keyless, short-lived certs tied to OIDC identity | Newer ecosystem, needs transparency log |

> **Interview:** "Does signing a commit prove the code is safe?" No. It proves who created the commit (or at least who held the key). It protects against impersonation and tampering in transit, not against malicious or buggy code. That is what review and CI are for.

## 6.3 Branching Strategies

A branching strategy is an agreement about which branches exist, how long they live, how code moves between them and how releases are cut. The right choice depends on how you release, not on fashion.

### 6.3.1 GitFlow

```text
main      o-------------------o-----------------o       (tagged releases only)
           \                 / \               /
hotfix      \               /   o---o---------o         hotfix/2.0.1
             \             /                 \
release       \     o--o--o  release/2.0      \
               \   /       \                   \
develop  o--o---o-o----o----o--o----o-----------o--o
             \        /         \      /
feature       o--o---o           o----o
```

- `main`: production releases, every commit tagged.
- `develop`: integration branch for the next release.
- `feature/*`: off develop, merged back to develop.
- `release/*`: off develop to stabilize, merged to main and develop.
- `hotfix/*`: off main, merged to main and develop.

Fits: software shipped in versions on a schedule, multiple supported versions, mobile apps, on-prem products. Costs: long-lived branches, merge overhead, delayed integration, and it is a poor fit for continuous deployment. Even its original author later recommended simpler flows for web applications.

### 6.3.2 GitHub Flow

```text
main     o----o------------o-----------o----   (always deployable)
               \          /  \        /
feature         o--o--o--o    o--o---o
                   (PR, CI, review, deploy, merge)
```

1. Branch off `main`.
2. Commit and push; open a pull request early.
3. CI runs; teammates review.
4. Deploy (to staging or even production from the branch) and verify.
5. Merge to `main`, which is always deployable.

Fits: web services and SaaS with continuous delivery, small to medium teams. Costs: needs good automated tests; no built-in notion of maintaining old versions.

### 6.3.3 Trunk-based development

```text
main (trunk)  o-o-o-o-o-o-o-o-o-o-o-o-o-o    (many small merges per day)
               \_/   \_/     \_/
           short-lived branches (< 1-2 days) or direct commits
```

- Everyone integrates into `main` at least daily.
- Branches, if used, live hours, not weeks.
- Incomplete features are hidden behind **feature flags**, not branches.
- Releases are cut from main (by tag or a short-lived release branch).

Fits: high-performing teams practising continuous integration in its literal sense; the DORA research associates it with better delivery performance. Costs: requires strong CI, fast tests, feature flag discipline and a culture of small changes.

### 6.3.4 Release branches

Often combined with GitHub Flow or trunk-based development when you must support versions in parallel:

```text
main          o--o--o--o--o--o--o--o--o--o
                    \           \
release/2.3          o--x' v2.3.1  \           (x' = cherry-picked fix)
                                    \
release/2.4                          o--y' v2.4.1
```

Rule: fix on `main` first, then cherry-pick (`-x`) to supported release branches. This guarantees no fix is lost in a future release. Fix directly on the release branch only when the code no longer exists on main, and forward-port explicitly.

### 6.3.5 Environment branches (and why to avoid them)

Some teams keep `dev`, `staging` and `prod` branches and promote by merging between them. This tends to drift: branches diverge, merges conflict, and "what is in prod?" becomes a diff exercise. Prefer one main branch plus promotion of **immutable artifacts** (a container digest or a tag) through environments. In GitOps (Part 7), environments are best modelled as directories or overlays, not branches.

### 6.3.6 Choosing

| Situation | Recommended |
|---|---|
| SaaS, deploy many times a day, strong CI | Trunk-based with feature flags |
| Web app, deploy on merge, moderate CI maturity | GitHub Flow |
| Versioned product, multiple supported releases | GitHub Flow or trunk + release branches |
| Scheduled releases with long QA cycles, regulated | GitFlow (or a simplified variant) |
| Infrastructure as code repo | Trunk-based/GitHub Flow; plan on PR, apply on merge |
| GitOps config repo | Single main branch, environments as directories |

## 6.4 Pull Requests and Code Review

A pull request (GitHub, Bitbucket) or merge request (GitLab) is a proposal to merge one branch into another, with discussion, review and automated checks attached.

### 6.4.1 What makes a good PR

- Small: ideally under 400 changed lines. Review quality drops sharply with size.
- One purpose, stated in the title (often a Conventional Commit, since squash merges use it).
- A description answering: what, why, how to test, risk and rollback plan.
- Linked ticket, screenshots or `terraform plan` output where relevant.
- Draft PRs for early feedback; mark ready when CI is green.

A PR template enforces this:

```text
<!-- .github/pull_request_template.md -->
## What
## Why
## How to test
## Risk and rollback
- [ ] Backward compatible
- [ ] Runbook/docs updated
```

### 6.4.2 Reviewing well

| Focus first | Focus later or automate |
|---|---|
| Correctness and edge cases | Formatting (formatters) |
| Security: secrets, permissions, injection | Lint style (linters) |
| Operability: logging, metrics, rollback | Typos in comments |
| Blast radius: IAM, network, data deletion | Naming bikesheds |

For infrastructure changes, review the plan, not just the code: a two-line Terraform diff can destroy a database.

### 6.4.3 Merge methods

| Method | Result on main | Pros | Cons |
|---|---|---|---|
| Merge commit | All branch commits plus a merge commit | Full history, easy whole-PR revert (`-m 1`) | Noisy graph |
| Squash and merge | One commit per PR | Clean, linear, one revert per PR | Loses individual commits; branch looks unmerged |
| Rebase and merge | Branch commits replayed linearly, no merge commit | Linear and keeps commits | New hashes; requires disciplined commits |

Most teams pick one and disable the others to keep history consistent. Squash merge is the most common default because it tolerates messy branch history.

### 6.4.4 Merge queues

On busy repositories, two PRs can each pass CI against an old main and break main when combined. A merge queue (GitHub merge queue, GitLab merge trains, Bitbucket via apps) tests each PR on top of main plus the PRs ahead of it, and merges only if that combination passes.

## 6.5 Branch Protection and Rulesets

Protect `main` and release branches so that policy is enforced by the server, not by goodwill.

| Rule | Why |
|---|---|
| Require pull request before merging | No direct pushes to main |
| Require N approvals | Four-eyes principle, audit compliance |
| Dismiss stale approvals on new commits | Approval applies to what is merged |
| Require review from code owners | Right experts approve their areas |
| Require status checks to pass (and branch up to date) | CI must be green on current main |
| Require signed commits | Identity verification |
| Require linear history | Enforces squash or rebase merges |
| Require conversation resolution | No ignored review comments |
| Block force pushes and deletions | Protects history |
| Restrict who can push / bypass | Limit emergency access, audit bypasses |
| Tag protection (`v*`) | Only release automation can create release tags |

GitHub **rulesets** extend classic branch protection: multiple rulesets can layer, they apply to branches or tags by pattern across many repositories at the organization level, and they support "evaluate" mode to test before enforcing. GitLab offers protected branches, protected tags, push rules (commit message regex, file size, secret-like file names) and approval rules. Bitbucket has branch permissions and merge checks.

## 6.6 CODEOWNERS

A `CODEOWNERS` file maps paths to the people or teams responsible. Platforms auto-request their review, and branch protection can require their approval.

```text
# .github/CODEOWNERS   (GitHub: .github/, root, or docs/; GitLab: same, plus sections)

# Default owners for everything
*                       @acme/platform-team

# Terraform: network changes need the network team
/terraform/network/     @acme/network-eng
/terraform/iam/         @acme/security @acme/platform-team

# Helm charts
/charts/                @acme/sre

# CI definitions are security-sensitive
/.github/workflows/     @acme/platform-security
/.gitlab-ci.yml         @acme/platform-security

# Protect the ownership file itself
/.github/CODEOWNERS     @acme/platform-leads
```

Rules: later matching lines take precedence (GitHub), so put general rules first and specific ones after. Owners must have write access to the repository. Protect `CODEOWNERS` itself, otherwise anyone can remove themselves from review requirements in their own PR.

GitLab adds sections and optional approval counts:

```text
[Terraform][2] @acme/network-eng @acme/platform-team
/terraform/
```

## 6.7 Platform Specifics

| Concept | GitHub | GitLab | Bitbucket |
|---|---|---|---|
| Change proposal | Pull request | Merge request | Pull request |
| CI | GitHub Actions (`.github/workflows/*.yml`) | GitLab CI (`.gitlab-ci.yml`) | Bitbucket Pipelines (`bitbucket-pipelines.yml`) |
| Branch rules | Branch protection, rulesets | Protected branches, push rules, approval rules | Branch permissions, merge checks |
| Ownership | `CODEOWNERS` | `CODEOWNERS` with sections | Code owners (Cloud) / reviewers add-ons |
| Merge serialization | Merge queue | Merge trains | Via apps |
| Secret scanning | Secret scanning, push protection | Secret detection, push protection | Secret scanning (Cloud) |
| Server refs for reviews | `refs/pull/N/head`, `refs/pull/N/merge` | `refs/merge-requests/N/head` | `refs/pull-requests/N/from` (Server) |
| Auto-close issues | `Fixes #123` | `Closes #123` | `Fixes PROJ-123` with Jira |
| CLI | `gh` | `glab` | (REST API, Atlassian CLI) |

Command line examples:

```bash
gh pr create --fill --base main --draft
gh pr checkout 123
gh pr view --web
gh pr merge 123 --squash --delete-branch
glab mr create --fill --target-branch main
glab mr checkout 45
```

GitLab also supports **push options** to create an MR directly from a push:

```bash
git push -o merge_request.create -o merge_request.target=main -o merge_request.remove_source_branch
```

## 6.8 Lab: Enforce Quality Before It Reaches Main

**Goal:** install hooks with the pre-commit framework, add a custom commit-msg hook, sign commits with SSH, and draft branch protection and CODEOWNERS.

1. Create a repository and configure pre-commit:

   ```bash
   cd /tmp && rm -rf lab6 && mkdir lab6 && cd lab6 && git init
   pipx install pre-commit || pip install --user pre-commit
   ```

   Write `.pre-commit-config.yaml` with `trailing-whitespace`, `check-yaml`, `detect-private-key` and `check-added-large-files` (section 6.1.3), then:

   ```bash
   pre-commit install
   git add .pre-commit-config.yaml && git commit -m "ci: add pre-commit"
   ```

2. Trigger a failure:

   ```bash
   printf -- "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n" > id_test
   git add id_test && git commit -m "feat: add key"     # blocked
   git rm --cached id_test && rm id_test
   ```

3. Add the commit-msg hook from section 6.1.1 in `.githooks/`, make it executable, set `git config core.hooksPath .githooks` and observe that a message like "updated stuff" is rejected while "docs: add readme" passes.

   > **Note:** `core.hooksPath` replaces `.git/hooks`, so pre-commit's installed hook is no longer used. Either install pre-commit into the same directory or keep only one mechanism; in practice teams choose pre-commit and add commit-msg checks to its config.

4. Configure SSH signing (section 6.2.1), commit, then run `git log --show-signature -1` and `git log --pretty="%h %G? %s"`.

5. Write a `CODEOWNERS` file assigning `/terraform/` and `/.github/` to different teams, protecting `CODEOWNERS` itself.

6. On a GitHub or GitLab sandbox repository, protect `main`: require a PR, one approval, code owner review, passing status check, and block force pushes. Try to `git push origin main` directly and record the error.

7. Write one paragraph recommending a branching strategy for a team that deploys a SaaS API ten times a day but also ships a versioned CLI quarterly.

## 6.9 Key Takeaways

- Hooks automate checks at commit and push time; share them via `core.hooksPath` or a manager like pre-commit.
- Client-side hooks can be bypassed; enforce critical rules in CI and on the server too.
- Sign commits and tags (SSH signing is the easiest path) and require signatures on protected branches where identity matters.
- Choose the branching strategy from your release model: trunk-based or GitHub Flow for continuous delivery, release branches for supported versions, GitFlow only for scheduled versioned releases.
- Keep PRs small and well described; standardize on one merge method; use merge queues on busy repos.
- Protect main and release branches with required reviews, status checks, code owners and no force pushes.
- CODEOWNERS routes reviews to the right experts; protect the file itself.
- Environments should be promotion of immutable artifacts or directories, not long-lived environment branches.
