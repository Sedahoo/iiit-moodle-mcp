# IIIT Moodle MCP

An [MCP](https://modelcontextprotocol.io) server for **IIIT Hyderabad Moodle**
([courses.iiit.ac.in](https://courses.iiit.ac.in/)). It lets Claude read your
courses, course materials, deadlines, assignments, grades, forums,
announcements and notifications, and download course files.

It only reads data. Nothing it does submits or changes anything on Moodle.

## Tools

| Tool | What it does |
|------|--------------|
| `moodle_whoami` | Your Moodle profile. Use it to check that auth works |
| `list_courses` | Your courses in progress (as on the dashboard); `include_past` for all |
| `get_course_contents` | Sections and activities of a course, with file URLs |
| `get_upcoming_deadlines` | Upcoming due dates and events across all courses |
| `list_assignments` | Assignments in current courses, latest due first |
| `get_assignment_status` | Assignment description, your submission, grade and feedback |
| `get_grades` | Course totals overview, or every grade item in one course |
| `list_forums` / `list_discussions` / `get_discussion` | Browse and read forums |
| `get_recent_announcements` | Recent Announcements posts from all current courses |
| `list_quizzes` | Quizzes with open and close times |
| `get_notifications` | Your Moodle notifications |
| `search_courses` | Search the site-wide course catalogue |
| `download_file` | Save a course file locally; returns the text of text and HTML files |

## Setup

```bash
uv sync          # or: pip install -e .
```

### Credentials

The site only accepts CAS sign-in (login.iiit.ac.in), so the server signs in
through CAS. It then runs Moodle's mobile-app launch flow to get a
web-service token. The token is cached in `~/.cache/iiit-moodle-mcp/token.json`
(mode 600) and is refreshed automatically when it expires.

Copy `.env.example` to `.env` and fill it in:

```env
IIIT_USERNAME=firstname.lastname@students.iiit.ac.in
IIIT_PASSWORD=your-cas-password
```

There are two other ways to authenticate:

- `MOODLE_TOKEN=...`: a ready-made web-service token. No login happens.
- `MOODLE_SESSION=...`: the `MoodleSession` cookie from a logged-in browser.
  The server swaps it for a token once.

### Configure Claude Code / Claude Desktop

```bash
claude mcp add iiit-moodle -- uv --directory /home/sedahoo/fun/mcps/moody run iiit-moodle-mcp
```

or in JSON:

```json
{
  "mcpServers": {
    "iiit-moodle": {
      "command": "uv",
      "args": ["--directory", "/home/sedahoo/fun/mcps/moody", "run", "iiit-moodle-mcp"]
    }
  }
}
```

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `IIIT_USERNAME` / `IIIT_PASSWORD` | *(empty)* | Your CAS login |
| `MOODLE_TOKEN` | *(empty)* | A web-service token to use as-is |
| `MOODLE_SESSION` | *(empty)* | A browser `MoodleSession` cookie |
| `MOODLE_DOWNLOAD_DIR` | `~/Downloads/moodle` | Where `download_file` saves files |
| `MOODLE_TOKEN_CACHE` | `~/.cache/iiit-moodle-mcp/token.json` | Token cache path |
| `MOODLE_BASE_URL` | `https://courses.iiit.ac.in` | Moodle host |

## Notes

- Times are shown in IST.
- This is an unofficial client. It uses the same web-service API as the
  official Moodle mobile app.
