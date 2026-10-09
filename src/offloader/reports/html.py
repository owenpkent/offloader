"""Self-contained HTML job report.

Thumbnails are inlined as data URIs so the file can be emailed or dropped in a
delivery folder without dragging an assets directory along.
"""

from __future__ import annotations

import base64
import html
import mimetypes
from pathlib import Path

from .. import PRODUCT_NAME, __version__
from ..models import FileEntry, Job
from ..util import (
    channel_layout_name,
    format_duration,
    format_elapsed,
    format_file_datetime,
    format_fps,
    format_job_datetime,
    format_size,
)

_STYLE = """
:root {
  --bg: #ffffff; --fg: #2b2b2b; --muted: #7f7f7f; --label: #4c4c4c;
  --band: #eef1f7; --rule: #d6d6d6; --ok: #1f7a3d; --bad: #b3261e;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #16181c; --fg: #e6e6e6; --muted: #9aa0a6; --label: #c8ccd1;
    --band: #1e2229; --rule: #33383f; --ok: #5cc98a; --bad: #ef6a60;
  }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; background: var(--bg); color: var(--fg);
       font: 13px/1.5 Verdana, "DejaVu Sans", system-ui, sans-serif; }
h1 { font-size: 26px; color: var(--muted); margin: 0 0 12px; }
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
           gap: 4px 24px; border-bottom: 2px solid var(--rule); padding-bottom: 14px; }
.summary div { font-size: 12px; }
.summary b { color: var(--label); }
.clip { display: flex; gap: 14px; padding: 8px 10px; align-items: flex-start; }
.clip:nth-child(even) { background: var(--band); }
.clip .meta { flex: 0 0 240px; font-size: 10px; color: var(--muted); }
.clip .meta .name { font-size: 12px; font-weight: bold; color: var(--label);
                    margin-bottom: 4px; word-break: break-all; }
.clip .meta b { color: var(--fg); font-weight: bold; }
.strip { display: flex; flex: 1 1 auto; gap: 0; min-width: 0; }
.strip img { width: 25%; height: auto; object-fit: contain; background: #000; }
.proxy-note, .companion-note { font-size: 10px; color: var(--muted);
              font-style: italic; margin-top: 3px; }
.noimg { flex: 1 1 auto; min-height: 78px; background: #1c1c1c; border-radius: 4px;
         color: #f0a92b; display: flex; align-items: center; justify-content: center;
         font-size: 12px; letter-spacing: 2px; }
h2 { font-size: 22px; color: var(--muted); font-weight: normal; margin: 34px 0 6px; }
.paths { font-size: 12px; margin-left: 6px; }
.paths b { color: var(--label); }
table { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 10px; }
th { text-align: left; color: var(--label); border-bottom: 1px solid var(--rule);
     padding: 4px 6px; white-space: nowrap; }
td { padding: 3px 6px; color: var(--muted); vertical-align: top; word-break: break-all; }
tbody tr:nth-child(odd) { background: var(--band); }
td.name { color: var(--label); font-weight: bold; white-space: nowrap; }
.status-Verified { color: var(--ok); font-weight: bold; }
.status-Failed { color: var(--bad); font-weight: bold; }
footer { margin-top: 28px; border-top: 1px solid var(--rule); padding-top: 8px;
         font-size: 10px; color: var(--muted); }
.wrap { overflow-x: auto; }
"""


def _data_uri(path: Path) -> str | None:
    try:
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    except OSError:
        return None


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _clip_meta(job: Job, entry: FileEntry) -> str:
    media = entry.media
    rows: list[str] = [f'<div class="name">{_esc(entry.name)}</div>']
    if entry.checksum:
        rows.append(f"<div><b>{_esc(job.hash_label)} Checksum:</b> {_esc(entry.checksum)}</div>")
    rows.append(
        f"<div><b>Size:</b> {_esc(format_size(entry.size))} &nbsp; "
        f"<b>Created:</b> {_esc(format_file_datetime(entry.created))}</div>"
    )
    bits = [b for b in (
        media.container,
        f"{media.width} x {media.height}" if media.is_video else None,
        media.video_codec,
        format_fps(media.fps) if media.fps else None,
    ) if b]
    if bits:
        rows.append("<div><b>" + "</b> &nbsp; <b>".join(_esc(b) for b in bits) + "</b></div>")

    timing = []
    if media.duration_sec:
        timing.append(f"<b>Duration:</b> {_esc(format_duration(media.duration_sec))}")
    if media.timecode:
        timing.append(f"<b>TC:</b> {_esc(media.timecode)}")
    if media.frame_count:
        timing.append(f"<b>Frames:</b> {media.frame_count}")
    if timing:
        rows.append("<div>" + " &nbsp; ".join(timing) + "</div>")

    camera = media.camera
    if camera and camera.summary():
        rows.append("<div><b>" + _esc(camera.model or "") + "</b> &nbsp; "
                    + _esc(camera.lens or "") + "</div>")
    if camera and camera.slate():
        extras = " &nbsp; ".join(
            _esc(x) for x in (camera.colour_science,
                              f"Cam {camera.camera_number}" if camera.camera_number
                              else None) if x)
        good = " &nbsp; <b>GOOD TAKE</b>" if camera.good_take else ""
        rows.append(f"<div><b>{_esc(camera.slate())}</b> &nbsp; {extras}{good}</div>")

    if media.audio_tracks:
        track = media.audio_tracks[0]
        count = len(media.audio_tracks)
        name = channel_layout_name(track.channels, track.layout)
        detail = [track.codec]
        if track.bit_rate_kbps:
            detail.append(f"{track.bit_rate_kbps:.2f} kb/s")
        if track.sample_rate_hz:
            detail.append(f"{track.sample_rate_hz} hz")
        rows.append(
            f"<div><b>{count} {_esc(name)} track{'s' if count > 1 else ''}</b> &nbsp; "
            + _esc(" &nbsp; ".join(detail)).replace("&amp;nbsp;", "&nbsp;")
            + "</div>"
        )
    return "".join(rows)


def write_html(job: Job, path: Path, *, thumbnails: bool = True, **_options) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    finished = job.finished or job.started

    summary = [
        ("Final Status", job.final_status),
        ("Offload Start Date", format_job_datetime(job.started)),
        ("OS Version", job.os_version),
        ("Size of offload", format_size(job.total_bytes)),
        ("Offload Finish Date", format_job_datetime(finished)),
        ("Processors", str(job.processors) if job.processors else ""),
        # The PDF's header string is pinned to the reference report's wording,
        # so the extra pass is said here rather than folded into it.
        ("Verification Type", job.verification_label
         + (" + second source read" if job.paranoid else "")),
        ("Total Time", format_elapsed(job.elapsed_sec)),
        ("System Ram", job.system_ram),
        ("Total Files", str(job.total_files)),
        ("Video Files", str(job.video_files)),
        ("Source", str(job.source_root)),
    ]

    clips: list[str] = []
    for entry in job.files:
        if thumbnails and entry.thumbnails:
            images = "".join(
                f'<img src="{uri}" alt="">'
                for uri in (_data_uri(t) for t in entry.thumbnails[:4]) if uri
            )
            strip = f'<div class="strip">{images}</div>'
        else:
            badge = _esc(entry.source.suffix.lstrip(".").upper() or "FILE")
            strip = f'<div class="noimg">{badge}</div>'
        provenance = ""
        if entry.thumbnail_source is not None:
            provenance = (f'<div class="proxy-note">Frames from proxy: '
                          f'{_esc(entry.thumbnail_source.name)}</div>')
        # Sidecars and proxies are shown with the clip they belong to. Copied
        # and listed as unrelated files, a missing one is invisible.
        if entry.companions:
            names = ", ".join(_esc(p.name) for p in entry.companions)
            provenance += f'<div class="companion-note">With: {names}</div>'
        elif entry.companion_of is not None:
            provenance += (f'<div class="companion-note">Belongs to: '
                           f'{_esc(entry.companion_of.name)}</div>')
        clips.append(
            f'<div class="clip"><div class="meta">{_clip_meta(job, entry)}'
            f'{provenance}</div>{strip}</div>'
        )

    rows: list[str] = []
    for entry in job.files:
        for number, destination in enumerate(entry.destinations, start=1):
            rows.append(
                "<tr>"
                f'<td class="name">{_esc(entry.name)}</td>'
                f"<td>{_esc(entry.checksum or '')}</td>"
                f"<td>{_esc(format_size(entry.size))}<br>({entry.size} bytes)</td>"
                f"<td>{_esc(entry.source)}</td>"
                f"<td>{number}: {_esc(destination.path)}</td>"
                f'<td class="status-{_esc(destination.status.value)}">'
                f"{_esc(destination.status.value)}</td>"
                f"<td>{_esc(format_file_datetime(entry.created))}</td>"
                f"<td>{_esc(format_file_datetime(entry.modified))}</td>"
                "</tr>"
            )

    destinations = "".join(
        f'<div><b>Destination {i}:</b> {_esc(root)}</div>'
        for i, root in enumerate(job.destination_roots, start=1)
    )

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(job.name)} Job Report</title>
<style>{_STYLE}</style></head><body>
<h1>{_esc(job.name)}</h1>
<div class="summary">
{''.join(f'<div><b>{_esc(k)}:</b> {_esc(v)}</div>' for k, v in summary if v)}
</div>
{''.join(clips)}
<h2>All file details for root source: {_esc(job.name)}</h2>
<div class="paths"><div><b>Full Path:</b> {_esc(job.source_root)}</div>{destinations}</div>
<div class="wrap"><table>
<thead><tr><th>File</th><th>{_esc(job.hash_label)}</th><th>Size</th><th>Source</th>
<th>Destination</th><th>Status</th><th>Created</th><th>Modified</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table></div>
<footer>{_esc(PRODUCT_NAME)} Version {_esc(__version__)}</footer>
</body></html>"""

    path.write_text(document, encoding="utf-8")
    return path
