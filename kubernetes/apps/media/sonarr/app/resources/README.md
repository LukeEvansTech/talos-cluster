# Sonarr Custom Scripts

Custom scripts triggered via Sonarr's Connect settings (Settings → Connect → Custom Script).

## Scripts

### refresh-series.sh

**Trigger:** On Grab

Automatically refreshes series metadata when episodes are grabbed that have TBA/TBD titles. This ensures episode names are updated once the metadata providers have the real titles.

### tag-codecs.sh

**Trigger:** On Import, On Upgrade

Automatically tags series with their video codec format after downloads complete:
- `codec:h265` - HEVC content
- `codec:h264` - AVC content (includes legacy divx/xvid/mpeg2)
- `codec:av1` - AV1 content
- `codec:other` - Unknown formats

Useful for filtering series by codec quality in Sonarr's UI.

## Setup

1. Navigate to Sonarr → Settings → Connect
2. Add → Custom Script
3. Configure path: `/scripts/<script-name>.sh`
4. Enable appropriate triggers per script
