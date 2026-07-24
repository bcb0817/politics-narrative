# Repository operating rules 🤖

## GitHub synchronization

After completing any requested source, configuration, documentation, or test
change in this repository:

1. Run the relevant syntax checks and automated tests.
2. Confirm that `.env`, credentials, tokens, Webhook URLs, logs, runtime data,
   databases, outputs, and backups are ignored and are not staged.
3. Review the intended diff and stage only repository changes belonging to the
   completed Bot work.
4. Commit the verified change with a concise description.
5. Push the current working branch to `origin`.
6. Open or update a draft pull request to the default branch when the change is
   not already on that branch.
7. Report the branch, commit, push result, pull request, and validation result.

Do not publish when tests fail, GitHub authentication is unavailable, secrets
are detected, or the intended change scope is ambiguous. Report the blocker
instead. Never commit `.env` or other live credentials.
