"""History tab: past swaps and their live status.

Split out of app.py as a mixin: every method runs bound to the SwapDesk
instance (self), so this class is never instantiated on its own."""
from __future__ import annotations

import customtkinter as ctk

import config as cfg
import providers as prov

from theme import *  # shared colours, fonts, formatters


class HistoryTabMixin:
    def _build_history_tab(self):
        self.history_wrap = ctk.CTkScrollableFrame(self.tab_history,
                                                   fg_color="transparent")
        self.history_wrap.pack(fill="both", expand=True)
        # Not force=True: rendering every history row is real work (a
        # widget per field per entry) that the user won't see until they
        # switch to this tab anyway. Mark it dirty and let the existing
        # _on_tab_changed path catch it up on first visit, same as every
        # later refresh while History isn't the active tab.
        self._history_dirty = True

    def _refresh_history(self, force: bool = False):
        # This gets called from _update_status() on every poll tick of
        # EVERY open deposit-tracking window (every 15s), on top of the
        # explicit callers below (create swap, save settings, clear
        # history). Rebuilding a widget-per-field row for the WHOLE list on
        # every call is wasted work when the user is on the Swap tab and
        # would never see it - with a long history and a couple of swaps
        # polling at once, that is a full rebuild firing in the background
        # every few seconds for a tab that isn't even on screen. Now it
        # only does the rebuild when the History tab is actually visible;
        # otherwise it just remembers it's stale and catches up the next
        # time the user switches to it (see _on_tab_changed below). The
        # underlying data is still written by cfg.update_history_status()
        # regardless, so nothing is lost by deferring the redraw.
        if not force and self.tabs.get() != "History":
            self._history_dirty = True
            return
        self._history_dirty = False
        for w in self.history_wrap.winfo_children():
            w.destroy()
        hist = cfg.load_history()
        if not hist:
            ctk.CTkLabel(self.history_wrap, text="No swaps yet.",
                         text_color=FAINT, font=F(13)).pack(pady=24)
            return
        for e in hist:
            if not isinstance(e, dict):
                continue
            row = card(self.history_wrap)
            row.pack(fill="x", pady=5, padx=4)
            frm, to = e.get('from', '?'), e.get('to', '?')
            top = f"{frm} → {to}    {e.get('send', '')} {frm}"
            ctk.CTkLabel(row, text=top, font=F(14, "bold"), text_color=TEXT
                         ).pack(anchor="w", padx=16, pady=(12, 0))
            # Resolve retired spellings so an old history entry keeps
            # its colour instead of silently falling back to grey.
            status = prov.normalize_status(e.get('status', ''))
            meta_color = STATUS_COLORS.get(status, MUTED)
            ctk.CTkLabel(row, text=f"{e.get('provider', '?')} · {e.get('time', '?')}",
                         text_color=MUTED, font=F(11)).pack(anchor="w", padx=16,
                                                            pady=(4, 0))
            if status:
                pill(row, status, fg=CARD_HOVER, fg_text=meta_color
                     ).pack(anchor="w", padx=16, pady=(6, 0), ipadx=8, ipady=2)
            ctk.CTkLabel(row, text=f"order {e.get('order_id', '?')}",
                         text_color=FAINT, font=F(10)).pack(anchor="w", padx=16,
                                                            pady=(6, 12))
