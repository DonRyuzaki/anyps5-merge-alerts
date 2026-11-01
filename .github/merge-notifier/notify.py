import base64
from datetime import datetime, timedelta, timezone
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

UPSTREAM = "boykopovar/AnyPS5"
AUTHOR = "DonRyuzaki"
OWNER = "DonRyuzaki/anyps5-merge-alerts"
STATE_BRANCH = "automation/anyps5-merge-notifications-state"
STATE_PATH = "state.json"
MAX_BODY = 8 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


open_url = build_opener(NoRedirect()).open


class NotificationError(Exception):
    pass


class DeliveryRejected(NotificationError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp(value):
    if not isinstance(value, str):
        raise NotificationError("Invalid notification timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise NotificationError("Invalid notification timestamp") from None
    if result.tzinfo is None:
        raise NotificationError("Notification timestamps must include a timezone")
    return result


def json_body(response):
    raw = response.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise NotificationError("Response exceeds the notification size limit")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise NotificationError("Invalid JSON response") from None


class GitHub:
    def __init__(self, token, opener=open_url, sleeper=time.sleep):
        if not token:
            raise NotificationError("GitHub token is missing")
        self.token, self.opener, self.sleep = token, opener, sleeper

    def request(self, method, path, payload=None):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "AnyPS5-merge-notifier"}
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                request = Request("https://api.github.com/" + path, data=data, headers=headers, method=method)
                with self.opener(request, timeout=30) as response:
                    return json_body(response)
            except HTTPError as error:
                if method == "GET" and error.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                    self.sleep(2 ** attempt)
                    continue
                raise NotificationError(f"GitHub {method} failed with HTTP {error.code}") from None
            except (URLError, TimeoutError, OSError):
                if method == "GET" and attempt + 1 < attempts:
                    self.sleep(2 ** attempt)
                    continue
                raise NotificationError(f"GitHub {method} could not be confirmed") from None

    def candidates(self):
        numbers = set()
        page = 1
        while True:
            query = urlencode({"q": f"repo:{UPSTREAM} is:pr author:{AUTHOR} is:merged",
                               "per_page": 100, "page": page, "sort": "created", "order": "asc"})
            data = self.request("GET", "search/issues?" + query)
            if not isinstance(data, dict) or data.get("incomplete_results") is not False:
                raise NotificationError("GitHub merge search is incomplete")
            total = data.get("total_count")
            items = data.get("items")
            if type(total) is not int or not 0 <= total <= 1000 or not isinstance(items, list):
                raise NotificationError("Unsupported GitHub merge search result")
            for item in items:
                number = item.get("number") if isinstance(item, dict) else None
                if type(number) is not int or number <= 0 or "pull_request" not in item:
                    raise NotificationError("Invalid pull request search item")
                numbers.add(number)
            if len(items) < 100:
                break
            page += 1
            if page > 10:
                if total == len(numbers):
                    break
                raise NotificationError("GitHub merge search exceeds its pagination limit")
        if len(numbers) != total:
            raise NotificationError("GitHub merge search changed during pagination; retry later")
        return sorted(numbers)

    def pull(self, number):
        return self.request("GET", f"repos/{UPSTREAM}/pulls/{number}")


def validate_state(state):
    if (not isinstance(state, dict) or state.get("version") != 1 or state.get("upstream") != UPSTREAM
            or state.get("author") != AUTHOR or not isinstance(state.get("delivered"), dict)
            or "pending" not in state):
        raise NotificationError("Notification state is invalid; refusing to reset delivery history")
    timestamp(state.get("heartbeat_at"))
    pending = state["pending"]
    if pending is not None and (not isinstance(pending, dict) or type(pending.get("number")) is not int
                                or pending["number"] <= 0):
        raise NotificationError("Pending delivery state is invalid")
    for number, entry in state["delivered"].items():
        if not re.fullmatch(r"[1-9][0-9]*", number) or not isinstance(entry, dict):
            raise NotificationError("Delivered notification state is invalid")
        timestamp(entry.get("merged_at"))
        if not isinstance(entry.get("message_id"), str) or not entry["message_id"].isdigit():
            raise NotificationError("Delivered Discord message identity is invalid")
    return state


class StateStore:
    def __init__(self, github):
        self.github = github
        self.path = f"repos/{OWNER}/contents/{STATE_PATH}"

    def load(self):
        data = self.github.request("GET", self.path + "?" + urlencode({"ref": STATE_BRANCH}))
        if not isinstance(data, dict) or data.get("encoding") != "base64" or not isinstance(data.get("sha"), str):
            raise NotificationError("Notification state file is missing or malformed")
        try:
            state = json.loads(base64.b64decode(data["content"], validate=False))
        except (KeyError, ValueError, RecursionError):
            raise NotificationError("Notification state file cannot be decoded") from None
        return validate_state(state), data["sha"]

    def save(self, state, sha):
        validate_state(state)
        body = json.dumps(state, sort_keys=True, indent=2) + "\n"
        payload = {"branch": STATE_BRANCH, "sha": sha, "message": "chore: persist merged PR notification state",
                   "content": base64.b64encode(body.encode("utf-8")).decode("ascii")}
        for attempt in range(3):
            try:
                result = self.github.request("PUT", self.path, payload)
                return result["content"]["sha"]
            except (NotificationError, KeyError, TypeError):
                try:
                    remote, remote_sha = self.load()
                except NotificationError:
                    remote = None
                if remote == state:
                    return remote_sha
                if remote is not None and remote_sha != sha:
                    raise NotificationError("Notification state changed concurrently; refusing to overwrite it") from None
                if attempt < 2:
                    self.github.sleep(2 ** attempt)
        raise NotificationError("Notification state could not be persisted; delivery stopped")


class Discord:
    def __init__(self, webhook, opener=open_url, sleeper=time.sleep):
        try:
            url = urlsplit(webhook)
            valid = (url.scheme == "https" and url.hostname in ("discord.com", "discordapp.com")
                     and url.port in (None, 443) and not url.username and not url.password and not url.fragment
                     and not url.query and re.fullmatch(r"/api(?:/v[0-9]+)?/webhooks/[0-9]+/[A-Za-z0-9_-]+", url.path))
        except ValueError:
            valid = False
        if not valid:
            raise NotificationError("Discord webhook secret is missing or invalid")
        self.url = urlunsplit(("https", url.netloc, url.path, "wait=true", ""))
        self.opener, self.sleep = opener, sleeper

    def send(self, pr):
        number = pr["number"]
        title = pr.get("title") if isinstance(pr.get("title"), str) else "Pull request"
        payload = {"allowed_mentions": {"parse": []}, "embeds": [{"title": f"Merged #{number}: {title}"[:256],
                   "url": f"https://github.com/{UPSTREAM}/pull/{number}", "color": 0x8957E5,
                   "description": f"Your pull request was merged into **{UPSTREAM}**.",
                   "timestamp": pr["merged_at"], "footer": {"text": "AnyPS5 · merged pull requests only"}}]}
        for attempt in range(3):
            try:
                request = Request(self.url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                  headers={"Content-Type": "application/json", "User-Agent": "AnyPS5-merge-notifier"})
                with self.opener(request, timeout=30) as response:
                    result = json_body(response)
                message_id = result.get("id") if isinstance(result, dict) else None
                if not isinstance(message_id, str) or not message_id.isdigit():
                    raise NotificationError("Discord message creation could not be confirmed")
                return message_id
            except HTTPError as error:
                if error.code == 429 and attempt < 2:
                    try:
                        result = json_body(error)
                        delay = result.get("retry_after", 1) if isinstance(result, dict) else 1
                    except NotificationError:
                        delay = 1
                    if type(delay) not in (int, float) or not 0 <= delay <= 30:
                        raise DeliveryRejected("Discord rate limit exceeds this run's retry budget") from None
                    self.sleep(delay)
                    continue
                if 400 <= error.code < 500:
                    raise DeliveryRejected(f"Discord rejected delivery with HTTP {error.code}") from None
                raise NotificationError("Discord delivery is uncertain; inspect the channel before retrying") from None
            except (URLError, TimeoutError, OSError):
                raise NotificationError("Discord delivery is uncertain; inspect the channel before retrying") from None


def is_merge(pr, number):
    if (not isinstance(pr, dict) or pr.get("number") != number or pr.get("merged") is not True
            or pr.get("state") != "closed" or pr.get("user", {}).get("login", "").lower() != AUTHOR.lower()
            or pr.get("base", {}).get("repo", {}).get("full_name", "").lower() != UPSTREAM.lower()
            or not pr.get("merged_at")):
        return False
    timestamp(pr["merged_at"])
    return True


def run(github, store, discord, dry_run=False):
    state, sha = store.load()
    if state["pending"] is not None:
        raise NotificationError(f"PR #{state['pending']['number']} has an uncertain delivery; reconcile it before continuing")
    numbers = github.candidates()
    sent = 0
    for number in numbers:
        if str(number) in state["delivered"]:
            continue
        pr = github.pull(number)
        if not is_merge(pr, number):
            continue
        if dry_run:
            print(f"Confirmed merged PR #{number}; dry run sends nothing")
            continue
        state["pending"] = {"number": number, "merged_at": pr["merged_at"], "prepared_at": now()}
        sha = store.save(state, sha)
        try:
            message_id = discord.send(pr)
        except DeliveryRejected:
            state["pending"] = None
            store.save(state, sha)
            raise
        state["delivered"][str(number)] = {"merged_at": pr["merged_at"], "message_id": message_id}
        state["pending"] = None
        state["heartbeat_at"] = now()
        sha = store.save(state, sha)
        sent += 1
        print(f"Delivered merged PR #{number}")
    if not dry_run and datetime.now(timezone.utc) - timestamp(state["heartbeat_at"]) >= timedelta(days=7):
        state["heartbeat_at"] = now()
        store.save(state, sha)
    print(f"Checked {len(numbers)} merged PRs; sent {sent} notifications")
    return sent


def main():
    try:
        if os.environ.get("GITHUB_REPOSITORY") != OWNER:
            raise NotificationError("This notifier runs only in its configured owner fork")
        github = GitHub(os.environ.get("GH_TOKEN", ""))
        discord = Discord(os.environ.get("DISCORD_WEBHOOK_URL", ""))
        run(github, StateStore(github), discord, "--dry-run" in sys.argv[1:])
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, NotificationError) else "unexpected " + type(error).__name__
        print("Notifier stopped: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
