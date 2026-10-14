"""Two fixes to Patchright 1.63.0's driver, applied once at image build.

1. A page whose main CDP session closes while Patchright sets up request
   interception (a tab closed by a site, by the user, or by the agent's watchdog)
   rejected a promise nothing awaits, and Node ended the whole driver: every later
   call failed with "Connection closed while reading from the driver". A closed
   main session means the page is gone, so it is ignored like a closed child one.

2. Patchright intercepts every request of every page (Fetch.enable on "*") so it
   can inject init scripts into documents, even when no route or init script is
   set. Stealthbox sets neither, and the interception broke some framework pages:
   a store's search results rendered on 2 loads in 5 with it, 5 in 5 without it
   (and with stock Playwright). It is now on only when a route needs it, as in
   stock Playwright. Patchright still never sends Runtime.enable.

Fails if Patchright's code no longer matches.
"""

import pathlib

import patchright

BUNDLE = pathlib.Path(patchright.__file__).parent / "driver" / "package" / "lib" / "coreBundle.js"
PATCHES = [
    ("""            if (info.isMain) return cb(info);
            return cb(info).catch((e) => {""",
     """            return cb(info).catch((e) => {"""),
    ("""        this._networkManager.setRequestInterception(true);
        this.initScriptTag""",
     """        this._networkManager.setRequestInterception(this._page.needsRequestInterception());
        this.initScriptTag"""),
]

source = BUNDLE.read_text()
for old, new in PATCHES:
    if source.count(old) != 1:
        raise SystemExit(f"{BUNDLE}: Patchright changed; re-check this patch:\n{old}")
    source = source.replace(old, new)
BUNDLE.write_text(source)
print("patched", BUNDLE)
