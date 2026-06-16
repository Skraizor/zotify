from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import sleep
from typing import Any, Callable
from uuid import uuid4


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


class JobRejected(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass


class SettingsLocked(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _image_url(item: dict[str, Any], *fallbacks: dict[str, Any] | None) -> str | None:
    for candidate in (item, *fallbacks):
        if not candidate:
            continue
        images = candidate.get("images") or []
        if images:
            return images[0].get("url")
    return None


def _names(items: list[dict[str, Any]] | None) -> str:
    return ", ".join(item.get("name", "") for item in items or [] if item.get("name"))


def _followers(item: dict[str, Any]) -> str:
    total = item.get("followers", {}).get("total")
    if total is None:
        return ""
    return f"{total:,} followers".replace(",", "")


def flatten_search_results(payload: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Convert Spotify search payloads into UI-ready result rows."""
    results: list[dict[str, Any]] = []
    specs = (
        ("tracks", "track", lambda item: _names(item.get("artists")), lambda item: _image_url(item, item.get("album"))),
        ("albums", "album", lambda item: _names(item.get("artists")), _image_url),
        ("artists", "artist", _followers, _image_url),
        ("playlists", "playlist", lambda item: item.get("owner", {}).get("display_name", ""), _image_url),
        ("episodes", "episode", lambda item: item.get("show", {}).get("name", ""), lambda item: _image_url(item, item.get("show"))),
        ("shows", "show", lambda item: item.get("publisher", ""), _image_url),
    )
    for plural, singular, subtitle_getter, image_getter in specs:
        for item in payload.get(plural, []) or []:
            metadata: dict[str, Any] = {}
            if item.get("explicit") is not None:
                metadata["explicit"] = item.get("explicit")
            results.append({
                "type": singular,
                "name": item.get("name", ""),
                "subtitle": subtitle_getter(item),
                "uri": item.get("uri", ""),
                "image_url": image_getter(item),
                "metadata": metadata,
            })
    return results


def flatten_library_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload: dict[str, list[dict[str, Any]]] = {
        "tracks": [],
        "albums": [],
        "artists": [],
        "playlists": [],
    }
    for item in items:
        if not item:
            continue
        if item.get("track"):
            payload["tracks"].append(item["track"])
        elif item.get("album"):
            payload["albums"].append(item["album"])
        elif item.get("type") == "artist" or item.get("followers"):
            payload["artists"].append(item)
        else:
            payload["playlists"].append(item)
    return flatten_search_results(payload)


@dataclass
class WebSettings:
    values: dict[str, Any] = field(default_factory=lambda: {
        "config_location": "",
        "root_path": "",
        "download_format": "copy",
        "download_quality": "auto",
        "skip_existing": True,
        "lyrics_to_file": True,
        "lyrics_to_metadata": True,
    })
    locked: bool = False

    def update(self, updates: dict[str, Any]) -> dict[str, Any]:
        if self.locked:
            raise SettingsLocked("Settings are locked after the Zotify session starts")
        for key, value in updates.items():
            if key in self.values:
                self.values[key] = value
        return self.values

    def lock(self) -> None:
        self.locked = True

    def to_args(self, base_args: Namespace) -> Namespace:
        args = Namespace(**vars(base_args))
        args.config_location = self.values["config_location"] or getattr(args, "config_location", None)
        args.root_path = self.values["root_path"] or getattr(args, "root_path", None)
        args.download_format = self.values["download_format"]
        args.download_quality = self.values["download_quality"]
        args.skip_existing = self.values["skip_existing"]
        args.lyrics_to_file = self.values["lyrics_to_file"]
        args.lyrics_to_metadata = self.values["lyrics_to_metadata"]
        return args


@dataclass
class Job:
    id: str
    name: str
    payload: dict[str, Any]
    status: str = "running"
    started_at: str = field(default_factory=_utc_now)
    ended_at: str | None = None
    result: Any = None
    error: BaseException | None = None
    thread: Thread | None = None
    cancel_event: Event = field(default_factory=Event)
    _events: Queue = field(default_factory=Queue)

    def push_event(self, event_type: str, message: str, **extra: Any) -> None:
        self._events.put({"type": event_type, "message": message, "time": _utc_now(), **extra})

    def drain_events(self) -> list[dict[str, Any]]:
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except Empty:
                return events

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "payload": self.payload,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": str(self.error) if self.error else None,
        }


class JobContext:
    def __init__(self, job: Job):
        self.job = job

    @property
    def cancel_requested(self) -> bool:
        return self.job.cancel_event.is_set()

    def log(self, message: str) -> None:
        self.job.push_event("log", message)

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            raise JobCancelled("Job cancelled")


class JobManager:
    def __init__(self):
        self._lock = Lock()
        self.current_job: Job | None = None

    def _active_job(self) -> Job | None:
        if self.current_job and self.current_job.status not in TERMINAL_STATUSES:
            return self.current_job
        return None

    def start(self, name: str, payload: dict[str, Any], handler: Callable[[JobContext], Any]) -> Job:
        with self._lock:
            if self._active_job():
                raise JobRejected("Another job is already running")
            job = Job(id=str(uuid4()), name=name, payload=payload)
            self.current_job = job

            def run() -> None:
                ctx = JobContext(job)
                try:
                    job.push_event("status", f"Started {name}")
                    job.result = handler(ctx)
                    job.status = "cancelled" if ctx.cancel_requested else "succeeded"
                except JobCancelled as exc:
                    job.status = "cancelled"
                    job.error = exc
                except BaseException as exc:
                    job.status = "failed"
                    job.error = exc
                    job.push_event("error", str(exc))
                finally:
                    job.ended_at = _utc_now()
                    job.push_event("status", job.status)

            job.thread = Thread(target=run, daemon=True)
            job.thread.start()
            return job

    def cancel_current(self) -> bool:
        with self._lock:
            job = self._active_job()
            if not job:
                return False
            job.cancel_event.set()
            job.status = "cancel_requested"
            job.push_event("status", "Cancel requested")
            return True

    def status(self) -> dict[str, Any]:
        job = self.current_job
        return {"active": bool(self._active_job()), "job": job.as_dict() if job else None}


class _PrinterSink:
    def __init__(self, ctx: JobContext):
        self.ctx = ctx

    def __call__(self, message: str, *_args: Any) -> None:
        if message:
            self.ctx.log(str(message).rstrip())


def _base_web_args(args: Namespace | None = None) -> Namespace:
    from zotify.__main__ import build_parser
    parsed = build_parser().parse_args([])
    if args:
        for key, value in vars(args).items():
            setattr(parsed, key, value)
    return parsed


def _run_with_printer_sink(ctx: JobContext, fn: Callable[[], Any]) -> Any:
    from zotify.termoutput import Printer
    sink = _PrinterSink(ctx)
    Printer.add_sink(sink)
    try:
        return fn()
    finally:
        Printer.remove_sink(sink)


def _ensure_booted(state: dict[str, Any], settings: WebSettings, base_args: Namespace, ctx: JobContext | None = None) -> None:
    if state["booted"]:
        return
    settings.lock()

    def boot() -> None:
        from zotify.config import Zotify
        Zotify.boot(settings.to_args(base_args))
        state["booted"] = True

    if ctx:
        _run_with_printer_sink(ctx, boot)
    else:
        boot()


def _download_uris(uris: list[str]) -> None:
    from zotify.api import Query
    from zotify.config import Zotify
    Query(Zotify.DATETIME_LAUNCH).request(" ".join(uris)).execute()


def _run_library_mode(kind: str) -> None:
    from zotify.api import FollowedArtist, LikedSong, SavedAlbum, UserPlaylist, VerifyLibrary
    from zotify.config import Zotify

    modes = {
        "liked": LikedSong,
        "playlists": UserPlaylist,
        "artists": FollowedArtist,
        "albums": SavedAlbum,
        "verify": VerifyLibrary,
    }
    if kind not in modes:
        raise ValueError(f"Unknown job kind: {kind}")
    obj = modes[kind](Zotify.DATETIME_LAUNCH)
    if hasattr(obj, "_interactive"):
        obj._interactive = False
    obj.execute()


def _fetch_library_items(kind: str) -> list[dict[str, Any]]:
    from zotify.api import FollowedArtist, LikedSong, SavedAlbum, UserPlaylist
    from zotify.config import Zotify

    modes = {
        "liked": LikedSong,
        "playlists": UserPlaylist,
        "artists": FollowedArtist,
        "albums": SavedAlbum,
    }
    if kind not in modes:
        raise ValueError(f"Unknown library kind: {kind}")
    obj = modes[kind](Zotify.DATETIME_LAUNCH)
    return flatten_library_items(obj.fetch_user_items())


def create_app(args: Namespace | None = None):
    try:
        from flask import Flask, Response, jsonify, request
    except ModuleNotFoundError as exc:
        raise RuntimeError("Flask is required for `zotify --web`. Install Zotify with web dependencies available.") from exc

    app = Flask(__name__)
    base_args = _base_web_args(args)
    settings = WebSettings()
    manager = JobManager()
    state = {"booted": False}
    boot_lock = Lock()

    @app.get("/")
    def index():
        return HTML_PAGE

    @app.get("/api/status")
    def status():
        return jsonify({"booted": state["booted"], "settings": settings.values, "settings_locked": settings.locked, **manager.status()})

    @app.post("/api/session/settings")
    def update_settings():
        try:
            return jsonify({"settings": settings.update(request.get_json(silent=True) or {})})
        except SettingsLocked as exc:
            return jsonify({"error": str(exc)}), 409

    @app.get("/api/search")
    def search():
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify({"results": []})
        with boot_lock:
            _ensure_booted(state, settings, base_args)
        from zotify.app import fetch_search_results
        return jsonify({"results": fetch_search_results(query, display=False)})

    @app.get("/api/library/<kind>")
    def library(kind: str):
        with boot_lock:
            _ensure_booted(state, settings, base_args)
        try:
            return jsonify({"results": _fetch_library_items(kind)})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 404

    @app.post("/api/jobs")
    def start_job():
        payload = request.get_json(silent=True) or {}
        kind = payload.get("kind", "urls")

        def handler(ctx: JobContext):
            with boot_lock:
                _ensure_booted(state, settings, base_args, ctx)
            from zotify.config import Zotify
            Zotify.CANCEL_REQUESTED = lambda: ctx.cancel_requested
            try:
                return _run_with_printer_sink(ctx, lambda: _handle_job_payload(kind, payload))
            finally:
                Zotify.CANCEL_REQUESTED = None

        try:
            job = manager.start(kind, payload, handler)
        except JobRejected as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"job": job.as_dict()}), 202

    @app.post("/api/jobs/current/cancel")
    def cancel_job():
        if not manager.cancel_current():
            return jsonify({"cancelled": False, "error": "No active job"}), 404
        return jsonify({"cancelled": True})

    @app.get("/api/jobs/current/events")
    def events():
        def stream():
            last_status = None
            while True:
                job = manager.current_job
                if not job:
                    yield "event: status\ndata: idle\n\n"
                    sleep(1)
                    continue
                if job.status != last_status:
                    last_status = job.status
                    yield f"event: status\ndata: {job.status}\n\n"
                for event in job.drain_events():
                    yield f"event: {event['type']}\ndata: {event['message']}\n\n"
                if job.status in TERMINAL_STATUSES:
                    break
                sleep(0.5)

        return Response(stream(), mimetype="text/event-stream")

    return app


def _handle_job_payload(kind: str, payload: dict[str, Any]) -> None:
    if kind in {"urls", "search"}:
        uris = payload.get("uris") or []
        if isinstance(uris, str):
            uris = [uris]
        urls = payload.get("urls")
        if urls:
            uris.extend(str(urls).split())
        if not uris:
            raise ValueError("No URLs or URIs supplied")
        _download_uris(uris)
        return
    _run_library_mode(kind)


def run_web(args: Namespace | None = None) -> None:
    app = create_app(args)
    host = getattr(args, "web_host", "127.0.0.1") if args else "127.0.0.1"
    port = getattr(args, "web_port", 4382) if args else 4382
    app.run(host=host, port=port, threaded=True)


HTML_PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Zotify</title>
  <style>
    :root { color-scheme: light; --bg: #f7f7f4; --panel: #ffffff; --ink: #1f2933; --muted: #65737e; --line: #d9ded8; --accent: #176b5f; --danger: #a63a3a; }
    * { box-sizing: border-box; }
    body { margin: 0; font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }
    header { display: flex; align-items: center; justify-content: space-between; padding: 14px 22px; border-bottom: 1px solid var(--line); background: #fbfbf8; }
    h1 { margin: 0; font-size: 20px; font-weight: 700; }
    main { display: grid; grid-template-columns: minmax(340px, 420px) minmax(0, 1fr); min-height: calc(100vh - 58px); }
    aside { border-right: 1px solid var(--line); padding: 18px; overflow: auto; }
    section { padding: 18px 22px; }
    h2 { font-size: 14px; margin: 0 0 10px; text-transform: uppercase; letter-spacing: .04em; color: #41505a; }
    label { display: grid; gap: 5px; margin: 0 0 10px; color: #394852; }
    input, textarea, select { width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 9px 10px; font: inherit; background: #fff; color: var(--ink); }
    textarea { min-height: 100px; resize: vertical; }
    button { border: 1px solid #0f5d52; border-radius: 6px; padding: 9px 12px; background: var(--accent); color: #fff; font-weight: 650; cursor: pointer; }
    button.secondary { background: #fff; color: var(--accent); }
    button.danger { border-color: var(--danger); background: var(--danger); }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .group { padding: 14px 0 18px; border-bottom: 1px solid var(--line); }
    .row { display: flex; gap: 8px; align-items: center; }
    .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .modes { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .result-list { display: grid; gap: 8px; margin-top: 12px; }
    .result { display: grid; grid-template-columns: 44px minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 9px; border: 1px solid var(--line); border-radius: 7px; background: var(--panel); }
    .thumb { width: 44px; height: 44px; border-radius: 5px; background: #e3e6e1; object-fit: cover; }
    .name { font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .sub { color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .status { display: flex; gap: 10px; align-items: center; color: var(--muted); }
    .dot { width: 9px; height: 9px; border-radius: 99px; background: #98a5ad; }
    .dot.running, .dot.cancel_requested { background: #c08223; }
    .dot.succeeded { background: #197a4f; }
    .dot.failed, .dot.cancelled { background: var(--danger); }
    pre { margin: 14px 0 0; padding: 14px; min-height: 380px; max-height: 65vh; overflow: auto; border: 1px solid var(--line); border-radius: 7px; background: #172027; color: #e8ede9; white-space: pre-wrap; }
    @media (max-width: 820px) { main { grid-template-columns: 1fr; } aside { border-right: 0; border-bottom: 1px solid var(--line); } }
  </style>
</head>
<body>
  <header>
    <h1>Zotify</h1>
    <div class="status"><span id="statusDot" class="dot"></span><span id="statusText">Idle</span></div>
  </header>
  <main>
    <aside>
      <div class="group">
        <h2>Settings</h2>
        <label>Config location <input id="config_location"></label>
        <label>Root output path <input id="root_path"></label>
        <div class="grid2">
          <label>Format <select id="download_format"><option>copy</option><option>mp3</option><option>ogg</option><option>opus</option><option>vorbis</option><option>aac</option><option>fdk_aac</option></select></label>
          <label>Quality <select id="download_quality"><option>auto</option><option>normal</option><option>high</option><option>very_high</option><option>lossless</option></select></label>
        </div>
        <label><span><input id="skip_existing" type="checkbox"> Skip existing</span></label>
        <label><span><input id="lyrics_to_file" type="checkbox"> Lyrics file</span></label>
        <label><span><input id="lyrics_to_metadata" type="checkbox"> Lyrics metadata</span></label>
        <button class="secondary" id="saveSettings">Save settings</button>
      </div>
      <div class="group">
        <h2>Download URLs</h2>
        <textarea id="urls" placeholder="Paste Spotify URLs or URIs"></textarea>
        <button id="downloadUrls">Start download</button>
      </div>
      <div class="group">
        <h2>Library</h2>
        <div class="modes">
          <button data-library="liked">Liked Songs</button>
          <button data-library="playlists">Playlists</button>
          <button data-library="artists">Artists</button>
          <button data-library="albums">Albums</button>
          <button data-mode="verify">Verify Library</button>
        </div>
      </div>
    </aside>
    <section>
      <div class="group">
        <h2>Search</h2>
        <div class="row">
          <input id="search" placeholder="Search tracks, albums, artists, playlists">
          <button id="searchButton">Search</button>
        </div>
        <div id="results" class="result-list"></div>
      </div>
      <div class="group">
        <div class="row">
          <h2 style="margin-right:auto">Current Job</h2>
          <button class="danger" id="cancel">Cancel</button>
        </div>
        <pre id="log"></pre>
      </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let eventSource;

    function appendLog(line) {
      const log = $("log");
      log.textContent += line + "\\n";
      log.scrollTop = log.scrollHeight;
    }

    function setStatus(status) {
      $("statusText").textContent = status || "idle";
      $("statusDot").className = "dot " + (status || "");
    }

    async function refreshStatus() {
      const resp = await fetch("/api/status");
      const data = await resp.json();
      if (data.job) setStatus(data.job.status);
      Object.entries(data.settings || {}).forEach(([key, value]) => {
        const el = $(key);
        if (!el) return;
        if (el.type === "checkbox") el.checked = Boolean(value);
        else el.value = value || "";
        el.disabled = data.settings_locked;
      });
    }

    function watchEvents() {
      if (eventSource) eventSource.close();
      if ("EventSource" in window) {
        eventSource = new EventSource("/api/jobs/current/events");
        eventSource.addEventListener("status", (event) => setStatus(event.data));
        eventSource.addEventListener("log", (event) => appendLog(event.data));
        eventSource.addEventListener("error", (event) => appendLog("ERROR: " + event.data));
        return;
      }
      fetchEventStream();
    }

    async function fetchEventStream() {
      const response = await fetch("/api/jobs/current/events");
      if (!response.body) {
        refreshStatus();
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        const chunks = buffer.split("\\n\\n");
        buffer = chunks.pop();
        chunks.forEach((chunk) => {
          const type = (chunk.match(/^event: (.*)$/m) || [null, "log"])[1];
          const data = (chunk.match(/^data: (.*)$/m) || [null, ""])[1];
          if (type === "status") setStatus(data);
          else if (type === "error") appendLog("ERROR: " + data);
          else if (data) appendLog(data);
        });
      }
    }

    async function saveSettings() {
      const payload = {
        config_location: $("config_location").value,
        root_path: $("root_path").value,
        download_format: $("download_format").value,
        download_quality: $("download_quality").value,
        skip_existing: $("skip_existing").checked,
        lyrics_to_file: $("lyrics_to_file").checked,
        lyrics_to_metadata: $("lyrics_to_metadata").checked,
      };
      const resp = await fetch("/api/session/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      if (!resp.ok) appendLog((await resp.json()).error);
      await refreshStatus();
    }

    async function startJob(payload) {
      $("log").textContent = "";
      const resp = await fetch("/api/jobs", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      const data = await resp.json();
      if (!resp.ok) { appendLog(data.error); return; }
      watchEvents();
      setStatus(data.job.status);
    }

    async function doSearch() {
      const q = $("search").value.trim();
      if (!q) return;
      const resp = await fetch("/api/search?q=" + encodeURIComponent(q));
      const data = await resp.json();
      $("results").textContent = "";
      data.results.forEach((item) => {
        $("results").appendChild(resultRow(item, false));
      });
    }

    function resultRow(item, selectable) {
      const row = document.createElement("div");
      row.className = "result";
      row.innerHTML = `<img class="thumb" alt="" src="${item.image_url || ""}"><div><div class="name"></div><div class="sub"></div></div><button>Download</button>`;
      row.querySelector(".name").textContent = item.name;
      row.querySelector(".sub").textContent = item.type + " - " + (item.subtitle || "");
      row.querySelector("button").addEventListener("click", () => startJob({kind: "search", uris: [item.uri]}));
      if (selectable) {
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = item.uri;
        checkbox.style.width = "18px";
        row.style.gridTemplateColumns = "18px 44px minmax(0, 1fr) auto";
        row.insertBefore(checkbox, row.firstChild);
      }
      return row;
    }

    async function loadLibrary(kind) {
      $("results").textContent = "";
      appendLog("Loading " + kind + "...");
      const resp = await fetch("/api/library/" + encodeURIComponent(kind));
      const data = await resp.json();
      if (!resp.ok) { appendLog(data.error); return; }
      const action = document.createElement("div");
      action.className = "row";
      action.innerHTML = `<button>Download selected</button><button class="secondary">Download all</button>`;
      action.querySelector("button").addEventListener("click", () => {
        const uris = Array.from(document.querySelectorAll("#results input[type=checkbox]:checked")).map((input) => input.value);
        if (uris.length) startJob({kind: "search", uris});
      });
      action.querySelector(".secondary").addEventListener("click", () => startJob({kind}));
      $("results").appendChild(action);
      data.results.forEach((item) => $("results").appendChild(resultRow(item, true)));
    }

    $("saveSettings").addEventListener("click", saveSettings);
    $("downloadUrls").addEventListener("click", () => startJob({kind: "urls", urls: $("urls").value}));
    $("searchButton").addEventListener("click", doSearch);
    $("search").addEventListener("keydown", (event) => { if (event.key === "Enter") doSearch(); });
    $("cancel").addEventListener("click", () => fetch("/api/jobs/current/cancel", {method: "POST"}));
    document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => startJob({kind: button.dataset.mode})));
    document.querySelectorAll("[data-library]").forEach((button) => button.addEventListener("click", () => loadLibrary(button.dataset.library)));
    refreshStatus();
  </script>
</body>
</html>
"""
