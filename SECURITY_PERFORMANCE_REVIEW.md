# Security and Performance Review Notes

Date: 2026-06-16

This document captures security, performance, and correctness issues found during a source review of the Zotify codebase. The findings are ordered by estimated impact.

## Findings

### High: Duplicate-path handling can overwrite existing files

- Location: `zotify/utils.py`, `check_path_dupes()`
- Related call sites: `zotify/api.py`, download and clone paths

`check_path_dupes()` counts files matching `path.stem + "*"` and assumes `stem_{count}` is unique. If duplicate suffixes have gaps, this can pick an existing filename. For example, `song.ogg`, `song_1.ogg`, and `song_3.ogg` cause the next candidate to become `song_3.ogg`.

Impact: downloads, conversions, or clones can overwrite existing files.

Recommended fix: generate candidates in a loop and return only when the candidate path does not exist. Prefer exclusive creation or atomic move semantics for final writes.

### High: OAuth callback may listen more broadly than intended

- Location: `zotify/config.py`, `Zotify.login()`
- Related: `Dockerfile` exposes port `4381`

The default redirect address is loopback, but OAuth setup calls `.set_listen_all(True)`. Combined with the Docker image exposing the callback port, this can expose the login callback outside localhost depending on runtime networking.

Impact: authentication callback/token handling may be reachable from unintended network peers.

Recommended fix: bind only to `127.0.0.1` by default. Require an explicit opt-in for non-loopback listening, and document the risk.

### Medium: Output templates can escape the configured root

- Location: `zotify/api.py`, `Track.fill_output_template()`
- Related: `zotify/utils.py`, `M3U8.fill_output_template()`

Metadata placeholder values are sanitized with `fix_filename()`, but static template path components are not validated. A configured output template containing an absolute path or `../` can write outside `ROOT_PATH`.

Impact: a malicious or accidental config can place files outside the intended music directory.

Recommended fix: resolve final output paths and reject paths outside the intended root unless an option explicitly allows absolute destinations. Sanitize or validate every template path component, not only placeholder replacements.

### Medium: Saved credentials do not enforce restrictive permissions

- Location: `zotify/config.py`, `Config.get_credentials_location()` and `Zotify.login()`

Credential files and parent directories are created through normal path and library calls without explicit permission hardening.

Impact: on systems with permissive umasks or shared user environments, saved tokens may be readable by other local users.

Recommended fix: create credential directories with `0700` where supported and write credential files with `0600`. Consider validating existing permissions and warning when they are too broad.

### Medium: Network calls have no timeout or size guard

- Location: `zotify/config.py`, `Zotify.invoke_url()`
- Location: `zotify/api.py`, `Track.write_audio_tags()` album art request
- Location: `zotify/api.py`, `Episode.download_directly()`

Several `requests.get()` calls omit timeouts. Album art is read through `.content`, which can load an unbounded response into memory.

Impact: the CLI can hang indefinitely on network stalls, and image downloads can consume excessive memory.

Recommended fix: use explicit connect/read timeouts, stream large responses, validate content type, and cap maximum album-art bytes before storing in tags or writing `cover.jpg`.

### Medium: Pagination is recursive and copies lists repeatedly

- Location: `zotify/config.py`, `Zotify.invoke_url_nextable()`

`handle_next()` recursively fetches pages and returns `items + handle_next(...)`. For large playlists or libraries this risks recursion depth failures and repeated list copying.

Impact: poor performance and possible crashes for large paginated result sets.

Recommended fix: replace recursion with an iterative loop that extends a single result list until there is no next page or the requested maximum is reached.

### Medium: Archive checks reread files repeatedly

- Location: `zotify/api.py`, `DLContent.check_skippable()`
- Location: `zotify/utils.py`, `SongArchive.ids()`

`check_skippable()` calls `SongArchive(...).ids()` for each item, and `ids()` rereads/parses the archive file each time.

Impact: large libraries can degrade toward O(n^2) file IO during skip checks.

Recommended fix: cache archive entries per archive filepath for the duration of a query run. Invalidate or append to the cache when `SongArchive.add_entry()` writes new entries.

### Medium: Optimized parent-album mode uses a stale variable

- Location: `zotify/api.py`, `Query.download()`

In the `DOWNLOAD_PARENT_ALBUM` branch, the loop iterates `for t in tracks_with_albums` but appends to `dlc_mapping[dlc]`. `dlc` is stale from a previous loop, so parent album stacks are attached to the wrong mapping key.

Impact: optimized downloads can miss, duplicate, or mis-plan downloads when parent-album mode is enabled.

Recommended fix: append to `dlc_mapping[t]` instead.

### Medium: Dependency supply chain is not reproducible

- Location: `pyproject.toml`

The project depends on `librespot` directly from a GitHub URL and leaves most dependencies unpinned.

Impact: installs are not reproducible, and dependency updates can silently change runtime behavior or introduce vulnerabilities.

Recommended fix: pin direct Git dependencies to commit SHAs for releases and maintain a lock file or constraints file for reproducible builds.

## Verification Notes

The existing test suite is small and currently focuses on lossless/FLAC behavior. It does not cover the issues above.

Attempted test commands during review:

```text
python -m pytest -q
```

Result: `python` was not available on PATH.

```text
python3 -m pytest -q
```

Result: `pytest` was not installed in the active Python environment.
