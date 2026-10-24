---
name: wiki
description: Efficiently accumulate and retrieve competition knowledge in the knowledge/ stock layer (atomic pages + INDEX). Distill reusable insights from the daily-report flow and promote them, deduplicating and keeping things current. Use when an insight emerges, for weekly cleanup, or to recall past knowledge.
argument-hint: "[add <topic> | find <query> | promote | consolidate / blank to view INDEX]"
---

# Knowledge Wiki Skill

`knowledge/` is a visible knowledge asset made of **explicit in-repo .md files** (committed and shared). It is distinct from Claude Code's `memory/` — memory is cross-session personal notes, this is competition knowledge that gets committed.

For the design philosophy and page conventions, see `knowledge/README.md`. This skill mechanizes its operation.

## Subcommands (dispatch on the first token of `$ARGUMENTS`)

### blank: status check
1. Read `knowledge/INDEX.md` (do not read all pages)
2. Summarize page counts per category and the status distribution
3. **Propose knowledge gaps**: insights present in recent daily reports but not yet promoted; pages stuck at `idea`/`testing`

### `add <topic or insight>`: distill and promote
1. Identify the target insight (from `$ARGUMENTS`, or the latest daily report / SESSION_NOTES)
2. **Dedup check**: scan INDEX; if a page on the same topic exists, **update it instead of creating a new one**
3. Decide the category (technique / data / error / decision / add a new one if needed)
4. Create or update a page following the `_template.md` frontmatter:
   - `id` matches the filename (kebab-case)
   - always record provenance in `source` (daily-report date / exp folder)
   - set `status` appropriately (`validated` if confirmed, `rejected` if discarded)
   - fill `impact` with numbers (CV/LB delta) if available
   - cross-link related pages via `links` and `[[id]]` in the body
5. **Distill the body**: takeaway → evidence → how to apply → caveats. Don't paste raw daily-report logs
6. Append one line to `INDEX.md` (under the right category, with a hook)

### `find <query>`: retrieve and load
1. Read `INDEX.md`, pick 2-3 pages relevant to the query from their hooks
2. Load **only** those pages and summarize
3. Follow related `[[links]]` or suggest doing so

### `promote`: harvest from the daily report
1. Read the latest `daily_reports/*.md`
2. Extract insights that have become "reusable conclusions" (exclude one-off work logs)
3. Cross-check against existing pages; **list the un-promoted ones as page proposals**
4. With user confirmation, run the `add` equivalent

### `consolidate`: cleanup (roughly weekly)
1. Bulk-load all page frontmatter (load bodies only as needed)
2. Detect and propose:
   - **duplicates**: multiple pages on the same topic → merge
   - **contradictions**: conflicting conclusions → confirm which is newer/correct
   - **staleness**: pages whose premise changed → mark `stale`
   - **orphans**: pages no one links to → add links
3. **Regenerate** `INDEX.md` (update page count and last-consolidated date)

## Principles

- **Daily report first, knowledge second.** Don't write straight to knowledge skipping flow (provenance is lost)
- **Stay atomic.** One topic per page. Split when it grows
- **Keep rejected insights** (`rejected`). "Didn't work" is valuable knowledge too
- **Keep INDEX current.** Every new page gets one line in INDEX
- When recalling, go **INDEX → only the relevant page**. Don't scan every page
