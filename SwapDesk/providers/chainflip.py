"""providers.chainflip: Chainflip via Broker-as-a-Service (BaaS).

Chainflip is a decentralized cross-chain protocol: swaps are executed by its
own validator set, not by a company holding your coins. BaaS is a hosted
broker in front of it, so unlike THORChain/Maya this one DOES need an API
key (your broker key), but the swap itself is still non-custodial and the
deposit address is a per-swap channel rather than a shared vault.

Two things that differ from every other provider here:

1. A refund address is MANDATORY, not optional. Chainflip requires a
   minimum accepted price on every swap; if the market moves past it the
   source funds are returned to the refund address. Without one there is
   nowhere to send a failed swap, so create_swap refuses rather than
   silently dropping the protection.

2. The swap-creation response does not echo the destination address back.
   The status endpoint does, so the destination check is done with a
   follow-up call. If that call can't answer yet the swap is reported as
   unverified rather than assumed good.

API reference: https://docs.chainflip-broker.io
"""
from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import ClassVar

import requests

from .base import ProviderError, Quote, Swap, SwapProvider
from .constants import (
    STATUS_COMPLETE,
    STATUS_CONFIRMING,
    STATUS_FAILED,
    STATUS_SENDING,
    STATUS_UNKNOWN,
    STATUS_WAITING,
    _dec,
)


class Chainflip(SwapProvider):
    name = "Chainflip"
    site = "https://chainflip-broker.io"

    # Read by preflight (same pattern as KYC_ON_FLAGGED): True means
    # create_swap hard-refuses without a refund_address, so preflight should
    # catch a missing one before the user reaches the confirm step rather
    # than letting create_swap be the first thing to say so.
    REQUIRES_REFUND_ADDRESS = True

    # Read by preflight, same pattern as KYC_ON_FLAGGED: True means this
    # provider's credential and swap addresses travel in the URL query
    # string rather than a POST body or header. VERIFIED against
    # Chainflip's own "Starting a Swap" API reference: /swap has no
    # documented POST or header-auth alternative, so this is a real API
    # constraint, not a missed fix (see _params() below). Disclosed rather
    # than left implicit, since it's a real difference in what a reverse
    # proxy or web-server access log can see.
    LOGS_SENSITIVE_DATA_IN_URL = True

    # Mainnet BaaS host. The docs' worked examples all use the testnet host
    # (perseverance.chainflip-broker.io); this is the same service without
    # that prefix, matching the BaaS product site. Point BASE at the
    # perseverance host to test against testnet.
    BASE = "https://chainflip-broker.io"

    # ticker -> Chainflip asset id ("chain.asset").
    #
    # Deliberately only assets whose mapping is unambiguous. Chainflip lists
    # the same ticker on several chains (usdc.eth, usdc.arb, usdc.sol, and
    # eth.eth vs eth.arb), and picking one for the user is the kind of guess
    # that sends funds to the wrong network. USDC is pinned to Ethereum
    # mainnet because that is what USDC means everywhere else in this app
    # (see EVM_TOKENS in zerox.py); the Arbitrum and Solana variants are
    # left out rather than guessed between.
    ASSETS: ClassVar[dict[str, str]] = {
        "BTC":  "btc.btc",
        "ETH":  "eth.eth",
        "SOL":  "sol.sol",
        "USDC": "usdc.eth",
    }

    # Chainflip's own status vocabulary.
    _MAP: ClassVar[dict[str, str]] = {
        "waiting": STATUS_WAITING,
        "receiving": STATUS_CONFIRMING,
        "swapping": STATUS_CONFIRMING,
        "sending": STATUS_SENDING,
        "sent": STATUS_SENDING,
        "completed": STATUS_COMPLETE,
        "failed": STATUS_FAILED,
    }

    # Slippage guard applied to the quoted price, in percent. Chainflip
    # refunds rather than completing a swap that breaches it, so this is a
    # floor on what the user receives, not a fee.
    SLIPPAGE_PCT = Decimal("2.5")

    # How long Chainflip keeps retrying before refunding, in blocks. One
    # block is 6 seconds, so 150 blocks is ~15 minutes: long enough to ride
    # out ordinary volatility, short enough that a stuck swap refunds the
    # same day.
    RETRY_BLOCKS = 150

    # How long a quote stays usable as the basis for the slippage floor.
    # Long enough to cover reading the rate and confirming the swap, short
    # enough that the floor still reflects a live market.
    QUOTE_TTL_SECONDS = 120

    def __init__(self, api_key: str = "", timeout: int = 20):
        super().__init__(timeout)
        self.api_key = (api_key or "").strip()
        self.assets: dict[str, str] = dict(self.ASSETS)
        self.names: dict[str, str] = {}
        # Last rate quoted, so the swap's minimum price can be derived from
        # the number the user actually approved. Guarded by a lock: quotes
        # are fetched on worker threads, one per provider.
        self._last_quote: tuple | None = None
        self._quote_lock = threading.Lock()

    def configured(self) -> bool:
        return bool(self.api_key)

    def _params(self, **kw) -> dict:
        # VERIFIED hard API constraint, not an oversight: Chainflip's BaaS
        # /swap endpoint only accepts apiKey, destinationAddress and
        # refundAddress as URL query parameters -- confirmed against
        # Chainflip's own "Starting a Swap" API reference
        # (docs.chainflip-broker.io/features/start-swap/), whose own worked
        # curl example sends every one of these as a query string:
        #   curl --location '.../swap?apikey=...&destinationAddress=...
        #                    &refundAddress=...&minimumPrice=...'
        # No POST body form and no header-based auth are documented for
        # this endpoint. That's the opposite of trocador.py's create_swap()
        # (deliberately POST, for the same class of data, because GET
        # params get logged by proxies/web servers) -- the difference here
        # is real, not a missed fix. Disclosed to the user via
        # LOGS_SENSITIVE_DATA_IN_URL / preflight rather than silently
        # matched to Trocador's approach, since there is nothing to switch
        # to.
        p = {"apiKey": self.api_key}
        p.update({k: v for k, v in kw.items() if v is not None})
        return p

    def _asset(self, ticker: str) -> str | None:
        return self.assets.get(ticker.upper())

    def _unsupported(self, ticker: str) -> str:
        known = ", ".join(sorted(self.assets)) or "none loaded"
        return (f"{self.name}: {ticker} isn't routed here ({known}). "
                f"Multi-network assets are left out rather than guessed at.")

    def fetch_coins(self) -> dict:
        """Refresh display names from /assets.

        Only names are taken from the live list. The ticker -> asset-id map
        is NOT rebuilt from it: /assets returns every network variant, and
        collapsing those to a bare ticker is exactly the guess ASSETS exists
        to avoid.
        """
        data = self._get(f"{self.BASE}/assets", params=self._params())
        rows = data.get("assets") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ProviderError(f"{self.name}: unexpected /assets response shape.")
        by_id = {}
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                by_id[str(row["id"])] = str(row.get("name") or row.get("ticker") or "")
        self.names = {t: by_id[a] for t, a in self.assets.items() if a in by_id}
        return dict(self.names)

    def get_quote(self, from_coin, to_coin, amount, destination: str = "") -> Quote:
        src, dst = self._asset(from_coin), self._asset(to_coin)
        if not src or not dst:
            return Quote(self.name, from_coin, to_coin, amount, None, None,
                         error=self._unsupported(from_coin if not src else to_coin),
                         unsupported=True)
        if not self.configured():
            return Quote(self.name, from_coin, to_coin, amount, None, None,
                         error=f"{self.name}: needs a BaaS API key (set it in Settings).")
        try:
            data = self._get(f"{self.BASE}/quotes",
                             params=self._params(sourceAsset=src,
                                                 destinationAsset=dst,
                                                 amount=str(amount)))
        except (ProviderError, requests.RequestException) as e:
            return Quote(self.name, from_coin, to_coin, amount, None, None,
                         error=str(e))

        quote = self._pick_regular_quote(data)
        if quote is None:
            return Quote(self.name, from_coin, to_coin, amount, None, None,
                         error=f"{self.name}: no quote returned for this pair/amount.")
        # estimatedPrice is the documented field and is a rate, not an
        # output amount, so the receive figure is derived rather than read.
        price = _dec(quote.get("estimatedPrice"))
        est = (amount * price) if price is not None else None
        eta = None
        secs = quote.get("estimatedDurationSeconds")
        if isinstance(secs, (int, float)):
            eta = max(1, int(secs // 60))
        if price is not None:
            with self._quote_lock:
                self._last_quote = (from_coin.upper(), to_coin.upper(),
                                    amount, price, time.monotonic())
        return Quote(self.name, from_coin, to_coin, amount, est, price,
                     eta_minutes=eta, raw=quote)

    @staticmethod
    def _pick_regular_quote(data):
        """Pick the instant ('regular') quote from the /quotes array.

        The endpoint also returns 'dca' quotes, which split a swap into
        timed chunks. Those price better but take much longer and behave
        differently on slippage, so an unattended pick of one would be a
        surprise. Only the regular quote is used.
        """
        rows = data if isinstance(data, list) else [data]
        regular = [q for q in rows
                   if isinstance(q, dict) and str(q.get("type", "regular")).lower() == "regular"]
        pool = regular or [q for q in rows if isinstance(q, dict)]
        return pool[0] if pool else None

    def create_swap(self, from_coin, to_coin, amount, settle_address,
                    refund_address="") -> Swap:
        src, dst = self._asset(from_coin), self._asset(to_coin)
        if not src or not dst:
            raise ProviderError(self._unsupported(from_coin if not src else to_coin))
        if not self.configured():
            raise ProviderError(f"{self.name}: needs a BaaS API key "
                                f"(set it in Settings).")
        if not refund_address:
            raise ProviderError(
                f"{self.name}: a refund address on the {from_coin.upper()} "
                f"chain is required. Chainflip enforces a minimum price on "
                f"every swap and returns the deposit if the market moves "
                f"past it, so without a refund address there is nowhere for "
                f"a failed swap to go.")

        # The floor is derived from the rate the user was shown and
        # approved, not from a rate fetched here. Re-quoting at this point
        # would move the floor with the market: if the price fell between
        # the quote and the confirmation, a fresh floor would sit below it
        # and the swap would complete at the worse rate instead of being
        # refunded. Anchoring to the approved rate means an adverse move
        # trips the protection, which is the direction the user asked for.
        approved = self._approved_rate(from_coin, to_coin, amount)
        if approved is None:
            raise ProviderError(
                f"{self.name}: no recent quote for {from_coin.upper()} -> "
                f"{to_coin.upper()} at this amount, so the minimum price "
                f"that protects you from slippage can't be set from a rate "
                f"you approved. Get a fresh rate and try again.")
        est_receive = amount * approved
        min_price = (approved * (Decimal(100) - self.SLIPPAGE_PCT) / Decimal(100))

        data = self._get(f"{self.BASE}/swap",
                         params=self._params(
                             sourceAsset=src, destinationAsset=dst,
                             destinationAddress=settle_address,
                             refundAddress=refund_address,
                             minimumPrice=f"{min_price:f}",
                             retryDurationInBlocks=str(self.RETRY_BLOCKS)))
        if not isinstance(data, dict):
            raise ProviderError(f"{self.name}: unexpected /swap response shape. "
                                f"Do not send funds.")
        deposit = data.get("address")
        swap_id = data.get("id")
        deposit = self._checked_deposit_address(deposit)
        if swap_id is None:
            raise ProviderError(f"{self.name}: response had no deposit address "
                                f"or swap id: try again.")
        return Swap(
            provider=self.name,
            order_id=str(swap_id),
            from_coin=from_coin,
            to_coin=to_coin,
            deposit_address=str(deposit),
            deposit_memo=None,      # deposit channel, no memo/tag involved
            send_amount=amount,
            deposit_min=None,
            deposit_max=None,
            estimated_receive=est_receive,
            settle_address=settle_address,
            expires_at=None,
            status=STATUS_WAITING,
            raw=data,
            settle_address_confirmed_by_provider=self._confirmed_destination(swap_id),
        )

    def _approved_rate(self, from_coin, to_coin, amount) -> Decimal | None:
        """The rate from the most recent matching quote, if still fresh.

        Matched on pair AND amount because Chainflip's price is
        amount-dependent, so a rate quoted for a different size is not the
        one the user approved. Returns None when there is no such quote or
        it has aged out, which the caller turns into "get a fresh rate"
        rather than silently substituting a new price.
        """
        with self._quote_lock:
            last = self._last_quote
        if not last:
            return None
        frm, to, amt, rate, at = last
        if frm != from_coin.upper() or to != to_coin.upper() or amt != amount:
            return None
        if (time.monotonic() - at) > self.QUOTE_TTL_SECONDS:
            return None
        return rate

    def _confirmed_destination(self, swap_id) -> str | None:
        """Read the destination Chainflip recorded, for the echo check.

        /swap doesn't return it, so this is a second call. Returns None on
        any failure, which callers treat as 'unverified' rather than 'ok':
        the channel may not be queryable the instant it is opened, and an
        exception here would abort a swap that is actually fine.
        """
        try:
            st = self._get(f"{self.BASE}/status-by-id",
                           params=self._params(swapId=str(swap_id)))
        except (ProviderError, requests.RequestException):
            return None
        status = st.get("status") if isinstance(st, dict) else None
        if isinstance(status, dict):
            return status.get("destinationAddress")
        return None

    def get_status(self, order_id) -> str:
        try:
            data = self._get(f"{self.BASE}/status-by-id",
                             params=self._params(swapId=str(order_id)))
        except (ProviderError, requests.RequestException):
            return STATUS_UNKNOWN
        status = data.get("status") if isinstance(data, dict) else None
        state = status.get("state") if isinstance(status, dict) else None
        return self._map_status(state, STATUS_UNKNOWN)
