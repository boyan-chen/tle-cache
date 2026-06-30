# SatelliteMap TLE Cache

This repository template publishes a small read-only TLE cache for SatelliteMap.
It is intended as an emergency compatibility layer while SatelliteMap still uses
traditional TLE text files.

## Public Files

After the first successful workflow run, GitHub Pages will expose files like:

```text
https://<github-user>.github.io/<repo-name>/active.tle
https://<github-user>.github.io/<repo-name>/starlink.tle
https://<github-user>.github.io/<repo-name>/formosat.tle
https://<github-user>.github.io/<repo-name>/status.json
https://<github-user>.github.io/<repo-name>/catalog/44343.tle
```

For the emergency SatelliteMap patch, point the existing active TLE download URL
to:

```text
https://<github-user>.github.io/<repo-name>/active.tle
```

## Setup Steps

1. Create a new GitHub repository, for example `tle-cache`.
2. Copy this folder's contents into that repository.
3. Push the repository to GitHub.
4. In GitHub, open `Settings > Pages`.
5. Under `Build and deployment`, choose `Deploy from a branch`.
6. Select branch `main` and folder `/docs`, then save.
7. Open `Actions > Update TLE Cache`.
8. Click `Run workflow` once to generate the first real TLE files.
9. Wait for GitHub Pages to publish the updated `docs` directory.
10. Test the public URL in a browser:

```text
https://<github-user>.github.io/<repo-name>/active.tle
```

If the workflow cannot commit, open `Settings > Actions > General` and make sure
workflow permissions allow read and write access.

## Local Git Commands

From this folder:

```powershell
git init
git branch -M main
git add .
git commit -m "Initial TLE cache"
git remote add origin https://github.com/<github-user>/<repo-name>.git
git push -u origin main
```

## How Updates Work

The workflow runs:

```text
17 */2 * * *
```

That means every 2 hours at minute 17 in UTC. The offset avoids the busiest
times around the top of the hour.

The update script downloads:

```text
https://celestrak.org/NORAD/elements/gp.php?GROUP=ACTIVE&FORMAT=TLE
https://celestrak.org/NORAD/elements/gp.php?GROUP=STARLINK&FORMAT=TLE
```

It also downloads each satellite listed in `tracked_sats.json` by catalog
number and combines those records into `formosat.tle`.

## Safety Behavior

The script validates that downloaded content is real three-line TLE data before
publishing it. If CelesTrak returns a "data has not updated" message, an error
page, or any malformed text, the script keeps the previous public file.

This prevents a temporary upstream response from overwriting a working TLE file.

The script also reads the previous `docs/status.json` before contacting
CelesTrak. If a catalog was updated successfully less than 2 hours ago, that
catalog is skipped and the existing public file is kept. This prevents manual
workflow reruns or overlapping schedules from creating unnecessary upstream
requests.

If an upstream request fails, the workflow still writes `status.json` so the
public status page records the error. It does not retry failed CelesTrak
requests in a loop.

## Tracking More Satellites

Edit `tracked_sats.json`:

```json
[
  {
    "name": "FORMOSAT-7-3",
    "catnr": "44343"
  }
]
```

Add more entries when you confirm their NORAD catalog numbers. The workflow will
publish one file per catalog number under `catalog/` and one combined
`formosat.tle`.

## Emergency Client Change

Keep the SatelliteMap parser unchanged for now. Only change the active TLE URL
from CelesTrak to the GitHub Pages URL:

```text
https://<github-user>.github.io/<repo-name>/active.tle
```

The file format remains traditional TLE, so the existing three-line parsing
logic can continue to work.
