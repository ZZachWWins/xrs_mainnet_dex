# Xeris Mainnet DEX

A new browser client for the deployed Xeris mainnet commissioning pool, using the original design, logo, token icons and fonts recovered from `dex.xerisweb.com`. This repository replaces the unavailable placeholder source. It contains public frontend code only; it does not contain treasury keys, reserve keys, wallet seeds or node state.

## Run and test

Node.js 22 or later is needed for checks. There are no npm dependencies.

```sh
npm test
npm run build
npm run dev
```

The local preview is `http://127.0.0.1:8769`. `dist/` contains the maintained HTML, CSS, ES modules and bundled assets; it can be served directly over HTTPS. `netlify.toml` sets the Netlify build command, Node version and publish directory. Import this repository, select `main`, and use `npm run build` with publish directory `dist`. No environment secrets are required. `dist/_redirects` preserves the old page URLs. `vercel.json` is an optional alternative configuration. A persistent MAINNET BETA label appears at the top of every page. Deploy the repository before assigning the `dex.xerisweb.com` custom domain; uploading source alone does not change its existing deployment or DNS.

## Implemented

- Homepage and responsive trading layout preserving the existing Xeris visual design.
- Live mainnet pool reserves, exact integer quotes, fee/minimum output and price impact.
- Pool and portfolio views, distinct native XRS / wXRS / xUSDC balances.
- Explicit Xeris wallet provider connection and mainnet/checkpoint binding.
- One wallet-approved action at a time: xUSDC → wXRS, wXRS → xUSDC, wrap or unwrap.
- Verification of the returned transaction message and Ed25519 signature before broadcast.
- Public localStorage journal written before a single submission; Web Locks prevent concurrent submissions in this browser. Pending transactions are reconciled using their original signature, including after reload. Unknown results never trigger automatic resends.
- Transaction activity and clear unavailable states for public USDC deposits/redemptions.

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

The pool holds wXRS and xUSDC. A native-XRS buy is a swap followed by a 1:1 unwrap; selling reverses those actions. The interface requests separate approvals, with an explicit amount for conversion. A two-step route costs 0.002 native XRS in network fees. Ordinary Solana wallet providers are never used to sign Xeris transactions. The supported injected provider is `window.xeris` from the Xeris wallet browser (or a compatible extension), with `connect`, `getRpcUrl` and `signTransaction`.

The 30-second quote lifetime and 0.5% minimum output require fresh review if approval is slow. Browsers without Web Locks, persistent localStorage or Ed25519 WebCrypto fail closed. Clearing browser data removes its local transaction journal; this is not cross-device recovery. Receipt checks establish reported Xeris execution, not cross-chain finality.

## USDC bridge status

The controlled operator test already completed Solana USDC → xUSDC → native XRS → xUSDC → Solana USDC. The new website has not yet completed a funded wallet-browser round trip.

Public automatic issuance/redemption is **disabled**. `operator-contract/` documents a separate offline request-intake foundation; it is not a deployed API. A verified request still needs chain evidence, global reserve/obligation accounting, scoped signing, payout reconciliation and recovery before funds can be accepted automatically. The website provides no deposit instruction or burn button while these components are unavailable. xUSDC is treasury-issued on Xeris, backed by an operator-controlled Solana USDC reserve; it is not Circle-issued native USDC or a trustless bridge.

The interface does not claim that this new client/operator code was covered by the L1's CertiK audit. L1 consensus and running nodes are unchanged.

## Validation

`npm test` runs 65 offline tests covering exact amounts, lossless JSON, pool identity, quote matching, wallet binding, transaction encoding, signature verification, balances/gas, storage failures, stale quotes, ambiguous submissions and receipt recovery. Codec bytes independently match all four action types from the public completed pilot artifacts. Tests use synthetic in-memory keys and mocked network writes. Browser checks use public mainnet reads; they do not spend funds.

Image and font origins are recorded in `asset-manifest.json`. Live chart points are observations from the current browser session, not fabricated historic candles. LP fees remain in pool reserves for LP shareholders; there is no separate 0.777% company cut in this pool.

The independent end-to-end harness additionally exercises the complete four-step Xeris round trip and a lost submission response through the actual client modules, preserving the original transaction ID across a simulated reload. This is offline application validation, not a funded browser/Web4 or Solana settlement run. Font license notices are included in `dist/assets/licenses/`.
