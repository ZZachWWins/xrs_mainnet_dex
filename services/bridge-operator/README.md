# Xeris settlement operator

This service connects the DEX's Solana USDC purchase and xUSDC redemption screens to durable, recipient-bound settlement. It is separate from the L1. The L1 binaries, consensus, fees, genesis, and producer services are unchanged.

## Routes

| Route | User approval | Operator settlement |
| --- | --- | --- |
| Solana USDC → native XRS | Ownership message, then exact USDC transfer with the intent digest in its memo | Verify finalized deposit; mint backed xUSDC; swap; unwrap; deliver native XRS to the entered mainnet address |
| xUSDC → Solana USDC | Recipient-bound ownership message, then exact xUSDC burn | Verify burn and chain evidence; execute the exact USDC payout through the reserve vault |
| Native XRS → Solana USDC | DEX wrap and sell approvals first; then the redemption above | Native XRS is never silently treated as xUSDC |

`api-contract.json` documents the HTTP contract. The executable binds only `127.0.0.1:8791`. The intended public prefix is `https://mainnetrpc.xerisweb.com/settlement/v1`. The website reads fresh capabilities before offering payment approval. A disabled or unreachable service prevents funding.

## Current operating limits

The initial journal policy permits at most 500,000 raw USDC per request (USDC has six decimals), one active settlement route, and an explicit local authorization lasting at most 24 hours. The disabled policy example caps the authorization at two requests and 1,000,000 raw USDC in aggregate. Each intent reserves its full exposure and one request before the source transaction is prepared. Cancelled or failed requests retain that authorization's reservation, so repeated dust requests cannot cause unbounded operator gas spending. Same-intent recovery does not consume another request. Exhausting the request count blocks new admission while permitting already-reserved obligations to finish within the remaining identity, money and expiry constraints. Raising limits or renewing an authorization is an operator decision, not a browser action.

Solana deposits/payouts require finalized RPC evidence. Xeris stages require exact signed-wire, receipt, state-accounting and block evidence from the three configured nodes, plus twelve later blocks. This is bounded agreement between the existing nodes; it is not a trustless bridge or a proof of irreversible cross-chain finality. No new bridge fee is added. Trading uses the existing pool fee and price impact; network fees are shown separately.

The current executor is deliberately serialized. It has not been demonstrated as a high-volume public service. Its archive must run continuously: the raw node feed exposes fifty blocks. It stores public evidence beyond that window but fails closed if an outage loses unseen raw blocks. Accounting windows are bounded to 150 blocks and known instruction effects. Missing, conflicting, old, or unsupported evidence requires reconciliation; it does not authorize a guessed payout, replacement transaction, or refund.

## Safety and recovery

SQLite WAL/FULL stores immutable intent bindings, exact original signed bytes, stage IDs, obligations, reservation budgets, and exclusive route fences. An original transaction is durably recorded before its one submission attempt. Timeout or crash recovery observes that original; it never automatically resends or replaces it. Confirmed failed mint evidence can permit a refund. Pending or uncertain mint evidence cannot.

Every startup pauses processing. `--check` cannot activate it. A current policy, signer identity checks, sufficient backing and gas, and complete observer evidence are required before `--enable-processing` can succeed. A graceful shutdown persists a pause and blocks new helper/network calls. A request already in flight is reconciled from its original signature after restart.

The local helper is the only component that loads a private key. It returns only a verified public identity result or a public signed transaction. It makes no network calls and emits generic failures. Policy files contain public identities, paths and limits, never key contents. Do not log or publish a policy, state directory, request bearer token, key file, or recovery database. Never put operator code or state under the site's `dist/` directory.

## Offline verification

Use Python 3.11 or later with SQLite, OpenSSL Ed25519 verification, and Node 22 or later for the local helper. The operator has no third-party Python dependencies. Check the installed Python build supports `hashlib.scrypt`. On the current Mac, pass the explicit Node executable if it is not in PATH.

```sh
python3 -m unittest discover -s . -p 'test_*.py'
```

Tests use synthetic identities and public fixtures. They never load an existing user key or submit a transaction. Two optional local SDK comparison tests can skip where the separately built reference SDK binary is absent; the portable frozen vectors and decoder/tampering tests still run. The browser suites are run from the repository root with `npm test` and `npm run build`.

## Paused deployment

Create a private state directory outside the website and all old pilot directories. Use a dedicated route wallet, not a treasury, producer, or LP wallet. A missing authorization path is appropriate for an initial paused deployment.

```sh
python3 operator_main.py \
  --state-directory /absolute/private/settlement-state \
  --operator-wallet PUBLIC_ROUTE_WALLET \
  --authorization-path /absolute/private/settlement-authorization.json \
  --authorization-id settlement-v2-pending
```

This process observes public data and serves disabled capabilities. It does not enable signing. `observer_daemon.py` can instead run the archive alone, with a separate state directory. The operator's archive and standalone archive must not share a state directory concurrently.

The current reader expects the two Mac node read ports 56001/56002 and RPC ports 50008/50010, plus the pinned droplet HTTPS endpoint. Keep the runtime on that Mac unless the reader topology is explicitly reviewed. Publish its loopback API through an existing approved SSH connection and the dedicated Nginx route in `deploy/`; do not expose port 8791 directly. Use an independently supervised tunnel and process, with pause-on-restart preserved. An unavailable tunnel makes capabilities unavailable.

A session tunnel is sufficient for paused HTTPS verification, not unattended operation. A per-user macOS LaunchAgent also depends on that user's login session. Before enabling public funds, validate an approved persistent service setup through disconnect, service restart and host reboot. Respect macOS protected-folder permissions; do not move existing private credentials or grant broad disk access merely to work around a denied background process. A separately authorized forwarding-only credential can isolate the tunnel's access from node administration.

## Activation and funded acceptance

`deploy/authorization.example.json` is intentionally disabled with placeholder paths and timestamps. It grants no authority. The operator must approve a concrete per-request/aggregate USDC budget, maximum request count, SOL fee/rent ceiling, exact signing identities and expiry. Only then create a private 0600 policy and explicitly activate. Do not reuse closed pilot or wallet-funding permissions. A startup flag alone is insufficient. Keep first funded acceptance restricted to the approved participants; the serialized beta's reservation capacity is not an anti-abuse queue for unrestricted traffic.

Before public intake, complete the following with a real user wallet and approved funds:

1. Verify the HTTPS capabilities, origin restrictions, exact chain identity, fresh reserve/backing, all signer identities and fee balances.
2. Approve a small deposit from a supported Solana wallet. Verify the quote and entered Xeris recipient, memo-bound signed transfer, original signature recovery, native delivery, and the receiving wallet's balance.
3. Approve the intended wrap/sell and redemption from the Xeris wallet. Verify the exact Solana recipient and actual final USDC credit, including a newly created token account when applicable.
4. Compare reserve USDC, issued xUSDC, pool balances, gas, and all outstanding obligations. Preserve public receipt IDs and measured timings without wallet secrets.
5. Verify refresh/reconnect on the actual iOS Web4, Android Web4 and extension providers intended for release. Provider schema tests do not replace device acceptance.

Keep the original databases and archive when pausing, upgrading or recovering. Do not reset them to clear an unresolved request. Reconcile ambiguous transactions by their stored IDs before considering an amended authorization. The source package and synthetic tests do not themselves establish completed public funded acceptance or extend the L1's CertiK review to this new application service.
