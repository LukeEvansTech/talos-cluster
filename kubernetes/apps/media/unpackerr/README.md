# unpackerr

Extracts scene RAR releases that Sonarr and Radarr grab through the seedbox.

The arrs send private-tracker grabs to the seedbox qBittorrent, and `downloads/seedbox-pull` copies each finished torrent to `/media/downloads/seedbox/<category>/`. Scene releases arrive there as RAR sets. Sonarr and Radarr don't unpack torrent downloads, so they only see the sample and the item sits at `importPending`. Unpackerr polls both arrs' queues every 2 minutes and extracts completed torrent items in place.

## Scope

Unpackerr acts on whatever path a queue item reports, as long as it can reach it. `UN_*_PATHS` is only a fallback for paths it can't reach, not a filter. So the scope is set by the mount: only `/mnt/pool/media/downloads/seedbox` is mounted, at `/media/downloads/seedbox`, the same path the arrs see. Home qBittorrent and SABnzbd output isn't mounted, so unpackerr leaves it alone.

## Clean-up

- **Original RARs:** never deleted (`DELETE_ORIG=false`).
- **Extracted copy:** removed 5 minutes after the arr imports it.
- **Whole entry:** `seedbox-pull` prunes it 14 days after it lands.

Size cap: v0.16 fails, without retrying, any Sonarr archive that unpacks past 20 GB or any Radarr archive past 75 GB. That would catch season packs, so the caps are raised to 400 GB for Sonarr and 200 GB for Radarr.

The pod runs as 1000:1000, the arrs' identity, so they can import what it writes.

## Alerts

Unpackerr's metrics are totals across all items, with no age per item. So each stuck-work alert pairs pending work with no progress in the same window, instead of just counting items.

| Alert                           | Fires when                                                                            |
| ------------------------------- | ------------------------------------------------------------------------------------- |
| `UnpackerrDown`                 | Prometheus has had no scrape of its metrics for 15 minutes                            |
| `UnpackerrQueueFetchFailing`    | It hasn't been able to read an arr's queue for 30 minutes                             |
| `UnpackerrExtractionStuck`      | Items have been queued or extracting for 3 hours, with no file extracted in that time |
| `UnpackerrExtractionFailed`     | An item has failed for good: retries used up, or a size, file-count or ratio cap hit  |
| `UnpackerrExtractedNotImported` | Extracted items have sat for 6 hours with no import recorded                          |

There's no alert on the `waiting` state. Every completed torrent in either queue counts as waiting before unpackerr checks it for archives, including ordinary non-RAR torrents that sit in the queue while they seed. A seedbox item that never arrives shows up as `importPending` in the arr queue instead.

Unpackerr forgets stale items after 24 hours, so every window is under that.
