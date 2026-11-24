"""Public-only adapter boundaries. Implementations must never log signed bodies.

Observer methods are read-only. Their returned prepared/context records are
persisted before signing, not recomputed for an unresolved transaction.
"""
from typing import Protocol


class Observer(Protocol):
    def snapshot(self) -> dict:
        """Fresh facts, not an admission decision:
        checked_at:int, checkpoint_verified:bool, archive_complete:bool,
        reserve_usdc_raw:str, xusdc_supply_raw:str, operator_native_raw:str,
        mint_authority_native_raw:str, solana_fee_lamports:str.
        Must independently establish pinned reserve/mint/config and all nodes.
        """

    def prepare_source(self, intent: dict) -> dict:
        """Return {prepared:dict,context:dict,expires_at:int,fee_raw:str}.
        buy: solana_deposit.build_deposit(...intent_digest); context includes
        source-window/checkpoint evidence. redeem: xeris_redemption.build_stage
        burn; context contains the persisted before-anchor. Signer = source_owner.
        No execution, signatures, payloads from callers or mutable intent fields.
        """

    def prepare_operator(self, intent: dict, stage: str, confirmed_stages: list) -> dict:
        """Same wrapper. Xeris prepared is xeris_purchase.build_stage;
        payout/refund prepared is solana_payout reviewed/preflight checked plan.
        Confirmed stage amounts are mandatory: never use a quote/balance sweep.
        Caller inserts intent_id in intent for route_id/payout intent_id binding.
        Refund destination = original Solana source_owner.
        """

    def validate_before_submission(self, intent: dict, stage: str, prepared: dict, context: dict) -> bool:
        """Recheck original blockhash validity via read-only RPC immediately before submission."""

    def observe(self, intent: dict, stage: str, prepared: dict,
                transaction_id: str, context: dict) -> dict:
        """Return {state:'confirmed'|'pending'|'failed',evidence_digest:hex64,
        amount_raw:str (confirmed only)}. Exact canonical successful original
        and independently accounted effect required for confirmed. Deposit/burn
        must bind original intent/signature; payout finalized CPI/poststate.
        Missing/expired receipts, transport errors, forks or ambiguity are
        pending, NEVER failed. Evidence stored durably by observer if needed.
        """


class Signer(Protocol):
    def readiness(self) -> dict:
        """{authorized:bool,checked_at:int,policy_id:str,signers:list[str],
        maximum_usdc_raw:str,remaining_total_usdc_raw:str,expires_at:int}.
        Must reflect new explicit scoped authorization, never the closed pilot.
        No keys or arbitrary message signing exposed over public HTTP.
        """

    def reserve_intent(self, intent: dict, fence: dict) -> None:
        """Durably reserve request and aggregate scope BEFORE source preparation.
        Idempotent per intent. A buy reservation covers mint OR pre-mint refund;
        a redemption covers payout. No reservation authorizes arbitrary signing.
        """

    def has_reservation(self, intent: dict) -> bool:
        """Validate and return the existing immutable reservation; never reserve implicitly."""

    def sign(self, intent: dict, stage: str, prepared: dict, fence: dict) -> str:
        """Return canonical signed transaction base64, independently scope-bound.
        Must be idempotent for (intent_id,stage,message_sha256), enforce fence.
        Signing is internal; runtime independently verifies returned signatures.
        """


class Broadcaster(Protocol):
    def submit(self, chain: str, transaction_base64: str,
               transaction_id: str, fence: dict) -> None:
        """Single original submission only after durable mark_submitted.
        Enforce current fence immediately at network boundary. No retry/re-sign.
        Response is NOT confirmation; only Observer may confirm chain effects.
        """
