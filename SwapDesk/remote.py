"""Route swaps through a SwapDesk API server instead of straight to providers.

This exists so an install can be given one API key rather than five sets of
provider credentials. The server holds the provider keys; the desktop app
holds a single key that authorises it to use them.

It is written as a SwapProvider subclass on purpose. app.py, preflight.py
and the history code all just iterate whatever build_providers() hands
back and call get_quote / create_swap / get_status, so a remote backend
that satisfies that interface drops in with no changes to any of them. The
alternative (a parallel "remote mode" branch through the whole UI) would
mean every future change to the swap flow has to be made twice.

What it does NOT do: it does not hide the fact that a third party is now in
the loop. Running against a remote API means that server sees your pair,
your amount and your destination address, which a direct provider call
also does, but now there is one operator seeing all of them across every
swap instead of the traffic being split per provider. The UI labels this
plainly rather than presenting it as a free upgrade.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import quote, urlsplit

from preflight import _addr_equal
from providers import (
    KNOWN_STATUSES,
    STATUS_UNKNOWN,
    ProviderError,
    ProviderNetworkError,
    Quote,
    Swap,
    SwapProvider,
    _dec,
)

# Session tokens are re-minted this many seconds before they actually
# expire, so a long-running swap flow never fails midway on a token that
# went stale between the quote and the create.
_TOKEN_SKEW_SECONDS = 60

# The only hosts an unencrypted http:// URL may point at.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_plain_http_to_remote(url: str) -> bool:
    """True if `url` is http:// pointing somewhere that ISN'T loopback.

    Parses instead of prefix-matching. The previous version of this check was
    a regex anchored on the text right after "http://", which a userinfo
    section walks straight past: in "http://127.0.0.1:8080@evil.com/" the
    "127.0.0.1:8080" is credentials, not a host, and the request goes to
    evil.com. That URL matched the old pattern and was allowed, which put the
    API key, the pair, the amount and the destination address on the wire in
    clear to an attacker-chosen host. urlsplit().hostname returns the real
    host (and strips the brackets from an IPv6 literal), so the comparison is
    against what requests will actually connect to.

    Shared with ui/settings_tab.on_save_settings so the entry-path check and
    the transport check can't drift apart; see _url() for why both exist.
    """
    if not url.lower().startswith("http://"):
        return False
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return True          # unparseable: treat as unsafe, not as loopback
    return (host or "").lower() not in _LOOPBACK_HOSTS

_HTTP_STATUS_IN_MSG = re.compile(r"\bHTTP (\d{3})\b")


def _http_status(exc) -> int | None:
    """The HTTP status behind a provider exception, or None.

    ProviderNetworkError carries status_code directly. A plain ProviderError
    (which is what SwapProvider._json raises for a non-gateway status such as
    401/403) does not, so fall back to reading it out of the message that
    _json formats. Only used to pick friendlier wording; every path re-raises
    unchanged when the status isn't one being special-cased."""
    code = getattr(exc, "status_code", None)
    if code is not None:
        return code
    m = _HTTP_STATUS_IN_MSG.search(str(exc))
    return int(m.group(1)) if m else None


class RemoteSwapDesk(SwapProvider):
    """One entry in the comparison, backed by a SwapDesk API server.

    The server fans out to every provider it has configured and returns
    them ranked, so this class takes the top row. `via` carries which
    upstream provider actually won, which is the same field Trocador uses
    for the same purpose, so the UI already knows how to display it.
    """

    # Read by preflight, same pattern as KYC_ON_FLAGGED / REQUIRES_REFUND_
    # ADDRESS / LOGS_SENSITIVE_DATA_IN_URL. True means the deposit address
    # this provider hands back was chosen by a server named in local config
    # rather than by a provider with a public identity. In plaintext mode that
    # config is an unauthenticated file: anything able to write it can point
    # this row at a host of its choosing, and every check in the flow still
    # passes, because they all validate the user's DESTINATION rather than the
    # deposit address. Disclosing the URL at the confirm step is what makes a
    # substituted server visible to the person about to fund it.
    THIRD_PARTY_ROUTING = True

    name = "SwapDesk API"
    site = ""
    # The server decides which providers it can reach; from here a key is
    # all that's ever needed, so there is no "unconfigured but still
    # quotable" state. Without a key this provider is skipped entirely.
    can_quote_without_config = False

    def __init__(self, base_url: str = "", api_key: str = "", timeout: int = 25):
        super().__init__(timeout=timeout)
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self._token = ""
        self._token_expires_at = 0
        # Quote fetching runs one thread per provider, so the token
        # check-then-mint in _auth_token() needs to be atomic.
        self._token_lock = threading.Lock()
        # (from, to, amount) -> quote_id from the most recent /quote call.
        # create_swap() is handed raw coins and an amount by app.py, not a
        # quote id, so the id issued during the quote step has to be
        # carried across. Keyed rather than a single slot because the user
        # can quote several pairs before committing to one.
        self._quote_ids: dict[tuple, str] = {}

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    # -- transport ------------------------------------------------------

    def _url(self, path: str) -> str:
        if not self.base_url:
            raise ProviderError("No SwapDesk API URL configured.")
        if not self.base_url.startswith(("http://", "https://")):
            raise ProviderError(
                "SwapDesk API URL must start with https:// (or http:// for "
                "a local server).")
        # Settings refuses this too, but that covers one entry path.
        # config.json is portable, so a hand-edit or a config copied from
        # another machine reaches here without passing that validator. Over
        # plain http to a remote host the API key, pair, amount and
        # destination all go out in clear. Loopback has no hop to intercept.
        if is_plain_http_to_remote(self.base_url):
            raise ProviderError(
                "Refusing to send your SwapDesk API key over plain http:// "
                "to a remote host: the key, the pair, the amount and your "
                "destination address would all travel unencrypted. Use "
                "https://, or run the server on this machine. (Fix the URL "
                "in Settings > SwapDesk API server.)")
        return f"{self.base_url}{path}"

    def _auth_token(self) -> str:
        """Trade the API key for a session token, cached until near expiry.

        The key itself only ever goes to /auth/client. Every other call
        carries the short-lived token, so the key isn't repeated on each
        request where a logging proxy or a crash dump could pick it up.

        Guarded by a lock: quotes are fetched concurrently (one thread per
        provider), so an unsynchronized check-then-mint let several threads
        each decide the token was stale and each POST /auth/client, burning
        calls against whatever rate limit the server enforces to arrive at
        the same token.
        """
        with self._token_lock:
            if self._token and time.time() < self._token_expires_at - _TOKEN_SKEW_SECONDS:
                return self._token
            try:
                data = self._post(
                    self._url("/auth/client"),
                    json={"client_version": "desktop", "platform": "desktop"},
                    headers={"X-API-Key": self.api_key})
            except ProviderNetworkError as e:
                if e.status_code == 503:
                    raise ProviderError(
                        "That SwapDesk API server isn't accepting client keys "
                        "(no keys configured on the server side).")
                raise
            except ProviderError as e:
                if _http_status(e) in (401, 403):
                    raise ProviderError(
                        "SwapDesk API rejected this API key. Check it in "
                        "Settings, or ask whoever runs the server for a new "
                        "one.")
                raise
            if not isinstance(data, dict):
                raise ProviderError(
                    "SwapDesk API returned an unexpected response to the "
                    "key exchange.")
            token = data.get("session_token")
            if not token:
                raise ProviderError("SwapDesk API returned no session token.")
            self._token = token
            self._token_expires_at = int(data.get("expires_at") or 0)
            return self._token

    def _call(self, method: str, path: str, payload: dict | None = None):
        token = self._auth_token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            if method == "post":
                return self._post(self._url(path), json=payload, headers=headers)
            return self._get(self._url(path), headers=headers)
        except ProviderNetworkError as e:
            if e.status_code == 429:
                # _request already retried this with backoff and honoured any
                # Retry-After header before giving up, so by the time it
                # reaches here waiting is genuinely the only option left.
                raise ProviderError(
                    "SwapDesk API rate limit reached. Wait a minute and retry.")
            raise
        except ProviderError as e:
            if _http_status(e) in (401, 403):
                # Token went stale earlier than advertised (server restarted
                # with a fresh SESSION_SECRET, most likely). Drop it and let
                # the next call mint a new one rather than failing
                # permanently.
                with self._token_lock:
                    self._token = ""
                    self._token_expires_at = 0
                raise ProviderError("SwapDesk API session expired, try again.")
            raise

    # -- SwapProvider interface -----------------------------------------

    def get_quote(self, from_coin, to_coin, amount, destination: str = "") -> Quote:
        if not self.configured():
            return Quote(self.name, from_coin, to_coin, amount, None, None,
                         error="Add your SwapDesk API URL and key in Settings.")
        try:
            data = self._call("post", "/quote", {
                "from_asset": from_coin,
                "to_asset": to_coin,
                "amount": str(amount),
                "destination_address": destination or "",
            })
        except ProviderError as e:
            return Quote(self.name, from_coin, to_coin, amount, None, None,
                         error=str(e))

        rows = data.get("quotes") or []
        if not rows:
            # The server distinguishes "no provider routes this pair" from
            # "everything broke", and that distinction drives very different
            # UI copy, so preserve it rather than flattening both to a
            # generic failure.
            errors = data.get("errors") or []
            unsupported = bool(errors) and all(e.get("unsupported") for e in errors)
            msg = (f"{self.name}: no provider on that server routes "
                   f"{from_coin.upper()} to {to_coin.upper()}."
                   if unsupported else
                   f"{self.name}: no quotes returned "
                   f"({len(errors)} provider(s) failed).")
            return Quote(self.name, from_coin, to_coin, amount, None, None,
                         error=msg, unsupported=unsupported)

        best = rows[0]  # server returns them already ranked best-first
        self._quote_ids[(from_coin, to_coin, str(amount))] = best.get("quote_id", "")
        return Quote(
            provider=self.name,
            from_coin=from_coin,
            to_coin=to_coin,
            send_amount=amount,
            estimated_receive=_dec(best.get("receive_amount")),
            rate=_dec(best.get("rate")),
            eta_minutes=best.get("eta_minutes"),
            # Two hops to name: the API picked a provider, and that provider
            # may itself be an aggregator that picked an exchange.
            via=" / ".join(x for x in (best.get("provider"), best.get("via")) if x),
            raw=best,
        )

    def create_swap(self, from_coin, to_coin, amount, settle_address,
                    refund_address="") -> Swap:
        quote_id = self._quote_ids.get((from_coin, to_coin, str(amount)), "")
        if not quote_id:
            raise ProviderError(
                f"{self.name}: no live quote for this pair and amount. "
                f"Get a fresh quote and try again.")
        # refund_address is transmitted. It used to be accepted by this
        # signature and then dropped on the floor, while preflight showed the
        # user two green ticks confirming its format and that it differed from
        # the destination. A user who fills that field is usually depositing
        # from an address they do not control (an exchange withdrawal), which
        # is exactly the case where a refund to the sending address is lost.
        # A server that does not use the field ignores an extra key; one that
        # does now gets it.
        payload = {
            "quote_id": quote_id,
            "destination_address": settle_address,
        }
        if refund_address:
            payload["refund_address"] = refund_address
        data = self._call("post", "/swap/create", payload)
        # Same guard every other provider uses, inherited from SwapProvider
        # rather than restated here. The local version was `if not
        # deposit_address`, which only rejects the falsy cases, so " ",
        # "addr\n" and a non-string all passed straight into the Swap and
        # would have been shown as the address to send funds to. That is the
        # exact drift _checked_deposit_address() was centralised to stop; a
        # ninth hand-written copy of it reintroduced the defect here.
        deposit_address = self._checked_deposit_address(
            data.get("deposit_address"))

        # The server echoes back the destination it independently read out
        # of the upstream provider's create response. Check it here too:
        # this app is the only place that knows what the user actually
        # approved, so verifying it locally is the whole point of that
        # field. A mismatch means the address the funds will be paid to is
        # not the one on screen, which is unrecoverable once a deposit is
        # broadcast.
        echoed = data.get("settle_address_confirmed_by_provider")
        if echoed and not _addr_equal(echoed.strip(), settle_address.strip()):
            raise ProviderError(
                f"{self.name}: SAFETY ABORT: the server reports the swap was "
                f"registered to {echoed!r}, not the address you approved "
                f"({settle_address!r}). Do not send anything to the deposit "
                f"address it returned.")

        # A single quote_id is only good for one create on the server side
        # anyway; drop it so a stale id can't be replayed from here.
        self._quote_ids.pop((from_coin, to_coin, str(amount)), None)

        swap_id = data.get("swap_id")
        if not swap_id or not str(swap_id).strip():
            # Every other provider refuses a response with no order id. Without
            # one the swap cannot be polled, cannot be looked up on the
            # server, and appears in history as an untrackable row -- while the
            # deposit address it came with is perfectly fundable.
            raise ProviderError(
                f"{self.name}: response had no swap id. Do not send funds. "
                f"Try again.")
        return Swap(
            provider=self.name,
            order_id=str(swap_id).strip(),
            from_coin=from_coin,
            to_coin=to_coin,
            deposit_address=deposit_address,
            deposit_memo=self._checked_memo(data.get("deposit_memo")),
            send_amount=amount,
            deposit_min=_dec(data.get("deposit_min")),
            deposit_max=_dec(data.get("deposit_max")),
            estimated_receive=_dec(data.get("estimated_receive")),
            settle_address=settle_address,
            expires_at=data.get("expiration"),
            raw=data,
            settle_address_confirmed_by_provider=echoed,
        )

    def get_status(self, order_id) -> str:
        # The id is percent-encoded rather than interpolated raw: it comes back
        # out of history.json, which is a plain file, and a value containing
        # "/" or "?" would otherwise reshape the request path.
        data = self._call("get", f"/swap/status/{quote(str(order_id), safe='')}")
        raw = str(data.get("status") or "")
        # Validated against the app's own vocabulary instead of passed through.
        # Every other provider maps its vendor status through a _MAP with a
        # safe default; this one trusted the server to have normalised
        # already. A server answering {"status": "Complete"} could therefore
        # forge a TERMINAL status: polling stops, the window says the coins
        # were sent, and update_history_status() writes it to disk
        # permanently. An unrecognised status is Unknown, which keeps polling.
        return raw if raw in KNOWN_STATUSES else STATUS_UNKNOWN
