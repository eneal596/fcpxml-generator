"""
FCPXML Generator service for Rough Cut Automation - v2.

Premiere-compatible FCPXML structure:
- Spine: single asset-clip referencing avatar audio at full duration (the anchor)
- Connected clips on lane="1": B-roll asset-clips and gaps positioned by offset
- Connected clip on lane="2" (talking_head_overlay only): avatar video scaled to corner

POST /generate
Body: { projectSettings, outcomes, totalAudioDuration }
Returns: { fcpxml, filename, stats }
"""

from flask import Flask, request, jsonify
import xml.sax.saxutils as saxutils
import os

app = Flask(__name__)

# ─── FCPXML constants ───
# Use 30/1 (true 30fps) instead of 29.97 to keep math simple.
FRAME_RATE = 30
TIMEBASE = 30000  # FCPXML rational denominator
FRAME_NUM = TIMEBASE // FRAME_RATE  # 1000 — numerator units per frame

PORTRAIT = (1080, 1920)
LANDSCAPE = (1920, 1080)
SQUARE = (1080, 1080)

# Avatar overlay corner placement (top-right, ~25% scale)
# Position is in pixels (FCP convention) relative to the sequence center.
# For a 1080x1920 portrait sequence:
#   center = (540, 960)
#   top-right at 25% scale = position (~+360, ~-680) from center
AVATAR_SCALE = 0.25
AVATAR_POS_X = 360
AVATAR_POS_Y = -680


def to_rational(seconds):
    """Convert seconds to FCPXML rational time like '90000/30000s'."""
    frames = max(round(seconds * FRAME_RATE), 1)
    numerator = frames * FRAME_NUM
    return f"{numerator}/{TIMEBASE}s"


def escape(s):
    return saxutils.escape(str(s)) if s is not None else ""


def get_dimensions(orientation):
    if orientation == "Landscape":
        return LANDSCAPE
    elif orientation == "Square":
        return SQUARE
    return PORTRAIT


def build_resources(outcomes, total_duration, dimensions):
    """Build <resources>: format + assets for avatar and each unique B-roll clip."""
    width, height = dimensions
    parts = []
    parts.append(
        f'        <format id="r1" name="FFVideoFormat{height}p{FRAME_RATE}" '
        f'frameDuration="{FRAME_NUM}/{TIMEBASE}s" width="{width}" height="{height}" '
        f'colorSpace="1-1-1 (Rec. 709)"/>'
    )

    # Avatar — duration matches the audio length exactly
    avatar_duration_str = to_rational(total_duration)
    parts.append(
        f'        <asset id="r2" name="avatar" src="./avatar.mp4" start="0s" '
        f'duration="{avatar_duration_str}" hasVideo="1" hasAudio="1" format="r1" '
        f'videoSources="1" audioSources="1" audioChannels="2" audioRate="48000"/>'
    )

    # Each unique B-roll asset gets its own resource id
    asset_ids = {}  # filename → (resource_id, src_path, asset_duration_str)
    next_id = 3

    for outcome in outcomes:
        if outcome.get("outcome") != "match":
            continue
        filename = outcome.get("chosenFilename", "")
        if not filename or filename in asset_ids:
            continue

        # Make sure filename has an extension (default .mp4)
        if not filename.lower().endswith((".mp4", ".mov", ".m4v")):
            filename_with_ext = filename + ".mp4"
        else:
            filename_with_ext = filename

        rid = f"r{next_id}"

        # Use a generous duration matching the source files — 10 minutes is enough
        # for any clip we'll use. Premiere will use the actual file duration once linked.
        # 10 min × 60 sec × 30 fps = 18000 frames × 1000 = 18000000/30000s
        asset_duration_str = "18000000/30000s"  # 10 minutes

        src_path = f"./Footage/{filename_with_ext}"

        parts.append(
            f'        <asset id="{rid}" name="{escape(filename)}" src="{escape(src_path)}" '
            f'start="0s" duration="{asset_duration_str}" hasVideo="1" hasAudio="1" '
            f'format="r1" videoSources="1" audioSources="1" audioChannels="2" audioRate="48000"/>'
        )
        asset_ids[filename] = (rid, src_path, asset_duration_str)
        next_id += 1

    return "\n".join(parts), asset_ids


def build_connected_clips(outcomes, asset_ids, total_duration):
    """
    Build the connected clips that sit on lane=1 (B-roll/gaps), positioned by offset.
    These are children of the spine's primary asset-clip (the avatar).
    """
    parts = []

    sorted_outcomes = sorted(
        outcomes, key=lambda o: (o.get("beat", {}).get("startTime") or 0)
    )

    for outcome in sorted_outcomes:
        beat = outcome.get("beat", {}) or {}
        start = beat.get("startTime") or 0
        end = beat.get("endTime") or start
        duration = max(end - start, 1.0 / FRAME_RATE)  # min 1 frame

        # Cap duration so it doesn't extend past the avatar audio
        if start >= total_duration:
            continue
        if start + duration > total_duration:
            duration = total_duration - start

        offset_str = to_rational(start)
        duration_str = to_rational(duration)
        outcome_type = outcome.get("outcome")

        if outcome_type == "match":
            filename = outcome.get("chosenFilename", "")
            asset_info = asset_ids.get(filename)
            if asset_info:
                rid, _, _ = asset_info
                # Connected B-roll clip on lane="1" (above the spine's avatar)
                parts.append(
                    f'                    <asset-clip name="{escape(filename)}" '
                    f'lane="1" offset="{offset_str}" ref="{rid}" '
                    f'duration="{duration_str}" start="0s"/>'
                )
                continue

        # Gap with marker for ai_gen / human_review
        if outcome_type == "ai_gen":
            marker_text = (
                f"[AI GEN] "
                f"{outcome.get('aiGenPrompt') or beat.get('visualDirection') or 'Generate B-roll'}"
            )
        elif outcome_type == "human_review":
            marker_text = (
                f"[HUMAN REVIEW] "
                f"{outcome.get('humanReviewReason') or 'Needs producer attention'}"
            )
        else:
            marker_text = f"[NO MATCH] beat {beat.get('beatId')}"

        # Gap on lane="1" — Premiere honors connected gaps with markers
        parts.append(
            f'                    <gap name="Gap" lane="1" offset="{offset_str}" '
            f'duration="{duration_str}" start="0s">\n'
            f'                        <marker start="0s" duration="{FRAME_NUM}/{TIMEBASE}s" '
            f'value="{escape(marker_text[:200])}"/>\n'
            f'                    </gap>'
        )

    return "\n".join(parts)


def build_avatar_overlay_clip(total_duration):
    """
    For talking_head_overlay video type, produce a connected video clip on lane="2"
    that shows the avatar scaled into the top-right corner.
    """
    duration_str = to_rational(total_duration)
    return (
        f'                    <video name="Avatar Overlay" lane="2" offset="0s" '
        f'ref="r2" duration="{duration_str}" start="0s">\n'
        f'                        <adjust-transform position="{AVATAR_POS_X} {AVATAR_POS_Y}" '
        f'scale="{AVATAR_SCALE} {AVATAR_SCALE}"/>\n'
        f'                    </video>'
    )


def build_fcpxml(project_settings, outcomes, total_audio_duration):
    tab_name = project_settings.get("tabName", "Untitled")
    video_type = project_settings.get("videoType", "narrated_story")
    orientation = project_settings.get("targetOrientation", "Portrait")
    dimensions = get_dimensions(orientation)

    resources_xml, asset_ids = build_resources(
        outcomes, total_audio_duration, dimensions
    )
    connected_clips_xml = build_connected_clips(
        outcomes, asset_ids, total_audio_duration
    )

    overlay_xml = ""
    if video_type == "talking_head_overlay":
        overlay_xml = build_avatar_overlay_clip(total_audio_duration)

    sequence_duration_str = to_rational(total_audio_duration)
    avatar_duration_str = to_rational(total_audio_duration)

    # Combine all connected clips and the optional overlay
    all_connected = connected_clips_xml
    if overlay_xml:
        all_connected = (overlay_xml + "\n" + connected_clips_xml).strip("\n")

    fcpxml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
    <resources>
{resources_xml}
    </resources>
    <library>
        <event name="{escape(tab_name)}">
            <project name="{escape(tab_name)}">
                <sequence format="r1" duration="{sequence_duration_str}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
                    <spine>
                        <asset-clip name="{escape(tab_name)}" ref="r2" offset="0s" duration="{avatar_duration_str}" start="0s" audioRole="dialogue">
{all_connected}
                        </asset-clip>
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
"""
    return fcpxml


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
        return jsonify({"error": str(e), "type": type(e).__name__}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "fcpxml-generator", "version": "v2"})


@app.route("/", methods=["GET"])
def root():
    return jsonify(
        {
            "service": "FCPXML Generator",
            "version": "v2",
            "endpoints": {
                "POST /generate": "Generate FCPXML from beat outcomes",
                "GET /health": "Health check",
            },
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
