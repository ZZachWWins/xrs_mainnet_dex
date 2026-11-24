# Xeris Mainnet DEX

A new browser client for the deployed Xeris mainnet pool, using the original design, logo, token icons and fonts recovered from `dex.xerisweb.com`. This repository replaces the unavailable placeholder source. It contains public frontend and read-only market-feed source; it does not contain treasury keys, reserve keys, wallet seeds or node state.

## Run and test

Node.js 22 or later is needed for checks. There are no npm dependencies.

```sh
npm test
npm run build
npm run dev
```

The local preview is `http://127.0.0.1:8769`. `dist/` contains the maintained HTML, CSS, ES modules and bundled assets; it can be served directly over HTTPS. `netlify.toml` sets the Netlify build command, Node version and publish directory. Import this repository, select `main`, and use `npm run build` with publish directory `dist`. No environment secrets are required. `dist/_redirects` preserves the old page URLs. `vercel.json` is an optional alternative configuration. The active Mainnet application displays a persistent MAINNET BETA label. The default Testnet homepage is an inactive preview with a prominent Mainnet switch. Deploy the repository before assigning the `dex.xerisweb.com` custom domain; uploading source alone does not change its existing deployment or DNS.

## Implemented

- Homepage and responsive trading layout preserving the existing Xeris visual design.
- Live mainnet pool reserves, exact integer quotes, fee/minimum output and price impact.
- A persistent MAINNET BETA market strip: circulating XRS market cap plus protocol launchpad listings. On-chain valuations remain xUSDC; the interface shows USD equivalents by default and supports current FX conversion into 14 other major currencies. Pool depth and LP fees stay off the header.
- Live Network view: circulating supply, actual mined emissions, scheduled block reward and the latest 12 blocks, with a compact homepage overview. A read-only SSE feed updates data; expired snapshots clear immediately.
- Pool and portfolio views, distinct native XRS / wXRS / xUSDC balances.
- Explicit Xeris wallet provider connection and mainnet/checkpoint binding.
- One wallet-approved action at a time: xUSDC → wXRS, wXRS → xUSDC, wrap or unwrap.
- Verification of the returned transaction message and Ed25519 signature before broadcast.
- Public localStorage journal written before a single submission; Web Locks prevent concurrent submissions in this browser. Pending transactions are reconciled using their original signature, including after reload. Unknown results never trigger automatic resends.
- A dedicated Bridge purchase tab with separate Solana wallet selection, manual Xeris mainnet recipient entry and explicit optional Xeris-wallet autofill.
- Source-network selection with official chain marks: Solana selected; Ethereum, Base, Arbitrum, Polygon and Avalanche shown as disabled “Coming later” options.
- Capability-gated Solana USDC purchases and xUSDC redemptions with explicit ownership/payment approvals, original-ID recovery and honest paused states.

## Mainnet contract

| Field | Value |
|---|---|
| RPC | `https://mainnetrpc.xerisweb.com` |
| Chain | `xeris-mainnet-v4` |
| Pool | `xrs-xusdc-mainnet-v1` |
| Wrapped XRS token | `xrs_native`, 9 decimals |
| xUSDC token | `xeris_usdc`, 6 decimals |
| LP fee | 30 bps / 0.3% |
| Network fee per transaction | 0.001 native XRS |
| Checkpoint slot | 27201 |
| Checkpoint hash | `0a401c139eef73f049010450d4aa13ef38636799784d01bbc0ecdb9c71facaf3` |

The pool holds wXRS and xUSDC. A native-XRS buy is a swap followed by a 1:1 unwrap; selling reverses those actions. The interface requests separate approvals, with an explicit amount for conversion. A two-step route costs 0.002 native XRS in network fees. Ordinary Solana wallet providers are never used to sign Xeris transactions. The shared injected provider is `window.xeris` for the Xeris iOS wallet, Android wallet and Chrome extension, with `connect`, `getRpcUrl` and `signTransaction`. Each wallet must implement the same mainnet-bound contract; connection guidance names all three products. This does not claim a funded compatibility run on every wallet build.

The 30-second quote lifetime and 0.5% minimum output require fresh review if approval is slow. Browsers without Web Locks, persistent localStorage or Ed25519 WebCrypto fail closed. Clearing browser data removes its local transaction journal; this is not cross-device recovery. Receipt checks establish reported Xeris execution, not cross-chain finality.

## USDC bridge status

The controlled operator test already completed Solana USDC → xUSDC → native XRS → xUSDC → Solana USDC. The new website has not yet completed a funded wallet-browser round trip.

The Bridge purchase route is **Solana USDC → native mainnet XRS**. The operator verifies the source deposit, mints intermediate xUSDC, buys wXRS, unwraps it and delivers the confirmed native amount to the immutable Xeris recipient. xUSDC is an intermediate stage, not a directly credited deposit product. Enter any valid mainnet Xeris recipient, including a new empty wallet; connecting Xeris is optional for this purchase. Format validation alone cannot establish network or ownership.

The withdrawal route is **Xeris xUSDC → Solana USDC**. Users starting with native XRS first approve a 1:1 wrap and a separately quoted wXRS sale using the existing conversion/trading controls, then return to Bridge and choose the exact xUSDC amount to redeem. Each stage has its own execution receipt; a sale is not a completed withdrawal. Wrap, sale and burn cost 0.003 native XRS in total network fees, paid separately from the amount converted. The 0.3% pool fee is included in swap proceeds. A direct xUSDC redemption burns only the reviewed amount and costs 0.001 native XRS; the operator pays Solana payout fees and receiving-account rent without a USDC deduction.

Bridge uses only `https://mainnetrpc.xerisweb.com/settlement/v1`. Fresh capabilities and per-route flags gate every funded step. An offline, malformed, stale or paused service cannot produce an enabled payment or burn action. This frontend build does not enable intake, configure an operator, or deploy a service. Public availability remains dependent on separately reviewed runtime readiness. The older `operator-contract/` directory is an intake design artifact, not a deployed service or the current implementation.

A purchase or redemption requires separate deliberate steps: review exact raw-unit quote, full source/recipient and receiving minimum; sign an intent-v2 ownership message; prepare and review the source payment and fee; approve one sign-only source transaction; and submit the durably saved signed transaction once. The browser reconstructs the canonical ownership message and independently checks Ed25519 signatures. Solana deposit bytes must contain only the pinned USDC TransferChecked plus the signed-intent Memo. Xeris redemption bytes must contain one exact xUSDC TokenBurn. The burn itself does not encode a Solana recipient: the previously authenticated immutable intent and exact prepared-message binding are mandatory before approval.

Requests use canonical u64 decimal strings: USDC/xUSDC have 6 decimals; native/wrapped XRS and SOL fees have 9. Settlement quotes expire after 60 seconds. Expected output is a quote; completion shows a separate independently confirmed `actual_output_raw` amount, never an inferred quote amount. Refunds require their verified amount and explicit Solana USDC asset. Source receipts must match the browser’s original saved source signature; conflicting or incomplete evidence remains unresolved.

Public request records, original signed source bytes, ownership proof and read-only status access are durably stored in separate localStorage namespaces before network submission. No seed or private key is requested or stored. Preserve site data until requests are resolved; cross-device recovery is not implemented. Timeout/ambiguous results never trigger automatic payment retries or replacement signatures. Explicit status recovery uses the original request and proof. An expired signed source may be presented to the API only for guaranteed observation-only recovery, without broadcasting; it continues to block a new request until resolved. Pausing does not disable observation of existing obligations.

The Solana picker supports compatible Wallet Standard registrations plus explicit Phantom, Solflare and Backpack adapters. It never silently selects another wallet. Standard accounts must advertise `solana:mainnet` and match their public-key bytes. Both message signing and sign-only transaction signing are required for purchase. Ordinary Solana providers never sign Xeris transactions, and no sign-and-send fallback exists. Mobile users open the purchase flow in their Solana wallet browser and enter their Xeris recipient; redemption runs in Xeris Web4 with a manually entered Solana recipient if needed. Cross-app deep-link pairing is not implemented.

The pinned official `@solana/web3.js` 1.98.4 browser bundle and MIT license are served locally under `dist/assets/vendor/`, with registry/tarball integrity provenance and a loader integrity check. It loads only after explicit Mainnet selection; Testnet remains the inactive default and starts no wallet or mainnet API code. No runtime CDN is used. Solana is the only source network; the other disabled network tiles cannot create a deposit instruction.

xUSDC is treasury-issued on Xeris, backed by an operator-controlled Solana USDC reserve. It is separate from Circle-issued USDC. This is operator-mediated settlement, not a trustless bridge or a claim of irreversible finality. The website has not completed a funded wallet-browser settlement round trip in this implementation.

The interface does not claim that this new client/operator code was covered by the L1's CertiK audit. L1 consensus and running nodes are unchanged.

The operator implementation and its deployment/runbook are in [`services/bridge-operator`](services/bridge-operator/README.md). They are excluded from Netlify's `dist` publish directory. The service starts paused, requires a fresh scoped local signing authorization for activation, and keeps durable original transaction IDs and obligations. Read the runbook's funded wallet/device acceptance requirements before enabling public payments; passing the offline suites is not that acceptance.

## Validation

`npm test` runs 213 offline tests covering exact amounts, lossless JSON, pool identity, quote matching, wallet binding, transaction encoding, signature verification, balances/gas, storage failures, stale quotes, ambiguous submissions and original-ID recovery. Bridge tests cover provider discovery and account races, canonical ownership proofs, strict source transaction semantics, paused gates, independent actual-output/refund fields, contradictory source receipts, partial local-storage failures, bounded stalled responses, and separate deliberate UI steps. They use synthetic public fixtures, mocked verification/wallets and mocked service writes; the existing trade harness also uses synthetic in-memory keys. No real wallet prompts, keys, signatures or funded transactions were used for bridge integration validation. Browser checks exercise public read-only unavailable states at desktop and narrow viewports.

Image and font origins are recorded in `asset-manifest.json`. Live chart points are observations from the current browser session, not fabricated historic candles. LP fees remain in pool reserves for LP shareholders; there is no separate 0.777% company cut in this pool.

The independent end-to-end harness additionally exercises the complete four-step Xeris round trip and a lost submission response through the actual client modules, preserving the original transaction ID across a simulated reload. This is offline application validation, not a funded browser/Web4 or Solana settlement run. Font license notices are included in `dist/assets/licenses/`.

## Market capitalization and live network data

Market cap is current pool price multiplied by circulating supply. The API denomination is **xUSDC**; the interface displays **USD-equivalent** estimates using intended 1:1 xUSDC/USDC backing, then converts to the selected fiat currency. This is not an independent xUSDC/USD market price or a redemption guarantee. The read-only feed uses a consistent on-chain census. All unlocked on-chain XRS counts, including unlocked genesis premine, mined wallet balances and active approved AMM inventory. Company or treasury ownership alone does not exclude holdings. Actual burned, staked and locked balances are excluded. Wrapped XRS is counted once. The interface never substitutes maximum supply, gross emissions or wrapped supply for circulation, and displays no FDV.

The feed is read from the configured mainnet origin:

- `GET /market/supply`: initial snapshot and fallback polling.
- `GET /market/stream`: named SSE `snapshot` events, automatic reconnect.
- `GET /market/status`: operational endpoint, not needed for display polling.

The client requires schema version 2, supply basis `unlocked_on_chain_v2`, and policy ID `xeris-mainnet-circulation-v2`. It validates the chain, denomination, complete census flag, matching block identities, exact circulation arithmetic and price/cap multiplication. Native-wallet, company-native, wrapped-wallet, company-wrapped and active AMM components are added once; protocol launch tokens likewise include unlocked company-wallet balances. Version 1 feeds fail closed rather than reusing the old ownership-based exclusions. It preserves large integer amounts with the lossless JSON parser. Expired or invalid snapshots clear market caps, supply and block rows; the client does not extrapolate emissions or invent counters. An SSE failure changes the visible state and starts one fallback read every eight seconds. Successful streaming cancels polling. Hidden tabs close the stream and clear timers, then reconnect on return.

Protocol launchpad discovery remains separate through `/v2/tokens?limit=32&after=...`, filtering `LaunchpadManaged` and `__launchpad_managed__`. Feed valuations join only on matching token ID, name and symbol, with circulation bounded by issued supply. The feed supplies exact canonical token/XRS pricing converted through the native XRS/xUSDC ratio. Missing prices or circulation display a dash. Incomplete or failed registry pagination shows an unavailable state, not an empty successful launchpad.

The Network policy dialog lists public company wallets for transparency and explicitly marks their unlocked holdings as included. Market-cap headers use compact million/billion notation while the Network view displays the full amount to two decimals; titles and accessible labels retain exact values. This is a read-only application integration; it adds no consensus, issuance, custody or mining behavior. The client displays collector-reported data and consistency checks, not an independent full-node proof.

## Live supply and mining service

The independently deployed read-only service is documented in [`services/market-feed/README.md`](services/market-feed/README.md). Source, published circulation policy and 57 Python accounting/HTTP/streaming tests are included. Netlify serves only `dist/`; it does not deploy this service. The service runs on the RPC host with no node/key-directory access. The existing node processes and consensus code were not changed.

The [iOS endpoint handoff](docs/mainnet-ios-defi-endpoints.md) records all current public DeFi routes, transaction requirements and the new JSON/SSE market APIs. The [wallet signature note](docs/mainnet-wallet-signature-status.md) distinguishes hybrid block authentication from current Ed25519 wallet spending.

## Display currencies

USD is the default. The header selector also supports EUR, GBP, JPY, CHF, CAD, AUD, CNY, INR, KRW, BRL, SGD, AED, ZAR and MXN. Monetary values use the appropriate symbol and distinguish ambiguous currencies. Native/wrapped/token quantities, percentage fees, quotes and signed transaction amounts retain their original units. Compact header values have more precise accessible labels; full card amounts are compacted only when needed to fit.

`dist/currency.mjs` reads the selected pair directly from [biquote](https://biquote.io/docs/) using `cache: "no-store"`, no credentials and strict provider freshness. It refreshes every 12 seconds while the page is active and when a currency is selected or the page resumes. Only the currency preference is persisted. FX quotes are exact decimal ratios; raw monetary amounts never pass through floating point.

Closed, stale, missing, malformed or 60-second-old quotes are unavailable. A valid in-memory quote may remain visible while the next request is pending, only until its original expiry; request failure clears it. No stale or saved rate is used as fallback. HTTP 429/503 retry delays are respected. The footer identifies the source quote timestamp. Regional closures and weekends can make non-USD estimates unavailable. These are indicative quotes; the free provider offers no SLA. Its [terms](https://biquote.io/terms.html) permit derived product display, but no raw FX relay/API is deployed.

The interface uses restrained text statuses without glowing status dots or outlined badges. The selected DEX version remains visible above the product interface.


## DEX version switch

**Testnet is the current default**, controlled by the single `DEFAULT_NETWORK` constant in `dist/mode.mjs`. It always opens the homepage, including unqualified legacy `#swap` links. Testnet is a grey, inert presentation-only view with the exact message “Available only on Xeris Mainnet Beta” and an active “Switch to Mainnet Beta” call to action outside the disabled surface. It does not import the active app, contact APIs, discover wallets, or enable transactions.

Explicit `?network=mainnet` loads the existing Mainnet Beta application and retains its safe selected page. The version selector performs full-document navigation; entering Testnet resets to the homepage and clears the fragment so the selector remains visible at the top. Unknown or duplicate `network` parameters show a version choice without loading the app. No Mainnet data is relabeled as Testnet data, no network preference is stored, and no actual Testnet integration is implied. To change the eventual launch default, edit that one constant and retest/build.
