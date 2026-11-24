# Xeris mainnet wallet and block signature status

Source checked November 23, 2026, Eastern time. This note distinguishes the current authorization rules from intended future security features. It accompanies the [iOS DeFi endpoint handoff](mainnet-ios-defi-endpoints.md).

## Current behavior

**Mainnet block authentication is hybrid; ordinary wallet spending currently remains Ed25519-based.** Calling the complete chain-and-wallet system “quantum-proof” would overstate the implemented protection.

| Layer | Current requirement |
|---|---|
| Ordinary native/token transfer, swap, wrap or unwrap | Valid Ed25519 transaction signatures matching the message's required-signer count. The current DEX uses one wallet signer. |
| Mainnet block proposer | Both Ed25519 and ML-DSA-65 signatures over the block's canonical message, mandatory from slot 1. |
| Registered PQ key | Used for proposer binding and protected key-management operations; does not add a general PQ spending-signature requirement to ordinary wallet actions. |
| Nonzero recovery view | Two distinct producer votes from the fixed three-producer roster; each vote is hybrid-signed. |
| Ordinary view-zero block | No recovery votes; its proposer must still supply the mandatory hybrid block signature. |
| Certified gap recovery | All three ordered hybrid producer votes. |
| Legacy `PqSignedTransfer`, variant 52 | Disabled in the instruction dispatcher. Not a usable way to enable PQ wallet spending. |

These requirements concern different messages. A hybrid-signed block commits to its included transactions, but it does not independently authenticate each transaction's owner using that owner's PQ key. Consequently, a hypothetical spending request with a valid forged Ed25519 wallet signature would not face an additional wallet ML-DSA verification gate on the reviewed ordinary paths. It would still have to satisfy the normal balance, fee, blockhash, duplicate and instruction rules. This is a statement about the authentication boundary, not evidence that such a forgery has occurred or is currently practical.

The wallet address is an identifier. The relevant gap is the **spending authorization rule**: registering another public key does not change which signatures the node demands before spending that address's funds. Changing only wallet UI or its signing implementation cannot make an optional signature mandatory for other clients; that would require corresponding protocol enforcement and review.

## Source evidence

All locations below refer to the inspected working node at `work/fresh-mainnet-node/src`.

| Evidence | Source |
|---|---|
| Reject zero required signers and require the exact declared signature count; call `tx.verify()` at admission | `network.rs:161–168`, `network.rs:211–220` |
| Consensus independently verifies every transaction's declared Ed25519 signatures | `ledger.rs:4768–4805` |
| Ordinary transaction semantic checks contain no global PQ-wallet-authentication gate | `ledger.rs:2018–2097` |
| Execution derives the caller from the transaction's first account key; native transfer checks caller equals sender | `ledger.rs:7776`, `ledger.rs:7907–7940` |
| Wrap/unwrap call the token functions using that authenticated caller | `ledger.rs:7942–7961`; `token.rs:1131`, `token.rs:1151` |
| Token transfer checks caller equals sender and normal token/balance restrictions | `token.rs:1365–1393` |
| Ordinary contract calls, including pool swaps, pass the same caller to the contract engine | `ledger.rs:8115`, `ledger.rs:8210–8219`; swap handlers `contracts.rs:3534`, `contracts.rs:3579` |
| Hybrid proposer activation is slot 1; validation requires both components | `ledger.rs:781`, `ledger.rs:4677–4728`; `crypto.rs:1341–1367` |
| Implementation uses FIPS-204 ML-DSA-65 despite retaining the internal name `dilithium3` | `crypto.rs:59–69`, alias `pqcrypto_mldsa::mldsa65 as dilithium3` |
| Exactly three producers; zero votes at view zero, exactly two at nonzero view; three for gap certificates | `main.rs:57`, `main.rs:68`, `main.rs:171–174`, `main.rs:255`, `main.rs:415–419` |
| PQ registry resolves the block proposer's key; rotation separately verifies current PQ-key authorization | `ledger.rs:4191`, `ledger.rs:4915`, `ledger.rs:11243–11320` |
| Legacy PQ transfer explicitly skipped as disabled | `ledger.rs:11323–11342` |
| `verify_pq_signed_transfer_dilithium` is defined but has no runtime call site in the inspected source | `crypto.rs:886`; source-wide reference search |
| Web DEX builds one signer and one instruction, with message header `[1,0,1]` | `work/sites/xeris-mainnet-dex/dist/trade.mjs:83–91` |

The disabled-transfer comment requires a new instruction variant with a monotonic account nonce and canonical message binding before re-enablement. It is not an enabled feature controlled by a wallet setting.

The public `/v2/validators` response also reported three current producers, all with blocks proposed:

- `4p897nUzAxU9xbaQH69fa3S3hvtn9o1STiobHX4vnLoL`
- `86TZWQYSiRkccYEAWfU2zbTvwcDouRFFWJ4TLkuYmkB1`
- `6Y4eJNvkhNoRgxYNEw93TtNvWYHKp3jvomzfVwsby6XH`

The producer count comes from a live read; the recovery thresholds come from source inspection. The validator API does not itself prove which cryptographic checks a binary enforces.

## Comparison with the supplied node ZIP

Read only the four public source entries listed below from `/Users/xerisforceserver-1/Downloads/xeriscointestnet-main (8).zip`, under `xeriscointestnet-main/src/`, and compared them with the working source. No key file was opened.

| Source entry | Comparison |
|---|---|
| `network.rs` | Entire file byte-identical |
| `token.rs` | Entire file byte-identical |
| `crypto.rs` | Entire file byte-identical |
| `ledger.rs` | Full file differs for the authorized treasury constant substitution and related tests; the inspected wallet authentication behavior is unchanged. |
| `ledger.rs` legacy `PqSignedTransfer` dispatcher arm | Byte-identical; starts at line 11323 in both files. The supplied ZIP already contains the disabled branch. |

SHA-256 evidence:

```text
network.rs, supplied and working:
2dd7b7b216324a4af7ed649bf51a6090ea91112cbeeffb67676f9c7e98b076a9

token.rs, supplied and working:
02fc006302d9bdc5bbbbfe7e760bda8a397f83fe0b836d76e36b2a028a56f33b

crypto.rs, supplied and working:
2546b7a99548b494e221e5c3b12001c6ab7a0f98f43d2def67fccf34b1391b5b

ledger.rs, supplied:
09dbeabcf6d7b63f2175443d2f7bf70dd511381ee7aa5d95590d2e9d33571a74

ledger.rs, working:
43beb87a4e5ca2ecfe8de9e69e6a896d962cfcd23c7d0ffae63af4f6323baf35

Identical disabled dispatcher slice, beginning at PqSignedTransfer
and ending immediately before PqAttest, including intervening whitespace:
01c72793d5b164a16ddde05aa0369dc327ee2f54b56dd4a872bd14cbaef83343
```

This comparison establishes that the disabled branch and ordinary spending-signature behavior were already present in the supplied source; the new DEX did not disable PQ transaction enforcement. This inspection did not modify protocol code, submit a transaction, perform a forgery test or independently certify the full system.
