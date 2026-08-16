"""
add_reviewers_rest.py

Automates adding reviewers to RQM's Formal Review blocks via direct HTTP
requests (urllib). Session auth is pulled automatically from your
browser's cookie store (via the `browser_cookie3` package) instead of
manually copy-pasting a Cookie header from DevTools -- see AUTHENTICATION
below. 


------------------------------------------------------------------------
FILES THIS SCRIPT READS
------------------------------------------------------------------------
config CSV (pass its path with --config). ArtifactStateId is optional
(fallback-only -- see AUTO-FETCHED artifactStateId above):
    TestCaseID,ArtifactItemId,DescriptorId,ApprovalType,ApprovalName,Reviewers
    3772877,_xxxxx,1,com.ibm.team.workitem.approvalType.review,Test Implementation review,"Palanivel Naveenkanth"

reviewer_ids.csv:
    Name,UserId
    Palanivel Naveenkanth,_cWdrAVxVEeqOvLcoC-Flow
    Mahaboob John Dudekula,_WS0mMKnGEeiiwvS9yOjuaA

Usage:
    python add_reviewers_rest.py --config config_dummy.csv
    python add_reviewers_rest.py --config config_dummy.csv --dry-run
"""

import argparse
import csv
import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import browser_cookie3
except ImportError:
    browser_cookie3 = None

BASE = "https://rb-alm-13-p.de.bosch.com/qm"
RQM_HOST = "rb-alm-13-p.de.bosch.com"

# Confirmed directly from a live GET capture on the dummy test case
# (2026-08-13): both "processArea" and "webContext.projectArea" in the
# real request are "_6Z5ioDVcEfCOKqHqdnmsJw" (digit 5), matching
# test_url.py -- NOT "_6ZSioDVcEfCOKqHqdnmsJw" (letter S), which is what
# an earlier version of this script had. Fixed here.
OSLC_CONFIG_CONTEXT = "_7yS48DVcEfCOKqHqdnmsJw"
WEB_CONTEXT_PROJECT_AREA = "_6Z5ioDVcEfCOKqHqdnmsJw"

APPROVAL_URL_BASE = (
    BASE + "/service/com.ibm.rqm.process.common.service.rest.IApprovalRestService/"
    "approvalGroupDTO"
)

# Confirmed via live DevTools capture (2026-08-16), traced through the RQM
# web bundle's own minified source to the exact GET that populates the
# browser's client-side artifact model (see the AUTO-FETCHED artifactStateId
# docstring section above for the full trail). Needs only the test case's
# plain numeric TestCaseID -- not artifactItemId -- plus the same
# oslc_config.context/webContext.projectArea already used above.
ARTIFACT_URL_BASE = (
    BASE + "/service/com.ibm.rqm.web.common.service.rest.ICompositeWebRestService/artifact"
)


def build_artifact_url(test_case_numeric_id):
    return (
        ARTIFACT_URL_BASE
        + f"?processArea={WEB_CONTEXT_PROJECT_AREA}"
        + "&artifactType=TestCase"
        + f"&oslc_config.context={OSLC_CONFIG_CONTEXT}"
        + f"&id={test_case_numeric_id}"
        + f"&webContext.projectArea={WEB_CONTEXT_PROJECT_AREA}"
    )


def build_approval_url(artifact_item_type_name=None):
    """
    Confirmed via live DevTools capture (2026-08-15) against draft test
    case 3772877's "Artifact Review" block: the real "Add reviewer" UI
    action always includes oslc_config.context, regardless of whether the
    parent test case is Draft or baselined/versioned. There is no
    draft-vs-versioned URL variant -- this always builds the same URL.

    The artifact_item_type_name parameter is accepted (and ignored) only
    so existing call sites that still pass it don't need to change.
    """
    return (
        APPROVAL_URL_BASE
        + f"?oslc_config.context={OSLC_CONFIG_CONTEXT}"
        + f"&webContext.projectArea={WEB_CONTEXT_PROJECT_AREA}"
    )


REVIEWER_IDS_FILE = "reviewer_ids.csv"
NEW_REVIEWER_STATUS = "com.ibm.team.workitem.approvalState.pending"

DEBUG_DIR = Path(__file__).parent / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

# Used to pull the server's "current" stateId out of a 409 error body,
# e.g. "...Current: ... stateId: [_abc123...] ...". Best-effort: if this
# doesn't match your actual error text, the raw body still gets dumped to
# ./debug/ so you can extract it by hand like before.
STATE_ID_FROM_409_RE = re.compile(r"stateId[:=]?\s*\[?\s*(_[A-Za-z0-9_\-]{10,})", re.IGNORECASE)


# ---------------- config / lookup loading ----------------

def load_config(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # ArtifactStateId is no longer required -- fetch_current_state_id() resolves
        # it fresh from the server for every run instead (see ARTIFACT_URL_BASE's
        # comment). Keep the column around as an optional manual fallback only, for
        # if the auto-fetch ever fails and you need to paste in a value by hand.
        required = {"TestCaseID", "ArtifactItemId", "DescriptorId", "ApprovalName", "Reviewers"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"{path} is missing required columns: {missing}")
        for row in reader:
            reviewers = [r.strip() for r in row["Reviewers"].split(",") if r.strip()]
            rows.append({
                "test_id": row["TestCaseID"].strip(),
                "artifact_item_id": row["ArtifactItemId"].strip(),
                "artifact_state_id_fallback": (row.get("ArtifactStateId") or "").strip(),
                "descriptor_id": row["DescriptorId"].strip(),
                "approval_type": (row.get("ApprovalType") or "com.ibm.team.workitem.approvalType.review").strip(),
                "approval_name": row["ApprovalName"].strip(),
                # Confirmed via live capture on 3772877 (a Draft, never-baselined test
                # case): the real "Add reviewer" UI action always sends
                # "VersionedTestCase" here, regardless of the parent test case's actual
                # Draft/baselined state. Leave this at the default for every row unless
                # you have your own live-capture evidence for a specific block.
                "artifact_item_type_name": (row.get("ArtifactItemTypeName") or "VersionedTestCase").strip(),
                # Confirmed via the same capture: "update" is correct even for the
                # first-ever reviewer added to a block, because the block itself
                # (identified by descriptorId) already exists by the time you add a
                # reviewer to it -- "Add reviewer" only ever updates its approver list.
                "cmd": (row.get("Cmd") or "update").strip(),
                "reviewers": reviewers,
            })
    return rows


def load_reviewer_ids(path):
    mapping = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mapping[row["Name"].strip()] = row["UserId"].strip()
    return mapping


# ---------------- HTTP session (cookie: browser-read, then exported-file fallback) ----------------

COOKIES_TXT_PATH = Path(__file__).parent / "cookies.txt"

# Substrings that mark a Chrome/Edge failure as "App-Bound Encryption
# blocked it", as opposed to some other, possibly-fixable error. Chrome
# 127+ / Edge 127+ encrypt their cookie key with a second, OS-level key
# that only the browser's own elevated helper process can unwrap
# (https://developer.chrome.com/docs/chromium/app-bound-encryption).
# browser_cookie3 can only get at that key by asking Windows to run that
# helper elevated, which fails outright on a locked-down/managed corporate
# machine where the signed-in user isn't a local admin. There is no
# workaround for this short of Tier 2 (cookies.txt export) -- it isn't a
# bug in this script, it's Chrome/Edge refusing non-elevated access.
_APP_BOUND_ENCRYPTION_MARKERS = ("admin", "elevat", "access is denied")


def _find_firefox_cookie_dbs():
    """
    Best-effort search for cookies.sqlite across the usual Windows/macOS/
    Linux Firefox profile locations. Used as a fallback when
    browser_cookie3.firefox() can't locate the profile on its own (seen on
    some managed/corporate installs where profiles.ini doesn't mark a
    default profile, which makes browser_cookie3 try to os.path.join()
    a None profile path and blow up with a TypeError).

    Newest-modified cookies.sqlite first, with "default-release" /
    "default" profiles preferred over anything else, since that's the
    profile normal daily browsing (and therefore your RQM login) lives in.
    """
    import glob
    import os

    search_roots = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        search_roots.append(os.path.join(appdata, "Mozilla", "Firefox", "Profiles"))
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        search_roots.append(os.path.join(localappdata, "Mozilla", "Firefox", "Profiles"))
    home = os.path.expanduser("~")
    search_roots.append(os.path.join(home, "Library", "Application Support", "Firefox", "Profiles"))
    search_roots.append(os.path.join(home, ".mozilla", "firefox"))

    found = []
    for root in search_roots:
        found += glob.glob(os.path.join(root, "*", "cookies.sqlite"))
    found = sorted(set(found))

    def sort_key(path):
        is_default = 0 if ("default-release" in path or "default" in path) else 1
        try:
            mtime = -os.path.getmtime(path)
        except OSError:
            mtime = 0
        return (is_default, mtime)

    return sorted(found, key=sort_key)


def _load_firefox_cookies():
    """
    Tries browser_cookie3's normal auto-detection first; if that fails
    (including the known "join() argument must be ... not 'NoneType'"
    profile-detection bug), falls back to scanning disk for cookies.sqlite
    directly and passing it in explicitly via cookie_file=, which skips
    browser_cookie3's profile-finding logic entirely.
    """
    try:
        cj = browser_cookie3.firefox(domain_name=RQM_HOST)
        if len(cj) > 0:
            return cj
    except Exception as e:
        auto_error = e
    else:
        auto_error = RuntimeError("no cookies found for this host in the auto-detected profile")

    candidates = _find_firefox_cookie_dbs()
    if not candidates:
        raise RuntimeError(f"auto-detect failed ({auto_error}); no cookies.sqlite found on disk either")

    file_errors = []
    for db_path in candidates:
        try:
            cj = browser_cookie3.firefox(cookie_file=db_path, domain_name=RQM_HOST)
            if len(cj) > 0:
                print(f"      (used profile: {db_path})")
                return cj
        except Exception as e:
            file_errors.append(f"{db_path}: {e}")
    raise RuntimeError(
        f"auto-detect failed ({auto_error}); tried {len(candidates)} profile(s) found on disk, "
        f"none had a cookie for {RQM_HOST}: " + "; ".join(file_errors)
    )


def _load_cookies_from_browsers():
    """
    Tier 1: reads the live RQM session cookie straight out of your
    browser's cookie store. Tries Firefox first (that's the browser you
    log into RQM with, and the one not affected by Chrome/Edge's
    App-Bound Encryption -- see _APP_BOUND_ENCRYPTION_MARKERS above), then
    falls back to Edge, then Chrome, in case one of those happens to have
    a usable RQM session and isn't locked down the same way. Uses the
    first one that actually has a cookie for RQM_HOST.
    """
    if browser_cookie3 is None:
        raise RuntimeError(
            "browser_cookie3 isn't installed. Run:\n"
            "    pip install browser-cookie3\n"
            "    pip install pywin32   (Windows only, needed to decrypt "
            "Chrome/Edge cookies -- not needed for Firefox)"
        )
    errors = []
    app_bound_blocked = []

    try:
        cj = _load_firefox_cookies()
        if len(cj) > 0:
            print(f"Loaded {len(cj)} cookie(s) from Firefox's live cookie store for {RQM_HOST}.")
            return cj
    except Exception as e:
        errors.append(f"Firefox: {e}")

    for loader, name in (
        (browser_cookie3.edge, "Edge"),
        (browser_cookie3.chrome, "Chrome"),
    ):
        try:
            cj = loader(domain_name=RQM_HOST)
            if len(cj) > 0:
                print(f"Loaded {len(cj)} cookie(s) from {name}'s live cookie store for {RQM_HOST}.")
                return cj
        except Exception as e:
            msg = str(e)
            errors.append(f"{name}: {msg}")
            if any(marker in msg.lower() for marker in _APP_BOUND_ENCRYPTION_MARKERS):
                app_bound_blocked.append(name)

    summary = (
        f"Could not read a live RQM session cookie from Firefox/Edge/Chrome for {RQM_HOST}.\n"
        + "\n".join(errors)
    )
    if app_bound_blocked:
        summary += (
            f"\n\n{'/'.join(app_bound_blocked)} refused non-elevated cookie access -- that's "
            "Chrome/Edge's App-Bound Encryption, which only its own elevated helper process can "
            "unwrap. On a managed/corporate machine without local admin rights this cannot be "
            "worked around from Tier 1. Skip straight to Tier 2 (export cookies.txt from Firefox "
            "with a browser extension) -- see this file's AUTHENTICATION docstring."
        )
    raise RuntimeError(summary)


def _load_cookies_from_file():
    """
    Tier 2 fallback: reads cookies.txt (standard Netscape cookie-file
    format) exported via a browser extension such as "Export Cookies" for
    Firefox (or "Get cookies.txt LOCALLY", which also supports Firefox).
    Useful when Firefox's profile can't be read directly (e.g. it's
    running and has its cookie DB locked).
    """
    if not COOKIES_TXT_PATH.exists():
        raise RuntimeError(
            f"No cookies.txt found at {COOKIES_TXT_PATH}. Export one: install a "
            "\"cookies.txt\" export extension in Firefox (e.g. \"Export Cookies\" "
            "from addons.mozilla.org), log into RQM, click the extension icon on an "
            f"RQM tab, export cookies for {RQM_HOST}, and save the file as "
            f"cookies.txt in {COOKIES_TXT_PATH.parent}."
        )
    cj = http.cookiejar.MozillaCookieJar(str(COOKIES_TXT_PATH))
    cj.load(ignore_discard=True, ignore_expires=True)
    if len(cj) == 0:
        raise RuntimeError(f"{COOKIES_TXT_PATH} exists but has no cookies in it. Re-export it.")
    print(f"Loaded {len(cj)} cookie(s) from {COOKIES_TXT_PATH.name}.")
    return cj


def _load_cookies_from_manual_paste():
    """
    Tier 3 last-resort fallback: prompts you to paste the Cookie header
    value straight from DevTools. Nothing to install, nothing to edit in
    this file -- just paste when asked. Used when both the live-browser
    read and cookies.txt are unavailable (e.g. extension installs are
    blocked by IT policy).

    How to get the value: in Firefox, F12 -> Network tab -> reload the
    RQM page -> click any request going to rb-alm-13-p.de.bosch.com ->
    Headers -> Request Headers -> copy everything after "Cookie:".
    """
    print(
        f"\nCouldn't get a cookie automatically for {RQM_HOST}.\n"
        "Paste the Cookie header value from DevTools (F12 -> Network tab -> "
        "any request to rb-alm-13-p.de.bosch.com -> Request Headers -> Cookie):"
    )
    raw = input("Cookie: ").strip()
    if not raw:
        raise RuntimeError("No cookie pasted.")

    cj = http.cookiejar.CookieJar()
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cj.set_cookie(http.cookiejar.Cookie(
            version=0, name=name.strip(), value=value.strip(),
            port=None, port_specified=False,
            domain=RQM_HOST, domain_specified=True, domain_initial_dot=False,
            path="/", path_specified=True,
            secure=True, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2965=False,
        ))
    if len(cj) == 0:
        raise RuntimeError("Pasted text didn't parse into any cookies -- check you copied the right header.")
    print(f"Using {len(cj)} manually pasted cookie(s) for {RQM_HOST}.")
    return cj


def load_browser_cookies():
    """Tries the live browser read (Firefox first), then the exported cookies.txt, then a manual paste prompt."""
    try:
        return _load_cookies_from_browsers()
    except RuntimeError as browser_error:
        try:
            return _load_cookies_from_file()
        except RuntimeError as file_error:
            print(f"{browser_error}\n{file_error}")
            return _load_cookies_from_manual_paste()


DEFAULT_HEADERS = [
    ("X-Requested-With", "XMLHttpRequest"),
    ("Referer", "https://rb-alm-13-p.de.bosch.com/qm/web/console/RAD7_VW-RADARBELT%20%28qm%29"),
    ("X-Com-Ibm-Team-Configuration-Versions", "LATEST"),
    ("Accept", "text/json"),
]
# Confirmed via live DevTools capture (2026-08-16): the browser's own GET
# against ICompositeWebRestService/artifact (used by fetch_current_state_id())
# sends this exact same header set, including
# "X-Com-Ibm-Team-Configuration-Versions: LATEST" -- unlike the earlier,
# now-removed OSLC resource endpoint, which broke when that header was
# present. One shared opener/header set now covers both the state-fetch GET
# and the approvalGroupDTO write POST.


def build_opener(cookiejar, headers=None):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookiejar))
    opener.addheaders = list(headers) if headers is not None else list(DEFAULT_HEADERS)
    return opener


class Session:
    """Wraps one opener/cookiejar, shared by both the state-fetch GET and the approvalGroupDTO write POST."""

    def __init__(self):
        cookiejar = load_browser_cookies()
        self.opener = build_opener(cookiejar, DEFAULT_HEADERS)

    def refresh(self):
        print("      reloading session cookie from browser...")
        cookiejar = load_browser_cookies()
        self.opener = build_opener(cookiejar, DEFAULT_HEADERS)


def sanity_check(opener):
    """Confirms the cookie actually works before touching any test cases."""
    req = urllib.request.Request(f"{BASE}/web/console")
    try:
        resp = opener.open(req)
        status = resp.status
        url = resp.geturl()
    except urllib.error.HTTPError as e:
        status = e.code
        url = e.geturl() if hasattr(e, "geturl") else ""
    if status != 200 or "login" in url.lower():
        raise RuntimeError(
            "Browser session doesn't look valid -- got redirected or a "
            f"non-200 response (status {status}, url {url}). "
            "Log into RQM in your browser and try again."
        )
    print("Session cookie looks valid.")


def fetch_current_state_id(session, test_case_numeric_id):
    """
    Auto-capture, the same way cookies get auto-loaded: fetches the
    artifact's current state straight from the server instead of requiring
    a manual DevTools capture of artifactStateId every time. See
    ARTIFACT_URL_BASE's comment (and the AUTO-FETCHED artifactStateId
    docstring section) for why this works and why it's necessary.

    IMPORTANT (corrected 2026-08-16 via live side-by-side capture): the
    approvalGroupDTO write's "artifactStateId" field is NOT
    value.testcase.stateId -- that field turned out to be a different,
    unrelated identifier that stays constant across writes. A live capture
    of the browser's own successful "Add reviewer" POST showed its
    artifactStateId matched value.versionableStateId instead (a sibling of
    "testcase" in this same response, not nested inside it) -- and that
    field visibly advances to a new value after each successful write
    (confirmed via the POST response's own artifactitemVersionableStateId),
    exactly the behavior expected of an optimistic-concurrency version
    pointer. This function pulls versionableStateId now, not testcase.stateId.

    Retries once after reloading the browser cookie on 401/403, same as
    add_reviewers() does for the actual write call.
    """
    url = build_artifact_url(test_case_numeric_id)
    req = urllib.request.Request(url)
    try:
        resp = session.opener.open(req)
        body = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print(f"      [{e.code}] session cookie looks stale while fetching state -- reloading and retrying once")
            session.refresh()
            req = urllib.request.Request(url)
            resp = session.opener.open(req)
            body = resp.read().decode("utf-8", errors="ignore")
        else:
            error_body = e.read().decode("utf-8", errors="ignore")
            _dump(test_case_numeric_id, f"state_fetch_http_{e.code}_error", error_body)
            raise RuntimeError(f"HTTP {e.code} while fetching current state for test case {test_case_numeric_id}: {error_body[:300]}") from e

    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        _dump(test_case_numeric_id, "state_fetch_bad_json", body)
        raise RuntimeError(f"Response for test case {test_case_numeric_id} wasn't valid JSON: {e}") from e

    # value.testcase.versionableStateId. Corrected again (2026-08-16) via a
    # full raw-JSON capture: earlier this was assumed to be a SIBLING of
    # "testcase" directly under "value" (value.versionableStateId), based on
    # Firefox DevTools' collapsed object-tree view, which visually flattens
    # nested properties together and made the true nesting level ambiguous.
    # The full raw response text makes the real structure unambiguous:
    # versionableStateId is a field INSIDE testcase, not a sibling of it --
    # and its value tracks correctly across writes (confirmed: it matched
    # the artifactitemVersionableStateId returned by the prior successful
    # write), so this really is the right field, just at the wrong path.
    try:
        state_id = data["soapenv:Body"]["response"]["returnValue"]["value"]["testcase"]["versionableStateId"]
    except (KeyError, TypeError) as e:
        _dump(test_case_numeric_id, "state_fetch_unexpected_shape", body)
        raise RuntimeError(
            f"Got a response for test case {test_case_numeric_id} but couldn't find "
            f"soapenv:Body/response/returnValue/value/testcase/versionableStateId in it ({e}). "
            "See the dumped response in ./debug/."
        ) from e

    if not state_id:
        raise RuntimeError(f"versionableStateId was empty/null for test case {test_case_numeric_id}")
    return state_id


# ---------------- payload / write action ----------------

def build_payload(artifact_item_id, artifact_state_id, descriptor_id, approval_type, approval_name,
                   artifact_item_type_name, cmd, reviewer_user_ids):
    # Your captured payloads show descriptorId unquoted (e.g. descriptorId:0),
    # i.e. a JSON number when it's small -- keep that shape if it parses as
    # an int, otherwise fall back to a string (some descriptorIds may be
    # UUID-style rather than small integers).
    try:
        descriptor_id_value = int(descriptor_id)
    except ValueError:
        descriptor_id_value = descriptor_id

    # NOTE: no "removedApprovers" key here -- the confirmed-working live
    # capture on 3772877 didn't send one for a plain add-a-reviewer action.
    operation = [{
        "cmd": cmd,
        "approvalObj": {
            "dueDate": "remove",
            "approvalType": approval_type,
            "descriptorId": descriptor_id_value,
            "name": approval_name,
            "comments": [],
            "approvers": [
                {"user": uid, "status": NEW_REVIEWER_STATUS}
                for uid in reviewer_user_ids
            ],
        },
    }]
    return {
        "artifactItemId": artifact_item_id,
        "artifactStateId": artifact_state_id,
        "artifactItemTypeName": artifact_item_type_name,
        "artifactPackageUri": "com.ibm.rqm.planning",
        "operationJson": json.dumps(operation),
    }


def _dump(test_id, label, content):
    stamp = f"{test_id}_{label}_{int(time.time())}.json"
    path = DEBUG_DIR / stamp
    path.write_text(content, encoding="utf-8")
    print(f"      (saved {path.name} -- open it to inspect the full response)")
    return path


def _post_once(opener, url, payload):
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    resp = opener.open(req)
    return resp.read().decode("utf-8", errors="ignore")


def add_reviewers(session, test_id, artifact_item_id, artifact_state_id, descriptor_id,
                   approval_type, approval_name, artifact_item_type_name, cmd, reviewer_user_ids):
    url = build_approval_url(artifact_item_type_name)
    payload = build_payload(artifact_item_id, artifact_state_id, descriptor_id, approval_type, approval_name,
                             artifact_item_type_name, cmd, reviewer_user_ids)

    try:
        body = _post_once(session.opener, url, payload)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print(f"      [{e.code}] session cookie looks stale -- reloading from browser and retrying once")
            session.refresh()
            try:
                body = _post_once(session.opener, url, payload)
            except urllib.error.HTTPError as e2:
                _dump(test_id, f"http_{e2.code}_after_refresh", e2.read().decode("utf-8", errors="ignore"))
                raise RuntimeError(
                    f"HTTP {e2.code} even after reloading the browser cookie -- "
                    "your browser session itself has probably expired. Log into "
                    "RQM again in your browser and re-run."
                ) from e2
        elif e.code == 409:
            error_body = e.read().decode("utf-8", errors="ignore")
            fresh_state_id_match = STATE_ID_FROM_409_RE.search(error_body)
            if fresh_state_id_match:
                fresh_state_id = fresh_state_id_match.group(1)
                print(f"      [409] stale ArtifactStateId -- retrying once with server-reported current id: {fresh_state_id}")
                retry_payload = build_payload(artifact_item_id, fresh_state_id, descriptor_id, approval_type, approval_name,
                                               artifact_item_type_name, cmd, reviewer_user_ids)
                try:
                    body = _post_once(session.opener, url, retry_payload)
                except urllib.error.HTTPError as e2:
                    _dump(test_id, "retry_failed", e2.read().decode("utf-8", errors="ignore"))
                    raise RuntimeError(f"Retry after 409 also failed: HTTP {e2.code}. See ./debug/.") from e2
            else:
                _dump(test_id, "409_no_stateid_found", error_body)
                raise RuntimeError(
                    "HTTP 409 (stale ArtifactStateId) and couldn't auto-extract the "
                    "current one from the error body -- open the ./debug/ dump and "
                    "pull it out manually like before."
                ) from e
        else:
            error_body = e.read().decode("utf-8", errors="ignore")
            _dump(test_id, f"http_{e.code}_error", error_body)
            raise RuntimeError(f"HTTP {e.code}. See ./debug/ for the full error body.") from e

    _dump(test_id, "response", body)

    # Safety check: don't trust a 200 blindly. Confirm the response text
    # actually mentions the block we intended (both its name AND its
    # descriptorId), the way test_url.py's captured payload proved
    # descriptorId 0 <-> "Test design review". If either is missing, warn
    # loudly instead of printing a false [OK].
    name_ok = f'"name":"{approval_name}"' in body
    id_ok = (f'"descriptorId":{descriptor_id}' in body) or (f'"descriptorId":"{descriptor_id}"' in body)
    if name_ok and id_ok:
        print(f"      [confirmed] server response matches expected block: descriptorId={descriptor_id}, name={approval_name!r}")
    else:
        print(
            f"      [WARNING] Server response does NOT clearly confirm this hit "
            f"descriptorId={descriptor_id} / name={approval_name!r}. "
            f"Do not assume this succeeded correctly -- inspect the dumped response "
            f"in ./debug/ before trusting it, and before running this against a "
            f"real (non-dummy) test case."
        )

    return body


# ---------------- main ----------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to config CSV (e.g. config_dummy.csv)")
    parser.add_argument("--dry-run", action="store_true", help="Build and print payloads but don't send them")
    args = parser.parse_args()

    config = load_config(args.config)
    reviewer_ids = load_reviewer_ids(REVIEWER_IDS_FILE)
    print(f"Loaded {len(config)} test case row(s) from {args.config}")
    print(f"Loaded {len(reviewer_ids)} reviewer ID(s) from {REVIEWER_IDS_FILE}")

    # Always create a session now, dry-run included: fetching the current state
    # is a safe, read-only GET (no different from loading the page in a browser),
    # and dry-run should show you the actual fresh state it would really use,
    # not a stale placeholder.
    session = Session()
    sanity_check(session.opener)

    results = []
    for row in config:
        test_id = row["test_id"]
        print(f"\n[{test_id}] block={row['approval_name']!r} descriptorId={row['descriptor_id']!r}")
        try:
            missing = [n for n in row["reviewers"] if n not in reviewer_ids]
            if missing:
                raise KeyError(f"No user ID on file for: {', '.join(missing)}. Add them to {REVIEWER_IDS_FILE} first.")
            uids = [reviewer_ids[n] for n in row["reviewers"]]

            # Auto-fetch, the same way cookies get auto-loaded: resolve the
            # current artifactStateId fresh from the server instead of trusting
            # whatever's in the CSV (which can go stale between the moment you
            # captured it and the moment this actually runs).
            try:
                state_id = fetch_current_state_id(session, test_id)
                print(f"      fetched current artifactStateId: {state_id}")
            except Exception as e:
                if row["artifact_state_id_fallback"]:
                    state_id = row["artifact_state_id_fallback"]
                    print(f"      [WARNING] auto-fetch failed ({e}); falling back to CSV value {state_id!r} -- this may be stale")
                else:
                    raise RuntimeError(f"couldn't auto-fetch current state and no ArtifactStateId fallback in CSV: {e}") from e

            if args.dry_run:
                url = build_approval_url(row["artifact_item_type_name"])
                payload = build_payload(
                    row["artifact_item_id"], state_id, row["descriptor_id"],
                    row["approval_type"], row["approval_name"], row["artifact_item_type_name"],
                    row["cmd"], uids,
                )
                print(f"      [dry-run] would POST to:\n      {url}")
                print("      with body:")
                print("     ", json.dumps(payload, indent=2))
                results.append((test_id, "DRY-RUN", ""))
                continue

            add_reviewers(
                session, test_id, row["artifact_item_id"], state_id,
                row["descriptor_id"], row["approval_type"], row["approval_name"],
                row["artifact_item_type_name"], row["cmd"], uids,
            )
            results.append((test_id, "OK", ""))
            print(f"      [OK] {test_id}: reviewers set to {row['reviewers']}")
        except Exception as e:
            results.append((test_id, "FAILED", str(e)))
            print(f"      [FAILED] {test_id}: {e}")

    print("\n--- Summary ---")
    ok = sum(1 for _, status, _ in results if status == "OK")
    print(f"{ok} / {len(results)} succeeded")
    for test_id, status, err in results:
        line = f"{test_id}: {status}"
        if err:
            line += f" -- {err}"
        print(line)


if __name__ == "__main__":
    main()