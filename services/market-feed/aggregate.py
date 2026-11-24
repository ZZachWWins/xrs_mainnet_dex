"""Exact, read-only XRS liquid-circulation accounting for the current RPC census.

This module performs no I/O. The collector supplies losslessly decoded JSON from
one unchanged canonical tip. It must finish every holder/pool cursor page and
set the corresponding *_complete flag only after seeing next_after == null.

Policy counts all unlocked native/wrapped holdings, including company treasury
balances, plus explicitly approved active AMM inventory. Burn addresses, stakes, unbonding,
timelocks, vesting and other contract custody are not spendable wallet balances
and are excluded, without subtracting them again. Company labels are descriptive
only: ownership does not exclude an unlocked balance from this supply policy.

Source: ledger.rs:9435-9487 (native pseudo escrow), contracts.rs:1865-1871,
2020-2027 (token locks leave wallet balances), token.rs:1131-1167 (wrap/unwrap),
explorer.rs:940-964 (100-account census), 1759-1787 (holder pages).
"""

import re


U64_MAX = (1 << 64) - 1
INITIAL_SUPPLY = 200_000_000 * 10**9
MAX_EMISSIONS = 500_000_000 * 10**9
ZERO_ADDRESS = "1" * 32
WRAPPED_XRS = "xrs_native"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class SupplyUnavailable(ValueError):
    """The caller must publish unavailable, never a partial supply number."""


def uint(value, label="amount"):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value):
        result = int(value)
    else:
        raise SupplyUnavailable(f"Invalid integer: {label}")
    if not 0 <= result <= U64_MAX:
        raise SupplyUnavailable(f"Integer out of range: {label}")
    return result


def public_address(address):
    """Match a canonical base58 encoding of exactly 32 public-key bytes."""
    if not isinstance(address, str) or not 32 <= len(address) <= 44:
        return False
    value = 0
    for char in address:
        digit = BASE58.find(char)
        if digit < 0:
            return False
        value = value * 58 + digit
    leading = len(address) - len(address.lstrip("1"))
    decoded_size = leading + (value.bit_length() + 7) // 8
    return decoded_size == 32


def _unique_strings(value, label, *, addresses=False, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise SupplyUnavailable(f"Missing policy: {label}")
    if any(not isinstance(item, str) or not item or
           (addresses and not public_address(item)) for item in value):
        raise SupplyUnavailable(f"Invalid policy: {label}")
    if len(set(value)) != len(value):
        raise SupplyUnavailable(f"Duplicate policy entry: {label}")
    return set(value)


def _tip(tip):
    if not isinstance(tip, dict):
        raise SupplyUnavailable("Missing canonical tip")
    slot = uint(tip.get("slot"), "tip slot")
    digest = tip.get("hash")
    if slot == 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SupplyUnavailable("Invalid canonical tip")
    return slot, digest


def _mapping(value, label):
    if not isinstance(value, dict):
        raise SupplyUnavailable(f"Missing {label}")
    return value


def aggregate_supply(bundle, policy):
    """Return JSON-safe decimal strings, or raise SupplyUnavailable.

    Bundle fields: tip_start/tip_end {slot,hash}; stats {total_accounts,
    current_slot}; balances {address:raw}; wrapped_token token metadata;
    holders [{address,balance}], holder_count, holders_complete; pools v2 rows,
    pool_count, pools_complete; economics {current_slot,total_mined_lamports}.
    Policy fields: company_addresses (classification-only address list),
    included_pool_ids, optional burn_addresses. The zero public key is always
    excluded. Included AMM inventory remains tradable even if its LP is company
    owned. A newly discovered funded XRS pool requires an explicit policy update.
    """
    _mapping(bundle, "census bundle")
    _mapping(policy, "circulation policy")
    start = _tip(bundle.get("tip_start"))
    if start != _tip(bundle.get("tip_end")):
        raise SupplyUnavailable("Canonical tip changed during census")
    slot, digest = start
    stats = _mapping(bundle.get("stats"), "stats")
    economics = _mapping(bundle.get("economics"), "economics")
    if uint(stats.get("current_slot"), "stats slot") != slot or \
            uint(economics.get("current_slot"), "economics slot") != slot:
        raise SupplyUnavailable("Census reads describe different slots")
    mined = uint(economics.get("total_mined_lamports"), "total emissions")
    if mined > MAX_EMISSIONS:
        raise SupplyUnavailable("Emissions exceed protocol cap")

    company = _unique_strings(policy.get("company_addresses"),
                              "company_addresses", addresses=True)
    burns = _unique_strings(policy.get("burn_addresses", []),
                            "burn_addresses", addresses=True) | {ZERO_ADDRESS}
    if company & burns:
        raise SupplyUnavailable("Company and burn address policies overlap")
    included_pools = _unique_strings(policy.get("included_pool_ids"), "included_pool_ids")

    balances = _mapping(bundle.get("balances"), "native balances")
    account_count = uint(stats.get("total_accounts"), "account count")
    if account_count > 100 or len(balances) != account_count:
        raise SupplyUnavailable("Native account census is incomplete (RPC limit 100)")

    totals = {name: 0 for name in (
        "native_wallets", "company_native", "native_burn_addresses",
        "native_nonwallet_custody", "wrapped_wallets", "company_wrapped",
        "wrapped_burn_addresses", "wrapped_nonwallet_custody", "amm_inventory",
        "inactive_amm_custody")}

    def wallet_amount(address, amount, prefix):
        if not isinstance(address, str) or not address:
            raise SupplyUnavailable("Invalid balance holder")
        if address in company:
            totals[f"company_{prefix}"] += amount
        elif address in burns:
            totals[f"{prefix}_burn_addresses"] += amount
        elif not public_address(address):
            # Internal __escrow_order_* balances are one custody representation;
            # don't add their matching contract fields or call them circulation.
            totals[f"{prefix}_nonwallet_custody"] += amount
        else:
            totals[f"{prefix}_wallets"] += amount

    native_sum = 0
    for address, raw in balances.items():
        amount = uint(raw, "native balance")
        native_sum += amount
        wallet_amount(address, amount, "native")

    token = _mapping(bundle.get("wrapped_token"), "wrapped token metadata")
    if token.get("token_id") != WRAPPED_XRS or uint(token.get("decimals"), "decimals") != 9 \
            or token.get("mint_policy") != "Standard":
        raise SupplyUnavailable("Unexpected wrapped XRS identity")
    wrapped_supply = uint(token.get("current_supply"), "wrapped supply")
    if wrapped_supply > INITIAL_SUPPLY + MAX_EMISSIONS:
        raise SupplyUnavailable("Wrapped supply exceeds native supply cap")
    holders = bundle.get("holders")
    if bundle.get("holders_complete") is not True or not isinstance(holders, list) \
            or len(holders) != uint(bundle.get("holder_count"), "holder count"):
        raise SupplyUnavailable("Wrapped holder census is incomplete")
    seen_holders, holder_sum = set(), 0
    for row in holders:
        _mapping(row, "holder row")
        address = row.get("address")
        if not isinstance(address, str) or address in seen_holders:
            raise SupplyUnavailable("Duplicate or invalid wrapped holder")
        seen_holders.add(address)
        amount = uint(row.get("balance"), "wrapped holder balance")
        if amount == 0:
            raise SupplyUnavailable("Unexpected zero wrapped holder row")
        holder_sum += amount
        wallet_amount(address, amount, "wrapped")

    pools = bundle.get("pools")
    if bundle.get("pools_complete") is not True or not isinstance(pools, list) \
            or len(pools) != uint(bundle.get("pool_count"), "pool count"):
        raise SupplyUnavailable("Pool census is incomplete")
    seen_pools, pool_sum = set(), 0
    for pool in pools:
        _mapping(pool, "pool row")
        pid = pool.get("pool_id")
        if not isinstance(pid, str) or not pid or pid in seen_pools:
            raise SupplyUnavailable("Duplicate or invalid pool identity")
        seen_pools.add(pid)
        a, b = pool.get("token_a"), pool.get("token_b")
        if not isinstance(a, str) or not isinstance(b, str) or not a or not b or a == b:
            raise SupplyUnavailable("Invalid AMM token pair")
        reserve_a, reserve_b = uint(pool.get("reserve_a"), "reserve A"), uint(pool.get("reserve_b"), "reserve B")
        if type(pool.get("is_active")) is not bool:
            raise SupplyUnavailable("Invalid pool activity flag")
        reserve = reserve_a if a == WRAPPED_XRS else reserve_b if b == WRAPPED_XRS else 0
        pool_sum += reserve
        if reserve and pool["is_active"]:
            if pid not in included_pools:
                raise SupplyUnavailable("Funded XRS pool requires circulation policy review")
            if reserve_a == 0 or reserve_b == 0 or uint(pool.get("total_lp_shares"), "LP supply") == 0:
                raise SupplyUnavailable("Funded XRS pool is not currently liquid")
            if uint(pool.get("fee_bps"), "pool fee") >= 10_000:
                raise SupplyUnavailable("Invalid XRS pool fee")
            totals["amm_inventory"] += reserve
        else:
            totals["inactive_amm_custody"] += reserve

    if holder_sum + pool_sum > wrapped_supply:
        raise SupplyUnavailable("Wrapped holdings exceed token supply")
    if native_sum + wrapped_supply > INITIAL_SUPPLY + mined:
        raise SupplyUnavailable("Native and wrapped holdings exceed gross issuance")
    # This residual covers locked/escrowed wrapped tokens. It is deliberately
    # not described as a complete lock census or subtracted from wallets twice.
    totals["wrapped_other_custody"] = wrapped_supply - holder_sum - pool_sum
    circulating = (totals["native_wallets"] + totals["company_native"] +
                   totals["wrapped_wallets"] + totals["company_wrapped"] + totals["amm_inventory"])
    return {
        "status": "available", "basis": "unlocked_on_chain_v2",
        "chain_id": "xeris-mainnet-v4", "slot": str(slot), "block_hash": digest,
        "decimals": 9, "circulating_raw": str(circulating),
        "total_emitted_raw": str(mined), "wrapped_supply_raw": str(wrapped_supply),
        "components_raw": {key: str(value) for key, value in totals.items()},
        "census": {"native_accounts": account_count, "wrapped_holders": len(holders),
                   "pools": len(pools), "native_account_limit": 100},
    }


def aggregate_token(token, holders, holder_count, pools, pool_count, policy, *,
                    holders_complete=False, pools_complete=False):
    """Liquid circulation for a protocol-launched token, without a price/FDV.

    The collector must include these reads inside the same canonical-tip bracket
    as the XRS census. All pools are required for circulation; only the canonical
    graduation pool should be used by the caller as an automatic price source.
    Minted supply is an accounting bound, never a substitute for circulation.
    """
    _mapping(token, "launch token")
    _mapping(policy, "circulation policy")
    token_id = token.get("token_id")
    if token.get("mint_policy") != "LaunchpadManaged" or \
            token.get("mint_authority") != "__launchpad_managed__" or \
            uint(token.get("decimals"), "launch token decimals") != 9 or \
            not isinstance(token_id, str) or not token_id.startswith("lp_") or \
            not 3 < len(token_id) <= 128:
        raise SupplyUnavailable("Unexpected protocol-launch token identity")
    supply = uint(token.get("current_supply"), "launch token supply")
    maximum = uint(token.get("max_supply"), "launch token maximum")
    if maximum == 0 or supply > maximum:
        raise SupplyUnavailable("Inconsistent launch token supply")
    company = _unique_strings(policy.get("company_addresses"),
                              "company_addresses", addresses=True)
    burns = _unique_strings(policy.get("burn_addresses", []),
                            "burn_addresses", addresses=True) | {ZERO_ADDRESS}
    if company & burns:
        raise SupplyUnavailable("Company and burn address policies overlap")
    if holders_complete is not True or not isinstance(holders, list) or \
            len(holders) != uint(holder_count, "launch token holder count"):
        raise SupplyUnavailable("Launch token holder census is incomplete")
    if pools_complete is not True or not isinstance(pools, list) or \
            len(pools) != uint(pool_count, "pool count"):
        raise SupplyUnavailable("Launch token pool census is incomplete")

    totals = {name: 0 for name in ("wallets", "company_wallets", "burn_addresses",
                                  "nonwallet_custody", "amm_inventory", "inactive_amm_custody")}
    seen_holders, holder_sum = set(), 0
    for row in holders:
        _mapping(row, "launch token holder row")
        address = row.get("address")
        if not isinstance(address, str) or not address or address in seen_holders:
            raise SupplyUnavailable("Duplicate or invalid launch token holder")
        seen_holders.add(address)
        amount = uint(row.get("balance"), "launch token holder balance")
        if amount == 0:
            raise SupplyUnavailable("Unexpected zero launch token holder row")
        holder_sum += amount
        category = "company_wallets" if address in company else "burn_addresses" if address in burns \
            else "nonwallet_custody" if not public_address(address) else "wallets"
        totals[category] += amount

    seen_pools, pool_sum = set(), 0
    for pool in pools:
        _mapping(pool, "pool row")
        pid = pool.get("pool_id")
        if not isinstance(pid, str) or not pid or pid in seen_pools:
            raise SupplyUnavailable("Duplicate or invalid pool identity")
        seen_pools.add(pid)
        a, b = pool.get("token_a"), pool.get("token_b")
        if not isinstance(a, str) or not isinstance(b, str) or not a or not b or a == b:
            raise SupplyUnavailable("Invalid AMM token pair")
        amount_a = uint(pool.get("reserve_a"), "reserve A")
        amount_b = uint(pool.get("reserve_b"), "reserve B")
        if type(pool.get("is_active")) is not bool:
            raise SupplyUnavailable("Invalid pool activity flag")
        amount = amount_a if a == token_id else amount_b if b == token_id else 0
        pool_sum += amount
        if amount and pool["is_active"]:
            if amount_a == 0 or amount_b == 0 or uint(pool.get("total_lp_shares"), "LP supply") == 0:
                raise SupplyUnavailable("Funded launch token pool is not currently liquid")
            if uint(pool.get("fee_bps"), "pool fee") >= 10_000:
                raise SupplyUnavailable("Invalid launch token pool fee")
            totals["amm_inventory"] += amount
        else:
            totals["inactive_amm_custody"] += amount
    if holder_sum + pool_sum > supply:
        raise SupplyUnavailable("Launch token holdings exceed minted supply")
    totals["other_contract_custody"] = supply - holder_sum - pool_sum
    return {"status": "available", "basis": "unlocked_on_chain_v2", "token_id": token_id,
            "decimals": 9, "circulating_raw": str(totals["wallets"] + totals["company_wallets"] + totals["amm_inventory"]),
            "issued_supply_raw": str(supply),
            "components_raw": {key: str(value) for key, value in totals.items()}}
