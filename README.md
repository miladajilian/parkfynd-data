# parkfynd-data

Street parking files for the Parkfynd iOS app, rebuilt every night from open data. Served by GitHub Pages:
`https://<owner>.github.io/parkfynd-data/v1/index.json`.

| Source | Licence / terms |
|---|---|
| Transportstyrelsen, RDT / STFS (local traffic regulations) | Public regulations (statutes are not protected by copyright, URL 9 §) |
| Trafikverket NVDB, API Öppna Data (road links, street names) | CC0 |
| Malmö stad, parkeringsavgifter and miljöparkering | CC0 |
| Göteborgs Stad, ParkingService | "fritt att använda, utan avgifter eller licenser" |

## Layout
- `docs/v1/index.json`: one entry per municipality with `fetchedAt`, `bounds`, `center`, `count` and each file's `bytes` and `sha256`.
- `docs/v1/<id>-ptillaten.json.deflate`, `<id>-servicedagar.json.deflate`: LTF-Tolken-shaped GeoJSON, raw DEFLATE.
- `docs/v1/<id>-snapshot-meta.json`.

The app checks the sha256 before using a file. The parking rules are interpreted conservatively: anything the tools can't read becomes "Check the sign" in the app.

## Running
- `.github/workflows/publish.yml` runs nightly. It needs the repository secrets `TRAFIKVERKET_API_KEY` and `GOTEBORG_APPID`.
- To rebuild chosen municipalities by hand, start the workflow from the Actions tab with e.g. `solna sollentuna`.
- The tools in `tools/` are copied from the app repo with `data-host/sync-tools.sh`. Change them there.
