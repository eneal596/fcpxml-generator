"""
FCPXML Generator service for Rough Cut Automation - v3 (OpenTimelineIO).

Uses Pixar's OpenTimelineIO library with the fcpx_xml adapter for reliable
Premiere-compatible FCPXML output.

POST /generate
Body: {
  "projectSettings": {
    "tabName": "GC-VID-C1",
    "videoType": "talking_head_overlay" | "narrated_story",
    "targetOrientation": "Portrait" | "Landscape" | "Square"
  },
  "outcomes": [
    {
      "beat": { "beatId": 1, "narratedCopy": "...", "startTime": 0, "endTime": 3.18, "duration": 3.18, "visualDirection": "..." },
      "outcome": "match" | "ai_gen" | "human_review",
      "chosenAssetId": "...",
      "chosenFilename": "...",
      "reasoning": "...",
      "aiGenPrompt": "...",
      "humanReviewReason": "..."
    }
  ],
  "totalAudioDuration": 60.6
}
Returns: { fcpxml, filename, stats }
"""

from flask import Flask, request, jsonify
import opentimelineio as otio
from opentimelineio.opentime import RationalTime, TimeRange
import os
import tempfile

app = Flask(__name__)

FRAME_RATE = 30
PORTRAIT = (1080, 1920)
LANDSCAPE = (1920, 1080)
SQUARE = (1080, 1080)


def get_dimensions(orientation):
    if orientation == "Landscape":
        return LANDSCAPE
    elif orientation == "Square":
        return SQUARE
    return PORTRAIT


def seconds_to_rational_time(seconds, frame_rate=FRAME_RATE):
    """Convert float seconds to OTIO RationalTime."""
    return RationalTime(round(seconds * frame_rate), frame_rate)


def make_external_reference(filename, asset_duration_seconds=600):
    """
    Create an ExternalReference pointing to a file in the project's Footage/ folder.
    Premiere uses these refs to find media on disk relative to the FCPXML location.
    """
    # Ensure filename has an extension
    if not filename.lower().endswith((".mp4", ".mov", ".m4v")):
        filename = filename + ".mp4"

    target_url = f"./Footage/{filename}"
    available_range = TimeRange(
        start_time=RationalTime(0, FRAME_RATE),
        duration=seconds_to_rational_time(asset_duration_seconds),
    )
    return otio.schema.ExternalReference(
        target_url=target_url,
        available_range=available_range,
    )


def make_avatar_reference(total_duration):
    """ExternalReference pointing to ./avatar.mp4 at the project root."""
    available_range = TimeRange(
        start_time=RationalTime(0, FRAME_RATE),
        duration=seconds_to_rational_time(total_duration),
    )
    return otio.schema.ExternalReference(
        target_url="./avatar.mp4",
        available_range=available_range,
    )


def build_timeline(project_settings, outcomes, total_audio_duration):
    """
    Build an OTIO Timeline:
    - V1 (broll_track): B-roll asset clips and gaps (one per beat)
    - V2 (avatar_video_track): avatar video (talking_head_overlay only)
    - A1 (avatar_audio_track): avatar audio for full duration
    """
    tab_name = project_settings.get("tabName", "Untitled")
    video_type = project_settings.get("videoType", "narrated_story")

    timeline = otio.schema.Timeline(name=tab_name)
    timeline.global_start_time = RationalTime(0, FRAME_RATE)

    # ─── V1: B-roll and gaps ───
    broll_track = otio.schema.Track(name="V1 — B-roll", kind=otio.schema.TrackKind.Video)

    sorted_outcomes = sorted(
        outcomes, key=lambda o: (o.get("beat", {}).get("startTime") or 0)
    )

    cursor = 0.0  # position on the V1 timeline in seconds
    for outcome in sorted_outcomes:
        beat = outcome.get("beat", {}) or {}
        start = beat.get("startTime") or 0
        end = beat.get("endTime") or start
        duration = max(end - start, 1.0 / FRAME_RATE)

        if start >= total_audio_duration:
            continue
        if start + duration > total_audio_duration:
            duration = total_audio_duration - start

        # If there's a gap before this beat, fill it with a Gap clip
        if start > cursor:
            gap_duration = start - cursor
            filler = otio.schema.Gap(
                name="(empty)",
                source_range=TimeRange(
                    start_time=RationalTime(0, FRAME_RATE),
                    duration=seconds_to_rational_time(gap_duration),
                ),
            )
            broll_track.append(filler)
            cursor = start

        outcome_type = outcome.get("outcome")
        clip_duration = seconds_to_rational_time(duration)

        if outcome_type == "match":
            filename = outcome.get("chosenFilename", "")
            if filename:
                clip = otio.schema.Clip(
                    name=filename,
                    media_reference=make_external_reference(filename),
                    source_range=TimeRange(
                        start_time=RationalTime(0, FRAME_RATE),
                        duration=clip_duration,
                    ),
                )
                broll_track.append(clip)
                cursor = start + duration
                continue

        # Gap with marker for ai_gen / human_review / failed match
        if outcome_type == "ai_gen":
            marker_text = (
                f"[AI GEN] "
                f"{outcome.get('aiGenPrompt') or beat.get('visualDirection') or 'Generate B-roll'}"
            )
            color = otio.schema.MarkerColor.YELLOW
        elif outcome_type == "human_review":
            marker_text = (
                f"[HUMAN REVIEW] "
                f"{outcome.get('humanReviewReason') or 'Needs producer attention'}"
            )
            color = otio.schema.MarkerColor.RED
        else:
            marker_text = f"[NO MATCH] beat {beat.get('beatId')}"
            color = otio.schema.MarkerColor.ORANGE

        gap = otio.schema.Gap(
            name=marker_text[:80],
            source_range=TimeRange(
                start_time=RationalTime(0, FRAME_RATE),
                duration=clip_duration,
            ),
        )
        marker = otio.schema.Marker(
            name=marker_text,
            color=color,
            marked_range=TimeRange(
                start_time=RationalTime(0, FRAME_RATE),
                duration=RationalTime(1, FRAME_RATE),
            ),
        )
        gap.markers.append(marker)
        broll_track.append(gap)
        cursor = start + duration

    # If V1 ends before total duration, pad with a final gap
    if cursor < total_audio_duration:
        pad_duration = total_audio_duration - cursor
        pad = otio.schema.Gap(
            name="(empty)",
            source_range=TimeRange(
                start_time=RationalTime(0, FRAME_RATE),
                duration=seconds_to_rational_time(pad_duration),
            ),
        )
        broll_track.append(pad)

    timeline.tracks.append(broll_track)

    # ─── V2: Avatar video overlay (only for talking_head_overlay) ───
    if video_type == "talking_head_overlay":
        avatar_video_track = otio.schema.Track(
            name="V2 — Avatar Overlay", kind=otio.schema.TrackKind.Video
        )
        avatar_video_clip = otio.schema.Clip(
            name="Avatar",
            media_reference=make_avatar_reference(total_audio_duration),
            source_range=TimeRange(
                start_time=RationalTime(0, FRAME_RATE),
                duration=seconds_to_rational_time(total_audio_duration),
            ),
        )
        avatar_video_track.append(avatar_video_clip)
        timeline.tracks.append(avatar_video_track)

    # ─── A1: Avatar audio ───
    avatar_audio_track = otio.schema.Track(
        name="A1 — Avatar Audio", kind=otio.schema.TrackKind.Audio
    )
    avatar_audio_clip = otio.schema.Clip(
        name="Avatar Audio",
        media_reference=make_avatar_reference(total_audio_duration),
        source_range=TimeRange(
            start_time=RationalTime(0, FRAME_RATE),
            duration=seconds_to_rational_time(total_audio_duration),
        ),
    )
    avatar_audio_track.append(avatar_audio_clip)
    timeline.tracks.append(avatar_audio_track)

    return timeline


def timeline_to_fcpxml(timeline):
    """Serialize an OTIO timeline to FCPXML using the fcpx_xml adapter."""
    # OTIO's fcpx_xml adapter writes to a file path — use a temp file then read back.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".fcpxml", delete=False, encoding="utf-8"
    ) as tmp:
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

        timeline = build_timeline(project_settings, outcomes, total_duration)
        fcpxml = timeline_to_fcpxml(timeline)
        filename = f"{project_settings.get('tabName', 'output')}.xml"

        return jsonify(
            {
                "fcpxml": fcpxml,
                "filename": filename,
                "stats": {
                    "outcomeCount": len(outcomes),
                    "matchCount": sum(1 for o in outcomes if o.get("outcome") == "match"),
                    "gapCount": sum(
                        1 for o in outcomes if o.get("outcome") in ("ai_gen", "human_review")
                    ),
                    "totalDuration": total_duration,
                },
            }
        )
    except Exception as e:
        import traceback
        return jsonify(
            {
                "error": str(e),
                "type": type(e).__name__,
                "traceback": traceback.format_exc()[-1500:],
            }
        ), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "ok",
            "service": "fcpxml-generator",
            "version": "v3-otio-fcp7",
            "otio_version": otio.__version__,
        }
    )


@app.route("/", methods=["GET"])
def root():
    return jsonify(
        {
            "service": "FCPXML Generator",
            "version": "v3 (OpenTimelineIO FCP7)",
            "endpoints": {
                "POST /generate": "Generate FCPXML from beat outcomes",
                "GET /health": "Health check",
            },
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
