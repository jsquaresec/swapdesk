"""providers.trocador: Trocador.app: privacy exchange AGGREGATOR."""
from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

import requests

from .base import ProviderError, Quote, Swap, SwapProvider, group_native_rows
from .constants import (
    COINS,
    STATUS_COMPLETE,
    STATUS_CONFIRMING,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_REFUNDED,
    STATUS_SENDING,
    STATUS_UNKNOWN,
    STATUS_WAITING,
    TROCADOR_NETWORK,
    _dec,
)


# Trocador.app: privacy-focused EXCHANGE AGGREGATOR
#
# One integration fans out to 20+ underlying exchanges (FixedFloat, ChangeNOW,
# MajesticBank, StealthEx, Godex, etc). Trocador itself is non-custodial: the
# chosen partner settles directly to the user's address.
#
# Auth: a single free "API-Key" header (copied from your Trocador profile;
# no KYC). We still ATTEMPT quotes without it, in case a route is public, and
# surface a clear message if the server requires the key.
class Trocador(SwapProvider):
    can_quote_without_config = True  # /new_rate works without a key (rate-limited)
    name = "Trocador"
    site = "https://trocador.app/en/register/affiliate"
    BASE = "https://trocador.app/api"

    _MAP: ClassVar[dict[str, str]] = {
        "new": STATUS_WAITING,
        "anonpaynew": STATUS_WAITING,
        "waiting": STATUS_WAITING,
        "confirming": STATUS_CONFIRMING,
        "sending": STATUS_SENDING,
        "finished": STATUS_COMPLETE,
        # NOT STATUS_COMPLETE: a partial-payment deposit may still need
        # top-up on the exchange side. See STATUS_PARTIAL above.
        "paid partially": STATUS_PARTIAL,
        "failed": STATUS_FAILED,
        "expired": STATUS_EXPIRED,
        "halted": STATUS_FAILED,
        "refunded": STATUS_REFUNDED,
    }

    def __init__(self, api_key: str = "", timeout: int = 25):
        super().__init__(timeout)
        self.api_key = (api_key or "").strip()
        # ticker -> Trocador network label. Seeded from the curated
        # TROCADOR_NETWORK table; refresh_all_coins() replaces/extends this
        # with Trocador's own live /api/coins listing.
        self.networks: dict[str, str] = dict(TROCADOR_NETWORK)
        self.names: dict[str, str] = {t: v[0] for t, v in COINS.items()}

    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        h = {}
        if self.api_key:
            h["API-Key"] = self.api_key
        return h

    def fetch_coins(self) -> dict:
        """Pull Trocador's full coin catalogue (GET /api/coins) and use it to
        REPLACE self.networks/self.names, so the app can offer every coin
        Trocador lists rather than just the hand-curated set. Trocador lists
        one row per (ticker, network), where a ticker has several we ONLY
        keep it if one row's network equals the ticker itself (native
        chain), the one case where picking "the" network isn't a guess. A
        ticker with several rows but none matching natively (e.g. a token
        that only exists as USDT/ETH, USDT/TRX, USDT/BSC, no row with
        network=="usdt") is skipped instead of taking "the first row seen":
        this app has no per-coin network selector, so an arbitrary pick here
        means a deposit/settle address on a chain the user never chose and
        can't see, a wrong-network transfer is typically unrecoverable.
        Returns {ticker: display_name}; raises on failure so callers can
        leave the curated defaults (plus the "Mainnet" guess for anything
        still unknown) in place."""
        data = self._get(f"{self.BASE}/coins", headers=self._headers())
        rows = data if isinstance(data, list) else data.get("coins") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ProviderError(f"{self.name}: unexpected /coins response shape.")
        native_by_ticker, skipped_ambiguous = group_native_rows(
            rows,
            ticker_of=lambda r: str(r.get("ticker") or r.get("symbol") or "").upper(),
            network_of=lambda r: str(r.get("network") or r.get("chain") or "").strip())
        networks = {t: str(r.get("network") or r.get("chain"))
                   for t, r in native_by_ticker.items()}
        names = {t: str(r.get("name") or t) for t, r in native_by_ticker.items()}
        if not networks:
            raise ProviderError(f"{self.name}: /coins returned no usable native coins.")
        self.networks = networks
        self.names = names
        self.skipped_ambiguous_coins = skipped_ambiguous
        return dict(self.names)

    def _net(self, ticker: str) -> str:
        return self.networks.get(ticker.upper(), "Mainnet")

    def _rate_params(self, from_coin, to_coin, amount) -> dict:
        return {
            "ticker_from": from_coin.lower(),
            "network_from": self._net(from_coin),
            "ticker_to": to_coin.lower(),
            "network_to": self._net(to_coin),
            "amount_from": str(amount),
            "payment": "False",
        }

    def get_quote(self, from_coin, to_coin, amount, destination: str = "") -> Quote:
        try:
            data = self._get(f"{self.BASE}/new_rate",
                             params=self._rate_params(from_coin, to_coin, amount),
                             headers=self._headers())
        except (ProviderError, requests.RequestException) as e:
            msg = str(e)
            if not self.api_key and ("401" in msg or "403" in msg or "key" in msg.lower()):
                msg = ("Trocador needs a free API-Key (get one on your Trocador "
                       "profile, then add it in Settings).")
            return Quote(self.name, from_coin, to_coin, amount, None, None, error=msg)

        # Trocador returns the best deal at the top level plus a `quotes` list.
        est = _dec(data.get("amount_to"))
        rate = (est / amount) if (est is not None and amount) else None
        quotes = data.get("quotes") or []
        best_via = None
        if isinstance(quotes, list) and quotes:
            # pick the highest amount_to as "best"
            def amt(q):
                return _dec(q.get("amount_to")) or Decimal(-1)
            best = max(quotes, key=amt)
            if _dec(best.get("amount_to")) is not None and (est is None or amt(best) > est):
                est = _dec(best.get("amount_to"))
                rate = (est / amount) if amount else None
            best_via = best.get("provider") or data.get("provider")
        best_via = best_via or data.get("provider")
        return Quote(self.name, from_coin, to_coin, amount, est, rate,
                     min_amount=_dec(data.get("minimum")),
                     max_amount=_dec(data.get("maximum")),
                     via=best_via, raw=data)

    def create_swap(self, from_coin, to_coin, amount, settle_address,
                    refund_address="") -> Swap:
        if not self.api_key:
            raise ProviderError("Trocador needs a free API-Key (add it in Settings).")
        payload = self._rate_params(from_coin, to_coin, amount)
        payload["address"] = settle_address
        if refund_address:
            payload["refund"] = refund_address
        # CRITICAL: Use POST so the destination/refund addresses travel in
        # the request body, not the URL, GET params get logged by proxies,
        # reverse proxies, and web-server access logs. If Trocador's API
        # ever rejects POST here, this must be reverted AND a prominent
        # privacy warning shown in the UI when Trocador is selected.
        data = self._post(f"{self.BASE}/new_trade", json=payload,
                          headers=self._headers())
        if not isinstance(data, dict):
            raise ProviderError(
                f"{self.name}: unexpected non-object response from create-swap "
                f"endpoint. Do not send funds. Response type: {type(data).__name__}")
        # Field names vary slightly across Trocador versions; read defensively.
        # address_provider / address_user are the names the current API docs
        # use; the _from/_to spellings are older and kept as fallbacks.
        deposit = (data.get("address_provider") or data.get("address_provider_from")
                   or data.get("address_from") or data.get("payinAddress")
                   or data.get("deposit_address") or "")
        memo = (data.get("address_provider_memo")
                or data.get("address_provider_from_memo") or data.get("memo_from")
                or data.get("payinExtraId"))
        trade_id = (data.get("trade_id") or data.get("id") or "")
        deposit = self._checked_deposit_address(deposit)
        if not trade_id:
            # None of the known field names matched, rather than silently
            # creating a Swap with a blank deposit address (which the UI
            # would happily render a QR code for and invite a deposit to),
            # fail loudly. This has bitten trades before when Trocador
            # renamed a response field across versions.
            raise ProviderError(
                f"{self.name}: response was missing a deposit address or "
                f"trade id. Do not send funds. Try again.")
        return Swap(
            provider=self.name,
            order_id=trade_id,
            from_coin=from_coin,
            to_coin=to_coin,
            deposit_address=deposit,
            deposit_memo=self._checked_memo(memo),
            send_amount=_dec(data.get("amount_from")) or amount,
            deposit_min=_dec(data.get("minimum")),
            deposit_max=_dec(data.get("maximum")),
            estimated_receive=_dec(data.get("amount_to")),
            settle_address=settle_address,
            expires_at=data.get("date_expiration") or data.get("expires_at"),
            status=self._map_status(data.get("status"), STATUS_WAITING),
            raw=data,
            # Field name for the echoed destination varies across Trocador
            # versions/partners like the deposit-address fields above; try
            # the known candidates. None (no candidate present) means "can't
            # independently verify for this response", not "confirmed ok",
            # callers must not treat a missing field as a pass.
            settle_address_confirmed_by_provider=(
                data.get("address_user") or data.get("address_provider_to")
                or data.get("address_to") or data.get("recipient_address")),
        )

    def get_status(self, order_id) -> str:
        data = self._get(f"{self.BASE}/trade", params={"id": order_id},
                         headers=self._headers())
        return self._map_status(data.get("status"), STATUS_UNKNOWN)


