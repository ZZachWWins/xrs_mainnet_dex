# Xeris mainnet market and mining feed

A separate read-only service for `xeris-mainnet-v4`. It polls existing node APIs on loopback, publishes a complete circulation census and streams block observations. It does not modify consensus, submit transactions, sign messages, read keys or access node files.

The deployed service is `xeris-market-feed.service` on `127.0.0.1:56100`, behind HTTPS at `mainnetrpc.xerisweb.com`.

| Public route | Response |
|---|---|
| `GET /market/supply` | Current full JSON snapshot; HTTP 503 if unavailable |
| `GET /market/status` | Freshness, chain, tip and coverage summary |
| `GET /market/stream` | SSE `snapshot` events with the same full JSON payload |

GET and OPTIONS are the only public methods. No credentials are required. JSON amounts are exact decimal strings; XRS has 9 decimals and the market-cap quote is xUSDC with 6 decimals. This is a pool-implied valuation in xUSDC, not an independent USD price feed.

## Accounting policy

Schema version **2** uses basis **`unlocked_on_chain_v2`**. Circulation counts all unlocked on-chain native XRS, unlocked wrapped XRS and approved active AMM inventory. This includes unlocked balances held by company wallets. Wrapping removes native funds and creates the same wrapped amount, so the combination counts each coin once. Burn addresses, staked, unbonding, locked and other contract-custodied holdings are excluded. Wrapped custody is reconciled against registry supply; gross issuance is only an accounting bound, never a substitute for the balance census.

The unlocked part of the 200-million genesis allocation and unlocked mining rewards are included, including amounts still held by the company. `policy.json` classifies publicly identified company wallets as `company_addresses` and `company_wallets`; the API publishes their roles and policy hash. These labels do not reduce circulation. Moving funds between a treasury wallet and another unlocked wallet leaves circulation unchanged; newly mined unlocked rewards increase it. Locking funds decreases circulation until those funds return to an unlocked wallet or approved active AMM. This is the project's published unlocked-supply definition, not an assertion that all included balances have been distributed to public holders.

Active AMM inventory remains tradable even when the company owns its LP shares. The configured mainnet pool is explicitly approved. Protocol launchpad pools are admitted automatically only after validating the `LaunchpadManaged` mint policy, reserved canonical pool ID, protocol pool owner, pair, reserves and fee. Other newly discovered funded XRS pools make the census unavailable until reviewed.

Launched-token circulation uses complete holder and pool data with the same custody exclusions and includes unlocked company balances. Only its canonical active graduation pool provides the token/XRS price, converted through the pinned XRS/xUSDC pool. Tokens without an available current pricing source have a null market cap. Maximum supply and cumulative average bonding-curve prices are never substituted.

## Consistency and limits

Every collection verifies the pinned mainnet checkpoint and brackets all reads with an unchanged canonical slot/hash. Counts and pagination must be complete, integer balances must conserve supply, and pool identity and reserves must agree across independent read routes. The server rejects a tip older than 120 seconds and a census taking more than 15 seconds. It reads no more than 25 pages per registry and enforces a shared deadline before each request.

The native `/balances` API returns at most 100 accounts. This service requires the full returned count to equal `stats.total_accounts` and refuses to publish supply if that count exceeds 100. **A complete off-chain account index is required before expanding beyond this bound.** Storing truncated snapshots in object storage does not solve enumeration. A later index should replay canonical history, retain reversible block deltas/checkpoints, handle reorganizations and reconcile account totals before supplying circulation. No durable history database is deployed by this service.

This is a trusted-node observation using a published supply policy. It is not a cryptographic light-client proof, an audit of beneficial ownership or a claim of CertiK coverage for this new service.

## Client behavior

Collection runs approximately every 5 seconds; snapshots expire after 20 seconds. Validate `chain_id`, `schema_version == "2"`, supply basis, quote units and expiry before displaying a live value. Version 1 used an earlier company-exclusion policy and must not be displayed under the version 2 label. On unavailable data, clear live market caps. SSE uses `event: snapshot`, includes an `id`, and advertises a 5-second reconnection delay. Connections close after at most 300 seconds and should reconnect. Pause streams when the app is backgrounded. Use bounded GET polling when streaming is unavailable.

The service permits 16 simultaneous streams and 32 HTTP worker connections. Nginx has separate feed budgets: 4 concurrent connections per IP, 32 aggregate, 2 requests/second per IP and 10 aggregate. These do not consume the native RPC proxy's connection pool. Mining totals are actual cumulative emissions; `current_block_reward_xrs` is the scheduled reward, not a guarantee of each block's realized payout.

## Run and verify

Python 3.12 on the deployed host; no third-party Python packages.

```sh
python3 -m unittest discover -s . -p 'test_*.py' -v
python3 service.py --host 127.0.0.1 --port 56100 --policy policy.json
```

Tests include complete/incomplete census accounting, treasury distributions, mining, locks, wrapping, burns, spoofed pools, precision, pagination, reorgs, stale data and real loopback HTTP/SSE.

The systemd unit uses an unprivileged dynamic user, read-only code, loopback-only networking, a 64 MiB memory cap and no access to node or key directories. Initial deployment scripts validate explicit public-file hashes, start only the feed, verify readiness and the canonical block, then validate/reload Nginx. They refuse an existing feed installation. Updating an installed release requires a separately reviewed atomic release procedure; do not rerun the initial installer blindly. The producer is never restarted by feed deployment.
