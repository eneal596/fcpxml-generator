"""
FCPXML Generator service for Rough Cut Automation.
Run on Replit (or any Python host).

POST /generate
Body: {
  "projectSettings": {
    "tabName": "GC-VID-C1",
    "videoType": "talking_head_overlay" | "narrated_story",
    "clientName": "...",
    "targetOrientation": "Portrait" | "Landscape" | "Square"
  },
  "outcomes": [  // from Step 5 Compute Retrieval Stats
    {
      "beat": { "beatId": 1, "section": "...", "narratedCopy": "...", "startTime": 0, "endTime": 3.18, "duration": 3.18 },
      "outcome": "match" | "ai_gen" | "human_review",
      "chosenAssetId": "...",
      "chosenFilename": "Copy of BROLL_TIMELAPSE_MOWING_FACE_Dave",
      "reasoning": "...",
      "aiGenPrompt": "...",
      "humanReviewReason": "..."
    },
    ...
  ],
  "totalAudioDuration": 60.6
}

Returns: { "fcpxml": "<?xml version=...>...", "filename": "GC-VID-C1.xml" }
"""

from flask import Flask, request, jsonify
import xml.sax.saxutils as saxutils
import os

app = Flask(__name__)

# ─── FCPXML constants ───
FRAME_RATE = 30
TIMEBASE = 30000  # FCPXML uses 30000/1001 for 29.97, or 30/1 for true 30. Using 30/1 here.
TIMEBASE_DENOM = 1

PORTRAIT = (1080, 1920)
LANDSCAPE = (1920, 1080)
SQUARE = (1080, 1080)

# Avatar overlay default (top-right corner, 25% scale)
AVATAR_SCALE = 0.25
AVATAR_OFFSET_X = 0.35  # right side
AVATAR_OFFSET_Y = -0.35  # top side (negative = up in FCP coordinate space)


def seconds_to_rational(seconds):
    """Convert seconds to FCPXML rational time format like '1500/30000s'."""
    frames = round(seconds * FRAME_RATE)
    numerator = frames * (TIMEBASE // FRAME_RATE)
    return f"{numerator}/{TIMEBASE}s"


def escape(s):
    return saxutils.escape(str(s)) if s else ""


def get_dimensions(orientation):
    if orientation == "Landscape":
        return LANDSCAPE
    elif orientation == "Square":
        return SQUARE
    return PORTRAIT  # default


def build_resources(outcomes, total_duration, video_type, dimensions):
    """Build the <resources> section: format + assets for each unique source file."""
    width, height = dimensions
    parts = []
    parts.append(f'    <format id="r1" name="FFVideoFormat{height}p{FRAME_RATE}" frameDuration="1001/30000s" width="{width}" height="{height}" colorSpace="1-1-1 (Rec. 709)"/>')

    # Avatar audio (and video, for talking_head_overlay)
    avatar_path = "./avatar.mp4"
    avatar_total = seconds_to_rational(total_duration)
    parts.append(f'    <asset id="r2" name="avatar" src="{avatar_path}" start="0s" duration="{avatar_total}" hasVideo="1" hasAudio="1" format="r1" videoSources="1" audioSources="1" audioChannels="2" audioRate="48000"/>')

    # Each unique B-roll asset gets its own resource id
    asset_ids = {}  # filename → resource id
    next_id = 3
    for outcome in outcomes:
        if outcome.get("outcome") != "match":
            continue
        filename = outcome.get("chosenFilename", "")
        if not filename or filename in asset_ids:
            continue
        # Don't double-extension if already there
        if not filename.lower().endswith((".mp4", ".mov", ".m4v")):
            filename_with_ext = filename + ".mp4"
        else:
            filename_with_ext = filename
        rid = f"r{next_id}"
        asset_ids[filename] = (rid, filename_with_ext)
        # We don't know the exact duration of the source file ahead of time.
        # Use a generous placeholder duration (1 hour) — Premiere will use whatever's in the actual file.
        placeholder_duration = "108000/30s"  # 1 hour at 30fps
        src_path = f"./Footage/{filename_with_ext}"
        parts.append(f'    <asset id="{rid}" name="{escape(filename)}" src="{escape(src_path)}" start="0s" duration="{placeholder_duration}" hasVideo="1" hasAudio="1" format="r1" videoSources="1" audioSources="1" audioChannels="2" audioRate="48000"/>')
        next_id += 1

    return "\n".join(parts), asset_ids


def build_spine(outcomes, asset_ids, video_type, total_duration):
    """Build the <spine> section: V1 (B-roll/gaps), audio track, optional V2 (avatar overlay)."""
    parts = []

    # Track the current timeline position for sequential placement
    # Outcomes should already be in beat order with valid startTime values
    sorted_outcomes = sorted(outcomes, key=lambda o: o.get("beat", {}).get("startTime") or 0)

    # ─── V1: B-roll clips and gap placeholders ───
    for outcome in sorted_outcomes:
        beat = outcome.get("beat", {})
        start = beat.get("startTime") or 0
        end = beat.get("endTime") or start
        duration = max(end - start, 0.5)  # minimum 0.5s

        offset_str = seconds_to_rational(start)
        duration_str = seconds_to_rational(duration)

        outcome_type = outcome.get("outcome")

        if outcome_type == "match":
            filename = outcome.get("chosenFilename", "")
            asset_info = asset_ids.get(filename)
            if asset_info:
                rid, _ = asset_info
                parts.append(
                    f'                <asset-clip name="{escape(filename)}" ref="{rid}" offset="{offset_str}" duration="{duration_str}" start="0s" tcFormat="NDF">\n'
                    f'                </asset-clip>'
                )
                continue

        # Gap clip with marker for ai_gen or human_review
        if outcome_type == "ai_gen":
            marker_text = f"[AI GEN] {outcome.get('aiGenPrompt') or beat.get('visualDirection') or 'Generate B-roll'}"
        elif outcome_type == "human_review":
            marker_text = f"[HUMAN REVIEW] {outcome.get('humanReviewReason') or 'Needs producer attention'}"
        else:
            marker_text = f"[NO MATCH] beat {beat.get('beatId')}"

        parts.append(
            f'                <gap name="{escape("Gap — " + marker_text[:40])}" offset="{offset_str}" duration="{duration_str}" start="0s">\n'
            f'                    <marker start="0s" duration="1/30s" value="{escape(marker_text)}"/>\n'
            f'                </gap>'
        )

    v1_content = "\n".join(parts)

    # ─── Spine wrapper ───
    # Audio: avatar's audio channel plays for the full duration.
    # Video: B-roll on the primary track (V1).
    # If talking_head_overlay: avatar video on V2 connected to V1 with scale/position.

    avatar_total_str = seconds_to_rational(total_duration)
    avatar_overlay_xml = ""
    if video_type == "talking_head_overlay":
        avatar_overlay_xml = (
            f'                <video name="Avatar Overlay" ref="r2" offset="0s" duration="{avatar_total_str}" start="0s">\n'
            f'                    <adjust-transform position="{AVATAR_OFFSET_X * 100} {AVATAR_OFFSET_Y * 100}" scale="{AVATAR_SCALE} {AVATAR_SCALE}"/>\n'
            f'                </video>'
        )

    # The spine combines audio + video sequentially.
    # Strategy: a single asset-clip referencing the avatar (which has both audio and video)
    # with B-roll layered on top via the spine's primary video track.
    #
    # Actually for clarity, we'll structure it as:
    # - Primary video track (V1) = B-roll clips and gaps, sequential
    # - Audio comes from a separate clip (avatar audio) connected to the timeline
    #
    # FCPXML's "spine" is the primary track. We put B-roll there, and connect avatar's audio
    # (and optionally video) as a "connected clip" anchored to the spine.

    spine = (
        '            <spine>\n'
        '                <!-- Avatar audio anchors the timeline -->\n'
        f'                <asset-clip name="Avatar VO" ref="r2" offset="0s" duration="{avatar_total_str}" start="0s" audioRole="dialogue">\n'
        f'{("                    " + avatar_overlay_xml.strip() if avatar_overlay_xml else "")}\n'
        '                </asset-clip>\n'
        '                <!-- B-roll clips and gaps -->\n'
        f'{v1_content}\n'
        '            </spine>'
    )

    return spine


def build_fcpxml(project_settings, outcomes, total_audio_duration):
    tab_name = project_settings.get("tabName", "Untitled")
    video_type = project_settings.get("videoType", "narrated_story")
    orientation = project_settings.get("targetOrientation", "Portrait")
    dimensions = get_dimensions(orientation)
    width, height = dimensions

    resources_xml, asset_ids = build_resources(outcomes, total_audio_duration, video_type, dimensions)
    spine_xml = build_spine(outcomes, asset_ids, video_type, total_audio_duration)

    total_duration_str = seconds_to_rational(total_audio_duration)

    fcpxml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
    <resources>
{resources_xml}
    </resources>
    <library>
        <event name="{escape(tab_name)}">
            <project name="{escape(tab_name)}">
                <sequence format="r1" duration="{total_duration_str}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
{spine_xml}
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

        return jsonify({
            "fcpxml": fcpxml,
            "filename": filename,
            "stats": {
                "outcomeCount": len(outcomes),
                "matchCount": sum(1 for o in outcomes if o.get("outcome") == "match"),
                "gapCount": sum(1 for o in outcomes if o.get("outcome") in ("ai_gen", "human_review")),
                "totalDuration": total_duration
            }
        })
    except Exception as e:
        return jsonify({"error": str(e), "type": type(e).__name__}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "fcpxml-generator"})


@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "service": "FCPXML Generator",
        "endpoints": {
            "POST /generate": "Generate FCPXML from beat outcomes",
            "GET /health": "Health check"
        }
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
