"""providers.stealthex: StealthEX instant exchange.

Non-custodial and account-free in the ordinary case: no registration, funds
move wallet to wallet, and the integrator only needs an API key.

The caveat this provider carries, and the reason it is flagged in the UI
rather than presented like the others: StealthEX reserves the right to ask
for identity verification when a swap is flagged, which in practice means
large or otherwise unusual amounts. Their own description is "without
mandatory KYC or entering any private data, except the cases when
transactions are marked as suspicious". That is a real exception to this
app's premise, and a user who picked SwapDesk to avoid handing over
documents should be told before they commit funds, not after the deposit is
already on-chain and the payout is held.

`KYC_ON_FLAGGED` below drives that disclosure. Settings shows it next to the
toggle, and preflight raises it as a warning on every StealthEX swap.

API: https://api.stealthex.io (v2). The key is passed as an api_key query
parameter.
"""
from __future__ import annotations

from typing import ClassVar

import requests

from .base import ProviderError, Quote, Swap, SwapProvider
from .constants import (
    COINS,
    STATUS_COMPLETE,
    STATUS_CONFIRMING,
    STATUS_EXCHANGING,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_REFUNDED,
    STATUS_SENDING,
    STATUS_UNKNOWN,
    STATUS_WAITING,
    _dec,
)


class StealthEX(SwapProvider):
    name = "StealthEX"
    site = "https://stealthex.io/partners/api/"

    BASE = "https://api.stealthex.io/api/v2"

    # Read by the Settings section and by preflight. True means: this
    # provider can require identity verification on a swap it flags, so the
    # no-KYC premise does not hold unconditionally.
    KYC_ON_FLAGGED = True

    # ticker -> StealthEX currency symbol. StealthEX uses plain lowercase
    # tickers rather than chain-qualified ids, so this is a direct mapping
    # for the coins this app already knows. fetch_coins() replaces it with
    # the live /currency list.
    #
    # USDC is left out on purpose, matching FixedFloat's _CODES: unlike
    # every other provider in this app, StealthEX's create_swap/get_quote
    # send no network field alongside "usdc", and this app has never
    # confirmed against StealthEX's live /currency catalogue that a bare
    # "usdc" is unambiguously the Ethereum-mainnet USDC every other part of
    # the app (and the COINS registry) assumes. Until that's verified, offer
    # it via StealthEX's native-chain coins only and exclude USDC here, the
    # same way FixedFloat does for the same reason.
    SYMBOLS: ClassVar[dict[str, str]] = {t: t.lower() for t in COINS if t != "USDC"}

    _MAP: ClassVar[dict[str, str]] = {
        "waiting": STATUS_WAITING,
        "confirming": STATUS_CONFIRMING,
        "exchanging": STATUS_EXCHANGING,
        "sending": STATUS_SENDING,
        "finished": STATUS_COMPLETE,
        "failed": STATUS_FAILED,
        "refunded": STATUS_REFUNDED,
        "expired": STATUS_EXPIRED,
        # Their own term for "we want documents before this proceeds". Not a
        # terminal state, but the user cannot resolve it from this app.
        "verifying": STATUS_UNKNOWN,
    }

    def __init__(self, api_key: str = "", timeout: int = 20):
        super().__init__(timeout)
        self.api_key = (api_key or "").strip()
        self.symbols: dict[str, str] = dict(self.SYMBOLS)
        self.names: dict[str, str] = {t: v[0] for t, v in COINS.items()}

    def configured(self) -> bool:
        return bool(self.api_key)

    def _params(self, **kw) -> dict:
        p = {"api_key": self.api_key}
        p.update({k: v for k, v in kw.items() if v is not None})
        return p

    def _sym(self, ticker: str) -> str | None:
        return self.symbols.get(ticker.upper())

    def _unsupported(self, ticker: str) -> str:
        known = ", ".join(sorted(self.symbols)) or "none loaded"
        return f"{self.name}: {ticker} isn't in its enabled set here ({known})."

    def fetch_coins(self) -> dict:
        """Replace the symbol table from /currency.

        Restricted to tickers this app already has an address pattern for.
        StealthEX lists well over a thousand assets, and adding one here
        without a curated pattern would put a coin in the picker that
        preflight then refuses, which reads as a bug rather than a guard.
        """
        data = self._get(f"{self.BASE}/currency", params=self._params())
        if not isinstance(data, list):
            raise ProviderError(f"{self.name}: unexpected /currency response shape.")
        syms = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").lower()
            tick = sym.upper()
            # USDC excluded here too: the live /currency list gives no
            # network qualification either, so an accepted "usdc" row would
            # be exactly as unverified as the curated SYMBOLS entry would
            # have been. See the SYMBOLS comment above.
            if sym and tick in COINS and tick != "USDC":
                syms[tick] = sym
        if not syms:
            raise ProviderError(f"{self.name}: /currency returned no usable coins.")
        self.symbols = syms
        self.names = {t: COINS[t][0] for t in syms}
        return dict(self.names)

    def get_quote(self, from_coin, to_coin, amount, destination: str = "") -> Quote:
        src, dst = self._sym(from_coin), self._sym(to_coin)
        if not src or not dst:
            return Quote(self.name, from_coin, to_coin, amount, None, None,
                         error=self._unsupported(from_coin if not src else to_coin),
                         unsupported=True)
        if not self.configured():
            return Quote(self.name, from_coin, to_coin, amount, None, None,
                         error=f"{self.name}: needs an API key (set it in Settings).")
        try:
            data = self._get(f"{self.BASE}/estimate/{src}/{dst}",
                             params=self._params(amount=str(amount)))
        except (ProviderError, requests.RequestException) as e:
            return Quote(self.name, from_coin, to_coin, amount, None, None,
                         error=str(e))

        est = _dec(data.get("estimated_amount")) if isinstance(data, dict) else _dec(data)
        if est is None:
            return Quote(self.name, from_coin, to_coin, amount, None, None,
                         error=f"{self.name}: no estimate returned for this pair/amount.")
        rate = (est / amount) if amount else None

        # Best-effort: a failure here must not lose an otherwise good quote,
        # so the min/max are simply left unset.
        mn = mx = None
        try:
            rng = self._get(f"{self.BASE}/range/{src}/{dst}", params=self._params())
            if isinstance(rng, dict):
                mn, mx = _dec(rng.get("min_amount")), _dec(rng.get("max_amount"))
        except (ProviderError, requests.RequestException):
            pass

        return Quote(self.name, from_coin, to_coin, amount, est, rate,
                     min_amount=mn, max_amount=mx, raw=data)

    def create_swap(self, from_coin, to_coin, amount, settle_address,
                    refund_address="") -> Swap:
        src, dst = self._sym(from_coin), self._sym(to_coin)
        if not src or not dst:
            raise ProviderError(self._unsupported(from_coin if not src else to_coin))
        if not self.configured():
            raise ProviderError(f"{self.name}: needs an API key "
                                f"(set it in Settings).")
        body = {
            "currency_from": src,
            "currency_to": dst,
            "address_to": settle_address,
            "amount_from": str(amount),
        }
        if refund_address:
            body["refund_address"] = refund_address
        data = self._post(f"{self.BASE}/exchange", params=self._params(), json=body)
        if not isinstance(data, dict):
            raise ProviderError(f"{self.name}: unexpected /exchange response shape. "
                                f"Do not send funds.")
        deposit = data.get("address_from")
        order_id = data.get("id")
        deposit = self._checked_deposit_address(deposit)
        if not order_id:
            raise ProviderError(f"{self.name}: response had no deposit address "
                                f"or order id: try again.")
        return Swap(
            provider=self.name,
            order_id=str(order_id),
            from_coin=from_coin,
            to_coin=to_coin,
            deposit_address=str(deposit),
            deposit_memo=self._checked_memo(data.get("extra_id_from")),
            send_amount=amount,
            deposit_min=None,
            deposit_max=None,
            estimated_receive=_dec(data.get("amount_to")),
            settle_address=settle_address,
            expires_at=None,
            status=self._map_status(data.get("status"), STATUS_WAITING),
            raw=data,
            # StealthEX echoes the destination it recorded, which is what
            # makes the independent destination check possible here.
            settle_address_confirmed_by_provider=data.get("address_to"),
        )

    def get_status(self, order_id) -> str:
        try:
            data = self._get(f"{self.BASE}/exchange/{order_id}",
                             params=self._params())
        except (ProviderError, requests.RequestException):
            return STATUS_UNKNOWN
        if not isinstance(data, dict):
            return STATUS_UNKNOWN
        return self._map_status(data.get("status"), STATUS_UNKNOWN)
