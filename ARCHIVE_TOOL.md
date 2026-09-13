# Save the repository from your phone

Open **Actions → Save repository conversations and attachments → Run workflow**.
When the run succeeds, select the `repository-archive` branch and open
`repository-archive/README.md`. A downloadable ZIP also appears on the run page.

The source branch is untouched. The archive branch contains a source checkout
plus the exported data, preserving previous archive commits. By default the
workflow exports its own repository. The optional `source_repository` field
can point to another public repository, which is useful for testing in a fork.

## What's saved

- Open and closed issues and PRs, including their original JSON bodies.
- All issue comments (including PR conversation comments), PR reviews and inline
  review comments; PR commits and changed-file metadata.
- Release notes, tags, release asset metadata, downloaded release binaries,
  and each release's generated source ZIP and TAR archives. Source-archive
  downloads use the same hash verification and failure reporting as uploads.
- GitHub-uploaded attachments in these bodies/comments, including HTML images,
  Markdown links and bare upload URLs. An asset manifest links original URLs to
  local files and SHA-256 hashes. Repeated URLs download once; subsequent runs
  verify local files before reusing them.
- Legacy repository-scoped asset links and `user-images.githubusercontent.com`
  uploads are included. New-style asset IDs must have UUID form, so textual
  placeholders such as `/assets/xxxx` are not treated as uploaded files; their
  original text remains in the saved conversations.
- A readable index and issue/PR conversation Markdown with comments and local attachment links.
  Raw JSON preserves the original text without rewriting.

## Run locally

Python 3.10+; no third-party packages:

```sh
python -m unittest discover -s tests -v
python scripts/repository_dump.py --repository OWNER/REPO --output repository-archive
```

Set `GH_TOKEN` or `GITHUB_TOKEN` for private repositories or larger exports.
The Actions workflow uses its built-in token. Tokens are never written to the
archive and are not sent to attachment hosts or cross-host redirects.

## Check the result

`manifest.json` records every collection count, each downloaded file's size/hash,
and any failed endpoint or download. A failure produces exit code 1 and a red
workflow run; the partial ZIP is retained for inspection, and the archive branch
is not updated. A complete result produces exit code 0.

This is an API snapshot taken during a run; edits made concurrently may appear
on a later run. Deleted or inaccessible material cannot be reconstructed. GitHub
Discussions (a separate GraphQL product), Actions logs, repository history, and
third-party hosted websites are outside the issue/PR/release export. Source files
already remain in the repository and archive branch checkout.

GitHub rejects ordinary Git files of 100 MiB or more. The workflow catches these
before pushing and retains the ZIP instead; configure Git LFS if your repository
contains such uploads. A ZIP-only run is not reported as a successful repository
archive. Workflow artifact retention is 30 days; committed archive files persist.
