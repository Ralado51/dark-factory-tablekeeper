# Run Stage 1

From this directory, build and start the standalone service:

```sh
docker build -t tablekeeper-stage1 .
docker run --rm --name tablekeeper-stage1 -e PORT=8080 -p 8080:8080 tablekeeper-stage1
```

Readiness: `curl http://localhost:8080/health`. No manual setup or runtime network access is required. The image includes the IANA timezone database. State is held in memory and is cleared on container restart. Test controls reset, export and import state without authentication; keep exports private because they include credentials and session tokens.

A different port works with matching mappings, for example `-e PORT=9090 -p 9090:9090`.

Additional contract checks can be run on a host with Python 3.12: `python verify_contract.py`. They launch two temporary service processes and print only check names and counts, never credentials or snapshots.
