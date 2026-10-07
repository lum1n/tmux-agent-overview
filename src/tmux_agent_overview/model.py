import math
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

KINDS = ("copilot", "claude", "codex", "pi", "opencode", "cursor")
STATES = ("unknown", "idle", "thinking", "running-tool", "waiting-permission", "errored")
COLORS = {"unknown": "colour244", "idle": "colour114", "thinking": "colour220",
          "running-tool": "colour81", "waiting-permission": "colour203", "errored": "colour203"}
ESCAPE = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|[P_^][\s\S]*?\x1b\\|\[[0-?]*[ -/]*[@-~]|.)")


def clean(text):
    text = ESCAPE.sub("", text)
    return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127 and unicodedata.category(c) not in ("Cc", "Cf")))


def clip(text, width):
    result, cells = [], 0
    for char in clean(text).replace("\t", "    "):
        size = 0 if unicodedata.combining(char) or char == "\ufe0f" else (2 if unicodedata.east_asian_width(char) in ("W", "F") else 1)
        if cells + size > width:
            break
        if char == "\n":
            break
        result.append(char)
        cells += size
    return "".join(result)


def card_margin(width, height):
    if width < 3 or height < 3:
        return 0, 0
    return min(2, max(0, (width - 3) // 2)), min(1, max(0, (height - 3) // 2))


def card_content_size(width, height):
    horizontal, vertical = card_margin(width, height)
    card_width, card_height = width - 2 * horizontal, height - 2 * vertical
    border = int(card_width >= 3 and card_height >= 3)
    return card_width - 2 * border, card_height - 2 * border


def card_frame(rows, width, height, heading="", selected=False, state="unknown", footer=""):
    horizontal, vertical = card_margin(width, height)
    content_width, content_height = card_content_size(width, height)
    bordered = width - 2 * horizontal >= 3 and height - 2 * vertical >= 3
    border = int(bordered)
    surface = "\x1b[38;5;252;48;5;235m"
    outline = f"\x1b[38;5;{81 if selected else 238};48;5;234m"
    frame = "\x1b[?25l\x1b[0m\x1b[38;5;252;48;5;234m\x1b[H\x1b[2J"
    if bordered:
        frame += f"\x1b[{vertical + 1};{horizontal + 1}H{outline}╭{'─' * content_width}╮"
        title = clip(" " + heading + " ", content_width) if heading else ""
        color = int(COLORS[state].removeprefix("colour"))
        frame += f"\x1b[{vertical + 1};{horizontal + 2}H\x1b[38;5;{color}m{title}"
    for index in range(content_height):
        y, x = vertical + border + index + 1, horizontal + border + 1
        if bordered:
            frame += f"\x1b[{y};{horizontal + 1}H{outline}│"
            frame += f"\x1b[{y};{horizontal + content_width + 2}H│"
        cursor = f"\x1b[{y};{x}H"
        frame += cursor + surface + " " * content_width
        if index < len(rows):
            frame += cursor + clip(rows[index], content_width)
    if bordered:
        frame += f"\x1b[{height - vertical};{horizontal + 1}H{outline}╰{'─' * content_width}╯"
        if footer:
            frame += f"\x1b[{height - vertical};{horizontal + 2}H\x1b[38;5;250m{clip(' ' + footer + ' ', content_width)}"
    return frame + "\x1b[H"


QUOTA_WARN, QUOTA_FULL = 80, 100
QUOTA_COLORS = {"ok": "colour252", "warn": "colour220", "full": "colour203"}


def parse_quota(event, used_key, resets_key):
    """(kind, (plan, windows, stale)) from a watcher or Hive reading, else None.

    Quota is optional everywhere, so a malformed reading is dropped, never fatal.
    """
    kind, windows, plan = event.get("kind"), event.get("windows"), event.get("plan", "")
    if kind not in KINDS or not isinstance(plan, str) or not isinstance(windows, list) or not 0 < len(windows) <= 8:
        return None
    parsed = []
    for window in windows:
        if not isinstance(window, dict):
            return None
        name, used, resets = window.get("label"), window.get(used_key), window.get(resets_key)
        if not isinstance(name, str) or type(used) not in (int, float) or not 0 <= used <= 100:
            return None
        reset = None
        if isinstance(resets, str) and len(resets) <= 64:
            try:
                stamp = datetime.fromisoformat(resets.replace("Z", "+00:00"))
                reset = stamp.timestamp() if stamp.tzinfo else None
            except (ValueError, OverflowError):
                reset = None
        parsed.append((clean(name)[:16], float(used), reset))
    return kind, (clean(plan)[:24], tuple(parsed), event.get("stale") is True)


def reset_hint(reset, now):
    if reset is None:
        return ""
    minutes = max(0, int(reset - now)) // 60
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h" if hours < 48 else f"{hours // 24}d"


def quota_summary(kind, quota, now, limit=3, show_plan=True):
    """Compact meter like Sessh's quota line: `claude Max 5x · 5h 33% · 7d full · 2h`."""
    plan, windows, stale = quota
    parts, tone = [kind] + ([plan] if plan and show_plan else []), "ok"
    for name, used, reset in windows[:limit]:
        if used >= QUOTA_FULL:
            tone = "full"
            hint = reset_hint(reset, now)
            parts.append(f"{name} full" + (f" · {hint}" if hint else ""))
        else:
            if used >= QUOTA_WARN and tone == "ok":
                tone = "warn"
            parts.append(f"{name} {used:.0f}%")
    if stale:
        parts.append("stale")
    return " · ".join(parts), tone


# Room for the window list tmux draws between status-left and status-right.
STATUS_RESERVE = 12


def fit_status(lefts, readings, width, now):
    """(status-left, [(text, tone)]) that fit `width`; tmux would cut status-right.

    `lefts` are status-left variants, longest first. Quota keeps priority: left
    text shrinks first, then meters compact, then meters drop (counted as +N).
    """
    room = max(0, width - STATUS_RESERVE)
    def meters(compact):
        return [(host + text, tone) for (host, kind, q) in readings
                for text, tone in [quota_summary(kind, q, now, *((1, False) if compact else ()))]]
    def size(left, items):
        return len(left) + sum(len(text) + 2 for text, _ in items)
    full, compact = meters(False), meters(True)
    for left, items in ([(left, full) for left in lefts] + [(lefts[-1], compact)]):
        if size(left, items) <= room:
            return left, items
    items = compact
    while items and size(lefts[-1], items) + 4 > room:
        items = items[:-1]
    if len(items) < len(readings):
        items = items + [(f"+{len(readings) - len(items)}", "ok")]
    return lefts[-1], items


def quota_limit(quota, now):
    """Reset hint when a quota window is exhausted, otherwise None."""
    full = [reset for _, used, reset in quota[1] if used >= QUOTA_FULL and (reset is None or reset > now)]
    if not full:
        return None
    known = [reset for reset in full if reset is not None]
    return reset_hint(max(known), now) if known else ""


def label(text):
    # Literal # characters must not become tmux format or style expressions.
    return clean(text).replace("\n", " ").replace("#", "_")[:160]


@dataclass
class Agent:
    pane: str
    window: str
    session: str
    session_name: str
    index: str
    command: str
    pid: int
    active: bool
    path: str
    kind: str
    state: str = "unknown"
    provenance: str = "local"
    host: str = ""
    host_label: str = ""
    hive_id: str = ""
    socket: str = ""
    generation: str = ""
    state_error: str = ""
    state_age: float = 0
    # Hive only: the agent's server watcher quota for this kind.
    quota: tuple | None = None
    window_name: str = ""

    @property
    def key(self):
        if not self.hive_id:
            return self.pane
        identity = json.dumps([self.host, self.socket, self.generation, self.pane])
        return "hive:" + hashlib.sha256(identity.encode()).hexdigest()

    @property
    def location(self):
        # Hive agents carry the window name as their index.
        name = f" {self.window_name}" if self.window_name and self.window_name != self.index else ""
        return f"{self.session_name}:{self.index}{name} | {self.pane} | {self.path.rsplit('/', 1)[-1]}"

    @property
    def title(self):
        host = f"{self.host_label} ({self.host})/" if self.hive_id else ""
        return f"{self.kind} | {host}{self.location}"


def capacity(width, height):
    return max(1, min(12, max(1, (width + 1) // 41) * max(1, (height + 1) // 11)))


def native_layout(panes, width, height):
    columns = min(len(panes), max(1, (width + 1) // 41))
    rows = math.ceil(len(panes) / columns)
    row_parts = []
    y = 0
    for row in range(rows):
        group = panes[row * columns:(row + 1) * columns]
        rh = (height - rows + 1) // rows + (row < (height - rows + 1) % rows)
        parts, x = [], 0
        for col, pane in enumerate(group):
            cw = (width - len(group) + 1) // len(group) + (col < (width - len(group) + 1) % len(group))
            parts.append(f"{cw}x{rh},{x},{y},{pane.lstrip('%')}")
            x += cw + 1
        row_parts.append(parts[0] if len(parts) == 1 else f"{width}x{rh},0,{y}" + "{" + ",".join(parts) + "}")
        y += rh + 1
    body = row_parts[0] if rows == 1 else f"{width}x{height},0,0[" + ",".join(row_parts) + "]"
    checksum = 0
    for char in body:
        checksum = ((checksum >> 1) | ((checksum & 1) << 15))
        checksum = (checksum + ord(char)) & 65535
    return f"{checksum:04x},{body}"


def classify(kind, text):
    lines = [line.strip() for line in clean(text).splitlines() if line.strip()]
    tail = "\n".join(lines[-4:]).lower()
    if lines and re.match(r"^(?:❯|>|›)\s*$", lines[-1]) and not re.search(r"esc to (?:interrupt|cancel)", tail):
        return "idle"
    if re.search(r"(?:do you (?:want to|trust)|allow (?:once|always|this)|permission required|\[y/n\]|approve this)", tail):
        return "waiting-permission"
    if re.search(r"(?:^|\n)(?:error:|fatal:|authentication failed|rate limit exceeded)", tail):
        return "errored"
    if re.search(r"(?:esc to (?:interrupt|cancel)|thinking(?:\.|…)|working(?:\.|…)|generating(?:\.|…))", tail):
        return "thinking"
    if re.search(r"(?:running tool|executing tool|running command)", tail):
        return "running-tool"
    prompts = {"copilot": r"^(?:❯|>|›)\s*$", "claude": r"^(?:❯|>)\s*$",
               "codex": r"^(?:›|>)\s*$", "pi": r"^(?:>|❯)\s*$",
               "opencode": r"^(?:>|❯)\s*$", "cursor": r"^(?:>|❯)\s*$"}
    if lines and re.match(prompts[kind], lines[-1]):
        return "idle"
    return "unknown"
