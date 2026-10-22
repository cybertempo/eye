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

The map is hand-written data inside the page (`NODES`, `EDGES`, `TOURS`). Update it in the same
change as the architecture it describes. It documents the code: nothing in EYE reads it, and it
is not a source of facts about observations.
