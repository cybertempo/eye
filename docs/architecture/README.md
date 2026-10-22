# Architecture map

[`system-map.html`](system-map.html) is an interactive map of this repository at commit
`f53afbc`. It shows every component, database table and link, in swimlanes ordered the way data
moves: sources, ingest, storage, workers, API, wire contract and browser. The control plane is
above the lanes and build, verify and deploy are below them. Blocks marked *planned* are named
packages with no code yet.

- **Open it** (developer laptop): open the file in a browser yourself. It is one static file
  and needs no server, account or build step. It loads fonts from Google Fonts and falls back
  to system fonts without network.
- **Use it**: zoom and pan, click a block for its files, guarantees and links, search with `/`,
  switch layers, or follow a guided tour. **Redesign** mode lets you drag, add, link, annotate
  and remove blocks in a draft. The draft is kept in browser storage, and nothing in the
  repository changes.
- **Claude features**: the owner's copy is published as a private claude.ai artifact. There the
  page can ask Claude about the map and preview proposed redesigns. It can also send a component
  or the draft to a Claude Code session watching the artifact, and keep the draft in the
  artifact's store (`drafts/main`). Opened from the repository, these features are absent and
  the page says so.

## How it stays current

Descriptions, layout and tours are hand-written in the page (`NODES`, `EDGES`, `TOURS`) as of
`f53afbc`. Everything that can be read from the code is derived instead.

- [`scripts/gen_system_map.py`](../../scripts/gen_system_map.py) (developer laptop, CI through
  `tests/test_system_map.py`, or the scheduled cloud session below) reads the checkout and
  prints JSON facts. It records file paths, line counts, first docstring lines, internal imports,
  the tables and views each migration creates, the CLI commands, the API routes and the wire
  schema versions. It uses the standard library only, with no network and no model. A file it
  cannot parse is reported `UNVERIFIED`, never dropped.
  `python3 -I scripts/gen_system_map.py --out facts.json` writes the facts to a file.
- A scheduled Claude task, *Refresh EYE system map*, runs daily at 05:40 UTC in a fresh cloud
  session. It reads the public `main` branch and stops if `main` hasn't changed. It also stops
  if `main` doesn't contain the generator yet, so the map stays on its snapshot until this script
  is merged. Otherwise it runs the generator and stores the facts in the artifact's database
  (`source/latest`). It never pushes, never republishes the page and never touches the private
  server. The schedule is managed in claude.ai, not in this repository.
- The page validates those facts and merges them over its snapshot:
  - updated line counts, tables, commands, routes and schema version
  - new files shown as amber *detected* blocks, linked by their imports
  - deleted files marked *gone*
  - unparseable files marked *UNVERIFIED*

  The source button in the header shows which commit the map follows and lists every change
  since the snapshot. On claude.ai it can also start the check immediately.

When a detected block deserves a real description, add it to `NODES` in the same change as the
code. The map documents the code: nothing in EYE reads it, and it is not a source of facts about
observations.
