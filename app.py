"""
FCPXML Generator + Asset Downloader service for Rough Cut Automation - v8.

Adds /download-to-drive endpoint that:
1. Calls Air's download URL endpoint for an asset version
2. Streams the resulting file directly to Google Drive
3. Returns success/error to caller (n8n)

This bypasses n8n's disk/memory entirely — file bytes never touch n8n.

Environment variables required:
  AIR_API_KEY               — Air API key (or use Bearer token)
  GOOGLE_SERVICE_ACCOUNT_JSON — JSON string of the service account credentials

Endpoints:
  POST /generate              — generate FCPXML (existing)
  POST /download-to-drive     — download a single Air asset to a Drive folder (NEW)
  GET  /health                — health check
"""

from flask import Flask, request, jsonify
import opentimelineio as otio
from opentimelineio.opentime import RationalTime, TimeRange
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
import os
import tempfile
import re
import requests
import json
import io
import traceback

app = Flask(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────
FRAME_RATE = 30
PORTRAIT = (1080, 1920)
LANDSCAPE = (1920, 1080)
SQUARE = (1080, 1080)

AIR_API_BASE = "https://api.air.inc/v1"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# ─── Drive service (cached, OAuth refresh-token based) ───────────────────────
_drive_service = None
_drive_creds = None

def get_drive_service():
    global _drive_service, _drive_creds
    if _drive_service is not None:
        # Refresh the access token if it's expiring or expired
        if _drive_creds and (_drive_creds.expired or not _drive_creds.valid):
            try:
                _drive_creds.refresh(GoogleAuthRequest())
            except Exception as e:
                # Force re-init on next call
                _drive_service = None
                _drive_creds = None
                raise
        if _drive_service is not None:
            return _drive_service

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")

    missing = [
        n for n, v in [
            ("GOOGLE_OAUTH_CLIENT_ID", client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
            ("GOOGLE_OAUTH_REFRESH_TOKEN", refresh_token),
        ] if not v
    ]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

    _drive_creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=DRIVE_SCOPES,
    )
    # Trigger an initial refresh to validate the credentials
    _drive_creds.refresh(GoogleAuthRequest())

    _drive_service = build("drive", "v3", credentials=_drive_creds, cache_discovery=False)
    return _drive_service


# ─── /download-to-drive endpoint ─────────────────────────────────────────────
@app.route("/download-to-drive", methods=["POST"])
def download_to_drive():
    """
    Body:
      {
        "assetId": "uuid",
        "versionId": "uuid",
        "filename": "name.mp4",
        "driveFolderId": "drive-folder-id",
        "skipIfExists": true
      }
    
    Returns:
      Success: { "ok": true, "driveFileId": "...", "filename": "...", "bytesUploaded": N }
      Skipped: { "ok": true, "skipped": true, "reason": "..." }
      Failure: { "ok": false, "error": "..." }, status 500
    """
    try:
        data = request.get_json(force=True)
        asset_id = data.get("assetId")
        version_id = data.get("versionId")
        filename = data.get("filename")
        drive_folder_id = data.get("driveFolderId")
        skip_if_exists = data.get("skipIfExists", True)
        
        if not all([asset_id, version_id, filename, drive_folder_id]):
            return jsonify({
                "ok": False,
                "error": "Missing required field. Need: assetId, versionId, filename, driveFolderId"
            }), 400
        
        drive = get_drive_service()
        
        # Optionally skip if file already exists in folder
        if skip_if_exists:
            q = f"name = '{filename.replace(chr(39), chr(92)+chr(39))}' and '{drive_folder_id}' in parents and trashed = false"
            existing = drive.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
            if existing.get("files"):
                return jsonify({
                    "ok": True,
                    "skipped": True,
                    "reason": "File already exists in Drive folder",
                    "driveFileId": existing["files"][0]["id"],
                    "filename": filename
                })
        
        # Step 1: ask Air for the actual download URL
        air_key = os.environ.get("AIR_API_KEY")
        if not air_key:
            return jsonify({"ok": False, "error": "AIR_API_KEY env var not set"}), 500
        
        workspace_id = os.environ.get("AIR_WORKSPACE_ID")
        if not workspace_id:
            return jsonify({"ok": False, "error": "AIR_WORKSPACE_ID env var not set"}), 500
        
        url_endpoint = f"{AIR_API_BASE}/assets/{asset_id}/versions/{version_id}/download"
        resp = requests.get(
            url_endpoint,
            headers={
                "x-api-key": air_key,
                "x-air-workspace-id": workspace_id,
            },
            timeout=30,
            allow_redirects=False,
        )
        
        download_url = None
        if 300 <= resp.status_code < 400 and resp.headers.get("location"):
            download_url = resp.headers["location"]
        elif resp.status_code == 200:
            try:
                body = resp.json()
                download_url = body.get("url") or body.get("downloadUrl") or body.get("download_url")
            except Exception:
                if resp.text.startswith("http"):
                    download_url = resp.text.strip()
        
        if not download_url:
            return jsonify({
                "ok": False,
                "error": f"Could not extract download URL from Air. Status: {resp.status_code}. Body: {resp.text[:300]}"
            }), 500
        
        # Step 2: stream the file from Air → Drive, in chunks, never holding full bytes
        # Use requests.get(stream=True) and feed to a generator-backed BytesIO
        with requests.get(download_url, stream=True, timeout=300) as file_resp:
            if file_resp.status_code != 200:
                return jsonify({
                    "ok": False,
                    "error": f"Failed to download from Air-provided URL. Status: {file_resp.status_code}"
                }), 500
            
            # Write to a tempfile so MediaIoBaseUpload can stream it to Drive
            # Tempfile is on Render disk (separate from n8n), and we delete it after
            with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp:
                tmp_path = tmp.name
                bytes_total = 0
                for chunk in file_resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        tmp.write(chunk)
                        bytes_total += len(chunk)
            
            try:
                # Determine MIME type from filename extension
                mime_type = "video/mp4"
                if filename.lower().endswith((".mov", ".m4v")):
                    mime_type = "video/quicktime"
                
                # MediaFileUpload with resumable=True streams the file from disk in 5MB
                # chunks — keeps memory usage low regardless of file size.
                media = MediaFileUpload(
                    tmp_path,
                    mimetype=mime_type,
                    resumable=True,
                    chunksize=5 * 1024 * 1024,  # 5MB chunks
                )
                metadata = {
                    "name": filename,
                    "parents": [drive_folder_id],
                }
                # Use a resumable upload, advancing chunk-by-chunk
                request_obj = drive.files().create(
                    body=metadata,
                    media_body=media,
                    fields="id,name",
                )
                response = None
                while response is None:
                    status, response = request_obj.next_chunk()
                created = response
                
                return jsonify({
                    "ok": True,
                    "driveFileId": created.get("id"),
                    "filename": created.get("name"),
                    "bytesUploaded": bytes_total
                })
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
    
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "type": type(e).__name__,
            "traceback": traceback.format_exc()[-1500:]
        }), 500


# ============================================================================
# Existing FCPXML generation logic — UNCHANGED from v7
# ============================================================================

def get_dimensions(orientation):
    if orientation == "Landscape":
        return LANDSCAPE
    elif orientation == "Square":
        return SQUARE
    return PORTRAIT


def seconds_to_rational_time(seconds, frame_rate=FRAME_RATE):
    return RationalTime(round(seconds * frame_rate), frame_rate)


def make_external_reference(filename, asset_duration_seconds=600, ext="mp4"):
    # If filename doesn't already have a video extension, append the provided ext
    if not filename.lower().endswith((".mp4", ".mov", ".m4v")):
        # Normalise ext (strip leading dot if present, lowercase)
        clean_ext = (ext or "mp4").lower().lstrip(".")
        filename = f"{filename}.{clean_ext}"
    target_url = f"Footage/{filename}"
    available_range = TimeRange(
        start_time=RationalTime(0, FRAME_RATE),
        duration=seconds_to_rational_time(asset_duration_seconds),
    )
    return otio.schema.ExternalReference(
        target_url=target_url, available_range=available_range,
    )


def make_avatar_reference(total_duration):
    available_range = TimeRange(
        start_time=RationalTime(0, FRAME_RATE),
        duration=seconds_to_rational_time(total_duration),
    )
    return otio.schema.ExternalReference(
        target_url="Footage/avatar.mp4", available_range=available_range,
    )


def is_hook_beat(beat):
    section = (beat.get("section") or "").strip().lower()
    return section.startswith("hook")


def make_placeholder_clip(label, duration_seconds):
    sanitised = re.sub(r'[^\w\s\-\[\]]', '_', label)[:100].strip()
    if not sanitised:
        sanitised = "PLACEHOLDER"
    fake_filename = f"{sanitised}.mp4"
    available_range = TimeRange(
        start_time=RationalTime(0, FRAME_RATE),
        duration=seconds_to_rational_time(duration_seconds),
    )
    media_ref = otio.schema.ExternalReference(
        target_url=f"Footage/_PLACEHOLDERS/{fake_filename}",
        available_range=available_range,
    )
    return otio.schema.Clip(
        name=label[:200],
        media_reference=media_ref,
        source_range=TimeRange(
            start_time=RationalTime(0, FRAME_RATE),
            duration=seconds_to_rational_time(duration_seconds),
        ),
    )


def close_inter_beat_gaps(outcomes, max_gap_to_close=2.0):
    sorted_outcomes = sorted(outcomes, key=lambda o: (o.get("beat", {}).get("startTime") or 0))
    for i in range(len(sorted_outcomes) - 1):
        current = sorted_outcomes[i]
        next_ = sorted_outcomes[i + 1]
        current_beat = current.get("beat") or {}
        next_beat = next_.get("beat") or {}
        cur_end = current_beat.get("endTime")
        next_start = next_beat.get("startTime")
        if cur_end is None or next_start is None:
            continue
        gap = next_start - cur_end
        if 0 < gap <= max_gap_to_close:
            midpoint = cur_end + (gap / 2.0)
            current_beat["endTime"] = midpoint
            next_beat["startTime"] = midpoint
            current_beat["duration"] = midpoint - (current_beat.get("startTime") or 0)
            next_beat["duration"] = (next_beat.get("endTime") or midpoint) - midpoint
    return sorted_outcomes


def build_track_for_outcomes(outcomes, asset_ids, segment_start, segment_end):
    track = otio.schema.Track(name="V1 — B-roll", kind=otio.schema.TrackKind.Video)
    segment_outcomes = []
    for o in outcomes:
        beat = o.get("beat", {}) or {}
        bs = beat.get("startTime") or 0
        be = beat.get("endTime") or bs
        if be <= segment_start or bs >= segment_end:
            continue
        segment_outcomes.append(o)
    segment_outcomes.sort(key=lambda o: (o.get("beat", {}).get("startTime") or 0))
    cursor = 0.0
    for outcome in segment_outcomes:
        beat = outcome.get("beat", {}) or {}
        original_start = beat.get("startTime") or 0
        original_end = beat.get("endTime") or original_start
        effective_start = max(original_start, segment_start) - segment_start
        effective_end = min(original_end, segment_end) - segment_start
        duration = max(effective_end - effective_start, 1.0 / FRAME_RATE)
        if effective_start > cursor + 0.001:
            gap_duration = effective_start - cursor
            filler = otio.schema.Gap(
                name="(silence)",
                source_range=TimeRange(
                    start_time=RationalTime(0, FRAME_RATE),
                    duration=seconds_to_rational_time(gap_duration),
                ),
            )
            track.append(filler)
            cursor = effective_start
        outcome_type = outcome.get("outcome")
        clip_duration = seconds_to_rational_time(duration)
        if outcome_type == "match":
            filename = outcome.get("chosenFilename", "")
            ext = outcome.get("chosenExt", "mp4")
            if filename:
                clip = otio.schema.Clip(
                    name=filename,
                    media_reference=make_external_reference(filename, ext=ext),
                    source_range=TimeRange(
                        start_time=RationalTime(0, FRAME_RATE),
                        duration=clip_duration,
                    ),
                )
                track.append(clip)
                cursor = effective_start + duration
                continue
        if outcome_type == "ai_gen":
            label = f"[AI GEN] {outcome.get('aiGenPrompt') or beat.get('visualDirection') or 'Generate B-roll'}"
        elif outcome_type == "human_review":
            label = f"[HUMAN REVIEW] {outcome.get('humanReviewReason') or beat.get('visualDirection') or 'Needs producer attention'}"
        else:
            label = f"[NO MATCH] beat {beat.get('beatId')}"
        placeholder = make_placeholder_clip(label, duration)
        track.append(placeholder)
        cursor = effective_start + duration
    segment_total = segment_end - segment_start
    if cursor < segment_total - 0.001:
        pad_duration = segment_total - cursor
        pad = otio.schema.Gap(
            name="(silence)",
            source_range=TimeRange(
                start_time=RationalTime(0, FRAME_RATE),
                duration=seconds_to_rational_time(pad_duration),
            ),
        )
        track.append(pad)
    return track


def identify_hook_segments(outcomes, total_duration):
    sorted_outcomes = sorted(outcomes, key=lambda o: (o.get("beat", {}).get("startTime") or 0))
    hooks = []
    body_start = None
    for outcome in sorted_outcomes:
        beat = outcome.get("beat", {}) or {}
        section = (beat.get("section") or "").strip()
        bs = beat.get("startTime") or 0
        be = beat.get("endTime") or bs
        if is_hook_beat(beat):
            hooks.append({"name": section, "start": bs, "end": be})
        else:
            if body_start is None:
                body_start = bs
    if body_start is None:
        body_start = total_duration
    body_end = total_duration
    return hooks, body_start, body_end


def build_concatenated_timeline(name, outcomes, video_type, segments):
    timeline = otio.schema.Timeline(name=name)
    timeline.global_start_time = RationalTime(0, FRAME_RATE)
    audio_track = otio.schema.Track(name="A1 — Avatar Audio", kind=otio.schema.TrackKind.Audio)
    for seg_start, seg_end in segments:
        seg_duration = seg_end - seg_start
        media_ref = make_avatar_reference(seg_end)
        clip = otio.schema.Clip(
            name="Avatar Audio", media_reference=media_ref,
            source_range=TimeRange(
                start_time=seconds_to_rational_time(seg_start),
                duration=seconds_to_rational_time(seg_duration),
            ),
        )
        audio_track.append(clip)
    video_overlay_track = None
    if video_type == "talking_head_overlay":
        video_overlay_track = otio.schema.Track(name="V2 — Avatar Overlay", kind=otio.schema.TrackKind.Video)
        for seg_start, seg_end in segments:
            seg_duration = seg_end - seg_start
            media_ref = make_avatar_reference(seg_end)
            clip = otio.schema.Clip(
                name="Avatar", media_reference=media_ref,
                source_range=TimeRange(
                    start_time=seconds_to_rational_time(seg_start),
                    duration=seconds_to_rational_time(seg_duration),
                ),
            )
            video_overlay_track.append(clip)
    v1_track = otio.schema.Track(name="V1 — B-roll", kind=otio.schema.TrackKind.Video)
    timeline_cursor = 0.0
    for seg_start, seg_end in segments:
        seg_duration = seg_end - seg_start
        segment_outcomes = sorted(
            [o for o in outcomes
             if (o.get("beat", {}) or {}).get("startTime") is not None
             and ((o.get("beat", {}).get("endTime") or 0) > seg_start)
             and ((o.get("beat", {}).get("startTime") or 0) < seg_end)],
            key=lambda o: (o.get("beat", {}).get("startTime") or 0),
        )
        cursor_in_seg = 0.0
        for outcome in segment_outcomes:
            beat = outcome.get("beat", {}) or {}
            original_start = beat.get("startTime") or 0
            original_end = beat.get("endTime") or original_start
            effective_start = max(original_start, seg_start) - seg_start
            effective_end = min(original_end, seg_end) - seg_start
            duration = max(effective_end - effective_start, 1.0 / FRAME_RATE)
            if effective_start > cursor_in_seg + 0.001:
                gap_dur = effective_start - cursor_in_seg
                filler = otio.schema.Gap(
                    name="(silence)",
                    source_range=TimeRange(
                        start_time=RationalTime(0, FRAME_RATE),
                        duration=seconds_to_rational_time(gap_dur),
                    ),
                )
                v1_track.append(filler)
                cursor_in_seg = effective_start
            outcome_type = outcome.get("outcome")
            if outcome_type == "match":
                filename = outcome.get("chosenFilename", "")
                ext = outcome.get("chosenExt", "mp4")
                if filename:
                    clip = otio.schema.Clip(
                        name=filename, media_reference=make_external_reference(filename, ext=ext),
                        source_range=TimeRange(
                            start_time=RationalTime(0, FRAME_RATE),
                            duration=seconds_to_rational_time(duration),
                        ),
                    )
                    v1_track.append(clip)
                    cursor_in_seg = effective_start + duration
                    continue
            if outcome_type == "ai_gen":
                label = f"[AI GEN] {outcome.get('aiGenPrompt') or beat.get('visualDirection') or 'Generate B-roll'}"
            elif outcome_type == "human_review":
                label = f"[HUMAN REVIEW] {outcome.get('humanReviewReason') or beat.get('visualDirection') or 'Needs producer attention'}"
            else:
                label = f"[NO MATCH] beat {beat.get('beatId')}"
            placeholder = make_placeholder_clip(label, duration)
            v1_track.append(placeholder)
            cursor_in_seg = effective_start + duration
        if cursor_in_seg < seg_duration - 0.001:
            pad_dur = seg_duration - cursor_in_seg
            pad = otio.schema.Gap(
                name="(silence)",
                source_range=TimeRange(
                    start_time=RationalTime(0, FRAME_RATE),
                    duration=seconds_to_rational_time(pad_dur),
                ),
            )
            v1_track.append(pad)
        timeline_cursor += seg_duration
    timeline.tracks.append(v1_track)
    if video_overlay_track:
        timeline.tracks.append(video_overlay_track)
    timeline.tracks.append(audio_track)
    return timeline


def build_timeline_for_segment(name, outcomes, video_type, segment_start, segment_end):
    timeline = otio.schema.Timeline(name=name)
    timeline.global_start_time = RationalTime(0, FRAME_RATE)
    asset_ids = {}
    v1 = build_track_for_outcomes(outcomes, asset_ids, segment_start, segment_end)
    timeline.tracks.append(v1)
    if video_type == "talking_head_overlay":
        v2 = otio.schema.Track(name="V2 — Avatar Overlay", kind=otio.schema.TrackKind.Video)
        media_ref = make_avatar_reference(segment_end)
        clip = otio.schema.Clip(
            name="Avatar", media_reference=media_ref,
            source_range=TimeRange(
                start_time=seconds_to_rational_time(segment_start),
                duration=seconds_to_rational_time(segment_end - segment_start),
            ),
        )
        v2.append(clip)
        timeline.tracks.append(v2)
    a1 = otio.schema.Track(name="A1 — Avatar Audio", kind=otio.schema.TrackKind.Audio)
    media_ref = make_avatar_reference(segment_end)
    clip = otio.schema.Clip(
        name="Avatar Audio", media_reference=media_ref,
        source_range=TimeRange(
            start_time=seconds_to_rational_time(segment_start),
            duration=seconds_to_rational_time(segment_end - segment_start),
        ),
    )
    a1.append(clip)
    timeline.tracks.append(a1)
    return timeline


def timeline_to_fcpxml(timeline):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".fcpxml", delete=False, encoding="utf-8") as tmp:
        tmp_path = tmp.name
    try:
        otio.adapters.write_to_file(timeline, tmp_path, adapter_name="fcp_xml")
        with open(tmp_path, "r", encoding="utf-8") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def inject_sequence_format_throughout(combined_xml, dimensions):
    width, height = dimensions
    format_block = f"""<format>
                            <samplecharacteristics>
                                <width>{width}</width>
                                <height>{height}</height>
                                <pixelaspectratio>square</pixelaspectratio>
                                <fielddominance>none</fielddominance>
                                <rate>
                                    <timebase>30</timebase>
                                    <ntsc>FALSE</ntsc>
                                </rate>
                                <colordepth>24</colordepth>
                            </samplecharacteristics>
                        </format>"""
    return combined_xml.replace("<format/>", format_block)


def deduplicate_avatar_throughout(combined_xml):
    pattern = re.compile(
        r'<file id="(file-\d+)">(\s*<pathurl>\./Footage/avatar\.mp4</pathurl>.*?)</file>',
        re.DOTALL,
    )
    matches = list(pattern.finditer(combined_xml))
    if not matches:
        return combined_xml
    canonical_id = matches[0].group(1)
    if len(matches) > 1:
        result = combined_xml
        for m in reversed(matches[1:]):
            start, end = m.span()
            bare_ref = f'<file id="{canonical_id}"/>'
            result = result[:start] + bare_ref + result[end:]
        combined_xml = result
    all_definitions = set(re.findall(r'<file id="(file-\d+)">', combined_xml))
    def fix_ref(m):
        ref_id = m.group(1)
        if ref_id in all_definitions:
            return m.group(0)
        return f'<file id="{canonical_id}"/>'
    combined_xml = re.sub(r'<file id="(file-\d+)"/>', fix_ref, combined_xml)
    return combined_xml


def wrap_in_bins(combined_xml):
    """
    Organize the project into Premiere bins:
    
    [Project root]
    ├── Hook V1/        → just the sequence -V1
    ├── Hook V2/        → just the sequence -V2
    ├── Hook V3/        → just the sequence -V3
    ├── Hook V4/        → just the sequence -V4
    ├── Hook V5/        → just the sequence -V5
    ├── Footage/        → standalone <clip> entries for each unique footage file
    └── Placeholders/   → standalone <clip> entries for each placeholder slug
    
    The Footage and Placeholders bins contain <clip> elements that reference
    the same file IDs as the sequences. Premiere reads these as "this media
    item lives in this bin", overriding its default of placing media items
    in the first sequence's parent bin.
    """
    sequence_blocks = list(re.finditer(
        r'(<sequence[^>]*>.*?</sequence>)',
        combined_xml,
        re.DOTALL,
    ))
    
    if len(sequence_blocks) <= 1:
        return combined_xml
    
    # Wrap each sequence in its Hook bin
    bin_wrapped = []
    for i, m in enumerate(sequence_blocks, start=1):
        seq_xml = m.group(1)
        name_match = re.search(r'<sequence[^>]*>\s*<name>([^<]+)</name>', seq_xml)
        if name_match:
            seq_name = name_match.group(1)
            var_match = re.search(r'-V(\d+)$', seq_name)
            bin_name = f"Hook V{var_match.group(1)}" if var_match else f"Variation {i}"
        else:
            bin_name = f"Variation {i}"
        
        bin_xml = f"""<bin>
                <name>{bin_name}</name>
                <children>
                    {seq_xml}
                </children>
            </bin>"""
        bin_wrapped.append(bin_xml)
    
    # Replace each sequence with its Hook bin (in reverse to keep offsets valid)
    result = combined_xml
    for m, new_bin in zip(reversed(sequence_blocks), reversed(bin_wrapped)):
        start, end = m.span(1)
        result = result[:start] + new_bin + result[end:]
    
    # Note: previous versions tried to add explicit Footage/Placeholders bins
    # using bare <file id=".."/> references, but FCP7 XML requires standalone
    # <clip> elements in bins to have their own full <file> definition with
    # <pathurl> etc. Bare references caused Premiere import failures.
    # 
    # For now we only wrap sequences in Hook bins. Premiere will auto-place
    # the media items somewhere on import, which the editor can rearrange.
    
    return result


def build_combined_fcpxml(sequences_xml_list, project_name, dimensions):
    if len(sequences_xml_list) == 1:
        result = inject_sequence_format_throughout(sequences_xml_list[0], dimensions)
        result = deduplicate_avatar_throughout(result)
        return result
    sequence_blocks = []
    id_offset = 0
    for i, xml in enumerate(sequences_xml_list):
        match = re.search(r'(<sequence[^>]*>.*?</sequence>)', xml, re.DOTALL)
        if not match:
            continue
        block = match.group(1)
        all_ids = re.findall(r'id="(?:sequence|clipitem|file)-(\d+)"', block)
        max_id_in_block = max([int(x) for x in all_ids]) if all_ids else 0
        def shift_id(m):
            prefix = m.group(1)
            num = int(m.group(2))
            return f'id="{prefix}-{num + id_offset}"'
        block = re.sub(r'id="(sequence|clipitem|file)-(\d+)"', shift_id, block)
        sequence_blocks.append(block)
        id_offset += max_id_in_block + 1
    if not sequence_blocks:
        return sequences_xml_list[0]
    combined_children = "\n            ".join(sequence_blocks)
    combined_xml = f"""<?xml version="1.0" ?>
<xmeml version="4">
    <project>
        <name>{project_name}</name>
        <children>
            {combined_children}
        </children>
    </project>
</xmeml>
"""
    combined_xml = inject_sequence_format_throughout(combined_xml, dimensions)
    combined_xml = deduplicate_avatar_throughout(combined_xml)
    combined_xml = wrap_in_bins(combined_xml)
    return combined_xml


def build_fcpxml(project_settings, outcomes, total_audio_duration):
    tab_name = project_settings.get("tabName", "Untitled")
    video_type = project_settings.get("videoType", "narrated_story")
    dimensions = get_dimensions(project_settings.get("targetOrientation", "Portrait"))
    outcomes = close_inter_beat_gaps(outcomes, max_gap_to_close=2.0)
    hooks, body_start, body_end = identify_hook_segments(outcomes, total_audio_duration)
    if len(hooks) <= 1:
        timeline = build_timeline_for_segment(
            name=tab_name, outcomes=outcomes, video_type=video_type,
            segment_start=0.0, segment_end=total_audio_duration,
        )
        single_xml = timeline_to_fcpxml(timeline)
        return build_combined_fcpxml([single_xml], tab_name, dimensions)
    sequences_xml = []
    for i, hook in enumerate(hooks, start=1):
        seq_name = f"{tab_name}-V{i}"
        timeline = build_concatenated_timeline(
            name=seq_name, outcomes=outcomes, video_type=video_type,
            segments=[(hook["start"], hook["end"]), (body_start, body_end)],
        )
        sequences_xml.append(timeline_to_fcpxml(timeline))
    return build_combined_fcpxml(sequences_xml, tab_name, dimensions)


@app.route("/generate", methods=["POST"])
def generate():
    try:
        data = request.get_json(force=True)
        project_settings = data.get("projectSettings", {})
        outcomes = data.get("outcomes", [])
        total_duration = data.get("totalAudioDuration", 0)
        if not outcomes:
            return jsonify({"error": "No outcomes provided"}), 400
        if not total_duration:
            return jsonify({"error": "totalAudioDuration is required"}), 400
        fcpxml = build_fcpxml(project_settings, outcomes, total_duration)
        filename = f"{project_settings.get('tabName', 'output')}.xml"
        hooks, _, _ = identify_hook_segments(outcomes, total_duration)
        return jsonify({
            "fcpxml": fcpxml, "filename": filename,
            "stats": {
                "outcomeCount": len(outcomes),
                "matchCount": sum(1 for o in outcomes if o.get("outcome") == "match"),
                "gapCount": sum(1 for o in outcomes if o.get("outcome") in ("ai_gen", "human_review")),
                "hookCount": len(hooks),
                "sequenceCount": len(hooks) if len(hooks) > 1 else 1,
                "totalDuration": total_duration,
            },
        })
    except Exception as e:
        return jsonify({
            "error": str(e), "type": type(e).__name__,
            "traceback": traceback.format_exc()[-1500:],
        }), 500


@app.route("/health", methods=["GET"])
def health():
    drive_ok = False
    try:
        get_drive_service()
        drive_ok = True
    except Exception as e:
        drive_ok = f"Error: {e}"
    return jsonify({
        "status": "ok", "service": "fcpxml-generator",
        "version": "v17-no-dot-slash-prefix",
        "otio_version": otio.__version__,
        "drive_credentials": drive_ok,
        "air_credentials": "ok" if os.environ.get("AIR_API_KEY") else "missing",
        "air_workspace_id": "ok" if os.environ.get("AIR_WORKSPACE_ID") else "missing",
    })


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "FCPXML Generator + Asset Downloader",
        "version": "v17",
        "endpoints": {
            "POST /generate": "Generate FCPXML from beat outcomes",
            "POST /download-to-drive": "Download an Air asset directly to a Drive folder",
            "GET /health": "Health check",
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
