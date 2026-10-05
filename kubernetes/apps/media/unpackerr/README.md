# unpackerr

Extracts scene RAR releases that Sonarr and Radarr grab through the seedbox.

The arrs send private-tracker grabs to the seedbox qBittorrent, and `downloads/seedbox-pull` copies each finished torrent to `/media/downloads/seedbox/<category>/`. Scene releases arrive there as RAR sets. Sonarr and Radarr don't unpack torrent downloads, so they only see the sample and the item sits at `importPending`. Unpackerr polls both arrs' queues every 2 minutes and extracts completed torrent items in place.

## Scope

Unpackerr acts on whatever path a queue item reports, as long as it can reach it. `UN_*_PATHS` is only a fallback for paths it can't reach, not a filter. So the scope is set by the mount: only `/mnt/pool/media/downloads/seedbox` is mounted, at `/media/downloads/seedbox`, the same path the arrs see. Home qBittorrent and SABnzbd output isn't mounted, so unpackerr leaves it alone.

## Clean-up

- **Original RARs:** never deleted (`DELETE_ORIG=false`).
- **Extracted copy:** removed 5 minutes after the arr imports it.
- **Whole entry:** `seedbox-pull` prunes it 14 days after it lands.

The pod runs as 1000:1000, the arrs' identity, so they can import what it writes.

## Alerts

| Alert                        | Fires when                                                                                                                                     |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `UnpackerrDown`              | Prometheus has had no scrape of its metrics for 15 minutes                                                                                     |
| `UnpackerrQueueFetchFailing` | It hasn't been able to read an arr's queue for 30 minutes                                                                                      |
| `UnpackerrExtractionStuck`   | Items have been queued for extraction, or failed, for 2 hours                                                                                  |
| `UnpackerrItemsWaiting`      | Completed items have sat waiting for 24 hours. Usually the files never arrived (seedbox-pull), or the arr can't import them for another reason |
