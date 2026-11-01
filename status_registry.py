# Created and maintained by Ashish Sharma (Stay Elite).
# Copyright (c) 2026 Ashish Sharma (Stay Elite)
# Licensed under the MIT License. See the LICENSE file for details.

"""
StayPresent - Bot Status Registry

Скопировано из StayPresent (MIT License, Ashish Sharma / Stay Elite)
https://github.com/StayElite/StayPresent

Патч: _web_route_service_labels() возвращает [] — не зависим от staypresent.web.
"""

import collections
import copy
import datetime as _datetime
import os
import threading
import time

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python < 3.9
    try:
        from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # type: ignore
    except ImportError:
        ZoneInfo = None
        ZoneInfoNotFoundError = LookupError

_lock = threading.Lock()
_bots = {}  # index -> live state dict, see reset() below

_MAX_INCIDENTS_PER_BOT = 200
_MAX_INCIDENTS_DISPLAYED = 20
_MAX_INCIDENTS_HISTORY = 200
_MAX_LOG_LINES_PER_BOT = 300
_LOG_LINES_IN_SNAPSHOT = 50
_MAX_LOG_LINE_CHARS = 2000

_STATE_RANK = {"operational": 0, "degraded": 1, "offline": 2}

_UPTIME_WINDOWS = {
    "uptime_24h": 24 * 60 * 60,
    "uptime_7d": 7 * 24 * 60 * 60,
    "uptime_30d": 30 * 24 * 60 * 60,
}

_MAX_DOWN_PERIOD_AGE = max(_UPTIME_WINDOWS.values())

WEB_SERVER_FILE_KEY = "web_server"

_global_service_overrides = {}

_pending_single_target_override = None


def _validate_optional_str(value, param_name: str, caller: str) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{caller}: '{param_name}' must be a str or None, got {type(value).__name__}.")


def update_service_override(key: str, name: str = None, description: str = None, caller: str = "staypresent") -> None:
    if not isinstance(key, str) or not key:
        raise TypeError(f"{caller}: service override key must be a non-empty str, got {key!r}.")
    _validate_optional_str(name, "services_name", caller)
    _validate_optional_str(description, "services_description", caller)
    if name is None and description is None:
        return
    global _global_service_overrides
    with _lock:
        entry = dict(_global_service_overrides.get(key, {}))
        if name is not None:
            entry["name"] = name
        if description is not None:
            entry["description"] = description
        _global_service_overrides = {**_global_service_overrides, key: entry}


def set_pending_single_target_override(name: str = None, description: str = None, caller: str = "staypresent") -> None:
    _validate_optional_str(name, "services_name", caller)
    _validate_optional_str(description, "services_description", caller)
    global _pending_single_target_override
    if name is None and description is None:
        _pending_single_target_override = None
        return
    _pending_single_target_override = {"name": name, "description": description, "caller": caller}


def resolve_pending_single_target_override(bot_configs: list) -> None:
    global _pending_single_target_override
    pending = _pending_single_target_override
    if pending is None:
        return
    _pending_single_target_override = None
    if len(bot_configs) > 1:
        raise TypeError(
            f"{pending['caller']}: 'services_name'/'services_description' given as a plain "
            "str only work when there's exactly one bot (or none) running in this process - "
            "with multiple bots, tag each one individually instead, e.g. "
            "bots=[{'file': 'a.py', 'services_name': 'Bot A'}, {'file': 'b.py', "
            "'services_name': 'Bot B'}]."
        )
    if len(bot_configs) == 1:
        cfg = bot_configs[0]
        key = cfg.get("file") or cfg.get("module")
    else:
        key = WEB_SERVER_FILE_KEY
    update_service_override(key, pending["name"], pending["description"], caller=pending["caller"])


_WEB_ROUTE_TYPE_LABELS = {
    "json": "JSON", "text": "Text", "html": "HTML",
    "markdown": "Markdown", "status": "Status",
}


def _label_for_web_route(path: str, entry: dict) -> str:
    if path and path != "/":
        label = path.strip("/").replace("_", " ").replace("-", " ").strip()
        if label:
            return label.title()

    title = entry.get("title")
    if title:
        return title

    if entry.get("type") in ("html", "markdown"):
        value = entry.get("value")
        if value:
            stem = os.path.splitext(os.path.basename(value))[0]
            if stem:
                return stem.replace("_", " ").replace("-", " ").title()

    return _WEB_ROUTE_TYPE_LABELS.get(entry.get("type"), "Web Server")


def _web_route_service_labels() -> list:
    """
    ПАТЧ: в оригинале StayPresent эта функция читает реестр маршрутов из
    staypresent.web и возвращает по строке на каждый status=True-роут.
    В нашем проекте статус-страница одна (на "/"), веб-сервер как
    отдельный сервис не трекается, поэтому всегда пустой список.
    """
    return []


def _new_bot_state(now: float, file_key, is_module: bool, display_name, status_visible: bool = True) -> dict:
    return {
        "file_key": file_key,
        "is_module": is_module,
        "display_name": display_name,
        "status_visible": bool(status_visible),
        "state": "operational",
        "first_started_at": now,
        "down_periods": [],
        "down_before_cutoff": 0.0,
        "restart_count": 0,
        "last_exit_code": None,
        "permanently_failed": False,
        "incidents": [],
        "log": collections.deque(maxlen=_MAX_LOG_LINES_PER_BOT),
    }


def reset(
    bot_configs: list,
    display_names: list = None,
    track_web_server: bool = False,
    bot_status: list = None,
) -> None:
    now = time.time()
    with _lock:
        _bots.clear()
        for i, cfg in enumerate(bot_configs):
            file_val = cfg.get("file")
            module_val = cfg.get("module")
            visible = True
            if bot_status is not None and i < len(bot_status) and bot_status[i] is not None:
                visible = bool(bot_status[i])
            _bots[i] = _new_bot_state(
                now,
                file_val or module_val,
                file_val is None and module_val is not None,
                display_names[i] if display_names else None,
                status_visible=visible,
            )
        if track_web_server:
            web_index = len(bot_configs)
            _bots[web_index] = _new_bot_state(now, WEB_SERVER_FILE_KEY, False, "Web Server")
    resolve_pending_single_target_override(bot_configs)


def _is_down(bot: dict) -> bool:
    return bool(bot["down_periods"]) and bot["down_periods"][-1][1] is None


def _trim_down_periods(bot: dict, now: float) -> None:
    cutoff = now - _MAX_DOWN_PERIOD_AGE
    periods = bot["down_periods"]
    kept = []
    changed = False
    for period in periods:
        start, end = period
        if end is not None and end < cutoff:
            bot["down_before_cutoff"] = bot.get("down_before_cutoff", 0.0) + (end - start)
            changed = True
            continue
        kept.append(period)
    if changed:
        bot["down_periods"] = kept


def _open_down_period(bot: dict, now: float) -> None:
    if not _is_down(bot):
        bot["down_periods"].append([now, None])
    _trim_down_periods(bot, now)


def _close_down_period(bot: dict, now: float) -> None:
    if _is_down(bot):
        bot["down_periods"][-1][1] = now
    _trim_down_periods(bot, now)


def _down_seconds_in_window(down_periods: list, now: float, window_start: float) -> float:
    total = 0.0
    for start, end in down_periods:
        period_end = now if end is None else end
        overlap_start = max(start, window_start)
        overlap_end = min(period_end, now)
        if overlap_end > overlap_start:
            total += overlap_end - overlap_start
    return total


def _uptime_pct(bot: dict, now: float, window_start: float, extra_down: float = 0.0) -> float:
    elapsed = now - window_start
    if elapsed <= 0:
        return 100.0
    down = _down_seconds_in_window(bot["down_periods"], now, window_start) + extra_down
    return round(max(0.0, min(100.0, (elapsed - down) / elapsed * 100)), 2)


def _add_incident(bot: dict, event: str, status: str, exit_code=None, log_line=None) -> None:
    bot["incidents"].insert(0, {
        "event": event, "status": status, "exit_code": exit_code,
        "log_line": log_line, "time": time.time(),
    })
    del bot["incidents"][_MAX_INCIDENTS_PER_BOT:]


def _last_nonempty_log_line(bot: dict):
    for entry in reversed(bot["log"]):
        if entry["line"].strip():
            return entry["line"]
    return None


def append_log(index: int, stream: str, line: str) -> None:
    if len(line) > _MAX_LOG_LINE_CHARS:
        line = line[:_MAX_LOG_LINE_CHARS] + f"... [truncated, {len(line)} chars total]"
    with _lock:
        bot = _bots.get(index)
        if bot is None:
            return
        bot["log"].append({"stream": stream, "line": line, "time": time.time()})


def mark_started(index: int) -> None:
    now = time.time()
    with _lock:
        bot = _bots.get(index)
        if bot is None:
            return
        _close_down_period(bot, now)
        bot["state"] = "operational"


def mark_recovered(index: int) -> None:
    with _lock:
        bot = _bots.get(index)
        if bot is None:
            return
        if bot["permanently_failed"]:
            return
        if not bot["incidents"]:
            return
        if bot["incidents"][0]["event"] == "recovered":
            return
        _add_incident(bot, event="recovered", status="resolved")


def mark_unresponsive(index: int) -> None:
    now = time.time()
    with _lock:
        bot = _bots.get(index)
        if bot is None:
            return
        bot["state"] = "degraded"
        _open_down_period(bot, now)
        _add_incident(
            bot, event="unresponsive", status="investigating",
            log_line=_last_nonempty_log_line(bot),
        )


def mark_crashed(index: int, exit_code: int, will_restart: bool) -> None:
    now = time.time()
    with _lock:
        bot = _bots.get(index)
        if bot is None:
            return
        bot["state"] = "degraded" if will_restart else "offline"
        _open_down_period(bot, now)
        bot["last_exit_code"] = exit_code
        if will_restart:
            bot["restart_count"] += 1
        _add_incident(
            bot,
            event="crash_restarting" if will_restart else "crash_no_restart",
            status="investigating" if will_restart else "identified",
            exit_code=exit_code,
            log_line=_last_nonempty_log_line(bot),
        )


def mark_permanently_failed(index: int, exit_code) -> None:
    now = time.time()
    with _lock:
        bot = _bots.get(index)
        if bot is None:
            return
        bot["state"] = "offline"
        bot["permanently_failed"] = True
        _open_down_period(bot, now)
        bot["last_exit_code"] = exit_code
        _add_incident(
            bot, event="permanent_failure", status="identified",
            exit_code=exit_code, log_line=_last_nonempty_log_line(bot),
        )


def mark_web_server_down(index: int, detail: str = None, failed_to_start: bool = False) -> None:
    now = time.time()
    with _lock:
        bot = _bots.get(index)
        if bot is None:
            return
        bot["state"] = "offline"
        bot["permanently_failed"] = True
        _open_down_period(bot, now)
        event = "web_server_failed_to_start" if failed_to_start else "web_server_down"
        _add_incident(bot, event=event, status="identified", log_line=detail)


def mark_clean_exit(index: int) -> None:
    now = time.time()
    with _lock:
        bot = _bots.get(index)
        if bot is None:
            return
        bot["state"] = "offline"
        _open_down_period(bot, now)
        bot["last_exit_code"] = 0


def _display_name(file_key, override: dict, fallback_display_name: str = None, is_module: bool = False) -> str:
    name = override.get("name") if override else None
    if name:
        return name

    if fallback_display_name is not None:
        if is_module:
            return f"{fallback_display_name} - Worker"

        stem = os.path.splitext(os.path.basename(fallback_display_name))[0] or fallback_display_name
        parent = os.path.basename(os.path.dirname(file_key)) if file_key else ""
        return f"{parent}/{stem} - Worker" if parent else f"{stem} - Worker"

    return file_key


_TIMEZONE_ALIASES = {
    "UTC": "UTC",
    "GMT": "UTC",
    "IST": "Asia/Kolkata",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "BST": "Europe/London",
    "CET": "Europe/Paris",
    "JST": "Asia/Tokyo",
    "AEST": "Australia/Sydney",
}


def resolve_timezone(name: str, caller: str = "staypresent") -> str:
    if not isinstance(name, str) or not name.strip():
        raise TypeError(f"{caller}: 'timezone' must be a non-empty str, got {name!r}.")
    raw = name.strip()

    if raw.upper() == "AUTO":
        return "auto"

    candidate = _TIMEZONE_ALIASES.get(raw.upper(), raw)

    if candidate.upper() == "UTC":
        return "UTC"

    if ZoneInfo is None:
        raise ValueError(
            f"{caller}: timezone={name!r} requires the 'zoneinfo' module, which isn't "
            "available on this Python install (zoneinfo is stdlib from Python 3.9 onward). "
            "Either upgrade to Python 3.9+, install the 'backports.zoneinfo' package "
            "(plus, on Windows, 'tzdata'), or leave 'timezone' unset (it defaults to "
            "\"auto\", which - along with \"UTC\" - never needs zoneinfo at all)."
        )
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"{caller}: timezone={name!r} is not a recognized timezone. Use \"auto\" (the "
            "default - follows each visitor's own browser timezone), a standard IANA zone "
            "key (e.g. \"Asia/Kolkata\", \"America/New_York\", \"Europe/London\" - see "
            "https://en.wikipedia.org/wiki/List_of_tz_database_time_zones), or one of these "
            f"short aliases: {', '.join(sorted(_TIMEZONE_ALIASES))}. If this already looks like "
            "a valid IANA key, your system may be missing timezone data - try "
            "`pip install tzdata`."
        ) from exc
    return candidate


_TIME_FORMAT_ALIASES = {
    "12H": "12h", "12HR": "12h", "12HRS": "12h", "12": "12h", "12-HOUR": "12h",
    "24H": "24h", "24HR": "24h", "24HRS": "24h", "24": "24h", "24-HOUR": "24h",
}


def resolve_time_format(value: str, caller: str = "staypresent") -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{caller}: 'time_format' must be a non-empty str, got {value!r}.")
    resolved = _TIME_FORMAT_ALIASES.get(value.strip().upper())
    if resolved is None:
        raise ValueError(
            f"{caller}: time_format={value!r} is not recognized - use \"12h\" (the default) "
            "or \"24h\"."
        )
    return resolved


def _tzinfo(tz_name: str):
    if not tz_name or tz_name in ("UTC", "auto"):
        return _datetime.timezone.utc
    return ZoneInfo(tz_name)


def current_year(tz_name: str = "UTC") -> str:
    return _datetime.datetime.now(_tzinfo(tz_name)).strftime("%Y")


def _format_time(ts: float, tz_name: str = "UTC", time_format: str = "12h") -> str:
    dt = _datetime.datetime.fromtimestamp(ts, tz=_tzinfo(tz_name))
    clock = dt.strftime("%H:%M") if time_format == "24h" else dt.strftime("%I:%M %p")
    return dt.strftime("%b %d, %Y at") + f" {clock} {dt.tzname() or tz_name}"


def _format_log_line(entry: dict, tz_name: str = "UTC", time_format: str = "12h") -> str:
    dt = _datetime.datetime.fromtimestamp(entry["time"], tz=_tzinfo(tz_name))
    stamp = dt.strftime("%H:%M:%S") if time_format == "24h" else dt.strftime("%I:%M:%S %p")
    if entry["stream"] == "stderr":
        return f"{stamp} [stderr] {entry['line']}"
    return f"{stamp} {entry['line']}"


_PUBLIC_INCIDENT_TITLES = {
    "crash_restarting": "Service disruption detected - automatically recovering",
    "crash_no_restart": "Service disruption detected",
    "permanent_failure": "Service is currently unavailable",
    "unresponsive": "Service is responding slowly or not responding - investigating",
    "recovered": "Service has recovered and is operating normally",
    "web_server_down": "Service is currently unavailable",
    "web_server_failed_to_start": "Service is currently unavailable",
}


def _incident_title(inc: dict, admin: bool) -> str:
    if not admin:
        return _PUBLIC_INCIDENT_TITLES.get(inc["event"], "Service disruption detected")
    if inc["event"] == "crash_restarting":
        return f"Crashed with exit code {inc['exit_code']}, restarting automatically"
    if inc["event"] == "crash_no_restart":
        return f"Crashed with exit code {inc['exit_code']}"
    if inc["event"] == "permanent_failure":
        return f"Stopped restarting after repeated crashes (exit code {inc['exit_code']})"
    if inc["event"] == "unresponsive":
        return "No heartbeat received in time - treating as hung, terminating and restarting"
    if inc["event"] == "recovered":
        return "Recovered - no further crashes since, incident closed"
    if inc["event"] == "web_server_down":
        return "Web server thread stopped unexpectedly (not auto-restarted)"
    if inc["event"] == "web_server_failed_to_start":
        return "Web server failed to start"
    return "Service disruption detected"


def snapshot(
    admin: bool = False, incident_limit: int = None, timezone: str = "UTC", time_format: str = "12h",
) -> dict:
    with _lock:
        services_override = copy.deepcopy(_global_service_overrides)

    if incident_limit is None:
        incident_limit = _MAX_INCIDENTS_DISPLAYED
    incident_limit = max(0, min(incident_limit, _MAX_INCIDENTS_HISTORY))

    now = time.time()
    with _lock:
        bots_copy = {}
        for i, bot in _bots.items():
            entry = {k: v for k, v in bot.items() if k not in ("log", "down_periods", "incidents")}
            entry["down_periods"] = [[start, end] for start, end in bot["down_periods"]]
            entry["incidents"] = list(bot["incidents"])
            if admin:
                entry["log"] = list(collections.deque(bot["log"], maxlen=_LOG_LINES_IN_SNAPSHOT))
            bots_copy[i] = entry

    matched_keys = set()
    services = []
    all_incidents = []
    overall = "operational"

    for i in sorted(bots_copy):
        bot = bots_copy[i]
        file_key = bot["file_key"]

        if file_key != WEB_SERVER_FILE_KEY and not bot.get("status_visible", True):
            continue

        override = services_override.get(file_key) or {}
        if file_key in services_override:
            matched_keys.add(file_key)

        uptime_pct = _uptime_pct(
            bot, now, bot["first_started_at"],
            extra_down=bot.get("down_before_cutoff", 0.0),
        )
        window_uptimes = {}
        for label, window_seconds in _UPTIME_WINDOWS.items():
            window_start = max(bot["first_started_at"], now - window_seconds)
            window_uptimes[label] = _uptime_pct(bot, now, window_start)

        def _build_service_entry(name: str, description: str) -> dict:
            entry = {
                "name": name,
                "description": description or "",
                "status": bot["state"],
                "uptime": uptime_pct,
                "restarts": bot["restart_count"],
            }
            entry.update(window_uptimes)
            return entry

        if file_key == WEB_SERVER_FILE_KEY and not override.get("name"):
            route_labels = _web_route_service_labels()
            if not route_labels:
                continue
            first_new_index = len(services)
            for name, description in route_labels:
                services.append(
                    _build_service_entry(name, description or override.get("description") or "")
                )
            representative_name = ", ".join(name for name, _ in route_labels)
            new_indices = range(first_new_index, len(services))
        else:
            name = _display_name(file_key, override, bot.get("display_name"), bot.get("is_module", False))
            services.append(_build_service_entry(name, override.get("description")))
            representative_name = name
            new_indices = range(len(services) - 1, len(services))

        if _STATE_RANK[bot["state"]] > _STATE_RANK[overall]:
            overall = bot["state"]

        if admin:
            formatted_log = [
                {
                    "time": entry["time"],
                    "stream": entry["stream"],
                    "line": entry["line"],
                    "display": _format_log_line(entry, timezone, time_format),
                }
                for entry in bot["log"]
            ]
            for idx in new_indices:
                services[idx]["log"] = formatted_log

        for inc in bot["incidents"]:
            entry = {
                "title": _incident_title(inc, admin),
                "status": inc["status"],
                "service": representative_name,
                "time": inc["time"],
            }
            if admin and inc.get("log_line"):
                entry["log_line"] = inc["log_line"]
            all_incidents.append(entry)

    for file_key, override in services_override.items():
        if file_key in matched_keys:
            continue

        status = "operational"
        services.append({
            "name": _display_name(file_key, override),
            "description": (override or {}).get("description") or "",
            "status": status,
            "uptime": None,
            "restarts": None,
        })

    all_incidents.sort(key=lambda inc: inc["time"], reverse=True)
    total_incidents = len(all_incidents)
    all_incidents = all_incidents[:incident_limit]
    for inc in all_incidents:
        inc["time_display"] = _format_time(inc["time"], timezone, time_format)

    return {
        "overall_status": overall,
        "services": services,
        "incidents": all_incidents,
        "total_incidents": total_incidents,
        "generated_at": now,
        "admin": admin,
        "timezone": timezone,
        "time_format": time_format,
    }
