# Crossing access and bypass shortcuts

GateAhead derives access from OpenStreetMap tags on the crossing and its connected roads. It shows separate access for cars, motorized two-wheelers, and pedestrians. A mapped mode is marked allowed when all usable mapped approaches agree and the crossing has no conditional tag for that mode. Conflicting, conditional or missing approach information can leave a mode unknown. Access labels are separate from predicted barrier status. The existing public road-crossing list determines which gates users can open.

The pilot includes two road overbridges serving three road crossing groups in northern Kerala. On 24 November 2026, the bridges were checked against the state road agency’s completed project list and the OSM road and rail geometry. For each published group, both approaches had a public mapped road connection via the bridge without using an at-grade crossing. This was a desktop verification. [RBDCK completed projects](https://web.rbdck.com/index.php/projectslisting/?stage=completed)

- Kottachery Rail Overbridge for crossings on Thulicheri–Nileshwar–Azhithala Road and Puthiyotta–Kallooravi Road in Kanhangad. [Bridge geometry](https://www.openstreetmap.org/way/1043083622)
- Cheruvathur–Padanna Rail Overbridge for the crossing north of Cheruvathur station. [Bridge geometry](https://www.openstreetmap.org/way/924999685), [Kerala road agency notification](https://web.rbdck.com/wp-content/uploads/2021/04/7.-CheruvathurPuliyananm.pdf)

Shortcut coordinates and mapped access data © OpenStreetMap contributors. [OpenStreetMap copyright and license](https://www.openstreetmap.org/copyright)

## Update the catalog

The packaged [crossing-details.json](../src/gateahead/data/crossing-details.json) is keyed by the same IDs returned by `/gates`. Copy it to `GATEAHEAD_DATA/crossing-details.json` to edit the full catalog for a deployment. Restart the API after editing. An empty JSON object disables the packaged pilot for that deployment.

Each record contains the gate’s `lat` and `lon`. A record applies only when its ID matches the selected gate and its coordinates are within 50 metres of the mapped gate. A record can contain `access` with `cars`, `two_wheelers`, and `pedestrians` values (`allowed`, `restricted`, or `unknown`), the ISO date `verified_on`, and an `evidence` URL. It can also contain `bypasses`, a list of up to five bridge points, each with `kind` (`flyover` or `underpass`), `name`, navigation `lat` and `lon`, `verified_on`, and `evidence`. Verification applies for up to 365 days and future dates are ignored. Recheck both approaches and update the evidence and date to renew an entry. Add `replacement: true` only when the bridge specifically replaces that crossing. Place the navigation point on the bridge roadway so the maps app can route to it. The user’s maps app chooses the route.

Use `GET /crossing-details?crossing_id=…&lat=…&lon=…` for crossing access and bypasses without looking up train predictions. Use the crossing's mapped coordinates. The response reports map freshness and resolves known legacy crossing IDs. A fresh map without the requested crossing returns `404` with `detail.code=crossing_not_found`.

## Review outdated details

The API accepts an outdated-details flag at `POST /crossing-details/flags`. It validates the selected crossing against current map data and stores one flag per installation and crossing for up to 90 days. Flags remain private and do not update displayed details automatically. Review the affected crossings in the API's SQLite database, then update the catalog or the relevant map record once verified.

```sql
SELECT crossing_id, COUNT(*) AS flags, datetime(MAX(created_at), 'unixepoch') AS latest_utc
FROM crossing_detail_flags
GROUP BY crossing_id
ORDER BY flags DESC, latest_utc DESC;
```
