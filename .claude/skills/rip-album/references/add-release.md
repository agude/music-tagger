# Adding a release to MusicBrainz

Use when `identify` exits 4: the CD is not in MusicBrainz. The user edits; the
operator supplies values and checks each tab. Start from the attach URL that
`identify` printed: it opens "Add release" with the disc's track lengths and
disc ID filled in.

Research first. Ask a subagent to search the web (Discogs by barcode and
catalog number, retailers, label press releases) for the release date,
country, label, catalog number, packaging, tracklist, and disc count. Require a
source URL for each fact and whether that source shows this barcode. Discogs
pages return 403 to fetches; `https://api.discogs.com/releases/<id>` and
`/database/search?barcode=<code>` work.

## Release information

| Field | Rule |
| --- | --- |
| Artist | Must be a linked artist (green), not typed text. Pick it with the magnifier. |
| Release group | Link the existing group (the original album), never a new one for a reissue. |
| Script | `Latin` for English titles. |
| Date / country | From the item in hand. The fine print names the marketing company and city ("Rhino ... Los Angeles" = `United States`). "Made in EU" names the pressing plant, not the release country. Add one release event per country only when the item shows it. Discogs "Europe" often just copies "Made in EU". |
| Label / Cat. No. | The label printed with the catalog number (Rhino `R2 ...`). Skip Discogs' habit of using the barcode as a Warner catalog number. |
| Packaging | Read https://musicbrainz.org/doc/Release/Packaging. All-cardboard folding sleeve, discs in slots, no tray = `Gatefold Cover` ("Also known as a Digisleeve"). `Digipak` has glued-in plastic trays. `Digifile` has one pocket for one disc. |
| Disambiguation | Optional, e.g. `2026 deluxe edition`. |

## Tracklist

- Paste titles into **Track parser** with Line numbers on and Track lengths
  and Artist names off. Lengths from the disc ID stay.
- Use MusicBrainz capitalization: lowercase short words ("on", "of", "in",
  "the") inside titles. Keep version labels printed on the sleeve.
- Add each further disc with **Add medium**, format `CD`. Its lengths stay
  empty until its disc ID is attached after ripping it.
- Medium title: only a title printed for that disc that the release title does
  not already cover ("Hotcakes, Outtakes, Rarities"). Leave it empty for
  "<Album> (2026 Remaster)".
- Confirm the track count against the sleeve; web tracklists disagree (a vinyl
  edition may add tracks).

## Recordings

- A remaster reuses the original recordings: link each album track to the
  recording that appears on the original album with a length within a second
  or two.
- Outtakes and alternate takes are separate recordings ("... (alternate
  version)"). If another edition already has them (the vinyl of the same
  reissue), link to those; otherwise add new recordings.
- Track titles stay as printed even when the recording name differs.

## Edit note (required)

State the source: item in hand, packaging, label and catalog number, barcode,
where disc titles and tracklist came from, the disc ID read with whipper and
the AccurateRip result, and URLs for any web facts (Discogs, retailer).

## After submitting

- Find the release with `mb discid <disc ID>`; the barcode search lags new
  releases by hours.
- Run `identify` for each later disc; it routes to attaching that disc's ID
  (exit 3). The web service may lag an attach by a minute or two.
- The new release has no cover art; see step 4 of the skill.
