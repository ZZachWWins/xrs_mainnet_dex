# Xeris mainnet DeFi endpoints — iOS / Xcode handoff

**Core endpoints verified November 23, 2026, 9:58 p.m. Eastern (November 24, 02:58 UTC); corrected version 2 market feed verified at 10:25 p.m. Eastern (03:25 UTC).** The mainnet API is live at **`https://mainnetrpc.xerisweb.com`**. Public reads, the fixed pool, both quote directions and the deployed market feed were checked. No transaction was submitted during these audits. The deployed Nginx configuration hashes match the reviewed configuration including the feed routes.

**Website deployment is a separate status:** at this check, `https://dex.xerisweb.com` still served the older Next.js website, not the new static `MAINNET BETA` application committed to GitHub. Deploy and verify the new website before treating that hostname as the completed mainnet wallet integration.

## 1. Addresses to configure

| Purpose | Address / value | Current status |
|---|---|---|
| Mainnet JSON-RPC, REST and signed submission | `https://mainnetrpc.xerisweb.com` | Live; use this HTTPS origin for all Xeris mainnet API calls below |
| New mainnet DEX website | `https://dex.xerisweb.com` | Intended Web4 destination; old application still served at audit time |
| New DEX source | [ZZachWWins/xrs_mainnet_dex](https://github.com/ZZachWWins/xrs_mainnet_dex) | New application source on `main` |
| Mainnet Android/browser reference | [ZZachWWins/xrs_mainnet_android](https://github.com/ZZachWWins/xrs_mainnet_android) | Mainnet defaults and browser integration on `main` |
| Existing launchpad website | `https://launchpad.xerisweb.com` | Reachable HTTP 200; not verified as a mainnet-integrated launchpad |
| Existing token-lock website | `https://lock.xerisweb.com` | Reachable HTTP 200; not verified as a mainnet-integrated locker |
| Existing explorer website | `https://explorer.xerisweb.com` | Reachable HTTP 200; do not assume it indexes this new mainnet |
| Mainnet network-status API | `https://mainnetrpc.xerisweb.com/v2/stats` | Live JSON, not a human-facing explorer website |
| Circulating supply / market / mining snapshot | `https://mainnetrpc.xerisweb.com/market/supply` | Live read-only JSON; schema and policy in section 10 |
| Market-feed readiness | `https://mainnetrpc.xerisweb.com/market/status` | Live read-only JSON; 200 ready / 503 unavailable |
| Market-feed events | `https://mainnetrpc.xerisweb.com/market/stream` | Live SSE, `event: snapshot`; not WebSocket or transaction subscriptions |
| Old testnet RPC | `https://rpc.xerisweb.com` | Separate old testnet; **do not use for mainnet** |
| Old DEX bookmark | `https://xeris-dex.vercel.app` | Legacy address; new Android defaults use `dex.xerisweb.com` |
| Optional Solana reserve reads | `https://api.mainnet-beta.solana.com` | Solana mainnet only; not a Xeris RPC or an automatic settlement API |

The old DEX bundle uses same-origin `/api/node` and `/api/rpc` proxies. These are **legacy website implementation paths**, not the new mainnet API contract. The new DEX calls `mainnetrpc.xerisweb.com` directly.

Do not configure raw droplet ports, a WebSocket URL, a Solana genesis hash as the Xeris chain ID, or a made-up bridge API. No API key, cookie or authorization token is required for the public Xeris reads. Transaction authority comes from the user's signature.

## 2. Network, asset and pool identity

| Setting | Exact value |
|---|---|
| Xeris consensus chain ID | `xeris-mainnet-v4` |
| Durable checkpoint route | `GET /v2/block/slot/27201` |
| Expected checkpoint slot | `27201` |
| Expected checkpoint hash | `0a401c139eef73f049010450d4aa13ef38636799784d01bbc0ecdb9c71facaf3` |
| Pool contract ID | `xrs-xusdc-mainnet-v1` |
| Pool type / active flag | `Swap` / `true` |
| Pool creation slot | `27201` |
| Pool owner | `HJBaUVbGbRm1JFt6kuC9koxvx2H5NuPsxt4cMuR315iX` |
| Pool token A | `xrs_native` — wrapped XRS, **9 decimals** |
| Pool token B | `xeris_usdc` — xUSDC, **6 decimals** |
| Native currency | XRS, **9 decimals**; balance is separate from wrapped XRS |
| Native XRS UI identifier | `xrs` is an app identifier; it is not the pool's token ID |
| LP trading fee | **30 basis points = 0.3%**, retained in pool reserves |
| Network transaction fee | **1,000,000 raw native XRS = 0.001 XRS** per transaction |
| xUSDC mint authority | `4p897nUzAxU9xbaQH69fa3S3hvtn9o1STiobHX4vnLoL` |

This is the existing three-producer mainnet commissioning chain on the Mac and droplet, separate from the old testnet. Chain ID is source configuration, not a field proven by `getVersion`. Compare the checkpoint's `data.slot` and `data.hash`; the check is a trusted-RPC identity check, not a cryptographic light-client proof.

Token and contract identifiers are case-sensitive strings. Display names and tickers are not unique identities. Native XRS, `xrs_native`, `xeris_usdc`, Solana USDC and the existing Solana Xeris token must remain distinct in the UI and data model.

At this audit the pool contained **709.7 wXRS and 10.101886 xUSDC**. This is a timestamped observation, not fixed pricing. Read fresh reserves and quotes for each trade. The original opening price of 0.01423 must not be hardcoded as the current executable price.

## 3. Complete public REST surface

All paths below are relative to `https://mainnetrpc.xerisweb.com`. Read routes accept **GET and OPTIONS**. `/submit` accepts **POST and OPTIONS**. Other methods return 405; paths outside the allowlist return 404. URI-encode individual IDs, rather than constructing paths from unchecked user input.

| Purpose | Method and path | Response / parameters |
|---|---|---|
| Pool discovery | `GET /v2/pools?limit=32&after=POOL_ID` | `{success,pools,count,next_after}`. **Array is `pools`, not `data`.** Default/max limit 32. `count` is the registry total, not necessarily this page's length. |
| Fixed pool details | `GET /contract/xrs-xusdc-mainnet-v1` | `{success,contract}`; pool fields are inside `contract.state.Swap`. Includes exact reserves, fee, total shares and `lp_shares`. |
| Buy wXRS using xUSDC | `GET /contract/xrs-xusdc-mainnet-v1/quote?input_token=xeris_usdc&amount=RAW` | `{success,quote}`. `amount` is xUSDC raw units, 6 decimals. |
| Sell wXRS for xUSDC | `GET /contract/xrs-xusdc-mainnet-v1/quote?input_token=xrs_native&amount=RAW` | Same quote shape. `amount` is wXRS raw units, 9 decimals. |
| Native and held-token balances | `GET /token/accounts/ADDRESS` | `{address,native_xrs,token_accounts}`. **`native_xrs` is a raw integer**, despite its name. Token rows have `token_id,symbol,balance,balance_display,decimals`. Only positive token holdings are listed. |
| Exact token balance and metadata | `GET /token/balance/ADDRESS/TOKEN_ID` | `{address,token_id,balance,token_info}`. Raw balance; metadata can be null for an unknown token. Useful for known tokens with zero holdings. |
| Token registry, explorer form | `GET /v2/tokens?limit=32&after=TOKEN_ID` | `{success,data,count,next_after}`; default/max limit 32. |
| Token registry, control form | `GET /tokens?limit=16&after=TOKEN_ID` | `{tokens,count,next_after}`; default/max limit **16**, no `success` wrapper. |
| Wallet summary / paged token holdings | `GET /wallet/ADDRESS?limit=32&after=TOKEN_ID` | `{address,balance,unit,tokens,token_count,next_after}`. `balance` is raw native XRS; `tokens` is a map. This route does not provide authoritative stake. |
| Account summary | `GET /v2/account/ADDRESS` | `{success,data}` with raw `balance`, raw `stake`, and display / validator fields. |
| Persisted account transaction history | `GET /v2/account/ADDRESS/transactions?page_size=20&before=CURSOR` | `{success,data,pagination,cursor}`. Pass returned `cursor` as the next `before`; page_size max 200. Legacy `page` is capped at 50. |
| Recent transaction detail | `GET /v2/tx/SIGNATURE` | `{success,data}` with status, instruction statuses, decoded details, block slot/hash and confirmations. Recent-block horizon applies; see section 7. |
| Stake metadata | `GET /stake/ADDRESS` | Raw `stake`, validator / blocks-proposed fields and reward estimates. These estimates do not grant permission to join the producer roster. |
| Validator list | `GET /v2/validators` | `{success,data,total_staked,total_staked_xrs,validator_count}`. |
| Network status | `GET /v2/stats` | `{success,data}`: current slot, height, transaction/account counts, total stake, validators and TPS estimate. |
| Recent block list | `GET /v2/blocks?page=1&page_size=20` | `{success,data,pagination}`; page_size default 20/max 100. |
| Block by slot | `GET /v2/block/slot/SLOT` | `{success,data}`. Has indexed disk fallback; use for the fixed checkpoint. |
| Block by hash | `GET /v2/block/hash/HEX_HASH` | `{success,data}`. Recent-block lookup; do not assume archival hash lookup. |
| Global transaction list | `GET /v2/transactions?page=1&page_size=20` | `{success,data,pagination}`; page_size default 20/max 100. |
| Liveness | `GET /health` | `{"status":"ok"}`. Does not prove synchronized or advancing consensus. |
| Circulating supply / market / mining | `GET /market/supply` | Full market snapshot; 200 when `status:"ready"`, 503 when `status:"unavailable"`. See section 10. |
| Market-feed status | `GET /market/status` | Snapshot metadata, freshness, `as_of`, coverage and optional reason; omits market amounts. Same 200/503 status rule. |
| Market-feed streaming | `GET /market/stream` | `text/event-stream`, named `snapshot` events containing the full snapshot. Established streams can publish an unavailable snapshot while HTTP remains 200. |
| **Signed transaction submission** | **`POST /submit`** | `{"tx_base64":"BASE64_SIGNED_LEGACY_TRANSACTION"}`. Sole public write endpoint. Acknowledgment is not execution confirmation. |

For cursor-based registries, keep requesting `after=next_after` until null. Reject repeated/nonadvancing cursors and apply a bounded loading budget. A transport error must not be rendered as a confirmed zero balance.

Pool summary prices (`price_a_in_b`, `price_b_in_a`) and quote `effective_price` use **raw-unit ratios**. Decimal-adjust them for display. Prefer exact reserves and amounts over rounded display fields. Summary volume, TVL and APR are node-derived estimates, not signing inputs or promised returns.

### Quote example — observed, not reusable authorization

```http
GET /contract/xrs-xusdc-mainnet-v1/quote?input_token=xeris_usdc&amount=500000
```

```json
{
  "success": true,
  "quote": {
    "input_token": "xeris_usdc",
    "input_amount": 500000,
    "output_token": "xrs_native",
    "output_amount": 33374770503,
    "fee": 1500,
    "fee_bps": 30,
    "price_impact_pct": "4.72",
    "effective_price": 0.00001498137642489724
  }
}
```

This observed quote spends 0.50 xUSDC and estimates 33.374770503 wXRS before a separate native-XRS unwrap. `fee` is in the **input token's** raw units. Requery before signing; reserves can change between requests.

Use exact decimal-to-integer conversion and lossless JSON parsing. Swift `UInt64` decoding is appropriate for raw u64 fields; use checked wide-integer arithmetic for products. Do not round amounts through `Double`, JavaScript `Number`, `balance_display` or `amount_xrs`. In transaction history, `amount_xrs` is not a safe token-value field: token burns/transfers can carry amounts with other decimals, and generic contract calls may show zero. Inspect `tx_type`, token ID and instruction details.

## 4. Complete JSON-RPC methods

Send **POST to `/`**, with `Content-Type: application/json`:

```json
{"jsonrpc":"2.0","id":1,"method":"getLatestBlockhash","params":[]}
```

| Method | Parameters | Relevant result |
|---|---|---|
| `getBalance` | `["ADDRESS"]` | `{context:{slot},value:RAW_NATIVE_XRS}` |
| `getAccountInfo` | `["ADDRESS"]` | `{context:{slot},value:{lamports,stake,isValidator,owner,executable}}` |
| `getSlot` | `[]` | Integer current slot |
| `getBlockHeight` | `[]` | Integer chain height |
| `getLatestBlockhash` | `[]` | `{context:{slot},value:{blockhash,lastValidBlockHeight}}` |
| `getRecentBlockhash` | `[]` | Alias of the current latest-blockhash response |
| `getBlock` | `[SLOT_NUMBER]` | Recent block summary, or null |
| `getTransaction` | `["SIGNATURE"]` | Recent transaction and execution metadata, or null |
| `getSignaturesForAddress` | `["ADDRESS",{"limit":20}]` | Persisted signature history; limit is clamped to 200. No `before` cursor here; use REST account history. |
| `getHealth` | `[]` | `"ok"` |
| `getVersion` | `[]` | Version strings; **not a network identity proof** |

Check HTTP status, top-level `error`, and **`result.error`**. Unknown methods and missing parameters can return an error inside an HTTP 200 `result`.

Not implemented as Xeris RPC methods: `sendTransaction`, `simulateTransaction`, `getFeeForMessage`, `getSignatureStatuses`, `getGenesisHash`, subscriptions or WebSocket service. Do not use an unmodified Solana RPC client workflow against this endpoint.

`blockhash` is a **64-character hexadecimal representation of 32 bytes**. Decode the hex into the legacy message's blockhash field. Some SDK encoders require base58 text for those same bytes; convert bytes correctly rather than passing the hex string as base58. `lastValidBlockHeight` is computed as **slot +150**; compare against `getSlot`, not a fixed wall-clock timeout.

## 5. Supported signed DeFi instructions

The pool trades token balances; native XRS must be wrapped separately. The current web DEX requests one action and one wallet approval at a time.

| Action | Signed instruction | Exact body |
|---|---|---|
| Native XRS → wXRS | Wrap variant **13** | `u32_le(13) || u64_le(amount_raw)` |
| wXRS → native XRS | Unwrap variant **14** | `u32_le(14) || u64_le(amount_raw)` |
| xUSDC → wXRS | ContractCall variant **4**, method `swap_b_to_a` | Pool ID `xrs-xusdc-mainnet-v1`; UTF-8 JSON args `{"amount":RAW,"min_output":RAW}` |
| wXRS → xUSDC | ContractCall variant **4**, method `swap_a_to_b` | Same args format; input uses 9 decimals, output 6 |
| Add pool liquidity | ContractCall variant 4, `add_liquidity` | `amount_a`, `amount_b`, `min_lp_shares`, `min_amount_a`, `min_amount_b` as exact raw JSON integers |
| Remove pool liquidity | ContractCall variant 4, `remove_liquidity` | `shares`, `min_amount_a`, `min_amount_b` as exact raw JSON integers |

For ContractCall, bytes are:

```text
u32_le(4)
u64_le(byte_length(pool_id)) || UTF8(pool_id)
u64_le(byte_length(method))  || UTF8(method)
u64_le(byte_length(args))   || UTF8(args_json)
```

The add/remove methods exist on-chain and can use the same signed `/submit` path; there is no separate published LP-quote endpoint or complete LP-management UI in the new website. Port the existing client integer/slippage calculations rather than guessing minima.

Use a **legacy Solana-style Message and Transaction envelope**, not a v0/versioned transaction and not a Solana SPL-token swap instruction. For these single-user DeFi actions, the fee payer/caller is the user's writable signer, the instruction references that account, and program ID is `11111111111111111111111111111111`. With this fixed one-instruction layout, message header is `[1,0,1]`, account keys are `[wallet,system_program]`, and instruction program index is 1. Transaction vectors use compact/short-vector lengths; Xeris instruction strings/vectors above use u64 lengths.

Ed25519-sign **the serialized compiled Message bytes directly**; no extra prefix, chain-ID string or prehash is added on this ordinary path. The complete wire transaction is base64 for `/submit`; the first 64-byte signature encoded as base58 is its canonical transaction ID.

**Wallet authorization and block authentication are different layers.** These ordinary wallet actions currently use one Ed25519 signature. Each mainnet block additionally requires its proposer to authenticate with both Ed25519 and ML-DSA-65. A registered PQ wallet key does not impose a general second-signature requirement on ordinary transfers, swaps, wraps or unwraps. The legacy `PqSignedTransfer` instruction is disabled in the supplied node source. Do not describe this as end-to-end quantum-proof wallet spending or implement that disabled instruction in iOS. See [wallet signature evidence](mainnet-wallet-signature-status.md).

The new DEX's client policy is a **30-second quote age** and **0.5% slippage**. For a reviewed output `Q`, its minimum is `ceil(Q ×9950 /10000)`, must remain positive, and must not exceed Q. Quote age is a client rule; blockhash validity is a separate protocol rule. Every step needs **0.001 native XRS** for fees, even when the input is xUSDC or wXRS. A swap+unwrap or wrap+swap therefore needs 0.002 native XRS across two transactions. No public automatic starter-gas service exists.

## 6. Required iOS Web4 wallet bridge

For the requested browser-based DEX flow, the iOS app must inject a compatible **`window.xeris`** provider into its WKWebView. Opening the website in an ordinary browser without that provider does not create a Xeris wallet connection. The new DEX deliberately does not fall back to `window.solana` or an ordinary Solana wallet.

Required provider contract:

```javascript
window.xeris.isXerisWallet === true // or isXeris === true
await window.xeris.connect()
// { publicKey: { toString: () => "BASE58_XERIS_ADDRESS" } }
// Existing provider also returns a String object carrying publicKey.

await window.xeris.connect({ onlyIfTrusted: true })
// Return the currently active, already-approved wallet; do not silently connect a different one.

await window.xeris.getRpcUrl()
// Exactly "https://mainnetrpc.xerisweb.com"

await window.xeris.signTransaction(unsignedTransactionBase64)
// { signature: "BASE58_SIGNATURE", signedTransaction: "BASE64_SIGNED_TRANSACTION" }
```

`signTransaction` must **sign without broadcasting**, preserve the reviewed message/account/program bytes, and return the complete signed transaction. The website verifies the signature and unchanged message, records the transaction ID, then makes its own single `/submit` request. `signAndSendTransaction` must not be substituted.

The existing injected script supports WKWebView messages at `window.webkit.messageHandlers.XerisWallet.postMessage(message)`, where `message` is JSON containing `{id,method,params}`. Native replies invoke `window.__xerisResponse(id,result,error)`. Native `connect` returns `{publicKey:"ADDRESS",network:"xeris-mainnet"}`; `getRpcUrl` returns `{rpcUrl:"https://mainnetrpc.xerisweb.com"}`; `signTransaction` returns the two fields above. Use origin-specific connection approval and wallet authentication before signing. Do not export mnemonics or private keys through JavaScript or RPC.

Rebind the origin, active wallet and network at each approval. Support disconnect/account-change events. The current DEX rechecks `connect({onlyIfTrusted:true})` rather than relying solely on a potentially stale `provider.publicKey` property.

The website currently requires persistent `localStorage`, **Web Locks** for cross-tab exclusion and **Ed25519 WebCrypto verification**. Verify these on the actual target iOS/WKWebView versions; unsupported environments stop trading. Its local public journal survives normal reloads, but clearing website data removes that journal. Native apps building their own transaction screens can use an equivalent durable native journal, but the Web4 flow still needs the injected provider contract.

Reference code: [provider script](https://github.com/ZZachWWins/xrs_mainnet_android/blob/main/assets/xeris_provider.js), [native bridge](https://github.com/ZZachWWins/xrs_mainnet_android/blob/main/lib/core/services/wallet_bridge.dart), [DEX transaction module](https://github.com/ZZachWWins/xrs_mainnet_dex/blob/main/dist/trade.mjs), [DEX read/quote module](https://github.com/ZZachWWins/xrs_mainnet_dex/blob/main/dist/model.mjs).

The existing Flutter iOS source has native swap feature gates. The new Android repository does not remove or validate those gates for an iOS release. No funded iOS/WKWebView mainnet trade has been demonstrated by this endpoint audit.

## 7. Confirmation and recovery — important receipt horizon

Persist **network, wallet, action, exact amounts, message and canonical signature before submitting**. Submit once. A timeout, HTTP 429/5xx, missing acknowledgment or mismatched acknowledgment leaves that original ID unresolved; none authorizes signing and sending a replacement.

For these one-instruction actions, `getTransaction` must match the original signature and a valid slot, with `meta.err:null` and exactly `instructionOutcomes:[{"index":0,"ok":true}]` before the UI calls the action successful. `OutcomeUnknown`, missing outcomes, null results and contradictory/malformed fields remain pending. The node's recognized failure shapes are `"ExecutionFailed"` or `{"InstructionError":[0,"ExecutionFailed"]}`; the current DEX additionally requires the single explicit `ok:false` outcome to resolve failure. Unknown error shapes remain pending.

**The node retains only 1000 recent blocks in memory.** JSON-RPC `getBlock`, `getTransaction`, REST `/v2/tx/SIGNATURE` and block-by-hash lookups depend on this recent window. An older transaction can become unavailable there even after successful execution. Do not equate null/not-found with rejection or nonexecution.

For illustration, this audit saw current slot 29145 while `getBlock(27201)` returned null, yet `GET /v2/block/slot/27201` returned the correct durable checkpoint. The controlled round-trip burn at slot 28325 was still within the recent window and its complete success receipt was available.

Account transaction history and `getSignaturesForAddress` use the persisted receipt/history store and remain useful for older activity. Their summaries are not a substitute for the full original instruction/message verification. Preserve completed receipts locally. If an unresolved transaction ages out, require reconciliation using the preserved ID/history and operator evidence instead of automatic replacement. Receipt success means reported Xeris execution; it is not an irreversible cross-chain finality proof.

## 8. Node APIs that are not publicly exposed

These routes exist in the supplied node but are excluded by the current mainnet proxy. Expect 404 at the public origin; do not wire visible iOS controls to them as though they were available.

| Capability | Implemented but blocked read paths |
|---|---|
| Generic contract discovery/state | `/contracts`, `/contract/OTHER_ID`, `/v2/contracts`, `/v2/contract/ID` |
| Other pool quotes | `/contract/OTHER_ID/quote` — only the fixed XRS/xUSDC pool is allowed |
| Launchpad discovery and quotes | `/launchpads`, `/launchpad/ID/quote?xrs_amount=RAW` |
| Launchpad vesting query | `/contract/ID/vesting/WALLET` — this is not generic TimeLock status |
| Historical pool charts | `/price-history?pool_id=ID&limit=N`, `/pools/price-history` |
| Raw node supply/mining/fee economics | `/network/economics` — remains blocked; use the new `/market/supply` snapshot for published market/mining data |
| Pending unstaking details | `/account/ADDRESS/unstaking` |
| Governance reads | `/governance/proposals`, `/governance/lock/ADDRESS` |
| Token-holder enumeration | `/v2/token/ID/holders` |
| RWA discovery/detail | `/v2/rwa`, `/v2/rwa/ID` |
| General explorer search | `/v2/search?q=QUERY` |

The public `/contracts`, `/launchpads` and `/price-history` paths were specifically checked and returned 404. The remaining exclusions are established by the deployed exact allowlist.

Blocking a REST read route is different from disabling an on-chain instruction. `/submit` can admit supported, correctly signed instructions according to the node's authorization rules; the proxy does not turn an unsupported user action into an authorized one.

### Direct mutation endpoints and unavailable features

- Direct `/token/mint`, `/token/transfer`, `/token/burn`, `/token/create`, `/contract/deploy`, `/contract/call`, `/rwa/create` and `/rwa/update_status` routes are both proxy-blocked and deliberately disabled in native handlers. Supported operations require signed instructions through `/submit`.
- Internal `/stake`, `/unstake` and `/pq-register` admission aliases are not public. The canonical public route is `/submit`; the fixed producer roster and other protocol restrictions still apply.
- `/stake/claim` and governance write routes are disabled in the node. Airdrop is disabled on this mainnet.
- **Limit-order execution and DCA tick execution are explicitly disabled pending an oracle.** Creation/cancellation code or an old website button does not establish working execution. Keep those user flows unavailable.
- **TimeLock exists on-chain**, with a `release` contract method after its unlock condition. There is no dedicated public TimeLock REST API, and generic contract state reads are currently blocked. The existing lock website is not proof of mainnet integration.
- No generic `/swap`, `/wrap`, `/unwrap`, `/add-liquidity` or `/remove-liquidity` HTTP mutation route exists for the public app; encode the signed instructions in section 5.

## 9. Solana USDC, xUSDC and settlement availability

There is currently **no deployed public automatic deposit, issuance, redemption, payout, claim-status or gas-onboarding API**. The operator request-intake code in the DEX repository is an offline foundation, not an accessible production service. Do not invent paths such as `/bridge/deposit` or `/redeem`, display a general-purpose deposit address, or burn users' xUSDC before a real redemption workflow is available.

A controlled operator round trip has already succeeded. That does not establish an automatic service for arbitrary users or a trustless bridge. xUSDC is treasury-issued on Xeris; it is not Circle-issued native USDC.

Optional public verification values:

| Value | Exact public identifier |
|---|---|
| Solana RPC | `https://api.mainnet-beta.solana.com` |
| Expected Solana mainnet genesis | `5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d` |
| Genuine Solana USDC mint | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` |
| Reserve vault PDA | `BqLiQacEpjPKon78RHLeTYUaaENgpvzDusCYqT3RpYfZ` |
| Reserve USDC token account | `BeM8R5hvdKGwiV5jKdug3kXoe6RaFaREjihKFNSjeBD6` |

Solana `getGenesisHash` and finalized `getTokenAccountBalance` were checked separately; the reserve held 10.101886 USDC, while Xeris registry supply was 10.101886 xUSDC. This observation alone does not prove every pending settlement obligation is covered. These identifiers are for verification, **not deposit instructions**.

## 10. Deployed market feed: circulation, market cap, mining and launch tokens

**Available now:** `/market/supply`, `/market/status` and `/market/stream` are deployed read-only routes under the mainnet HTTPS origin. No authentication is required. They are served by a separate bounded collector, without changing the L1 node. The collector can read node-local routes that remain blocked to public clients; do not expose or substitute those internal routes in iOS.

The corrected feed reports **all unlocked on-chain XRS × pool spot price**, with `valuation_basis:"circulating_supply"` and no FDV substitution. This definition includes unlocked premine and mined rewards held by the company as well as public wallet balances. Ownership alone is not an exclusion. The API quote currency is **xUSDC**, not an independently verified USD price. Preserve that denomination in data and transactions. The DEX presents USD-equivalent valuations using intended 1:1 xUSDC backing, and can translate those display values to other fiat currencies; see the display-currency section below. These estimates are neither a redemption guarantee nor executable trade quotes.

### Snapshot schema, version `"2"`

`GET /market/supply` returns these top-level fields when ready:

| Field | Shape / meaning |
|---|---|
| `schema_version`, `chain_id`, `status` | `"2"`, `"xeris-mainnet-v4"`, `"ready"` |
| `observed_at`, `observed_at_ms`, `expires_at_ms` | ISO UTC time and decimal-string Unix milliseconds. Ready snapshots expire 20 seconds after observation. |
| `as_of` | `{slot,block_hash,block_height}`; integer fields are decimal strings. This identifies the common chain tip used for the census. |
| `supply` | `{status:"available",basis:"unlocked_on_chain_v2",chain_id,slot,block_hash,decimals:9,circulating_raw,total_emitted_raw,wrapped_supply_raw,components_raw,census}`. Raw amounts are decimal strings. |
| `market` | `{token_id:"native_xrs",symbol:"XRS",circulating_raw,decimals:"9",quote_token_id:"xeris_usdc",quote_symbol:"xUSDC",quote_decimals:"6",market_cap_quote_raw,price_quote_raw_numerator,price_xrs_raw_denominator,price_pool_id,valuation_basis,price_basis:"pool_spot"}`. `native_xrs` is this feed's native market identifier, not the wrapped token ID. |
| `mining` | Decimal-string `total_mined_raw,total_staked_raw,emission_cap_raw,genesis_allocation_raw,last_block_timestamp_ms,last_block_age_ms`; `current_block_reward_xrs` is a decimal display string. Supply/stake raw fields use 9 decimals. |
| `blocks` | Recent rows `{slot,hash,proposer,timestamp_ms,transaction_count}`, with integer values as decimal strings. A live activity feed, not an archival receipt API. |
| `policy` | `{id:"xeris-mainnet-circulation-v2",sha256,company_wallets:[{address,role}],included_pool_ids,automatic_pool_rule,description,native_account_limit:"100"}`. Company-wallet labels are descriptive, not exclusions; approved AMM inventory rules remain explicit. |
| `coverage` | `{complete:true,native_accounts,wrapped_holders,pools,collection_ms}`. Counts/timing here are decimal strings; `supply.census` counts and `supply.decimals` are JSON integers. |
| `launchpad_tokens` | Array described below; currently empty on this mainnet. |

`components_raw` separates `native_wallets`, `company_native`, `native_burn_addresses`, `native_nonwallet_custody`, `wrapped_wallets`, `company_wrapped`, `wrapped_burn_addresses`, `wrapped_nonwallet_custody`, `amm_inventory`, `inactive_amm_custody` and `wrapped_other_custody`.

The published circulation policy includes unlocked native balances, unlocked wXRS balances and approved active AMM inventory, including the balances labeled `company_native` and `company_wrapped`. The calculation is `native_wallets + company_native + wrapped_wallets + company_wrapped + amm_inventory`. Unlocked treasury premine and company-held mining proceeds therefore count. Actual burn addresses, staking, unbonding, locked and other nonliquid contract custody remain excluded. Pool inventory counts even when the company owns its LP shares. Wrapped XRS is counted once, not added again as independent native issuance. The policy and custody residuals are disclosed; this is not a complete named inventory of every lock contract.

The collector requires a complete native balance census, complete paginated holders and pools, and the same canonical tip before and after collection. The current native balance source is bounded to **100 accounts**: if the account count exceeds that limit or another completeness/identity check fails, the feed becomes unavailable instead of publishing a partial circulating total. New funded XRS pools need policy approval unless they satisfy the collector's verified canonical launch-pool rule. A launchpad registry alone is provenance, not a circulating-supply measurement.

At the version 2 follow-up, slot 29659 reported `circulating_raw:"200293590767123232"`, or **200,293,590.767123232 XRS**, and `market_cap_quote_raw:"2850983542990"`, or **2,850,983.542990 xUSDC**. Gross issued XRS was 200,296,590.767123232, including **3,000 staked XRS** excluded from the unlocked total. New unlocked mining rewards count even when credited to company wallets. These are timestamped observations, not constants or an amount the small pool can pay out.

For native XRS, `market_cap_quote_raw = floor(circulating_raw × price_quote_raw_numerator / price_xrs_raw_denominator)`, in 6-decimal xUSDC raw units. Displayed price per whole XRS is `(numerator / 10^6) / (denominator / 10^9)`. Use checked wide integer arithmetic or rational values; do not multiply through floating-point amounts. `xrs_native.current_supply` by itself still does not measure native circulating supply. Count the premine according to its current unlocked on-chain balances, not a hardcoded allocation or an ownership exclusion.

Version 2 supersedes the earlier company-excluding policy. Require `schema_version:"2"`, `supply.basis:"unlocked_on_chain_v2"` and `policy.id:"xeris-mainnet-circulation-v2"` for this integration. Do not reuse historical version 1 circulation or market-cap values as the current metric.

### Launched-token markets

Each `launchpad_tokens` row contains `token_id,name,symbol,decimals:"9",circulating_raw,quote_decimals:"6",quote_token_id:"xeris_usdc",quote_symbol:"xUSDC",valuation_basis:"circulating_supply",market_cap_quote_raw,status,supply`.

The row's `supply` is `{status:"available",basis:"unlocked_on_chain_v2",token_id,decimals:9,circulating_raw,issued_supply_raw,components_raw}`. Its components are `wallets,company_wallets,burn_addresses,nonwallet_custody,amm_inventory,inactive_amm_custody,other_contract_custody`. The census includes all active liquid pools for that token, plus unlocked wallet holdings including company wallets: `wallets + company_wallets + amm_inventory`. Burned and nonliquid custody amounts remain excluded; issued supply is an accounting bound, not the circulation value.

A row becomes `status:"ready"` only after verifying its reserved canonical graduation pool, protocol owner `__launchpad_pool__`, active token/wXRS pair, fee and matching reserves. It then includes `price_pool_id,price_xrs_raw_numerator,price_token_raw_denominator`; its xUSDC market cap combines that token/wXRS spot ratio with the main XRS/xUSDC spot ratio. If the canonical pool is absent or empty, the row is `status:"pricing_unavailable"` with `market_cap_quote_raw:null`, while its circulation can still be available. Do not fill the gap with another token's same-symbol market or FDV. **Zero launch-token rows were present during live verification**, so the nonempty row schema is source-reviewed rather than demonstrated by a live launched token.

### Streaming, expiry and failure behavior

Use `GET /market/stream` with `Accept: text/event-stream`, listen for **`event: snapshot`**, and parse each `data:` as the full `/market/supply` snapshot. Frames also carry an `id`; it is a process-local publication counter, not an on-chain sequence or durable replay cursor. The stream sends `retry: 5000`. The collector normally refreshes about every 5 seconds, connections end after at most 300 seconds, and clients reconnect after 5 seconds. Heartbeat comments can appear between snapshots. Reconnection delivers the current state; there is no history-replay guarantee.

The service permits at most **16 simultaneous SSE streams**; excess streams receive 429. Use one shared stream per app session and bounded GET polling as a fallback, approximately every 5 seconds with backoff. Stop background polling/streams when the app no longer needs them. Native Swift can parse SSE through streaming `URLSession`; browser clients can use `EventSource`.

For JSON routes, HTTP **503** carries `{schema_version:"2",chain_id:"xeris-mainnet-v4",status:"unavailable",reason}` when initialization, collection or freshness checks fail. An existing SSE connection sends the same unavailable object as a snapshot event without changing its HTTP 200 response. On `unavailable`, malformed data, wrong network/schema, incomplete coverage or passed `expires_at_ms`, **clear live price/circulation/market-cap values** and show unavailable or explicitly stale history. Do not keep displaying the last value as live, and do not replace unavailable values with zero.

`GET /market/status` returns only available metadata fields from `schema_version,chain_id,status,reason,observed_at,observed_at_ms,expires_at_ms,as_of,coverage`, with the same 200/503 readiness rule. All three routes are credential-free and uncached, and the public response has one `Access-Control-Allow-Origin: *` header. This SSE feed is distinct from WebSockets and does not enable node transaction subscriptions.

## 11. Transport and app-loading requirements

- HTTPS with normal system certificate validation. Keep normal ATS protections; no cleartext/certificate bypass is needed for mainnet.
- Read traffic: **5 requests/second per IP**, burst 8; **10/second aggregate**, burst 12. Each backend permits 3 concurrent requests, also 3 per client IP.
- Submissions: **10/minute per IP**, **20/minute aggregate**, each with one immediate burst slot; the unchanged node has an additional shared-loopback write quota.
- HTTP 429 includes `Retry-After: 10`. Pace/coalesce reads; retry reads within a bounded budget. Do not automatically retry transaction writes.
- JSON-RPC bodies: 64 KiB maximum. `/submit` bodies: 256 KiB maximum. Native RPC proxy read timeout 10 seconds; market SSE uses a 65-second idle proxy timeout with heartbeats. JSON is uncached (`Cache-Control:no-store`).
- Market routes have separate budgets: 2 requests/second per IP (burst 4), 10/second aggregate (burst 20); 4 concurrent connections per IP and 32 total at the proxy. The service independently caps SSE streams at 16. Follow section 10's shared-stream/polling guidance.
- CORS permits `*`, with GET/POST/OPTIONS and `Content-Type`; it is credential-free. No WebSocket forwarding exists.
- Load holdings first. Price, pool, stake and history enrichment must not hold the balance screen behind an indefinite spinner. Retain only same-wallet/same-network cached balances, visibly stale, or show an explicit retryable error.
- Pin network selection before starting providers; a saved testnet preference must not redirect a mainnet build to the old chain. Clear network-specific caches when selection changes.

## 12. Verification record and source basis

[Read-only endpoint evidence](mainnet-ios-defi-endpoint-verification.json) preserves the original audit and historical version 1 feed evidence. **Use `market_feed_v2_followup` for the current policy:** its supply/status reads returned HTTP 200 and schema 2; SSE independently delivered ready schema 2 snapshots at slots 29661 and 29662. Company components were checked as included in the exact circulating total. The earlier `market_feed_followup` is explicitly marked superseded and retains the deployment configuration hashes. [Market-feed deployment evidence](mainnet-market-feed-public-verification.json) is a separate deployment record; the version-tagged follow-up identifies this handoff's current check. This is bounded verification, not continuous monitoring or a load test.

Configuration hashes matched the droplet's deployed files:

```text
mainnetrpc.xerisweb.com.conf
d2f9c95338f237dd2e21a28ac19533bdf703d9b9455ee8cde5472eb18551ba80

mainnetrpc-proxy-common.conf
b5a7b66c94308f3be8fcfe2cf1526f7ab139d0a4fb0c14b852d589abc714427f
```

Source inspection covered the supplied node's `network.rs`, `explorer.rs`, `contracts.rs`, `ledger.rs` and `tx_store.rs`; the deployed proxy allowlist; new DEX `model.mjs`/`trade.mjs`; the mainnet Android provider/bridge source; and the market service's `aggregate.py`, `collector.py` and `service.py`. The original read-only audit made no proxy changes. The subsequent authorized market-feed deployment added three proxy routes and reloaded Nginx; the hash above is that updated configuration, independently matched to the deployed file. No L1 node, genesis or key files were changed by these endpoint/feed tasks. No private-key contents were inspected and no transaction was submitted during these audits or market-feed deployment.

## Display currencies and current FX rates

Fiat presentation is separate from the Xeris transaction protocol. Market-feed raw valuations remain six-decimal xUSDC; swaps, balances, fee calculations and wallet messages retain their original token units. For the default USD display, the DEX assumes one xUSDC represents one USD of intended reserve backing. That is a display convention, not a measured xUSDC/USD exchange price or a promise of immediate redemption.

The frontend currency module obtains indicative FX quotes directly from [biquote](https://biquote.io/docs/) with credential-free, cache-disabled browser requests. It rejects closed, stale, missing, malformed or more-than-60-second-old quotes and does not fall back to a saved rate. Only the chosen currency code may be stored as a preference. On a rate failure, that currency's fiat estimates become unavailable while token amounts remain visible. Weekends and regional market closures can therefore leave a non-USD selection unavailable.

Use the current `dist/currency.mjs` implementation and tests as the integration contract. Convert with integer/rational arithmetic, preserving raw token values; EURUSD, GBPUSD and AUDUSD require inversion for units per USD, while USDxxx pairs are direct. Quote timestamps describe the underlying FX observation, not merely when the app fetched it. Rates are indicative and affect display only.

No Xeris `/fx` endpoint is deployed. The provider's [terms](https://biquote.io/terms.html) permit product display and derived calculations but forbid exposing its raw feed through our public API. The free service has no uptime SLA. For an iOS-native currency selector, apply the same freshness, denomination and unavailable-state rules; the Web4-hosted DEX handles its own selector.
