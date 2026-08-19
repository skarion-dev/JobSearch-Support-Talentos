# GitHub-based jobs app deployment

The spare PC runs `github_sync.ps1` every 10 minutes through the
`JobSearchGitHubDeploy` Windows task.

The updater:

- fetches `origin/master` without storing GitHub credentials;
- refuses to run during the 7 PM autopilot;
- refuses to overwrite tracked local changes;
- refuses divergent history and untracked-file collisions;
- fast-forwards only after GitHub has the approved commit;
- runs `python -m compileall -q app scripts`;
- checks `http://127.0.0.1:3100/_stcore/health`;
- rolls back to the previous commit if validation fails.

`.env`, `data/`, logs, and other local runtime files remain outside the
version-controlled deployment path.

One-time bootstrap: publish the existing production commits to GitHub. The
spare PC currently has commits `b636bfb` and `3b7ea66` locally, while GitHub
does not. The updater intentionally blocks until the GitHub branch contains
those commits, so it cannot replace production with an older checkout.
