import copy
import unittest

from aggregate import aggregate_supply, aggregate_token, public_address, SupplyUnavailable, ZERO_ADDRESS


COMPANY = "4p897nUzAxU9xbaQH69fa3S3hvtn9o1STiobHX4vnLoL"
USER = "6Y4eJNvkhNoRgxYNEw93TtNvWYHKp3jvomzfVwsby6XH"
OTHER = "86TZWQYSiRkccYEAWfU2zbTvwcDouRFFWJ4TLkuYmkB1"
POLICY = {"company_addresses": [COMPANY], "included_pool_ids": ["main-pool"]}
BASE_CIRCULATION = 199_999_098 * 10**9


def census():
    return {
        "tip_start": {"slot": "30000", "hash": "ab" * 32},
        "tip_end": {"slot": "30000", "hash": "ab" * 32},
        "stats": {"total_accounts": 3, "current_slot": 30000},
        "economics": {"current_slot": 30000, "total_mined_lamports": 300_000 * 10**9},
        "balances": {COMPANY: 199_999_000 * 10**9, USER: 10**9, "__escrow_order_1": 3 * 10**9},
        "wrapped_token": {"token_id": "xrs_native", "decimals": 9,
                          "mint_policy": "Standard", "current_supply": 100 * 10**9},
        "holders": [{"address": COMPANY, "balance": 5 * 10**9}, {"address": USER, "balance": 2 * 10**9}],
        "holder_count": 2, "holders_complete": True,
        "pools": [{"pool_id": "main-pool", "token_a": "xrs_native", "token_b": "xeris_usdc",
                   "reserve_a": 90 * 10**9, "reserve_b": 2 * 10**6, "is_active": True,
                   "total_lp_shares": 100, "fee_bps": 30}],
        "pool_count": 1, "pools_complete": True,
    }


class AggregationTests(unittest.TestCase):
    def test_exact_liquid_census_does_not_double_count_wrapping_or_custody(self):
        got = aggregate_supply(census(), POLICY)
        self.assertEqual(got["circulating_raw"], str(BASE_CIRCULATION))
        self.assertEqual(got["basis"], "unlocked_on_chain_v2")
        self.assertEqual(got["components_raw"]["company_wrapped"], str(5 * 10**9))
        self.assertEqual(got["components_raw"]["native_nonwallet_custody"], str(3 * 10**9))
        self.assertEqual(got["components_raw"]["wrapped_other_custody"], str(3 * 10**9))

    def test_mining_into_company_treasury_increases_unlocked_circulation(self):
        before = census()
        after = copy.deepcopy(before)
        after["balances"][COMPANY] += 10 * 10**9
        after["economics"]["total_mined_lamports"] += 10 * 10**9
        a, b = aggregate_supply(before, POLICY), aggregate_supply(after, POLICY)
        self.assertEqual(int(a["circulating_raw"]) + 10 * 10**9, int(b["circulating_raw"]))
        self.assertNotEqual(a["total_emitted_raw"], b["total_emitted_raw"])

    def test_distribution_from_treasury_preserves_already_unlocked_circulation(self):
        bundle = census()
        bundle["balances"][COMPANY] -= 123456789
        bundle["balances"][USER] += 123456789
        self.assertEqual(aggregate_supply(bundle, POLICY)["circulating_raw"], str(BASE_CIRCULATION))

    def test_wrap_and_unwrap_preserve_circulation(self):
        bundle = census()
        before = aggregate_supply(bundle, POLICY)["circulating_raw"]
        bundle["balances"][USER] -= 500_000_000
        bundle["holders"][1]["balance"] += 500_000_000
        bundle["wrapped_token"]["current_supply"] += 500_000_000
        self.assertEqual(aggregate_supply(bundle, POLICY)["circulating_raw"], before)

    def test_locks_leave_wallet_circulation_and_are_not_subtracted_twice(self):
        bundle = census()
        bundle["holders"][1]["balance"] -= 100_000_000
        got = aggregate_supply(bundle, POLICY)
        self.assertEqual(got["circulating_raw"], str(BASE_CIRCULATION - 100_000_000))
        self.assertEqual(got["components_raw"]["wrapped_other_custody"], str(3_100_000_000))

    def test_token_burn_reduces_supply_without_gross_issuance_approximation(self):
        bundle = census()
        bundle["holders"][1]["balance"] -= 100_000_000
        bundle["wrapped_token"]["current_supply"] -= 100_000_000
        got = aggregate_supply(bundle, POLICY)
        self.assertEqual(got["circulating_raw"], str(BASE_CIRCULATION - 100_000_000))
        self.assertEqual(got["components_raw"]["wrapped_other_custody"], "3000000000")

    def test_zero_public_key_and_internal_accounts_are_excluded(self):
        bundle = census()
        bundle["balances"][ZERO_ADDRESS] = 123
        bundle["stats"]["total_accounts"] += 1
        bundle["holders"].append({"address": "__token_escrow", "balance": 42})
        bundle["holder_count"] += 1
        got = aggregate_supply(bundle, POLICY)
        self.assertEqual(got["circulating_raw"], str(BASE_CIRCULATION))
        self.assertEqual(got["components_raw"]["native_burn_addresses"], "123")
        self.assertEqual(got["components_raw"]["wrapped_nonwallet_custody"], "42")

    def test_integer_precision_above_javascript_safe_range(self):
        bundle = census()
        bundle["balances"][USER] = "9007199254740993"
        bundle["balances"][COMPANY] = 0
        got = aggregate_supply(bundle, POLICY)
        self.assertEqual(got["circulating_raw"], "9007296254740993")

    def test_truncated_account_census_never_publishes_partial_supply(self):
        for count in [4, 101]:
            bundle = census()
            bundle["stats"]["total_accounts"] = count
            with self.assertRaises(SupplyUnavailable):
                aggregate_supply(bundle, POLICY)

    def test_changed_tip_reorg_or_mixed_slot_read_is_rejected(self):
        for change in [lambda b: b["tip_end"].update(slot=30001),
                       lambda b: b["tip_end"].update(hash="cd" * 32),
                       lambda b: b["economics"].update(current_slot=29999),
                       lambda b: b["stats"].update(current_slot=29999)]:
            bundle = census()
            change(bundle)
            with self.assertRaises(SupplyUnavailable):
                aggregate_supply(bundle, POLICY)

    def test_missing_and_duplicate_pages_fail_closed(self):
        for change in [lambda b: b.update(holders_complete=False),
                       lambda b: b.update(pools_complete=False),
                       lambda b: b.update(holder_count=3),
                       lambda b: b.update(pool_count=2),
                       lambda b: b["holders"].__setitem__(1, copy.deepcopy(b["holders"][0]))]:
            bundle = census()
            change(bundle)
            with self.assertRaises(SupplyUnavailable):
                aggregate_supply(bundle, POLICY)

    def test_unreviewed_or_empty_funded_pool_is_unavailable(self):
        for change in [lambda p: p.update(pool_id="new-pool"), lambda p: p.update(reserve_b=0),
                       lambda p: p.update(total_lp_shares=0), lambda p: p.update(fee_bps=10000),
                       lambda p: p.update(token_b="xrs_native")]:
            bundle = census()
            change(bundle["pools"][0])
            with self.assertRaises(SupplyUnavailable):
                aggregate_supply(bundle, POLICY)

    def test_inactive_pool_inventory_is_excluded(self):
        bundle = census()
        bundle["pools"][0]["is_active"] = False
        got = aggregate_supply(bundle, POLICY)
        self.assertEqual(got["circulating_raw"], str(BASE_CIRCULATION - 90 * 10**9))
        self.assertEqual(got["components_raw"]["inactive_amm_custody"], "90000000000")

    def test_reversed_pool_orientation_counts_identical_xrs_inventory(self):
        bundle = census()
        p = bundle["pools"][0]
        p["token_a"], p["token_b"] = p["token_b"], p["token_a"]
        p["reserve_a"], p["reserve_b"] = p["reserve_b"], p["reserve_a"]
        self.assertEqual(aggregate_supply(bundle, POLICY)["circulating_raw"], str(BASE_CIRCULATION))

    def test_impossible_supply_and_unsafe_number_types_fail(self):
        for change in [lambda b: b["wrapped_token"].update(current_supply=1),
                       lambda b: b["wrapped_token"].update(decimals=6),
                       lambda b: b["balances"].update({USER: True}),
                       lambda b: b["balances"].update({USER: 1.0}),
                       lambda b: b["balances"].update({USER: "1e9"}),
                       lambda b: b["balances"].update({USER: -1}),
                       lambda b: b["balances"].update({USER: 1 << 64})]:
            bundle = census()
            change(bundle)
            with self.assertRaises(SupplyUnavailable):
                aggregate_supply(bundle, POLICY)

    def test_exact_pubkey_validation_and_company_classification_does_not_exclude(self):
        for key in [COMPANY, USER, OTHER, ZERO_ADDRESS]:
            self.assertTrue(public_address(key))
        for key in ["0" * 44, "1" * 31, "1" * 33, "z" * 44, "__escrow_order_1"]:
            self.assertFalse(public_address(key))
        unclassified = aggregate_supply(census(), {**POLICY, "company_addresses": []})
        self.assertEqual(unclassified["circulating_raw"], str(BASE_CIRCULATION))
        self.assertEqual(unclassified["components_raw"]["company_native"], "0")
        with self.assertRaises(SupplyUnavailable):
            aggregate_supply(census(), {"included_pool_ids": ["main-pool"]})

    def test_native_lock_and_release_changes_supply_without_ownership_exclusion(self):
        bundle = census()
        bundle["balances"][COMPANY] -= 10**9
        bundle["balances"]["__escrow_order_1"] += 10**9
        self.assertEqual(aggregate_supply(bundle, POLICY)["circulating_raw"], str(BASE_CIRCULATION - 10**9))
        bundle["balances"][COMPANY] += 10**9
        bundle["balances"]["__escrow_order_1"] -= 10**9
        self.assertEqual(aggregate_supply(bundle, POLICY)["circulating_raw"], str(BASE_CIRCULATION))

    def test_staking_debit_is_excluded_even_for_company_owner(self):
        bundle = census()
        bundle["balances"][COMPANY] -= 1000 * 10**9
        # The node moves this amount into its separate stake map. Adding a
        # supplied stake summary must not cause the liquid census to add it back.
        bundle["stats"]["total_staked"] = 1000 * 10**9
        self.assertEqual(aggregate_supply(bundle, POLICY)["circulating_raw"], str(BASE_CIRCULATION - 1000 * 10**9))


class LaunchTokenTests(unittest.TestCase):
    def fixture(self):
        token = {"token_id": "lp_example", "decimals": 9, "current_supply": 1000,
                 "max_supply": 10000, "mint_policy": "LaunchpadManaged",
                 "mint_authority": "__launchpad_managed__"}
        holders = [{"address": COMPANY, "balance": 100}, {"address": USER, "balance": 200},
                   {"address": "__escrow", "balance": 50}]
        pools = [{"pool_id": "lp_example_xrs_pool", "token_a": "lp_example", "token_b": "xrs_native",
                  "reserve_a": 500, "reserve_b": 50, "total_lp_shares": 100, "fee_bps": 30, "is_active": True}]
        return token, holders, pools

    def calculate(self, token, holders, pools, **flags):
        return aggregate_token(token, holders, len(holders), pools, len(pools), POLICY,
                               **{"holders_complete": True, "pools_complete": True, **flags})

    def test_real_liquid_holdings_include_company_but_exclude_contract_custody(self):
        result = self.calculate(*self.fixture())
        self.assertEqual(result["circulating_raw"], "800")
        self.assertEqual(result["basis"], "unlocked_on_chain_v2")
        self.assertEqual(result["issued_supply_raw"], "1000")
        self.assertEqual(result["components_raw"]["other_contract_custody"], "150")
        self.assertNotIn("market_cap", result)

    def test_each_pool_inventory_counts_once_even_when_not_canonical_price_pool(self):
        token, holders, pools = self.fixture()
        pools.append({**pools[0], "pool_id": "other-market", "token_a": "xeris_usdc", "token_b": "lp_example",
                      "reserve_a": 50, "reserve_b": 100})
        result = self.calculate(token, holders, pools)
        self.assertEqual(result["circulating_raw"], "900")
        self.assertEqual(result["components_raw"]["other_contract_custody"], "50")

    def test_ungraduated_zero_supply_is_valid_without_imaginary_market_cap(self):
        token, _, _ = self.fixture()
        token["current_supply"] = 0
        result = self.calculate(token, [], [])
        self.assertEqual(result["circulating_raw"], "0")
        self.assertNotIn("market_cap", result)

    def test_provenance_and_supply_bounds_fail_closed(self):
        for changes in [{"mint_policy": "Standard"}, {"mint_authority": COMPANY},
                        {"current_supply": 10001}, {"current_supply": 849}, {"decimals": 6}]:
            token, holders, pools = self.fixture()
            token.update(changes)
            with self.assertRaises(SupplyUnavailable):
                self.calculate(token, holders, pools)

    def test_missing_page_duplicate_holder_and_duplicate_pool_are_unavailable(self):
        token, holders, pools = self.fixture()
        for flags in [{"holders_complete": False}, {"pools_complete": False}]:
            with self.assertRaises(SupplyUnavailable):
                self.calculate(token, holders, pools, **flags)
        with self.assertRaises(SupplyUnavailable):
            self.calculate(token, holders + [holders[0]], pools)
        with self.assertRaises(SupplyUnavailable):
            self.calculate(token, holders, pools + [pools[0]])

    def test_inactive_pool_is_custody_until_reactivated(self):
        token, holders, pools = self.fixture()
        pools[0]["is_active"] = False
        result = self.calculate(token, holders, pools)
        self.assertEqual(result["circulating_raw"], "300")
        self.assertEqual(result["components_raw"]["inactive_amm_custody"], "500")


if __name__ == "__main__":
    unittest.main()
