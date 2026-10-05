# Run Stage 2

From this directory, build and start the standalone service:

```sh
docker build -t tablekeeper-stage2 .
docker run --rm --name tablekeeper-stage2 -e PORT=8080 -p 8080:8080 tablekeeper-stage2
```

Browser routes: `/`, `/signup`, `/login`, `/lookup`. The entire product and its assets are served by this image.

Readiness: `curl http://localhost:8080/health`. No manual setup or runtime network access is required. The image includes the IANA timezone database. State is held in memory and is cleared on container restart. Test controls reset, export and import state without authentication; keep exports private because they include credentials and session tokens.

A different port works with matching mappings, for example `-e PORT=9090 -p 9090:9090`.

Additional cumulative contract checks can be run on a host with Python 3.12: `python verify_stage2.py`. The actual Stage 1 upgrade check reads the unchanged sibling `../stage-1/app.py`. Use `python verify_browser.py` in a Playwright-equipped environment with both stage folders present and `BROWSER_EVIDENCE` set to a writable output directory. They launch two temporary service processes and print only check names and counts, never credentials or snapshots.
