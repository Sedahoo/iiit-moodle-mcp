"""MCP server for IIIT Hyderabad Moodle (https://courses.iiit.ac.in/).

Exposes your Moodle account as MCP tools: courses, course contents and files,
upcoming deadlines, assignments and submission status, grades, forums,
announcements and notifications.

Auth
----
The site only allows CAS (login.iiit.ac.in) sign-in, so Moodle's plain
username/password token endpoint rejects IIIT accounts. Instead this server:

1. logs in through CAS with IIIT_USERNAME / IIIT_PASSWORD (or reuses a
   browser MOODLE_SESSION cookie), then
2. runs the Moodle mobile-app "launch" flow, which hands back a web-service
   token for the `moodle_mobile_app` service.

The token is cached on disk and refreshed automatically when it expires. If
you already have a token (Preferences -> Security keys), set MOODLE_TOKEN and
no login happens at all. Run `moodle_whoami` to confirm auth works.
"""

from __future__ import annotations

import base64
import html
import json
import mimetypes
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

try:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError
except ModuleNotFoundError:  # mcp SDK 2.x renamed FastMCP -> MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a .env in CWD or the project root, without
    overriding variables already set in the real environment."""
    for base in (os.getcwd(), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
        path = os.path.join(base, ".env")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip().strip("'\""))
        except OSError:
            pass


_load_dotenv()

BASE_URL = os.environ.get("MOODLE_BASE_URL", "https://courses.iiit.ac.in").rstrip("/")
USERNAME = os.environ.get("IIIT_USERNAME", "").strip()
PASSWORD = os.environ.get("IIIT_PASSWORD", "")
SESSION_COOKIE = os.environ.get("MOODLE_SESSION", "").strip()
DOWNLOAD_DIR = Path(
    os.path.expanduser(os.environ.get("MOODLE_DOWNLOAD_DIR", "~/Downloads/moodle"))
)
TOKEN_CACHE = Path(
    os.path.expanduser(
        os.environ.get("MOODLE_TOKEN_CACHE", "~/.cache/iiit-moodle-mcp/token.json")
    )
)

SERVICE = "moodle_mobile_app"
# Moodle's launch flow behaves as it does for the official app with this UA.
APP_UA = "Mozilla/5.0 (Linux) MoodleMobile"
IST = timezone(timedelta(hours=5, minutes=30))
TIMEOUT = httpx.Timeout(60.0)

mcp = _Server("iiit-moodle")


class MoodleError(ToolError):
    """A structured error from Moodle, CAS or the transport.

    Subclasses ToolError so the SDK passes the message to the client instead
    of replacing it with a generic "Error executing tool"."""


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
_state: dict[str, Any] = {"token": os.environ.get("MOODLE_TOKEN", "").strip(), "userid": None}


def _read_cached_token() -> str:
    try:
        return json.loads(TOKEN_CACHE.read_text()).get("token", "")
    except (OSError, ValueError):
        return ""


def _write_cached_token(token: str) -> None:
    try:
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(json.dumps({"token": token, "base_url": BASE_URL}))
        TOKEN_CACHE.chmod(0o600)
    except OSError:
        pass


def _cas_login(client: httpx.Client) -> None:
    """Sign in to Moodle through CAS, leaving a MoodleSession cookie in client."""
    resp = client.get(f"{BASE_URL}/login/index.php", params={"authCAS": "CAS"})
    if "login.iiit.ac.in" not in str(resp.url):
        return  # already logged in (e.g. via MOODLE_SESSION)
    m = re.search(r'name="execution"\s+value="([^"]+)"', resp.text)
    if not m:
        raise MoodleError("Could not find the CAS login form; the login page may have changed.")
    resp = client.post(
        str(resp.url),
        data={
            "username": USERNAME,
            "password": PASSWORD,
            "execution": m.group(1),
            "_eventId": "submit",
            "geolocation": "",
            "deviceFingerprint": "",
        },
    )
    if "login.iiit.ac.in" in str(resp.url):
        raise MoodleError(
            "CAS login failed: check IIIT_USERNAME / IIIT_PASSWORD "
            "(username is usually your full IIIT email)."
        )


def _fetch_token() -> str:
    """Obtain a fresh web-service token via CAS + the mobile launch flow."""
    if not (SESSION_COOKIE or (USERNAME and PASSWORD)):
        raise MoodleError(
            "No credentials. Set IIIT_USERNAME and IIIT_PASSWORD (your CAS login), "
            "or MOODLE_SESSION (a logged-in browser cookie), or MOODLE_TOKEN."
        )
    passport = str(secrets.randbelow(10**9))
    with httpx.Client(
        timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": APP_UA}
    ) as client:
        if SESSION_COOKIE:
            client.cookies.set("MoodleSession", SESSION_COOKIE, domain=BASE_URL.split("://", 1)[1])
        else:
            _cas_login(client)

        # Walk the launch redirects by hand: the last hop is a moodlemobile://
        # URL that httpx cannot (and must not) follow.
        url = (
            f"{BASE_URL}/admin/tool/mobile/launch.php?service={SERVICE}"
            f"&passport={passport}&urlscheme=moodlemobile&confirmed=1"
        )
        for _ in range(10):
            resp = client.get(url, follow_redirects=False)
            loc = resp.headers.get("location", "")
            if not loc:
                # Moodle sometimes emits a meta-refresh / JS redirect instead.
                m = re.search(r'(moodlemobile://token=[A-Za-z0-9+/=]+)', resp.text)
                loc = m.group(1) if m else ""
            if loc.startswith("moodlemobile://token="):
                break
            if not loc or resp.status_code not in (301, 302, 303, 307, 308):
                if "login" in str(resp.url) or "/login/" in loc:
                    raise MoodleError("Not logged in to Moodle; MOODLE_SESSION is probably expired.")
                raise MoodleError(f"Mobile launch flow did not return a token (HTTP {resp.status_code}).")
            url = urljoin(str(resp.url), loc)
            if "/login/index.php" in url and not SESSION_COOKIE:
                _cas_login(client)
                url = (
                    f"{BASE_URL}/admin/tool/mobile/launch.php?service={SERVICE}"
                    f"&passport={passport}&urlscheme=moodlemobile&confirmed=1"
                )
        else:
            raise MoodleError("Too many redirects while fetching a Moodle token.")

    decoded = base64.b64decode(loc.split("token=", 1)[1]).decode()
    parts = decoded.split(":::")
    if len(parts) < 2 or not parts[1]:
        raise MoodleError("Malformed token returned by Moodle launch flow.")
    return parts[1]


def _token(refresh: bool = False) -> str:
    if not refresh:
        if _state["token"]:
            return _state["token"]
        cached = _read_cached_token()
        if cached:
            _state["token"] = cached
            return cached
    token = _fetch_token()
    _state["token"] = token
    _write_cached_token(token)
    return token


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #
def _flatten(params: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested args into Moodle's PHP-style form keys (a[0][b]=...)."""
    out: dict[str, Any] = {}
    for key, val in params.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(val, dict):
            out.update(_flatten(val, name))
        elif isinstance(val, (list, tuple)):
            out.update(_flatten(dict(enumerate(val)), name))
        elif isinstance(val, bool):
            out[name] = int(val)
        elif val is not None:
            out[name] = val
    return out


_TOKEN_ERRORS = {"invalidtoken", "accessexception"}


def _call(wsfunction: str, **params: Any) -> Any:
    """Call a Moodle web-service function, re-authenticating once if needed."""
    for attempt in range(2):
        data = {
            "wstoken": _token(refresh=attempt > 0),
            "wsfunction": wsfunction,
            "moodlewsrestformat": "json",
            **_flatten(params),
        }
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                resp = client.post(f"{BASE_URL}/webservice/rest/server.php", data=data)
        except httpx.HTTPError as exc:
            raise MoodleError(f"Could not reach Moodle: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            raise MoodleError(f"Non-JSON response ({resp.status_code}) from {wsfunction}.")
        if isinstance(body, dict) and body.get("exception"):
            code = body.get("errorcode", "")
            # Token expired/revoked: fetch a new one, unless the user pinned one.
            if code in _TOKEN_ERRORS and attempt == 0 and not os.environ.get("MOODLE_TOKEN"):
                continue
            raise MoodleError(f"{code or 'error'}: {body.get('message', body)}")
        return body
    raise MoodleError("Authentication failed after refreshing the token.")


def _userid() -> int:
    if _state["userid"] is None:
        _state["userid"] = _call("core_webservice_get_site_info")["userid"]
    return _state["userid"]


def _ts(value: Any) -> str | None:
    """Unix timestamp -> readable IST datetime (None for 0/empty)."""
    if not value:
        return None
    return datetime.fromtimestamp(int(value), IST).strftime("%Y-%m-%d %H:%M IST")


def _text(raw: Any, limit: int = 4000) -> str:
    """Strip HTML to readable plain text."""
    if not raw:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", str(raw))
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s).strip()
    return s if len(s) <= limit else s[:limit] + " …[truncated]"


def _files(items: list[dict] | None) -> list[dict]:
    return [
        {
            "filename": f.get("filename"),
            "size": f.get("filesize"),
            "mimetype": f.get("mimetype"),
            "fileurl": f.get("fileurl"),
        }
        for f in items or []
        if f.get("fileurl")
    ]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def moodle_whoami() -> dict:
    """Show the logged-in Moodle user and site. Use this to verify auth works."""
    info = _call("core_webservice_get_site_info")
    _state["userid"] = info["userid"]
    return {
        "userid": info["userid"],
        "username": info.get("username"),
        "fullname": info.get("fullname"),
        "site": info.get("sitename"),
        "moodle_release": info.get("release"),
    }


def _course_ids(include_past: bool = False) -> list[int]:
    return [c["id"] for c in list_courses(include_past)]


@mcp.tool()
def list_courses(include_past: bool = False) -> list[dict]:
    """List your courses.

    Args:
        include_past: Include past and future enrolments too (default: only
            courses in progress, as on the Moodle dashboard).

    Returns course id (use it in other tools), short/full name and dates.
    """
    if include_past:
        courses = _call("core_enrol_get_users_courses", userid=_userid())
    else:
        # IIIT courses mostly have no end date, so filter by date is useless;
        # this is what the dashboard's "In progress" tab uses.
        courses = _call(
            "core_course_get_enrolled_courses_by_timeline_classification",
            classification="inprogress",
        ).get("courses", [])
    return [
        {
            "id": c["id"],
            "shortname": c.get("shortname"),
            "fullname": html.unescape(c.get("fullname") or ""),
            "start": _ts(c.get("startdate")),
            "end": _ts(c.get("enddate")),
            "progress": c.get("progress"),
        }
        for c in courses
    ]


@mcp.tool()
def get_course_contents(course_id: int, include_descriptions: bool = False) -> list[dict]:
    """Get a course's sections and activities (resources, assignments, forums,
    quizzes, links...), including downloadable file URLs.

    Args:
        course_id: Course id from list_courses.
        include_descriptions: Include each activity's description text (longer).

    Pass a file's `fileurl` to download_file to fetch/read it.
    """
    sections = _call("core_course_get_contents", courseid=course_id)
    out = []
    for s in sections:
        mods = []
        for m in s.get("modules", []):
            if m.get("uservisible") is False:
                continue
            item: dict[str, Any] = {
                "id": m["id"],
                "instance": m.get("instance"),
                "type": m.get("modname"),
                "name": html.unescape(m.get("name") or ""),
                "url": m.get("url"),
            }
            files = _files(m.get("contents"))
            if m.get("modname") == "url":
                item["link"] = next(
                    (c.get("fileurl") for c in m.get("contents", []) if c.get("type") == "url"), None
                )
            elif files:
                item["files"] = files
            if include_descriptions and m.get("description"):
                item["description"] = _text(m["description"], 1500)
            for d in m.get("dates", []) or []:
                item.setdefault("dates", {})[d.get("label")] = _ts(d.get("timestamp"))
            mods.append(item)
        if not mods and not s.get("summary"):
            continue
        out.append({
            "section": html.unescape(s.get("name") or ""),
            "summary": _text(s.get("summary"), 1500) or None,
            "activities": mods,
        })
    return out


@mcp.tool()
def get_upcoming_deadlines(days: int = 30, limit: int = 50) -> list[dict]:
    """List upcoming deadlines and events (assignment due dates, quiz closes...)
    across all courses, soonest first.

    Args:
        days: How far ahead to look (default 30).
        limit: Max events to return.
    """
    now = int(datetime.now(IST).timestamp())
    data = _call(
        "core_calendar_get_action_events_by_timesort",
        timesortfrom=now - 86400,  # include things due earlier today / overdue-ish
        timesortto=now + days * 86400,
        limitnum=min(limit, 50),
    )
    return [
        {
            "name": e.get("name"),
            "course": html.unescape((e.get("course") or {}).get("fullname") or ""),
            "course_id": (e.get("course") or {}).get("id"),
            "type": e.get("modulename"),
            "instance": e.get("instance"),
            "due": _ts(e.get("timesort")),
            "overdue": bool(e.get("overdue")),
            "action": (e.get("action") or {}).get("name"),
            "actionable": (e.get("action") or {}).get("actionable"),
            "url": e.get("url"),
        }
        for e in data.get("events", [])
    ]


@mcp.tool()
def list_assignments(course_id: int = 0, include_past: bool = False) -> list[dict]:
    """List assignments with their due dates, latest due first.

    Args:
        course_id: Limit to one course (0 = all current courses).
        include_past: With course_id=0, also cover past courses.

    Use the returned `assign_id` with get_assignment_status.
    """
    params = {"courseids": [course_id] if course_id else _course_ids(include_past)}
    data = _call("mod_assign_get_assignments", **params)
    out = []
    for c in data.get("courses", []):
        for a in c.get("assignments", []):
            out.append({
                "assign_id": a["id"],
                "cmid": a.get("cmid"),
                "course": html.unescape(c.get("fullname") or ""),
                "course_id": c.get("id"),
                "name": a.get("name"),
                "opens": _ts(a.get("allowsubmissionsfromdate")),
                "due": _ts(a.get("duedate")),
                "cutoff": _ts(a.get("cutoffdate")),
                "max_grade": a.get("grade"),
            })
    out.sort(key=lambda a: a["due"] or "", reverse=True)
    return out


@mcp.tool()
def get_assignment_status(assign_id: int) -> dict:
    """Get an assignment's full description, attachments, your submission
    status (submitted files/text, timestamps) and grade/feedback if released.

    Args:
        assign_id: `assign_id` from list_assignments (not the course-module id).
    """
    st = _call("mod_assign_get_submission_status", assignid=assign_id)
    out: dict[str, Any] = {}
    intro = st.get("assignmentdata") or {}
    if intro:
        out["description"] = _text(intro.get("intro"))
        out["attachments"] = _files(intro.get("attachments", {}).get("intro"))
    last = st.get("lastattempt") or {}
    sub = last.get("submission") or (last.get("teamsubmission") or {})
    if sub:
        files, texts = [], []
        for p in sub.get("plugins", []):
            for fa in p.get("fileareas", []) or []:
                files += _files(fa.get("files"))
            for ef in p.get("editorfields", []) or []:
                if ef.get("text"):
                    texts.append(_text(ef["text"]))
        out["submission"] = {
            "status": sub.get("status"),
            "modified": _ts(sub.get("timemodified")),
            "grading_status": last.get("gradingstatus"),
            "can_edit": last.get("canedit"),
            "files": files,
            "online_text": texts or None,
        }
    fb = st.get("feedback") or {}
    if fb:
        comments = []
        files = []
        for p in fb.get("plugins", []):
            for ef in p.get("editorfields", []) or []:
                if ef.get("text"):
                    comments.append(_text(ef["text"]))
            for fa in p.get("fileareas", []) or []:
                files += _files(fa.get("files"))
        out["feedback"] = {
            "grade": _text(fb.get("gradefordisplay")),
            "graded_on": _ts(fb.get("gradeddate")),
            "comments": comments or None,
            "files": files or None,
        }
    return out


@mcp.tool()
def get_grades(course_id: int = 0) -> Any:
    """Get grades.

    Args:
        course_id: 0 = overview of your total grade in every course;
            a course id = every grade item in that course with feedback.
    """
    if not course_id:
        data = _call("gradereport_overview_get_course_grades", userid=_userid())
        names = {c["id"]: c["fullname"] for c in list_courses(include_past=True)}
        return [
            {
                "course_id": g["courseid"],
                "course": names.get(g["courseid"]),
                "grade": g.get("grade"),
                "rank": g.get("rank"),
            }
            for g in data.get("grades", [])
        ]
    data = _call("gradereport_user_get_grade_items", courseid=course_id, userid=_userid())
    items = []
    for ug in data.get("usergrades", []):
        for gi in ug.get("gradeitems", []):
            items.append({
                "item": gi.get("itemname") or ("Course total" if gi.get("itemtype") == "course" else gi.get("itemtype")),
                "type": gi.get("itemmodule") or gi.get("itemtype"),
                "grade": gi.get("gradeformatted"),
                "range": f'{gi.get("grademin")}–{gi.get("grademax")}',
                "percentage": gi.get("percentageformatted"),
                "weight": gi.get("weightformatted"),
                "feedback": _text(gi.get("feedback"), 1500) or None,
                "graded_on": _ts(gi.get("gradedategraded")),
            })
    return items


@mcp.tool()
def list_forums(course_id: int) -> list[dict]:
    """List a course's forums (including the Announcements / news forum).

    Use the returned `forum_id` with list_discussions.
    """
    data = _call("mod_forum_get_forums_by_courses", courseids=[course_id])
    return [
        {
            "forum_id": f["id"],
            "cmid": f.get("cmid"),
            "name": html.unescape(f.get("name") or ""),
            "type": f.get("type"),
            "discussions": f.get("numdiscussions"),
            "intro": _text(f.get("intro"), 500) or None,
        }
        for f in data
    ]


@mcp.tool()
def list_discussions(forum_id: int, page: int = 0, per_page: int = 20) -> list[dict]:
    """List discussions in a forum, most recent first.

    Args:
        forum_id: `forum_id` from list_forums.
        page / per_page: Pagination.
    """
    data = _call(
        "mod_forum_get_forum_discussions",
        forumid=forum_id, sortorder=1, page=page, perpage=per_page,
    )
    return [
        {
            "discussion_id": d["discussion"],
            "subject": d.get("subject") or d.get("name"),
            "author": d.get("userfullname"),
            "created": _ts(d.get("created")),
            "last_post": _ts(d.get("timemodified")),
            "replies": d.get("numreplies"),
            "pinned": d.get("pinned"),
            "preview": _text(d.get("message"), 600),
            "attachments": _files(d.get("attachments")) or None,
        }
        for d in data.get("discussions", [])
    ]


@mcp.tool()
def get_discussion(discussion_id: int) -> list[dict]:
    """Read all posts in a forum discussion, in order.

    Args:
        discussion_id: `discussion_id` from list_discussions.
    """
    data = _call(
        "mod_forum_get_discussion_posts",
        discussionid=discussion_id, sortby="created", sortdirection="ASC",
    )
    return [
        {
            "post_id": p["id"],
            "reply_to": p.get("parentid") or None,
            "author": (p.get("author") or {}).get("fullname"),
            "posted": _ts(p.get("timecreated")),
            "subject": p.get("subject"),
            "message": _text(p.get("message")),
            "attachments": _files(p.get("attachments")) or None,
        }
        for p in data.get("posts", [])
    ]


@mcp.tool()
def get_recent_announcements(days: int = 14, limit_per_course: int = 5) -> list[dict]:
    """Recent posts from the Announcements forum of every current course.

    Args:
        days: Only discussions active in the last N days.
        limit_per_course: Max discussions per course.
    """
    courses = list_courses()
    if not courses:
        return []
    forums = _call("mod_forum_get_forums_by_courses", courseids=[c["id"] for c in courses])
    names = {c["id"]: c["fullname"] for c in courses}
    cutoff = datetime.now(IST).timestamp() - days * 86400
    out = []
    for f in forums:
        if f.get("type") != "news":
            continue
        data = _call(
            "mod_forum_get_forum_discussions",
            forumid=f["id"], sortorder=1, page=0, perpage=limit_per_course,
        )
        for d in data.get("discussions", []):
            if (d.get("timemodified") or 0) < cutoff:
                continue
            out.append({
                "course": names.get(f.get("course")),
                "discussion_id": d["discussion"],
                "subject": d.get("subject") or d.get("name"),
                "author": d.get("userfullname"),
                "posted": _ts(d.get("created")),
                "message": _text(d.get("message"), 1500),
                "attachments": _files(d.get("attachments")) or None,
            })
    out.sort(key=lambda d: d["posted"] or "", reverse=True)
    return out


@mcp.tool()
def list_quizzes(course_id: int = 0, include_past: bool = False) -> list[dict]:
    """List quizzes with open/close times and time limits.

    Args:
        course_id: Limit to one course (0 = all current courses).
        include_past: With course_id=0, also cover past courses.
    """
    params = {"courseids": [course_id] if course_id else _course_ids(include_past)}
    data = _call("mod_quiz_get_quizzes_by_courses", **params)
    return [
        {
            "quiz_id": q["id"],
            "cmid": q.get("coursemodule"),
            "course_id": q.get("course"),
            "name": q.get("name"),
            "opens": _ts(q.get("timeopen")),
            "closes": _ts(q.get("timeclose")),
            "time_limit_min": (q.get("timelimit") or 0) // 60 or None,
            "attempts_allowed": q.get("attempts") or "unlimited",
            "max_grade": q.get("grade"),
        }
        for q in data.get("quizzes", [])
    ]


@mcp.tool()
def get_notifications(limit: int = 20, unread_only: bool = False) -> list[dict]:
    """Your recent Moodle notifications (new grades, forum posts, due soon...).

    Args:
        limit: Max notifications.
        unread_only: Only unread ones.
    """
    data = _call(
        "message_popup_get_popup_notifications",
        useridto=_userid(), newestfirst=1, limit=limit, offset=0,
    )
    out = []
    for n in data.get("notifications", []):
        if unread_only and n.get("read"):
            continue
        out.append({
            "subject": n.get("subject"),
            "text": _text(n.get("smallmessage") or n.get("fullmessage"), 800),
            "from": n.get("userfromfullname"),
            "time": _ts(n.get("timecreated")),
            "read": n.get("read"),
            "url": n.get("contexturl"),
        })
    return out


@mcp.tool()
def search_courses(query: str) -> list[dict]:
    """Search the whole Moodle site's course catalogue by name (not just
    your enrolments)."""
    data = _call(
        "core_course_search_courses", criterianame="search", criteriavalue=query, perpage=30
    )
    return [
        {"id": c["id"], "shortname": c.get("shortname"), "fullname": html.unescape(c.get("fullname") or "")}
        for c in data.get("courses", [])
    ]


_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/javascript")


@mcp.tool()
def download_file(fileurl: str, save: bool = True, read: bool = True) -> dict:
    """Download a Moodle file (a `fileurl` from any other tool).

    Args:
        fileurl: A pluginfile.php / webservice/pluginfile.php URL.
        save: Save it under MOODLE_DOWNLOAD_DIR (default ~/Downloads/moodle).
        read: For text/HTML files (e.g. Page activities), return the text
            content too. PDFs and other binaries are only saved.

    Returns the saved path, size, type and (when applicable) text.
    """
    if not fileurl.startswith(BASE_URL):
        raise MoodleError(f"Only {BASE_URL} file URLs are allowed.")
    url = fileurl.replace("/pluginfile.php/", "/webservice/pluginfile.php/", 1) \
        if "/webservice/pluginfile.php/" not in fileurl else fileurl

    def fetch(token: str) -> httpx.Response:
        try:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
                return client.get(url, params={"token": token})
        except httpx.HTTPError as exc:
            raise MoodleError(f"Download failed: {exc}") from exc

    resp = fetch(_token())
    ctype = resp.headers.get("content-type", "").split(";")[0]
    if ctype == "application/json":  # Moodle errors come back as JSON
        try:
            err = resp.json()
        except ValueError:
            err = {}
        if err.get("errorcode") in _TOKEN_ERRORS:
            resp = fetch(_token(refresh=True))
            ctype = resp.headers.get("content-type", "").split(";")[0]
        elif err.get("error") or err.get("exception"):
            raise MoodleError(f"{err.get('errorcode')}: {err.get('error') or err.get('message')}")
    if not resp.is_success:
        raise MoodleError(f"Download failed (HTTP {resp.status_code}).")

    name = re.sub(r"[^\w.\- ()]+", "_", httpx.URL(url).path.rsplit("/", 1)[-1]) or "file"
    if "." not in name:
        name += mimetypes.guess_extension(ctype) or ""
    out: dict[str, Any] = {"filename": name, "size": len(resp.content), "mimetype": ctype}

    if save:
        # Group by the context id in the URL so same-named files don't clash.
        m = re.search(r"pluginfile\.php/(\d+)/", url)
        dest = DOWNLOAD_DIR / (m.group(1) if m else "misc") / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        out["saved_to"] = str(dest)

    if read and ctype.startswith(_TEXT_TYPES):
        text = resp.content.decode(resp.encoding or "utf-8", errors="replace")
        out["text"] = _text(text, 20000) if "html" in ctype else text[:20000]
    return out


if __name__ == "__main__":
    mcp.run()
