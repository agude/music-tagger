# ISRC Conflicts

`tag` cannot apply a release when a file's ISRC is absent from the
MusicBrainz recording's ISRC list. It reports the conflict and blocks writes
to **every track in the album**. Resolve the conflict in a diff JSON, then
apply that diff with `write-tags`.

## Recognize the output

`tag --dry-run` prints ISRC issues to stderr. It exits 0 even when the real
run will fail.

| Line | Meaning | Blocks writes |
| --- | --- | --- |
| `ISRC conflict: <path>: file=<code>; MusicBrainz=[<codes>]; unresolved` | The file's code is not in the MusicBrainz list | Yes |
| `Warning: <path>: ISRC <code> is unverified: MusicBrainz lists no ISRCs.` | MusicBrainz has no codes to compare against | No |

When unresolved conflicts are present, `tag` without `--dry-run` exits 1 and
writes nothing. Do not run it. Use the procedure below instead.

Warnings alone do not block writes. Continue with the normal apply step and
list the unverified codes in the final summary.

## Resolve

`<album-dir>` must be an absolute path. The diff JSON records file paths as
given, and `write-tags` uses those paths.

### 1. Build the diff JSON

Create the review files outside the album directory so that they cannot
reach the library:

```bash
REVIEW=$(mktemp -d /tmp/music-tagger-review-XXXXXX)
uv run music-tagger read "<album-dir>" -o "$REVIEW/evidence.json"
uv run music-tagger mb release <uuid> -o "$REVIEW/release.json"
uv run music-tagger diff --evidence "$REVIEW/evidence.json" --release "$REVIEW/release.json" -o "$REVIEW/diff.json"
```

The digest ends with `<file>: N unresolved conflict(s)` for each affected
track.

### 2. Set each resolution

Each conflict in `diff.json` has this shape:

```json
{"field": "isrc", "old_value": "USIR19902220", "candidate_values": ["USDW10110279"], "resolution": null}
```

Set `resolution` to one of these values:

- `old_value` keeps the file's code. Nothing is written to `isrc`.
- One entry from `candidate_values` replaces the file's code.

To keep every file code in the album:

```bash
jq '(.tracks[].conflicts[] | select(.field == "isrc")) |= (.resolution = .old_value)' \
  "$REVIEW/diff.json" > "$REVIEW/diff.resolved.json"
```

To replace the code for one track:

```bash
jq --arg path "<absolute-file-path>" --arg code "<candidate>" \
  '(.tracks[] | select(.path == $path) | .conflicts[] | select(.field == "isrc")) |= (.resolution = $code)' \
  "$REVIEW/diff.json" > "$REVIEW/diff.resolved.json"
```

To make several edits, run each `jq` command on the output of the previous
one.

Rules:

- Keep each conflict entry in the JSON. It records the decision in the
  change log.
- Do not add an `isrc` entry to `changes`. The resolution generates that
  change. A duplicate entry blocks writes.
- A `null` resolution, or a value that is neither `old_value` nor in
  `candidate_values`, blocks writes for the whole album.

### 3. Apply

```bash
uv run music-tagger write-tags --diff "$REVIEW/diff.resolved.json" --dry-run
```

Confirm three things in the output:

1. Every conflict line reads `resolved as <code>`.
2. The change list matches the list from `tag --dry-run`, plus one `isrc`
   change for each replaced code.
3. The skill's overwrite safety gate passes for this change list.

Then write and log:

```bash
uv run music-tagger write-tags --diff "$REVIEW/diff.resolved.json" --log ~/Projects/music-tagger/changes.log
```

The log entry contains each conflict, with the file code, the MusicBrainz
codes, and the resolution.

### 4. Clean up

```bash
rm -rf "$REVIEW"
```

Continue with the skill's next step. `write-tags` replaces the `tag` apply
step. Do not run `tag` afterward.
