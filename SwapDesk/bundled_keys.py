"""Optional affiliate credentials shipped with a build.

User keys entered in Settings always win, nothing here gets written to
data/config.json. Only plain header keys are bundleable, config.py
refuses signing creds (FixedFloat api_secret, SideShift secret) from
here since publishing those lets anyone sign as that account.

Assume anything bundled is public, it comes back out of the binary in
minutes. Check provider affiliate terms before shipping a key.
"""

# Fill these in for a keyed build. Leave empty for a bring-your-own build;
# the app works either way, it just starts with fewer providers available.
#
# IF THE REPOSITORY IS PUBLIC, DO NOT PUT KEYS HERE. This file is tracked.
# Put them in keys.local.json (gitignored) or in the environment, and let
# build.py bake them into the binary. See "Building a keyed release" in the
# README. That way the public source stays a bring-your-own-keys build and
# only the compiled artifacts carry credentials.

BUNDLED_KEYS = {
    "trocador":  {"api_key": ""},
    "changenow": {"api_key": ""},
    "dex":       {"api_key": ""},
}

# build.py writes keys_baked.py (gitignored, deleted after the build) and
# PyInstaller compiles it in. Absent in a source checkout, which is exactly
# what makes running from source a bring-your-own-keys build.
try:
    import keys_baked as _baked
    for _provider, _fields in getattr(_baked, "KEYS", {}).items():
        if _provider in BUNDLED_KEYS:
            BUNDLED_KEYS[_provider].update(
                {k: v for k, v in _fields.items() if v})
except ImportError:
    pass

# Never populated. config.py enforces this. See _SIGNING_FIELDS there.
# Listed explicitly so nobody "helpfully" adds them later.
NEVER_BUNDLE = {
    "fixedfloat": ("api_key", "api_secret"),
    "sideshift":  ("secret", "affiliate_id"),
}

# Shown in the UI wherever a bundled credential is in use. Keep it true.
DISCLOSURE = (
    "This build ships with the project's own affiliate keys, so it works "
    "without any signup. Swaps created on them are credited to the project's "
    "affiliate account, which is how it's funded. The rate you're quoted is "
    "the rate you get, and the app still never touches your coins. Enter your "
    "own keys in Settings to route around it entirely."
)


def bundled_for(provider: str) -> dict:
    """Bundled credential fields for one provider; empty if none."""
    return {k: v for k, v in BUNDLED_KEYS.get(provider, {}).items() if v}


def has_any_bundled() -> bool:
    return any(bundled_for(p) for p in BUNDLED_KEYS)
