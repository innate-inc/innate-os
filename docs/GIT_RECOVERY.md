# Git Repository Recovery

Guide for recovering from corrupted Git objects without re-cloning to a new folder.

## Symptoms

```
fatal: loose object <sha> is corrupt
fatal: bad object HEAD
error: <remote> did not send all necessary objects
```

## Quick Recovery (if all work is pushed)

If all your commits have been pushed to the remote, you can reinitialize in place:

```bash
cd ~/innate-os

# Remove corrupted git database
rm -rf .git

# Clone bare repo into .git
git clone --bare git@github.com:innate-inc/maurice-prod.git .git

# Convert from bare repo
git config --unset core.bare

# Force checkout to desired branch (overwrites local files with remote versions)
git checkout -f main
# or
git checkout -f <your-branch>
```

### Keep Local File Changes

If you want to preserve local file modifications after checkout:

```bash
git checkout -f main
git reset --mixed HEAD  # keeps working files, unstages everything
```

## Why This Works

1. `rm -rf .git` removes the corrupted Git database
2. `git clone --bare` fetches a fresh copy of all refs and objects
3. `git config --unset core.bare` converts it to a normal repo
4. `git checkout -f` forces checkout, overwriting "untracked" files (which are your existing files that Git now sees as untracked)

## When You Must Re-clone

If you have **unpushed commits**, they will be lost with this method. In that case:

1. Copy any modified files to a backup location
2. Re-clone the entire repository
3. Copy back your changes

## Why Simple Fetch Doesn't Work

When Git objects are corrupted, `git fetch` fails because:

1. Fetch needs to read local refs to determine what's new vs. what exists
2. Local branch points to corrupted commit
3. Git can't read corrupted object → fetch aborts
4. Chicken-and-egg: can't fetch to fix what prevents fetching
